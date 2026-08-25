"""get_account 的当日盈亏 (daily P&L) 字段。"""
import json

import pytest

import trading_server as ts


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ts, "KILL_SWITCH", tmp_path / "KILL_SWITCH")
    return monkeypatch


def _account(monkeypatch, equity, last_equity):
    monkeypatch.setattr(ts, "_get", lambda url, params=None: {
        "equity": equity, "cash": "5000", "buying_power": "10000",
        "last_equity": last_equity})


def test_get_account_reports_daily_pl(env):
    _account(env, "10250", "10000")
    a = json.loads(ts.get_account())
    assert a["daily_pl"] == pytest.approx(250.0)
    assert a["daily_pl_pct"] == pytest.approx(2.5)


def test_get_account_daily_pl_null_when_no_last_equity(env):
    """新账户 last_equity=0,不能除零,当日盈亏应为 None。"""
    _account(env, "10250", "0")
    a = json.loads(ts.get_account())
    assert a["daily_pl"] is None
    assert a["daily_pl_pct"] is None
