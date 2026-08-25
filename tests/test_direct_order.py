"""place_direct_bracket_buy/sell:免确认一步下单(仅限老板明说"直接下单")。

自动取止损(买=当日最低,空=当日最高)、按账户总值百分比算股数、
硬风控照常全跑,allow_direct_order=false 时整体禁用。
"""
import json

import pytest

import trading_server as ts


@pytest.fixture
def env(monkeypatch, tmp_path):
    """equity=10000, price=100, day_low=98, day_high=103。"""
    monkeypatch.setattr(ts, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ts, "KILL_SWITCH", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(ts, "_latest_price", lambda s: 100.0)
    monkeypatch.setattr(ts, "_day_low_high", lambda s: (98.0, 103.0))

    def fake_get(url, params=None):
        if url.endswith("/v2/account"):
            return {"equity": "10000", "cash": "10000", "buying_power": "20000",
                    "last_equity": "10000"}
        if url.endswith("/v2/positions"):
            return []
        return []

    monkeypatch.setattr(ts, "_get", fake_get)
    posted = {}
    monkeypatch.setattr(ts, "_post", lambda url, payload:
                        posted.update(payload) or {"id": "oid", "status": "accepted"})
    for k, v in [("symbol_whitelist", []), ("max_daily_trades", 6),
                 ("max_stop_distance_pct", 5), ("max_position_pct_of_equity", 50),
                 ("allow_short", True), ("allow_direct_order", True)]:
        monkeypatch.setitem(ts.CONFIG, k, v)
    return posted


def test_direct_buy_auto_stop_and_qty(env):
    """30% 仓位 = $3000 → 30 股;止损自动取当日最低 98。"""
    r = json.loads(ts.place_direct_bracket_buy("NVDA", pct_of_equity=30))
    assert r["ok"] is True
    assert env["side"] == "buy"
    assert env["qty"] == "30"
    assert env["stop_loss"]["stop_price"] == "98.0"
    assert ts._load_state()["trades_today"] == 1


def test_direct_sell_auto_stop_is_day_high(env):
    r = json.loads(ts.place_direct_bracket_sell("NVDA", pct_of_equity=10))
    assert r["ok"] is True
    assert env["side"] == "sell"
    assert env["stop_loss"]["stop_price"] == "103.0"


def test_direct_buy_explicit_qty_and_stop(env):
    r = json.loads(ts.place_direct_bracket_buy("NVDA", qty=5, stop_loss=97.0))
    assert r["ok"] is True
    assert env["qty"] == "5"
    assert env["stop_loss"]["stop_price"] == "97.0"


def test_direct_buy_risk_checks_still_enforced(env, monkeypatch):
    """当日最低距现价 6% > 5% 上限 → 拒单,不下单。"""
    monkeypatch.setattr(ts, "_day_low_high", lambda s: (94.0, 103.0))
    r = json.loads(ts.place_direct_bracket_buy("NVDA", pct_of_equity=30))
    assert r["ok"] is False
    assert "side" not in env


def test_direct_disabled_by_config(env, monkeypatch):
    monkeypatch.setitem(ts.CONFIG, "allow_direct_order", False)
    r = json.loads(ts.place_direct_bracket_buy("NVDA", pct_of_equity=30))
    assert r["ok"] is False
    assert any("allow_direct_order" in e for e in r["errors"])


def test_direct_buy_qty_below_one_rejected(env, monkeypatch):
    monkeypatch.setattr(ts, "_get", lambda url, params=None:
                        {"equity": "100"} if url.endswith("/v2/account") else [])
    r = json.loads(ts.place_direct_bracket_buy("NVDA", pct_of_equity=30))
    assert r["ok"] is False
