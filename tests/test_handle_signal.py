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


def test_handle_signal_runs_under_lock(env, monkeypatch):
    seen = []

    def fake_open():
        seen.append(ws._SIGNAL_LOCK.locked())
        return False

    monkeypatch.setattr(ws, "_market_open", fake_open)
    ws.handle_signal(sig())
    assert seen == [True]
