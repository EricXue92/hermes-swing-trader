"""做空 (bracket sell) 工具与镜像风控的测试。

风控镜像:做空止损必须高于现价、距离 ≤5%、同吃 30% 仓位与日限笔数;
做空止损只允许下移 (move_stop_down)。
"""
import json

import pytest

import trading_server as ts


@pytest.fixture
def env(monkeypatch, tmp_path):
    """隔离状态文件/急停文件,固定行情与账户。equity=10000, price=100。"""
    monkeypatch.setattr(ts, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ts, "KILL_SWITCH", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(ts, "_latest_price", lambda s: 100.0)

    def fake_get(url, params=None):
        if url.endswith("/v2/account"):
            return {"equity": "10000"}
        if url.endswith("/v2/positions"):
            return []
        if url.endswith("/v2/orders"):
            return []
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(ts, "_get", fake_get)
    monkeypatch.setitem(ts.CONFIG, "symbol_whitelist", [])
    monkeypatch.setitem(ts.CONFIG, "max_daily_trades", 3)
    monkeypatch.setitem(ts.CONFIG, "max_stop_distance_pct", 5)
    monkeypatch.setitem(ts.CONFIG, "max_position_pct_of_equity", 30)
    monkeypatch.setitem(ts.CONFIG, "require_confirmation", True)
    return tmp_path


# ---------- preview_bracket_sell ----------

def test_preview_sell_rejects_stop_below_price(env):
    r = json.loads(ts.preview_bracket_sell("NVDA", 10, stop_loss=99.0))
    assert r["approved"] is False
    assert any("高于" in e for e in r["errors"])


def test_preview_sell_rejects_stop_distance_over_limit(env):
    # 100 → 106 是 6% > 5%
    r = json.loads(ts.preview_bracket_sell("NVDA", 10, stop_loss=106.0))
    assert r["approved"] is False
    assert any("超过上限" in e for e in r["errors"])


def test_preview_sell_rejects_position_over_30pct(env):
    # 40 股 * $100 = $4000 > $3000 (30% of 10000)
    r = json.loads(ts.preview_bracket_sell("NVDA", 40, stop_loss=104.0))
    assert r["approved"] is False
    assert any("仓位金额" in e for e in r["errors"])


def test_preview_sell_approves_valid_short(env):
    r = json.loads(ts.preview_bracket_sell("NVDA", 10, stop_loss=104.0))
    assert r["approved"] is True
    s = r["summary"]
    assert s["side"] == "sell_short"
    assert s["stop_loss"] == 104.0
    assert s["max_loss_usd"] == pytest.approx((104.0 - 100.0) * 10)
    assert r["confirm_token"]


def test_buy_token_rejected_by_place_sell(env, monkeypatch):
    """buy 预览的令牌不能用于做空下单(令牌须绑定方向)。"""
    buy = json.loads(ts.preview_bracket_buy("NVDA", 10, stop_loss=96.0))
    assert buy["approved"] is True
    r = json.loads(ts.place_bracket_sell("NVDA", 10, stop_loss=104.0,
                                         confirm_token=buy["confirm_token"]))
    assert r["ok"] is False


# ---------- place_bracket_sell ----------

def test_place_sell_submits_short_bracket_and_counts_trade(env, monkeypatch):
    posted = {}

    def fake_post(url, payload):
        posted.update(payload)
        return {"id": "oid-1", "status": "accepted"}

    monkeypatch.setattr(ts, "_post", fake_post)
    token = json.loads(ts.preview_bracket_sell("NVDA", 10, 104.0))["confirm_token"]
    r = json.loads(ts.place_bracket_sell("NVDA", 10, 104.0, confirm_token=token))
    assert r["ok"] is True
    assert posted["side"] == "sell"
    assert posted["order_class"] == "oto"
    assert posted["stop_loss"]["stop_price"] == "104.0"
    assert ts._load_state()["trades_today"] == 1


def test_place_sell_without_token_rejected(env):
    r = json.loads(ts.place_bracket_sell("NVDA", 10, 104.0, confirm_token=""))
    assert r["ok"] is False
    assert any("confirm_token" in e for e in r["errors"])


def test_place_sell_blocked_by_daily_limit(env):
    s = ts._load_state()
    s["trades_today"] = 3
    ts._save_state(s)
    r = json.loads(ts.preview_bracket_sell("NVDA", 10, 104.0))
    assert r["approved"] is False
    assert any("最大下单次数" in e for e in r["errors"])


def test_place_sell_blocked_by_kill_switch(env):
    (env / "KILL_SWITCH").touch()
    r = json.loads(ts.preview_bracket_sell("NVDA", 10, 104.0))
    assert r["approved"] is False
    assert any("急停" in e for e in r["errors"])


# ---------- move_stop_down(做空止损只下移) ----------

def _open_short_stop(monkeypatch, stop_price="104"):
    """挂一张做空仓位的止损买单 (side=buy, type=stop)。"""
    def fake_get(url, params=None):
        if url.endswith("/v2/orders"):
            return [{"id": "so-1", "type": "stop", "side": "buy",
                     "stop_price": stop_price}]
        if url.endswith("/v2/account"):
            return {"equity": "10000"}
        if url.endswith("/v2/positions"):
            return []
        raise AssertionError(f"unexpected GET {url}")
    monkeypatch.setattr(ts, "_get", fake_get)


def test_move_stop_down_rejects_raising(env, monkeypatch):
    _open_short_stop(monkeypatch)
    r = json.loads(ts.move_stop_down("NVDA", new_stop=105.0))
    assert r["ok"] is False
    assert any("只允许下移" in e for e in r["errors"])


def test_move_stop_down_accepts_lower(env, monkeypatch):
    _open_short_stop(monkeypatch)
    patched = {}

    class FakeResp:
        status_code = 200
        text = ""

    def fake_patch(url, headers=None, json=None, timeout=None):
        patched.update(json)
        return FakeResp()

    monkeypatch.setattr(ts.requests, "patch", fake_patch)
    r = json.loads(ts.move_stop_down("NVDA", new_stop=102.0))
    assert r["ok"] is True
    assert patched["stop_price"] == "102.0"
