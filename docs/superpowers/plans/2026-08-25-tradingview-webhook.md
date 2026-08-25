# TradingView Webhook 信号接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** TradingView 警报经 webhook 进入现有硬风控流程,Telegram 按钮确认后在 Alpaca 模拟盘下单。

**Architecture:** 新增单文件 `webhook_server.py` 独立进程:FastAPI 收 `POST /hook/{secret}`,后台线程用 requests 长轮询专用 Telegram bot 处理确认按钮。直接 `import trading_server` 复用 `preview_bracket_buy` / `place_bracket_buy` 等函数(已验证 `@mcp.tool()` 装饰后仍是普通可调用函数),`.env`、`risk_config.json`、`KILL_SWITCH`、`state.json` 天然共享。待确认信号存内存 `PendingStore`。

**Tech Stack:** Python 3.12、FastAPI + uvicorn、requests(手写 Telegram API)、pytest + httpx(测试)、cloudflared(部署,不在代码内)。

**Spec:** `docs/superpowers/specs/2026-08-25-tradingview-webhook-design.md`

## Global Constraints

- 只做多、仅美股;`action` 只接受 `"buy"`,`symbol` 为 1–5 位字母
- 不修改 `trading_server.py` 的任何一行
- 风控与确认令牌全部复用 `trading_server` 现有实现,不在新代码里重写风控规则
- 新配置字段:`webhook_risk_pct_per_trade`(默认 1)、`webhook_signal_ttl_minutes`(默认 30),读不到时用默认值
- 新环境变量:`WEBHOOK_SECRET`、`TG_BOT_TOKEN`、`TG_CHAT_ID`(`.env` 已被 hook 保护,不可用 Read 工具读,追加内容用 shell)
- Telegram 只信任 `TG_CHAT_ID`;webhook 鉴权 URL path 与 body 双重比对,失败返回 403
- 所有事件追加写 `signals_log.jsonl`;宁可漏信号,不可重复下单(Telegram 失败不重试下单动作)
- 服务监听 `127.0.0.1:8787`,公网暴露由 cloudflared 负责
- 中文注释与用户可见文案,风格与 `trading_server.py` 一致

---

### Task 1: 依赖、配置与纯函数层(payload 校验 / qty 计算 / 鉴权 / 审计日志)

**Files:**

- Modify: `pyproject.toml`(uv 命令自动改)
- Modify: `risk_config.json`
- Modify: `.env.example`(shell 追加)
- Create: `webhook_server.py`
- Test: `tests/test_webhook_pure.py`

**Interfaces:**

- Consumes: `trading_server.CONFIG`(已加载的 risk_config dict)
- Produces(后续任务依赖的确切签名):
  - `class TVSignal(BaseModel)`:字段 `secret: str, action: str, symbol: str, price: float | None = None, stop: float | None = None`;校验失败抛 `pydantic.ValidationError`,`symbol` 自动大写
  - `check_secret(path_secret: str, body_secret: str) -> bool`
  - `calc_qty(equity: float, price: float, stop: float, risk_pct: float, max_pos_pct: float) -> int`
  - `log_event(event: str, **data) -> None`(追加写 `SIGNALS_LOG`)
  - 模块常量 `RISK_PCT: float`、`TTL_MINUTES: float`、`SIGNALS_LOG: Path`

- [ ] **Step 1: 添加依赖**

```bash
cd /Users/xue/hermes-swing-trader
uv add "fastapi>=0.115" "uvicorn>=0.30"
uv add --dev "pytest>=8" "httpx>=0.27"
```

- [ ] **Step 2: pyproject.toml 追加 pytest 配置**(让 `tests/` 能 import 根目录模块)

在 `pyproject.toml` 末尾追加:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

- [ ] **Step 3: 更新 risk_config.json**(整文件替换为)

```json
{
  "max_position_pct_of_equity": 30,
  "max_stop_distance_pct": 5,
  "max_daily_trades": 6,
  "require_confirmation": true,
  "symbol_whitelist": [],
  "allow_short": false,
  "webhook_risk_pct_per_trade": 1,
  "webhook_signal_ttl_minutes": 30,
  "notes": {
    "max_position_pct_of_equity": "单笔仓位金额占账户总值的最大百分比",
    "max_stop_distance_pct": "止损价距离入场价的最大百分比,超过则拒单",
    "max_daily_trades": "每日最大下单次数",
    "require_confirmation": "true 时 place_bracket_buy 需要传 confirm_token(由 preview 返回),实现「先回报、你确认、再执行」",
    "symbol_whitelist": "留空 [] 表示不限制;填 [\"NVDA\",\"AAPL\"] 则只允许这些标的",
    "allow_short": "本系统只做多,保持 false",
    "webhook_risk_pct_per_trade": "TradingView 信号自动算仓位时,单笔风险(现价-止损)×股数 占账户净值的百分比",
    "webhook_signal_ttl_minutes": "TradingView 信号推送到 Telegram 后的确认时效(分钟),超时作废"
  }
}
```

