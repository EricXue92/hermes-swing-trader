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
