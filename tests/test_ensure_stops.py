"""ensure_stops:给缺失止损保护的持仓自动补挂 GTC 止损单。

背景:2026-08-25 的 SMCI 订单止损腿还是 tif=day,收盘过期后持仓裸奔;
GTC 修复只管新订单,存量持仓需要这个工具兜底。
规则:做多止损 = 当日最低价,做空止损 = 当日最高价;护体操作不计当日笔数。
"""
import json

import pytest

import trading_server as ts


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ts, "KILL_SWITCH", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(ts, "_day_low_high", lambda s: (95.0, 105.0))
    posted = []

    def fake_post(url, payload):
        posted.append(payload)
        return {"id": f"order-{len(posted)}", "status": "accepted"}

    monkeypatch.setattr(ts, "_post", fake_post)
    return {"tmp": tmp_path, "posted": posted}


def _mock_get(monkeypatch, positions, open_orders):
    def fake_get(url, params=None):
        if url.endswith("/v2/positions"):
            return positions
        if url.endswith("/v2/orders"):
            return open_orders
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(ts, "_get", fake_get)


def test_no_positions(env, monkeypatch):
    _mock_get(monkeypatch, [], [])
    r = json.loads(ts.ensure_stops())
    assert r["ok"] is True
    assert r["repaired"] == 0
    assert env["posted"] == []


def test_repairs_unprotected_long(env, monkeypatch):
    _mock_get(monkeypatch,
              [{"symbol": "SMCI", "qty": "10", "side": "long"}], [])
    r = json.loads(ts.ensure_stops())
    assert r["ok"] is True
    assert r["repaired"] == 1
    (order,) = env["posted"]
    assert order["symbol"] == "SMCI"
    assert order["side"] == "sell"
    assert order["type"] == "stop"
    assert order["time_in_force"] == "gtc"
    assert order["qty"] == "10"
    assert float(order["stop_price"]) == 95.0  # 当日最低价


def test_repairs_unprotected_short(env, monkeypatch):
    _mock_get(monkeypatch,
              [{"symbol": "NVDA", "qty": "-5", "side": "short"}], [])
    r = json.loads(ts.ensure_stops())
    assert r["repaired"] == 1
    (order,) = env["posted"]
    assert order["side"] == "buy"
    assert order["qty"] == "5"
    assert float(order["stop_price"]) == 105.0  # 当日最高价


def test_skips_protected_position(env, monkeypatch):
    _mock_get(monkeypatch,
              [{"symbol": "SMCI", "qty": "10", "side": "long"}],
              [{"symbol": "SMCI", "side": "sell", "type": "stop",
                "qty": "10", "stop_price": "35.88"}])
    r = json.loads(ts.ensure_stops())
    assert r["repaired"] == 0
    assert env["posted"] == []


def test_kill_switch_blocks(env, monkeypatch):
    _mock_get(monkeypatch,
              [{"symbol": "SMCI", "qty": "10", "side": "long"}], [])
    (env["tmp"] / "KILL_SWITCH").touch()
    r = json.loads(ts.ensure_stops())
    assert r["ok"] is False
    assert env["posted"] == []