- [ ] **Step 4: 追加 .env.example**(该类文件被 hook 拦截 Read,用 shell 追加,勿用 Read/Edit 工具)

```bash
cat >> /Users/xue/hermes-swing-trader/.env.example <<'EOF'

# --- TradingView webhook 接收服务 (webhook_server.py) ---
# 随机串,生成方法: python -c "import secrets; print(secrets.token_urlsafe(24))"
WEBHOOK_SECRET=
# 信号确认专用 Telegram bot(找 @BotFather 新建,与 Hermes 的 bot 分开)
TG_BOT_TOKEN=
# 你的 Telegram 数字 chat id(私聊 @userinfobot 可查),既是推送目标也是操作白名单
TG_CHAT_ID=
EOF
```

- [ ] **Step 5: 写失败测试** `tests/test_webhook_pure.py`

```python
import json

import pytest
from pydantic import ValidationError

import webhook_server as ws


# ---- calc_qty ----

def test_calc_qty_normal():
    # 净值 10 万,风险 1% = $1000;现价 100 止损 95,每股风险 $5 → 200 股
    # 仓位上限 30% = $30000 → 300 股,不构成约束
    assert ws.calc_qty(100_000, 100, 95, 1, 30) == 200


def test_calc_qty_clamped_by_position_limit():
    # 止损极近:每股风险 $0.5 → 2000 股 = $20 万,被 30% 上限截为 300 股
    assert ws.calc_qty(100_000, 100, 99.5, 1, 30) == 300


def test_calc_qty_zero_when_stop_not_below_price():
    assert ws.calc_qty(100_000, 100, 101, 1, 30) == 0
    assert ws.calc_qty(100_000, 100, 100, 1, 30) == 0


def test_calc_qty_zero_when_risk_budget_too_small():
    # 净值 1000,风险 1% = $10,每股风险 $20 → 0 股
    assert ws.calc_qty(1_000, 100, 80, 1, 30) == 0


# ---- TVSignal 校验 ----

def test_signal_rejects_non_buy_action():
    with pytest.raises(ValidationError):
        ws.TVSignal(secret="s", action="sell", symbol="NVDA")


def test_signal_rejects_bad_symbol():
    for bad in ("NVDA.US", "BRK.B", "TOOLONG", "", "12AB"):
        with pytest.raises(ValidationError):
            ws.TVSignal(secret="s", action="buy", symbol=bad)


def test_signal_normalizes_symbol_and_optional_fields():
    sig = ws.TVSignal(secret="s", action="buy", symbol="nvda", price=100.5)
    assert sig.symbol == "NVDA"
    assert sig.stop is None


# ---- check_secret ----

def test_check_secret_requires_both_match(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "topsecret")
    assert ws.check_secret("topsecret", "topsecret")
    assert not ws.check_secret("topsecret", "wrong")
    assert not ws.check_secret("wrong", "topsecret")


def test_check_secret_false_when_env_missing(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    assert not ws.check_secret("", "")


# ---- log_event ----

def test_log_event_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "SIGNALS_LOG", tmp_path / "log.jsonl")
    ws.log_event("signal_received", symbol="NVDA")
    ws.log_event("signal_rejected", symbol="AAPL", reason="risk")
    lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "signal_received"
    assert first["symbol"] == "NVDA"
    assert "ts" in first
```

- [ ] **Step 6: 运行确认失败**

Run: `uv run pytest tests/test_webhook_pure.py -v`
Expected: 全部 FAIL/ERROR,报 `ModuleNotFoundError: No module named 'webhook_server'`

- [ ] **Step 7: 创建 `webhook_server.py`**(第一部分)

```python
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
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if not expected:
        return False
    return (hmac.compare_digest(path_secret, expected)
            and hmac.compare_digest(body_secret, expected))


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
```

- [ ] **Step 8: 运行确认通过**

Run: `uv run pytest tests/test_webhook_pure.py -v`
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock risk_config.json .env.example webhook_server.py tests/test_webhook_pure.py
git commit -m "feat: webhook 服务纯函数层(payload 校验/qty 计算/鉴权/审计日志)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: PendingStore 待确认信号存储(含 TTL)

