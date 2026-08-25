import webhook_server as ws


# ---- 纯函数 ----

def test_build_confirm_keyboard():
    kb = ws.build_confirm_keyboard("abc123")
    row = kb["inline_keyboard"][0]
    assert row[0]["callback_data"] == "confirm:abc123"
    assert row[1]["callback_data"] == "reject:abc123"


def test_parse_callback_data():
    assert ws.parse_callback_data("confirm:abc123") == ("confirm", "abc123")
    assert ws.parse_callback_data("reject:abc123") == ("reject", "abc123")
    assert ws.parse_callback_data("nonsense") is None
    assert ws.parse_callback_data("delete:abc") is None
    assert ws.parse_callback_data("confirm:") is None


# ---- HTTP 封装(mock requests.post)----

class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_send_text_returns_message_id(monkeypatch):
    calls = {}

    def fake_post(url, json=None, timeout=None):
        calls["url"] = url
        calls["payload"] = json
        return FakeResp({"ok": True, "result": {"message_id": 42}})

    monkeypatch.setattr(ws.requests, "post", fake_post)
    client = ws.TelegramClient("TOKEN", "12345")
    msg_id = client.send_text("hello", ws.build_confirm_keyboard("x"))
    assert msg_id == 42
    assert calls["url"].endswith("/botTOKEN/sendMessage")
    assert calls["payload"]["chat_id"] == "12345"
    assert "reply_markup" in calls["payload"]


def test_send_text_returns_none_on_api_error(monkeypatch):
    monkeypatch.setattr(ws.requests, "post",
                        lambda *a, **k: FakeResp({"ok": False, "description": "bad"}))
    assert ws.TelegramClient("T", "1").send_text("hi") is None


def test_send_text_returns_none_on_exception(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("net down")

    monkeypatch.setattr(ws.requests, "post", boom)
    assert ws.TelegramClient("T", "1").send_text("hi") is None


def test_get_updates_returns_empty_list_on_failure(monkeypatch):
    monkeypatch.setattr(ws.requests, "post",
                        lambda *a, **k: FakeResp({"ok": False}))
    assert ws.TelegramClient("T", "1").get_updates(0) == []
