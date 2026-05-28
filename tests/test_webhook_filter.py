"""
Testa a logica de filtragem do endpoint webhook sem subir RabbitMQ real.
Usa monkeypatch para substituir `publish` por um stub que guarda o payload.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_and_published(monkeypatch):
    import app.webhook as webhook_mod
    import app.main as main_mod

    published: list[dict] = []

    async def fake_publish(message: dict) -> None:
        published.append(message)

    monkeypatch.setattr(webhook_mod, "publish", fake_publish)
    return TestClient(main_mod.app), published


def test_ignores_own_messages_from_n8n(client_and_published):
    client, pub = client_and_published
    r = client.post("/testslug", json={"message": {"track_source": "n8n", "text": "x"}})
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert pub == []


def test_ignores_ia_source(client_and_published):
    client, pub = client_and_published
    r = client.post("/testslug", json={"message": {"track_source": "IA", "text": "x"}})
    assert r.json()["status"] == "ignored"
    assert pub == []



def test_ignores_from_me_own_echo_by_id(client_and_published, monkeypatch):
    """Eco do proprio bot (id registrado no envio) e descartado."""
    import app.webhook as webhook_mod

    client, pub = client_and_published

    async def fake_is_outbound_id(msg_id: str) -> bool:
        return msg_id == "owner:ABC123"

    monkeypatch.setattr(webhook_mod.rds, "is_outbound_id", fake_is_outbound_id)

    r = client.post("/testslug", json={
        "message": {
            "fromMe": True,
            "wasSentByApi": True,
            "id": "owner:ABC123",
            "chatid": "5511999990000@c.us",
            "text": "Oi! Eu sou a Bia",
        }
    })
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert r.json()["reason"] == "own outbound echo (id)"
    assert pub == []


def test_queues_from_me_human_panel(client_and_published, monkeypatch):
    """Atendente humana pelo painel chega fromMe + wasSentByApi, mas SEM
    track_source e SEM id de eco do bot -> deve ser enfileirada para que o
    consumer bloqueie o agente (set_block)."""
    import app.webhook as webhook_mod

    client, pub = client_and_published

    async def fake_is_outbound_id(msg_id: str) -> bool:
        return False

    async def fake_consume_outbound_echo(phone: str, text: str) -> bool:
        return False

    monkeypatch.setattr(webhook_mod.rds, "is_outbound_id", fake_is_outbound_id)
    monkeypatch.setattr(webhook_mod.rds, "consume_outbound_echo", fake_consume_outbound_echo)

    r = client.post("/testslug", json={
        "message": {
            "fromMe": True,
            "wasSentByApi": True,
            "track_source": "",
            "id": "owner:HUMAN999",
            "chatid": "5511999990000@c.us",
            "text": "Oi Gustavo, tudo bem?",
        }
    })
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert len(pub) == 1
    assert pub[0]["from_me"] is True
    assert pub[0]["phone"] == "5511999990000"


def test_ignores_from_me_outbound_echo(client_and_published, monkeypatch):
    import app.webhook as webhook_mod

    client, pub = client_and_published[:2]

    async def fake_consume_outbound_echo(phone: str, text: str) -> bool:
        return phone == "5511999990000" and text == "Conversa reiniciada."

    monkeypatch.setattr(webhook_mod.rds, "consume_outbound_echo", fake_consume_outbound_echo)

    r = client.post("/testslug", json={
        "message": {
            "fromMe": True,
            "chatid": "5511999990000@c.us",
            "text": "Conversa reiniciada.",
        }
    })
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert r.json()["reason"] == "outbound echo"
    assert pub == []


def test_ignores_reset_confirmation_even_without_outbound_marker(client_and_published, monkeypatch):
    import app.webhook as webhook_mod

    client, pub = client_and_published[:2]

    async def fake_consume_outbound_echo(phone: str, text: str) -> bool:
        return False

    monkeypatch.setattr(webhook_mod.rds, "consume_outbound_echo", fake_consume_outbound_echo)

    r = client.post("/testslug", json={
        "message": {
            "fromMe": True,
            "chatid": "5511999990000@c.us",
            "text": "Conversa reiniciada.",
        }
    })
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert r.json()["reason"] == "reset confirmation echo"
    assert pub == []


@pytest.mark.asyncio
async def test_consumer_does_not_block_reset_confirmation(monkeypatch):
    import app.consumer as consumer_mod

    blocked: list[str] = []

    async def fake_set_block(phone: str, *args, **kwargs) -> None:
        blocked.append(phone)

    monkeypatch.setattr(consumer_mod.rds, "set_block", fake_set_block)

    await consumer_mod._process_message({
        "phone": "5511999990000",
        "chat_id": "5511999990000@c.us",
        "from_me": True,
        "msg_type": "Conversation",
        "msg": "Conversa reiniciada.",
    })

    assert blocked == []


def test_ignores_when_no_phone(client_and_published):
    client, pub = client_and_published
    r = client.post("/testslug", json={"message": {"text": "oi"}})
    assert r.json()["status"] == "ignored"
    assert pub == []


def test_queues_text_message(client_and_published):
    client, pub = client_and_published
    r = client.post("/testslug", json={
        "message": {
            "sender_pn": "5511999990000@c.us",
            "senderName": "Fulano",
            "text": "quero info",
        }
    })
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert len(pub) == 1
    msg = pub[0]
    assert msg["phone"] == "5511999990000"
    assert msg["push_name"] == "Fulano"
    assert msg["msg"] == "quero info"
    assert msg["msg_type"] == "Conversation"


def test_queues_audio_message(client_and_published):
    client, pub = client_and_published
    r = client.post("/testslug", json={
        "message": {
            "sender_pn": "5511888880000@c.us",
            "messageType": "audioMessage",
            "mediaUrl": "https://example.com/audio.ogg",
        }
    })
    assert r.json()["status"] == "queued"
    assert pub[0]["msg_type"] == "AudioMessage"
    assert pub[0]["media_url"] == "https://example.com/audio.ogg"


def test_ignores_unknown_message_type(client_and_published):
    client, pub = client_and_published
    r = client.post("/testslug", json={
        "message": {"sender_pn": "5511999990000@c.us", "messageType": "stickerMessage"}
    })
    assert r.json()["status"] == "ignored"
    assert pub == []


def test_health_endpoint(client_and_published):
    client, _ = client_and_published
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