**Files:**

- Modify: `webhook_server.py`(文件末尾追加)
- Test: `tests/test_pending_store.py`

**Interfaces:**

- Consumes: 无(纯内存结构)
- Produces:
  - `@dataclass class PendingSignal`:字段 `signal_id: str, symbol: str, qty: int, stop: float, confirm_token: str, summary_text: str, expires_at: float, tg_message_id: int | None = None`
  - `class PendingStore`:方法 `add(sig: PendingSignal) -> None`、`get(signal_id: str) -> PendingSignal | None`、`remove(signal_id: str) -> None`、`has_symbol(symbol: str) -> bool`、`pop_expired(now: float) -> list[PendingSignal]`
  - 模块级单例 `PENDING = PendingStore()`(测试中用 `monkeypatch.setattr(ws, "PENDING", ws.PendingStore())` 换新)

- [ ] **Step 1: 写失败测试** `tests/test_pending_store.py`

```python
import webhook_server as ws


def make_sig(sid="abc123", symbol="NVDA", expires_at=1000.0):
    return ws.PendingSignal(signal_id=sid, symbol=symbol, qty=10, stop=95.0,
                            confirm_token="tok", summary_text="text",
                            expires_at=expires_at)


def test_add_get_remove():
    store = ws.PendingStore()
    sig = make_sig()
    store.add(sig)
    assert store.get("abc123") is sig
    store.remove("abc123")
    assert store.get("abc123") is None
    store.remove("abc123")  # 重复删除不报错


def test_has_symbol():
    store = ws.PendingStore()
    store.add(make_sig(symbol="NVDA"))
    assert store.has_symbol("NVDA")
    assert not store.has_symbol("AAPL")


def test_pop_expired_removes_and_returns_only_expired():
    store = ws.PendingStore()
    old = make_sig(sid="old", symbol="NVDA", expires_at=100.0)
    fresh = make_sig(sid="new", symbol="AAPL", expires_at=9999.0)
    store.add(old)
    store.add(fresh)
    expired = store.pop_expired(now=500.0)
    assert expired == [old]
    assert store.get("old") is None
    assert store.get("new") is fresh
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_pending_store.py -v`
Expected: FAIL,报 `AttributeError: ... has no attribute 'PendingSignal'`

- [ ] **Step 3: 实现**(`webhook_server.py` 末尾追加)

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_pending_store.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add webhook_server.py tests/test_pending_store.py
git commit -m "feat: PendingStore 待确认信号存储与 TTL 过期

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Telegram 客户端(手写 Bot API)

**Files:**

- Modify: `webhook_server.py`(文件末尾追加)
- Test: `tests/test_telegram_client.py`

**Interfaces:**

- Consumes: `log_event`(Task 1)
- Produces:
  - `build_confirm_keyboard(signal_id: str) -> dict` — inline 键盘 payload,callback_data 为 `confirm:<sid>` / `reject:<sid>`
  - `parse_callback_data(data: str) -> tuple[str, str] | None` — 返回 `(action, signal_id)`,非法返回 None
  - `class TelegramClient`:构造 `(token: str, chat_id: str)`;方法 `send_text(text: str, reply_markup: dict | None = None) -> int | None`(返回 message_id,失败 None)、`edit_text(message_id: int, text: str) -> None`、`answer_callback(callback_id: str, text: str = "") -> None`、`get_updates(offset: int) -> list[dict]`
  - `_tg() -> TelegramClient` — 模块级懒加载单例(全局变量 `TG: TelegramClient | None = None`;测试中 `monkeypatch.setattr(ws, "TG", FakeTG())` 即可拦截)

- [ ] **Step 1: 写失败测试** `tests/test_telegram_client.py`

