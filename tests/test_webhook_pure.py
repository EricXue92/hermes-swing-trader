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
