#!/usr/bin/env python3
"""
TradingView 警报 webhook 接收服务

链路:TradingView 警报 → POST /hook/{secret} → 复用 trading_server 硬风控预检
→ 专用 Telegram bot 推送订单摘要 + 确认按钮 → 人工确认后下单 Alpaca 模拟盘。

设计原则:
1. 风控与确认令牌全部复用 trading_server,本文件不重写任何风控规则。
2. 信号有时效(risk_config.json 的 webhook_signal_ttl_minutes),超时作废。
3. 宁可漏信号,不可重复下单:Telegram 调用失败只记日志,不重试下单动作。

运行:uv run python webhook_server.py(监听 127.0.0.1:8787,公网由 cloudflared 暴露)
"""

import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor
from pathlib import Path

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, field_validator

import trading_server as ts

BASE_DIR = Path(__file__).resolve().parent
SIGNALS_LOG = BASE_DIR / "signals_log.jsonl"

RISK_PCT = float(ts.CONFIG.get("webhook_risk_pct_per_trade", 1))
TTL_MINUTES = float(ts.CONFIG.get("webhook_signal_ttl_minutes", 30))


# ---------------- 信号 payload ----------------

class TVSignal(BaseModel):
    """TradingView 警报消息体。警报模板见 README。"""
    secret: str
    action: str
    symbol: str
    price: float | None = None   # TradingView 触发价,仅记日志,下单以实时行情为准
    stop: float | None = None    # 可选;不传则服务端取当日最低价

    @field_validator("action")
    @classmethod
    def _only_buy(cls, v: str) -> str:
        if v != "buy":
            raise ValueError("action 只支持 buy(本系统只做多)")
        return v

    @field_validator("symbol")
    @classmethod
    def _us_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.isalpha() or not (1 <= len(v) <= 5):
            raise ValueError("symbol 必须是 1-5 位字母的美股代码")
        return v


# ---------------- 纯函数 ----------------

def check_secret(path_secret: str, body_secret: str) -> bool:
    """URL path 与 body 双重比对 WEBHOOK_SECRET;环境变量缺失一律拒绝。"""
    expected = os.environ.get("WEBHOOK_SECRET", "").encode()
    if not expected:
        return False
    return (hmac.compare_digest(path_secret.encode(), expected)
            and hmac.compare_digest(body_secret.encode(), expected))


def calc_qty(equity: float, price: float, stop: float,
             risk_pct: float, max_pos_pct: float) -> int:
    """按单笔风险比例算股数,再按仓位上限截断。算不出正数返回 0(信号放弃)。"""
    if price <= 0 or stop >= price:
        return 0
    qty = floor(equity * risk_pct / 100 / (price - stop))
    cap = floor(equity * max_pos_pct / 100 / price)
    return max(0, min(qty, cap))


def log_event(event: str, **data) -> None:
    """追加一行审计日志到 signals_log.jsonl,方便逐笔复盘。"""
    line = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}
    with SIGNALS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


# ---------------- 待确认信号 ----------------

@dataclass
class PendingSignal:
    signal_id: str
    symbol: str
    qty: int
    stop: float
    confirm_token: str
    summary_text: str
    expires_at: float            # time.time() 时间戳
    tg_message_id: int | None = None