```python
import webhook_server as ws


# ---- 纯函数 ----

def test_build_confirm_keyboard():
    kb = ws.build_confirm_keyboard("abc123")
    row = kb["inline_keyboard"][0]
    assert row[0]["callback_data"] == "confirm:abc123"
    assert row[1]["callback_data"] == "reject:abc123"


def test_parse_callback_data():
    assert ws.parse_callback_data("confirm:abc123") == ("confirm", "abc123")
    assert ws.parse_callback_data("reject:abc123") == ("reject", "abc123")
    assert ws.parse_callback_data("nonsense") is None
    assert ws.parse_callback_data("delete:abc") is None
    assert ws.parse_callback_data("confirm:") is None


# ---- HTTP 封装(mock requests.post)----

class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_send_text_returns_message_id(monkeypatch):
    calls = {}

    def fake_post(url, json=None, timeout=None):
        calls["url"] = url
        calls["payload"] = json
        return FakeResp({"ok": True, "result": {"message_id": 42}})

    monkeypatch.setattr(ws.requests, "post", fake_post)
    client = ws.TelegramClient("TOKEN", "12345")
    msg_id = client.send_text("hello", ws.build_confirm_keyboard("x"))
    assert msg_id == 42
    assert calls["url"].endswith("/botTOKEN/sendMessage")
    assert calls["payload"]["chat_id"] == "12345"
    assert "reply_markup" in calls["payload"]


def test_send_text_returns_none_on_api_error(monkeypatch):
    monkeypatch.setattr(ws.requests, "post",
                        lambda *a, **k: FakeResp({"ok": False, "description": "bad"}))
    assert ws.TelegramClient("T", "1").send_text("hi") is None


def test_send_text_returns_none_on_exception(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("net down")

    monkeypatch.setattr(ws.requests, "post", boom)
    assert ws.TelegramClient("T", "1").send_text("hi") is None


def test_get_updates_returns_empty_list_on_failure(monkeypatch):
    monkeypatch.setattr(ws.requests, "post",
                        lambda *a, **k: FakeResp({"ok": False}))
    assert ws.TelegramClient("T", "1").get_updates(0) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_telegram_client.py -v`
Expected: FAIL,报 `AttributeError: ... 'build_confirm_keyboard'`

- [ ] **Step 3: 实现**(`webhook_server.py` 末尾追加)

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_telegram_client.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add webhook_server.py tests/test_telegram_client.py
git commit -m "feat: 手写 Telegram Bot API 客户端与确认键盘

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 信号处理核心 handle_signal

**Files:**

- Modify: `webhook_server.py`(文件末尾追加)
- Test: `tests/test_handle_signal.py`

**Interfaces:**

- Consumes: `TVSignal`、`calc_qty`、`log_event`、`PENDING`/`PendingSignal`、`_tg()`(前面任务);`ts._get`、`ts._latest_price`、`ts.preview_bracket_buy`、`ts.TRADE_API`、`ts.DATA_API`、`ts.CONFIG`
- Produces:
  - `handle_signal(sig: TVSignal) -> None` — 完整编排:盘中检查→去重→定 stop→算 qty→preview→推送卡片→入 PENDING
  - `_market_open() -> bool`、`_day_low(symbol: str) -> float | None`(测试挂桩点)

- [ ] **Step 1: 写失败测试** `tests/test_handle_signal.py`

