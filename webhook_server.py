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
