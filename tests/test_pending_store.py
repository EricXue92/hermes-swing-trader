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