```python
import json

import pytest

import trading_server as ts
import webhook_server as ws


class FakeTG:
    def __init__(self):
        self.sent = []          # [(text, reply_markup)]
        self.edited = []

    def send_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return 42

    def edit_text(self, message_id, text):
        self.edited.append((message_id, text))

    def answer_callback(self, callback_id, text=""):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake = FakeTG()
    monkeypatch.setattr(ws, "TG", fake)
    monkeypatch.setattr(ws, "PENDING", ws.PendingStore())
    monkeypatch.setattr(ws, "SIGNALS_LOG", tmp_path / "log.jsonl")
    return fake


def sig(symbol="NVDA", stop=None):
    return ws.TVSignal(secret="s", action="buy", symbol=symbol, price=100.0, stop=stop)


def test_market_closed_ignores_signal(env, monkeypatch):
    monkeypatch.setattr(ws, "_market_open", lambda: False)
    ws.handle_signal(sig())
    assert "非常规盘中" in env.sent[0][0]
    assert not ws.PENDING.has_symbol("NVDA")


def test_duplicate_pending_ignored(env, monkeypatch):
    monkeypatch.setattr(ws, "_market_open", lambda: True)
    ws.PENDING.add(ws.PendingSignal(signal_id="x", symbol="NVDA", qty=1, stop=1.0,
                                    confirm_token="t", summary_text="", expires_at=9e12))
    ws.handle_signal(sig())
    assert "待确认" in env.sent[0][0]


def test_qty_zero_rejected(env, monkeypatch):
    monkeypatch.setattr(ws, "_market_open", lambda: True)
    monkeypatch.setattr(ws, "_day_low", lambda s: 99.9)
    monkeypatch.setattr(ts, "_latest_price", lambda s: 100.0)
    # 净值太小 → qty 0
    monkeypatch.setattr(ts, "_get", lambda url, params=None: {"equity": "10"})
    ws.handle_signal(sig())
    assert "不足 1 股" in env.sent[0][0]
    assert not ws.PENDING.has_symbol("NVDA")


def test_risk_rejection_notified(env, monkeypatch):
    monkeypatch.setattr(ws, "_market_open", lambda: True)
    monkeypatch.setattr(ws, "_day_low", lambda s: 95.0)
    monkeypatch.setattr(ts, "_latest_price", lambda s: 100.0)
    monkeypatch.setattr(ts, "_get", lambda url, params=None: {"equity": "100000"})
    monkeypatch.setattr(ts, "preview_bracket_buy", lambda symbol, qty, stop_loss: json.dumps(
        {"approved": False, "errors": ["已达当日最大下单次数 6 笔。"]}))
    ws.handle_signal(sig())
    assert "风控拒绝" in env.sent[0][0]
    assert "6 笔" in env.sent[0][0]


def test_approved_signal_pushed_and_pending(env, monkeypatch):
    monkeypatch.setattr(ws, "_market_open", lambda: True)
    monkeypatch.setattr(ws, "_day_low", lambda s: 95.0)
    monkeypatch.setattr(ts, "_latest_price", lambda s: 100.0)
    monkeypatch.setattr(ts, "_get", lambda url, params=None: {"equity": "100000"})
    monkeypatch.setattr(ts, "preview_bracket_buy", lambda symbol, qty, stop_loss: json.dumps({
        "approved": True,
        "summary": {"symbol": symbol, "qty": qty, "est_price": 100.0,
                    "est_notional": qty * 100.0, "pct_of_equity": 20.0,
                    "stop_loss": stop_loss, "stop_distance_pct": 5.0,
                    "max_loss_usd": qty * 5.0},
        "confirm_token": "tok123",
    }))
    ws.handle_signal(sig())
    text, keyboard = env.sent[0]
    assert "NVDA" in text and "确认" in json.dumps(keyboard, ensure_ascii=False)
    assert ws.PENDING.has_symbol("NVDA")
    pending = next(iter(ws.PENDING._items.values()))
    assert pending.confirm_token == "tok123"
    assert pending.qty == 200          # 10万×1% / (100-95)
    assert pending.tg_message_id == 42


def test_uses_payload_stop_when_provided(env, monkeypatch):
    monkeypatch.setattr(ws, "_market_open", lambda: True)
    monkeypatch.setattr(ws, "_day_low", lambda s: (_ for _ in ()).throw(AssertionError("不该查 day_low")))
    monkeypatch.setattr(ts, "_latest_price", lambda s: 100.0)
    monkeypatch.setattr(ts, "_get", lambda url, params=None: {"equity": "100000"})
    captured = {}

    def fake_preview(symbol, qty, stop_loss):
        captured["stop"] = stop_loss
        return json.dumps({"approved": False, "errors": ["x"]})

    monkeypatch.setattr(ts, "preview_bracket_buy", fake_preview)
    ws.handle_signal(sig(stop=97.5))
    assert captured["stop"] == 97.5


def test_exception_reported_to_telegram(env, monkeypatch):
    def boom():
        raise RuntimeError("Alpaca API 错误 429")

    monkeypatch.setattr(ws, "_market_open", boom)
    ws.handle_signal(sig())
    assert "出错" in env.sent[0][0]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_handle_signal.py -v`
Expected: FAIL,报 `AttributeError: ... 'handle_signal'`(或 `_market_open`)

- [ ] **Step 3: 实现**(`webhook_server.py` 末尾追加)

```python
# ---------------- 信号处理 ----------------

def _market_open() -> bool:
    return bool(ts._get(f"{ts.TRADE_API}/v2/clock").get("is_open"))


def _day_low(symbol: str) -> float | None:
    """当日最低价,按交易规则用作默认止损。"""
    snap = ts._get(f"{ts.DATA_API}/v2/stocks/{symbol}/snapshot")
    low = (snap.get("dailyBar") or {}).get("l")
    return float(low) if low is not None else None


def handle_signal(sig: TVSignal) -> None:
    """TradingView 信号完整处理:盘中检查→去重→定止损→算股数→风控预检→推送确认。"""
    symbol = sig.symbol
    log_event("signal_received", symbol=symbol, tv_price=sig.price, tv_stop=sig.stop)
    try:
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
        pending.tg_message_id = _tg().send_text(text, build_confirm_keyboard(signal_id))
        PENDING.add(pending)
        log_event("signal_pending", symbol=symbol, signal_id=signal_id,
                  qty=qty, stop=stop)
    except Exception as e:
        log_event("signal_error", symbol=symbol, error=str(e))
        _tg().send_text(f"⚠️ {symbol} 信号处理出错:{str(e)[:200]}")
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_handle_signal.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 跑全量测试防回归**

Run: `uv run pytest -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add webhook_server.py tests/test_handle_signal.py
git commit -m "feat: handle_signal 信号编排(盘中检查/去重/定参/风控预检/推送)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: FastAPI webhook 端点

