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
