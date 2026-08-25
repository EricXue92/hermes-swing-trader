#!/usr/bin/env python3
"""
Hermes 语言交互交易系统 - MCP 工具服务器 (Alpaca 模拟盘)

设计原则:
1. 硬风控在代码层,不依赖模型自觉。所有限制读取 risk_config.json。
2. 默认「预览 -> 用户确认 -> 执行」两步下单,防止模型误触发。
3. 只做多、必带止损(bracket order)、止损只允许上移。
4. KILL_SWITCH 文件存在时,一切下单/改单操作直接拒绝。

环境变量:
  APCA_API_KEY_ID / APCA_API_SECRET_KEY  Alpaca 模拟盘 key (paper trading)
依赖:
  pip install mcp requests
运行(stdio 模式,由 Hermes 拉起):
  python trading_server.py
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import date
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

# ---------------- 基础配置 ----------------
BASE_DIR = Path(__file__).resolve().parent

# 加载 .env 文件(已存在的环境变量优先,不会被覆盖)
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

CONFIG = json.loads((BASE_DIR / "risk_config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE_DIR / "state.json"          # 记录当日下单次数、已用确认令牌
KILL_SWITCH = BASE_DIR / "KILL_SWITCH"        # 该文件存在 = 急停
RUNTIME_SECRET = secrets.token_hex(16)        # 每次启动随机,签发确认令牌用

TRADE_API = "https://paper-api.alpaca.markets"   # 模拟盘。切真钱前请三思并全面复核!
DATA_API = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("APCA_API_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("APCA_API_SECRET_KEY", ""),
}

mcp = FastMCP("swing-trading")

# ---------------- 工具函数 ----------------

def _get(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    if r.status_code >= 400:
        raise RuntimeError(f"Alpaca API 错误 {r.status_code}: {r.text[:300]}")
    return r.json()


def _post(url: str, payload: dict) -> dict:
    r = requests.post(url, headers=HEADERS, json=payload, timeout=15)
    if r.status_code >= 400:
        raise RuntimeError(f"Alpaca API 错误 {r.status_code}: {r.text[:300]}")
    return r.json()


def _load_state() -> dict:
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        s = {}
    today = date.today().isoformat()
    if s.get("day") != today:
        s = {"day": today, "trades_today": 0, "used_tokens": []}
    return s


def _save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def _order_fingerprint(symbol: str, qty: float, stop: float) -> str:
    msg = f"{symbol}|{qty}|{stop}"
    return hmac.new(RUNTIME_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]


def _latest_price(symbol: str) -> float:
    data = _get(f"{DATA_API}/v2/stocks/{symbol}/trades/latest")
    return float(data["trade"]["p"])


def _risk_check(symbol: str, qty: float, stop_loss: float) -> tuple[list[str], dict]:
    """执行全部硬风控检查。返回 (错误列表, 上下文数据)。错误列表为空 = 通过。"""
    errors: list[str] = []
    symbol = symbol.upper()

    if KILL_SWITCH.exists():
        return (["急停开关已激活 (KILL_SWITCH 文件存在),所有交易操作被禁止。请联系管理员。"], {})

    wl = CONFIG.get("symbol_whitelist") or []
    if wl and symbol not in wl:
        errors.append(f"{symbol} 不在允许交易的白名单内: {wl}")

    state = _load_state()
    if state["trades_today"] >= CONFIG["max_daily_trades"]:
        errors.append(f"已达当日最大下单次数 {CONFIG['max_daily_trades']} 笔。")

    account = _get(f"{TRADE_API}/v2/account")
    equity = float(account["equity"])
    price = _latest_price(symbol)

    # 不重复建仓 / 不摊平
    positions = _get(f"{TRADE_API}/v2/positions")
    if any(p["symbol"] == symbol for p in positions):
        errors.append(f"已持有 {symbol},规则禁止重复建仓或加仓摊平。")

    # 止损必须低于现价
    if stop_loss >= price:
        errors.append(f"止损价 {stop_loss} 必须低于当前价 {price:.2f}。")
    else:
        stop_dist_pct = (price - stop_loss) / price * 100
        if stop_dist_pct > CONFIG["max_stop_distance_pct"]:
            errors.append(
                f"止损距离 {stop_dist_pct:.2f}% 超过上限 {CONFIG['max_stop_distance_pct']}%。"
                f"本笔交易应放弃(规则:不通过缩小仓位强行进场)。"
            )

    # 仓位上限
    notional = qty * price
    max_notional = equity * CONFIG["max_position_pct_of_equity"] / 100
    if notional > max_notional:
        errors.append(
            f"仓位金额 ${notional:,.0f} 超过账户 {CONFIG['max_position_pct_of_equity']}% 上限"
            f" (${max_notional:,.0f})。最多可买 {int(max_notional // price)} 股。"
        )

    ctx = {"price": price, "equity": equity, "notional": notional,
           "stop_dist_pct": round((price - stop_loss) / price * 100, 2) if stop_loss < price else None}
    return errors, ctx


# ---------------- MCP 工具 ----------------

@mcp.tool()
def get_account() -> str:
    """查询账户概况:总市值、现金、购买力、当日盈亏。只读操作。"""
    a = _get(f"{TRADE_API}/v2/account")
    return json.dumps({
        "equity": a["equity"], "cash": a["cash"],
        "buying_power": a["buying_power"],
        "kill_switch_active": KILL_SWITCH.exists(),
        "trades_today": _load_state()["trades_today"],
        "max_daily_trades": CONFIG["max_daily_trades"],
    }, ensure_ascii=False)


@mcp.tool()
def get_positions() -> str:
    """查询当前全部持仓:标的、数量、成本、现价、浮动盈亏。只读操作。"""
    ps = _get(f"{TRADE_API}/v2/positions")
    out = [{"symbol": p["symbol"], "qty": p["qty"], "avg_entry": p["avg_entry_price"],
            "current": p["current_price"], "unrealized_pl": p["unrealized_pl"],
            "unrealized_plpc": f'{float(p["unrealized_plpc"]) * 100:.2f}%'} for p in ps]
    return json.dumps(out or {"message": "当前空仓"}, ensure_ascii=False)


@mcp.tool()
def get_quote(symbol: str) -> str:
    """查询单个标的的实时行情:最新价、当日开高低、昨收、成交量。
    symbol: 美股代码,如 NVDA。只读操作。"""
    symbol = symbol.upper()
    snap = _get(f"{DATA_API}/v2/stocks/{symbol}/snapshot")
    d, prev = snap.get("dailyBar", {}), snap.get("prevDailyBar", {})
    return json.dumps({
        "symbol": symbol,
        "latest_price": snap.get("latestTrade", {}).get("p"),
        "day_open": d.get("o"), "day_high": d.get("h"),
        "day_low": d.get("l"), "day_volume": d.get("v"),
        "prev_close": prev.get("c"), "prev_high": prev.get("h"),
        "note": "day_low 即当日最低价,按规则用作止损参考",
    }, ensure_ascii=False)


@mcp.tool()
def get_daily_bars(symbol: str, days: int = 30) -> str:
    """获取标的最近 N 个交易日的日 K 线 (开高低收/成交量),用于识别阻力位、
    前高、均线趋势等。symbol: 股票代码; days: 天数,默认 30,最大 120。只读操作。"""
    days = min(days, 120)
    data = _get(f"{DATA_API}/v2/stocks/{symbol.upper()}/bars",
                params={"timeframe": "1Day", "limit": days, "adjustment": "split"})
    bars = [{"date": b["t"][:10], "o": b["o"], "h": b["h"], "l": b["l"],
             "c": b["c"], "v": b["v"]} for b in data.get("bars", [])]
    return json.dumps(bars, ensure_ascii=False)


@mcp.tool()
def preview_bracket_buy(symbol: str, qty: float, stop_loss: float) -> str:
    """【下单第一步】预览一笔带止损的买入订单并执行全部风控检查。
    不会真的下单。返回检查结果;若通过,返回 confirm_token。
    须把预览内容完整汇报给用户,拿到用户明确同意后,
    再用 confirm_token 调用 place_bracket_buy 执行。
    symbol: 股票代码; qty: 股数; stop_loss: 止损价(按规则=当日最低价)。"""
    errors, ctx = _risk_check(symbol, qty, stop_loss)
    if errors:
        return json.dumps({"approved": False, "errors": errors}, ensure_ascii=False)
    token = _order_fingerprint(symbol.upper(), qty, stop_loss)
    return json.dumps({
        "approved": True,
        "summary": {
            "symbol": symbol.upper(), "qty": qty,
            "est_price": ctx["price"], "est_notional": round(ctx["notional"], 2),
            "pct_of_equity": round(ctx["notional"] / ctx["equity"] * 100, 1),
            "stop_loss": stop_loss, "stop_distance_pct": ctx["stop_dist_pct"],
            "max_loss_usd": round((ctx["price"] - stop_loss) * qty, 2),
        },
        "confirm_token": token,
        "instruction": "请将 summary 汇报给用户;用户明确回复同意后,调用 place_bracket_buy 并传入此 token。",
    }, ensure_ascii=False)


@mcp.tool()
def place_bracket_buy(symbol: str, qty: float, stop_loss: float, confirm_token: str = "") -> str:
    """【下单第二步】以市价买入并同时挂止损单 (bracket order)。
    风控会再次完整校验;配置要求确认时,必须传入 preview 返回且用户已同意的 confirm_token。
    symbol: 股票代码; qty: 股数; stop_loss: 止损价; confirm_token: 预览令牌。"""
    symbol = symbol.upper()
    errors, _ = _risk_check(symbol, qty, stop_loss)
    if errors:
        return json.dumps({"ok": False, "errors": errors}, ensure_ascii=False)

    state = _load_state()
    if CONFIG.get("require_confirmation", True):
        expected = _order_fingerprint(symbol, qty, stop_loss)
        if confirm_token != expected:
            return json.dumps({"ok": False, "errors": [
                "confirm_token 缺失或与订单参数不符。请先调用 preview_bracket_buy,"
                "把结果汇报给用户并获得同意后再执行。"]}, ensure_ascii=False)
        if confirm_token in state["used_tokens"]:
            return json.dumps({"ok": False, "errors": ["该确认令牌已被使用,请重新预览。"]},
                              ensure_ascii=False)

    order = _post(f"{TRADE_API}/v2/orders", {
        "symbol": symbol, "qty": str(qty), "side": "buy",
        "type": "market", "time_in_force": "day",
        "order_class": "oto",
        "stop_loss": {"stop_price": str(stop_loss)},
    })
    state["trades_today"] += 1
    if confirm_token:
        state["used_tokens"].append(confirm_token)
    _save_state(state)
    return json.dumps({"ok": True, "order_id": order["id"], "status": order["status"],
                       "message": f"已提交 {symbol} 市价买入 {qty} 股,止损 {stop_loss}。"},
                      ensure_ascii=False)


@mcp.tool()
def move_stop_up(symbol: str, new_stop: float) -> str:
    """上移某持仓的止损价以保护利润。硬规则:新止损必须高于旧止损,禁止下移。
    symbol: 股票代码; new_stop: 新止损价。"""
    if KILL_SWITCH.exists():
        return json.dumps({"ok": False, "errors": ["急停开关已激活,禁止改单。"]}, ensure_ascii=False)
    symbol = symbol.upper()
    orders = _get(f"{TRADE_API}/v2/orders", params={"status": "open", "symbols": symbol})
    stops = [o for o in orders if o["type"] == "stop" and o["side"] == "sell"]
    if not stops:
        return json.dumps({"ok": False, "errors": [f"{symbol} 没有找到未成交的止损单。"]}, ensure_ascii=False)
    old = stops[0]
    if new_stop <= float(old["stop_price"]):
        return json.dumps({"ok": False, "errors": [
            f"新止损 {new_stop} 不高于当前止损 {old['stop_price']}。止损只允许上移。"]}, ensure_ascii=False)
    r = requests.patch(f"{TRADE_API}/v2/orders/{old['id']}", headers=HEADERS,
                       json={"stop_price": str(new_stop)}, timeout=15)
    if r.status_code >= 400:
        return json.dumps({"ok": False, "errors": [r.text[:300]]}, ensure_ascii=False)
    return json.dumps({"ok": True, "message": f"{symbol} 止损已从 {old['stop_price']} 上移至 {new_stop}。"},
                      ensure_ascii=False)


@mcp.tool()
def list_open_orders() -> str:
    """查询所有未成交订单(含挂着的止损单)。只读操作。"""
    orders = _get(f"{TRADE_API}/v2/orders", params={"status": "open"})
    out = [{"id": o["id"], "symbol": o["symbol"], "side": o["side"], "type": o["type"],
            "qty": o["qty"], "stop_price": o.get("stop_price"),
            "limit_price": o.get("limit_price"), "status": o["status"]} for o in orders]
    return json.dumps(out or {"message": "没有未成交订单"}, ensure_ascii=False)


if __name__ == "__main__":
    if not HEADERS["APCA-API-KEY-ID"]:
        raise SystemExit("请先设置环境变量 APCA_API_KEY_ID / APCA_API_SECRET_KEY (Alpaca 模拟盘 key)")
    mcp.run(transport="stdio")