**Files:**

- Modify: `webhook_server.py`(文件末尾追加)
- Test: `tests/test_hook_endpoint.py`

**Interfaces:**

- Consumes: `TVSignal`、`check_secret`、`handle_signal`
- Produces: `app: FastAPI`,路由 `POST /hook/{path_secret}`;鉴权失败 403,payload 非法 422(pydantic 自动),成功 `{"ok": true}` 并后台执行 `handle_signal`

- [ ] **Step 1: 写失败测试** `tests/test_hook_endpoint.py`

```python
import pytest
from fastapi.testclient import TestClient

import webhook_server as ws


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "sekret")
    calls = []
    monkeypatch.setattr(ws, "handle_signal", lambda sig: calls.append(sig))
    c = TestClient(ws.app)
    c.calls = calls
    return c


def good_payload():
    return {"secret": "sekret", "action": "buy", "symbol": "NVDA", "price": 100.0}


def test_valid_signal_accepted(client):
    r = client.post("/hook/sekret", json=good_payload())
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(client.calls) == 1
    assert client.calls[0].symbol == "NVDA"


def test_wrong_path_secret_403(client):
    r = client.post("/hook/wrong", json=good_payload())
    assert r.status_code == 403
    assert client.calls == []


def test_wrong_body_secret_403(client):
    payload = good_payload() | {"secret": "wrong"}
    r = client.post("/hook/sekret", json=payload)
    assert r.status_code == 403
    assert client.calls == []


def test_invalid_payload_422(client):
    r = client.post("/hook/sekret", json={"secret": "sekret", "action": "sell",
                                          "symbol": "NVDA"})
    assert r.status_code == 422
    assert client.calls == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_hook_endpoint.py -v`
Expected: FAIL,报 `AttributeError: ... 'app'`

- [ ] **Step 3: 实现**(`webhook_server.py`:顶部 import 区加一行,末尾追加路由)

顶部 import 区(`import trading_server as ts` 之前)加:

```python
from fastapi import BackgroundTasks, FastAPI, HTTPException
```

文件末尾追加:

```python
# ---------------- HTTP 入口 ----------------

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/hook/{path_secret}")
async def hook(path_secret: str, sig: TVSignal, background_tasks: BackgroundTasks):
    """TradingView 警报入口。鉴权后立即返回 200,处理放后台(TradingView 超时很短)。"""
    if not check_secret(path_secret, sig.secret):
        raise HTTPException(status_code=403)
    background_tasks.add_task(handle_signal, sig)
    return {"ok": True}
```

说明:路由函数体内的 `handle_signal` 是运行时的模块全局查找,`monkeypatch.setattr(ws, "handle_signal", ...)` 改的正是模块 `__dict__`,所以测试能拦截到,直接用上面的最简写法即可。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_hook_endpoint.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add webhook_server.py tests/test_hook_endpoint.py
git commit -m "feat: FastAPI /hook/{secret} 端点,双重鉴权+后台处理

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 确认按钮回调与轮询线程

**Files:**

- Modify: `webhook_server.py`(文件末尾追加)
- Test: `tests/test_handle_callback.py`

**Interfaces:**

- Consumes: `PENDING`、`parse_callback_data`、`_tg()`、`log_event`;`ts.place_bracket_buy(symbol, qty, stop_loss, confirm_token) -> str`(JSON)
- Produces:
  - `handle_callback(cb: dict) -> None` — cb 是 Telegram callback_query 对象(取 `cb["from"]["id"]`、`cb["id"]`、`cb["data"]`)
  - `sweep_expired() -> None` — 清理过期 pending 并编辑其消息
  - `poll_loop() -> None` — 死循环:getUpdates → handle_callback → sweep_expired;异常打日志睡 5 秒

- [ ] **Step 1: 写失败测试** `tests/test_handle_callback.py`