class PendingStore:
    """内存中的待确认信号。进程重启即清空(可接受,等新信号)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, PendingSignal] = {}

    def add(self, sig: PendingSignal) -> None:
        with self._lock:
            self._items[sig.signal_id] = sig

    def get(self, signal_id: str) -> PendingSignal | None:
        with self._lock:
            return self._items.get(signal_id)

    def remove(self, signal_id: str) -> None:
        with self._lock:
            self._items.pop(signal_id, None)

    def has_symbol(self, symbol: str) -> bool:
        with self._lock:
            return any(s.symbol == symbol for s in self._items.values())

    def pop_expired(self, now: float) -> list[PendingSignal]:
        with self._lock:
            expired = [s for s in self._items.values() if now > s.expires_at]
            for s in expired:
                self._items.pop(s.signal_id, None)
            return expired


PENDING = PendingStore()


# ---------------- Telegram(手写 Bot API,不引框架) ----------------

TG_API = "https://api.telegram.org"


def build_confirm_keyboard(signal_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ 确认下单", "callback_data": f"confirm:{signal_id}"},
        {"text": "❌ 放弃", "callback_data": f"reject:{signal_id}"},
    ]]}


def parse_callback_data(data: str) -> tuple[str, str] | None:
    action, _, sid = data.partition(":")
    if action not in ("confirm", "reject") or not sid:
        return None
    return action, sid


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self.base = f"{TG_API}/bot{token}"
        self.chat_id = chat_id

    def _call(self, method: str, payload: dict) -> dict | list | None:
        """统一封装;失败只记 stderr 返回 None(宁可漏通知,不让异常中断主流程)。"""
        try:
            r = requests.post(f"{self.base}/{method}", json=payload, timeout=35)
            data = r.json()
            if not data.get("ok"):
                print(f"[telegram] {method} 失败: {str(data)[:200]}", file=sys.stderr)
                return None
            return data["result"]
        except Exception as e:
            print(f"[telegram] {method} 异常: {e}", file=sys.stderr)
            return None

    def send_text(self, text: str, reply_markup: dict | None = None) -> int | None:
        payload: dict = {"chat_id": self.chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        return result["message_id"] if isinstance(result, dict) else None

    def edit_text(self, message_id: int, text: str) -> None:
        self._call("editMessageText",
                   {"chat_id": self.chat_id, "message_id": message_id, "text": text})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery",
                   {"callback_query_id": callback_id, "text": text})

    def get_updates(self, offset: int) -> list[dict]:
        result = self._call("getUpdates", {"offset": offset, "timeout": 25,
                                           "allowed_updates": ["callback_query"]})
        return result if isinstance(result, list) else []


TG: TelegramClient | None = None


def _tg() -> TelegramClient:
    """懒加载单例;测试里直接 monkeypatch 全局 TG。"""
    global TG
    if TG is None:
        TG = TelegramClient(os.environ.get("TG_BOT_TOKEN", ""),
                            os.environ.get("TG_CHAT_ID", ""))
    return TG


# ---------------- 信号处理 ----------------

_SIGNAL_LOCK = threading.Lock()  # 串行处理信号,消除去重检查与入队之间的竞态


def _market_open() -> bool:
    return bool(ts._get(f"{ts.TRADE_API}/v2/clock").get("is_open"))


def _day_low(symbol: str) -> float | None:
    """当日最低价,按交易规则用作默认止损。"""
    snap = ts._get(f"{ts.DATA_API}/v2/stocks/{symbol}/snapshot")
    low = (snap.get("dailyBar") or {}).get("l")
    return float(low) if low is not None else None


def handle_signal(sig: TVSignal) -> None:
    """TradingView 信号完整处理:盘中检查→去重→定止损→算股数→风控预检→推送确认。"""
    with _SIGNAL_LOCK:
        symbol = sig.symbol
        try:
            log_event("signal_received", symbol=symbol, tv_price=sig.price, tv_stop=sig.stop)
            if not _market_open():
                log_event("signal_ignored", symbol=symbol, reason="market_closed")
                _tg().send_text(f"⏸ {symbol} 信号已忽略:当前非常规盘中时段。")
                return
            if PENDING.has_symbol(symbol):
                log_event("signal_ignored", symbol=symbol, reason="duplicate_pending")
                _tg().send_text(f"⏸ {symbol} 信号已忽略:已有同标的待确认信号。")
                return

            stop = sig.stop if sig.stop is not None else _day_low(symbol)
            if stop is None:
                log_event("signal_rejected", symbol=symbol, reason="no_day_low")
                _tg().send_text(f"❌ {symbol} 信号作废:无法获取当日最低价作为止损。")
                return

            price = ts._latest_price(symbol)
            equity = float(ts._get(f"{ts.TRADE_API}/v2/account")["equity"])
            qty = calc_qty(equity, price, stop, RISK_PCT,
                           float(ts.CONFIG["max_position_pct_of_equity"]))
            if qty < 1:
                log_event("signal_rejected", symbol=symbol, reason="qty_lt_1",
                          price=price, stop=stop)
                _tg().send_text(f"❌ {symbol} 信号作废:按 {RISK_PCT}% 风险算出股数"
                                f"不足 1 股(现价 {price:.2f} 止损 {stop:.2f})。")
                return

            preview = json.loads(ts.preview_bracket_buy(symbol, qty, stop))
            if not preview.get("approved"):
                errors = preview.get("errors", [])
                log_event("signal_rejected", symbol=symbol, reason="risk_check", errors=errors)
                _tg().send_text(f"❌ {symbol} 信号被风控拒绝:\n- " + "\n- ".join(errors))
                return

            s = preview["summary"]
            signal_id = secrets.token_hex(4)
            text = (f"📈 TradingView 信号:买入 {s['symbol']}\n"
                    f"股数: {s['qty']}   现价: ${s['est_price']}\n"
                    f"金额: ${s['est_notional']:,}({s['pct_of_equity']}% 仓位)\n"
                    f"止损: {s['stop_loss']}(距离 {s['stop_distance_pct']}%)\n"
                    f"最大亏损: ${s['max_loss_usd']:,}\n"
                    f"⏰ {int(TTL_MINUTES)} 分钟内有效")
            pending = PendingSignal(signal_id=signal_id, symbol=symbol, qty=qty, stop=stop,
                                    confirm_token=preview["confirm_token"],
                                    summary_text=text,
                                    expires_at=time.time() + TTL_MINUTES * 60)
            msg_id = _tg().send_text(text, build_confirm_keyboard(signal_id))
            if msg_id is None:
                # 卡片没送达就不入队:否则信号既无按钮可点,又占着同 symbol 去重位,
                # Telegram 恢复后重发的警报反而会被挡掉
                log_event("signal_push_failed", symbol=symbol, signal_id=signal_id)
                return
            pending.tg_message_id = msg_id
            PENDING.add(pending)
            log_event("signal_pending", symbol=symbol, signal_id=signal_id,
                      qty=qty, stop=stop)
        except Exception as e:
            log_event("signal_error", symbol=symbol, error=str(e))
            _tg().send_text(f"⚠️ {symbol} 信号处理出错:{str(e)[:200]}")


# ---------------- HTTP 入口 ----------------

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/hook/{path_secret}")
async def hook(path_secret: str, sig: TVSignal, background_tasks: BackgroundTasks):
    """TradingView 警报入口。鉴权后立即返回 200,处理放后台(TradingView 超时很短)。"""
    # 注:FastAPI 先做 pydantic 校验再进函数体,payload 非法时返回 422 早于鉴权 403。
    # 属有意取舍:schema 本就公开在 README,422 不泄露 secret 相关信息
    if not check_secret(path_secret, sig.secret):
        raise HTTPException(status_code=403)
    background_tasks.add_task(handle_signal, sig)
    return {"ok": True}


# ---------------- 确认回调与轮询 ----------------

def handle_callback(cb: dict) -> None:
    """处理 Telegram 按钮点击。只信任 TG_CHAT_ID 白名单用户。"""
    cb_id = cb.get("id", "")
    if str(cb.get("from", {}).get("id")) != os.environ.get("TG_CHAT_ID", ""):
        log_event("callback_unauthorized", from_id=str(cb.get("from", {}).get("id")))
        _tg().answer_callback(cb_id, "无权操作")
        return
    parsed = parse_callback_data(cb.get("data", ""))
    if not parsed:
        _tg().answer_callback(cb_id)
        return
    action, signal_id = parsed
    pending = PENDING.get(signal_id)
    if pending is None:
        log_event("callback_unknown_signal", data=cb.get("data", ""))
        _tg().answer_callback(cb_id, "信号不存在或已处理")
        return

    if action == "reject":
        PENDING.remove(signal_id)
        _tg().answer_callback(cb_id, "已放弃")
        if pending.tg_message_id:
            _tg().edit_text(pending.tg_message_id, pending.summary_text + "\n\n❌ 已放弃")
        log_event("signal_discarded", symbol=pending.symbol, signal_id=signal_id)
        return

    if time.time() > pending.expires_at:
        PENDING.remove(signal_id)
        _tg().answer_callback(cb_id, "已过期")
        if pending.tg_message_id:
            _tg().edit_text(pending.tg_message_id,
                            pending.summary_text + "\n\n⌛ 已过期,未下单")
        log_event("signal_expired", symbol=pending.symbol, signal_id=signal_id)
        return

    # 确认下单:先移除 pending 再下单(fail-closed:下单抛异常时信号作废,
    # 绝不留下可重按确认的入口 —— 宁可漏信号,不可重复下单)。
    # place_bracket_buy 内部会再跑一遍完整风控
    PENDING.remove(signal_id)
    try:
        result = json.loads(ts.place_bracket_buy(pending.symbol, pending.qty,
                                                 pending.stop, pending.confirm_token))
    except Exception as e:
        _tg().answer_callback(cb_id, "下单异常")
        if pending.tg_message_id:
            _tg().edit_text(pending.tg_message_id,
                            pending.summary_text
                            + f"\n\n⚠️ 下单异常,信号已作废,请人工核查 Alpaca 面板:{str(e)[:200]}")
        log_event("order_error", symbol=pending.symbol, signal_id=signal_id, error=str(e))
        return
    if result.get("ok"):
        _tg().answer_callback(cb_id, "已下单")
        if pending.tg_message_id:
            _tg().edit_text(pending.tg_message_id,
                            pending.summary_text + f"\n\n✅ {result.get('message', '已提交')}")
        log_event("order_placed", symbol=pending.symbol, signal_id=signal_id,
                  order_id=result.get("order_id"))
    else:
        errors = result.get("errors", [])
        _tg().answer_callback(cb_id, "下单失败")
        if pending.tg_message_id:
            _tg().edit_text(pending.tg_message_id,
                            pending.summary_text + "\n\n❌ 下单失败:\n- " + "\n- ".join(errors))
        log_event("order_failed", symbol=pending.symbol, signal_id=signal_id,
                  errors=errors)


def sweep_expired() -> None:
    """懒清理:把超时 pending 的消息标为已过期。由轮询线程每轮顺带调用。"""
    for sig in PENDING.pop_expired(time.time()):
        if sig.tg_message_id:
            _tg().edit_text(sig.tg_message_id, sig.summary_text + "\n\n⌛ 已过期,未下单")
        log_event("signal_expired", symbol=sig.symbol, signal_id=sig.signal_id)


def poll_loop() -> None:
    """Telegram 长轮询线程主循环。"""
    offset = 0
    while True:
        try:
            for update in _tg().get_updates(offset):
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
            sweep_expired()
        except Exception as e:
            print(f"[poll] 异常: {e}", file=sys.stderr)
            time.sleep(5)


# ---------------- 入口 ----------------

def main() -> None:
    missing = [k for k in ("APCA_API_KEY_ID", "WEBHOOK_SECRET", "TG_BOT_TOKEN", "TG_CHAT_ID")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"缺少环境变量: {', '.join(missing)}(请在 .env 中配置)")
    threading.Thread(target=poll_loop, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    main()