```python
import json

import pytest

import trading_server as ts
import webhook_server as ws


class FakeTG:
    def __init__(self):
        self.sent, self.edited, self.answers = [], [], []

    def send_text(self, text, reply_markup=None):
        self.sent.append(text)
        return 42

    def edit_text(self, message_id, text):
        self.edited.append((message_id, text))

    def answer_callback(self, callback_id, text=""):
        self.answers.append(text)


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake = FakeTG()
    monkeypatch.setattr(ws, "TG", fake)
    monkeypatch.setattr(ws, "PENDING", ws.PendingStore())
    monkeypatch.setattr(ws, "SIGNALS_LOG", tmp_path / "log.jsonl")
    monkeypatch.setenv("TG_CHAT_ID", "777")
    return fake


def add_pending(expires_at=9e12):
    sig = ws.PendingSignal(signal_id="sid1", symbol="NVDA", qty=200, stop=95.0,
                           confirm_token="tok", summary_text="摘要",
                           expires_at=expires_at, tg_message_id=42)
    ws.PENDING.add(sig)
    return sig


def cb(data="confirm:sid1", user_id=777):
    return {"id": "cbid", "from": {"id": user_id}, "data": data}


def test_non_whitelisted_user_rejected(env):
    add_pending()
    ws.handle_callback(cb(user_id=666))
    assert env.answers == ["无权操作"]
    assert ws.PENDING.get("sid1") is not None      # 未被消费


def test_unknown_signal(env):
    ws.handle_callback(cb(data="confirm:nope"))
    assert "不存在或已处理" in env.answers[0]


def test_reject_flow(env):
    add_pending()
    ws.handle_callback(cb(data="reject:sid1"))
    assert ws.PENDING.get("sid1") is None
    assert "已放弃" in env.edited[0][1]


def test_expired_on_confirm(env):
    add_pending(expires_at=1.0)                    # 早已过期
    ws.handle_callback(cb())
    assert ws.PENDING.get("sid1") is None
    assert "已过期" in env.edited[0][1]


def test_confirm_places_order(env, monkeypatch):
    add_pending()
    captured = {}

    def fake_place(symbol, qty, stop_loss, confirm_token=""):
        captured.update(symbol=symbol, qty=qty, stop=stop_loss, token=confirm_token)
        return json.dumps({"ok": True, "order_id": "oid", "status": "accepted",
                           "message": "已提交 NVDA 市价买入 200 股,止损 95.0。"})

    monkeypatch.setattr(ts, "place_bracket_buy", fake_place)
    ws.handle_callback(cb())
    assert captured == {"symbol": "NVDA", "qty": 200, "stop": 95.0, "token": "tok"}
    assert ws.PENDING.get("sid1") is None
    assert "已提交" in env.edited[0][1]


def test_confirm_place_failure_reported(env, monkeypatch):
    add_pending()
    monkeypatch.setattr(ts, "place_bracket_buy",
                        lambda *a, **k: json.dumps({"ok": False, "errors": ["已持有 NVDA"]}))
    ws.handle_callback(cb())
    assert "下单失败" in env.edited[0][1]
    assert "已持有 NVDA" in env.edited[0][1]


def test_sweep_expired_edits_messages(env):
    add_pending(expires_at=1.0)
    ws.sweep_expired()
    assert ws.PENDING.get("sid1") is None
    assert "已过期" in env.edited[0][1]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_handle_callback.py -v`
Expected: FAIL,报 `AttributeError: ... 'handle_callback'`

- [ ] **Step 3: 实现**(`webhook_server.py` 末尾追加)

```python
# ---------------- 确认回调与轮询 ----------------

def handle_callback(cb: dict) -> None:
    """处理 Telegram 按钮点击。只信任 TG_CHAT_ID 白名单用户。"""
    cb_id = cb.get("id", "")
    if str(cb.get("from", {}).get("id")) != os.environ.get("TG_CHAT_ID", ""):
        _tg().answer_callback(cb_id, "无权操作")
        return
    parsed = parse_callback_data(cb.get("data", ""))
    if not parsed:
        _tg().answer_callback(cb_id)
        return
    action, signal_id = parsed
    pending = PENDING.get(signal_id)
    if pending is None:
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

    # 确认下单;place_bracket_buy 内部会再跑一遍完整风控
    result = json.loads(ts.place_bracket_buy(pending.symbol, pending.qty,
                                             pending.stop, pending.confirm_token))
    PENDING.remove(signal_id)
    if result.get("ok"):
        _tg().answer_callback(cb_id, "已下单")
        if pending.tg_message_id:
            _tg().edit_text(pending.tg_message_id,
                            pending.summary_text + f"\n\n✅ {result['message']}")
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_handle_callback.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 跑全量测试防回归**

Run: `uv run pytest -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add webhook_server.py tests/test_handle_callback.py
git commit -m "feat: Telegram 确认回调、过期清理与长轮询线程

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: main 入口、README 与端到端手测

**Files:**

- Modify: `webhook_server.py`(文件末尾追加)
- Modify: `README.md`
- Modify: `.gitignore`(追加 `signals_log.jsonl`)

**Interfaces:**

- Consumes: `app`、`poll_loop`
- Produces: `main() -> None` 与 `__main__` 入口;`uv run python webhook_server.py` 可直接启动

- [ ] **Step 1: 实现 main**(`webhook_server.py` 末尾追加)

```python
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
```

- [ ] **Step 2: .gitignore 追加**

```bash
printf '\nsignals_log.jsonl\n' >> /Users/xue/hermes-swing-trader/.gitignore
```

- [ ] **Step 3: README 新增章节**(插在「## 日常操作」之前)

````markdown
## TradingView 信号接入(可选)

在 TradingView 上画突破位/设警报,信号经 webhook 进入本系统风控,
Telegram 按钮确认后在 Alpaca 模拟盘成交。仅美股、只做多。

### 准备

1. **专用 Telegram bot**:找 @BotFather 新建一个 bot(与 Hermes 的 bot 分开),
   拿到 token;私聊 @userinfobot 查自己的数字 chat id;先给新 bot 发一条 /start
2. **`.env` 追加**(参考 `.env.example`):`WEBHOOK_SECRET`(随机串,
   `python -c "import secrets; print(secrets.token_urlsafe(24))"` 生成)、
   `TG_BOT_TOKEN`、`TG_CHAT_ID`
3. **Cloudflare Tunnel**(免费):
   ```bash
   brew install cloudflared
   cloudflared tunnel --url http://localhost:8787
   ```
````

记下输出的 `https://xxx.trycloudflare.com` 地址(快速模式,重启会变;
要固定域名可用 `cloudflared tunnel create` 配置命名隧道)

### 启动

```bash
uv run python webhook_server.py
```

### TradingView 警报配置(需支持 webhook 的付费套餐)

- Webhook URL:`https://<你的隧道域名>/hook/<WEBHOOK_SECRET>`
- 警报消息(Message)填:
  ```json
  {"secret": "<WEBHOOK_SECRET>", "action": "buy", "symbol": "{{ticker}}", "price": {{close}}}
  ```
  可选加 `"stop": 你的止损价`;不加则自动用当日最低价(符合本系统规则)

### 信号流程

信号到达 → 非盘中/重复/风控不过 → Telegram 告知原因并作废;
通过 → 收到订单摘要卡片(股数按单笔风险 `webhook_risk_pct_per_trade`% 自动计算)
→ 30 分钟内点【✅ 确认下单】成交,点【❌ 放弃】或超时作废。
急停(`KILL_SWITCH`)、日限笔数等硬风控与 Hermes 通道完全共享。
审计日志逐条记录在 `signals_log.jsonl`。

````

- [ ] **Step 4: 启动冒烟测试**

```bash
cd /Users/xue/hermes-swing-trader
uv run python webhook_server.py &
sleep 3
# 错误 secret → 403
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8787/hook/wrong \
  -H 'Content-Type: application/json' \
  -d '{"secret":"wrong","action":"buy","symbol":"NVDA"}'
kill %1
````

Expected: 服务正常启动(缺 TG 环境变量时报 `缺少环境变量` 属预期,先在 .env 里补齐);curl 输出 `403`

- [ ] **Step 5: 全量测试 + Commit**

Run: `uv run pytest -v` → 全部 PASS

```bash
git add webhook_server.py README.md .gitignore
git commit -m "feat: webhook 服务入口与 TradingView 接入文档

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: 端到端手测清单**(需用户配合,写给用户)

1. `.env` 补齐 `WEBHOOK_SECRET` / `TG_BOT_TOKEN` / `TG_CHAT_ID`,给新 bot 发过 /start
2. 起服务 + cloudflared 隧道
3. 盘中用 curl 带正确 secret 发一条 NVDA 信号 → Telegram 应收到订单卡片 →
   点确认 → Alpaca 模拟盘面板见到 bracket 订单
4. 拒绝路径:盘外发信号(应提示非盘中)、同 symbol 连发两条(第二条提示重复)、
   点放弃(消息更新为已放弃)
5. TradingView 建真实警报(现价上方一档的穿越条件)完整跑通
