"""
Whitelist `ALLOWED_PHONES`: se configurada, apenas numeros listados sao
processados. Vazia significa "aceita todos".
"""
import pytest

from app import consumer
from app.config import settings


@pytest.mark.asyncio
async def test_empty_whitelist_allows_any_phone(monkeypatch):
    settings.ALLOWED_PHONES = ""
    called = False

    async def fake_set_block(phone):
        nonlocal called
        called = True

    monkeypatch.setattr(consumer.rds, "set_block", fake_set_block)

    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "from_me": True,  # aciona o caminho B -> set_block
        "chat_id": "5511999990000@c.us",
    })
    assert called, "Sem whitelist, o processamento deveria prosseguir"


@pytest.mark.asyncio
async def test_whitelist_blocks_non_listed_phone(monkeypatch):
    settings.ALLOWED_PHONES = "5511888880000"
    called = False

    async def fake_set_block(phone):
        nonlocal called
        called = True

    monkeypatch.setattr(consumer.rds, "set_block", fake_set_block)

    # Phone fora da whitelist; mesmo com from_me=True nao deve chegar ao set_block
    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "from_me": True,
        "chat_id": "5511999990000@c.us",
    })
    assert not called, "Phone fora da whitelist deveria ter sido ignorado antes"

    # Reset
    settings.ALLOWED_PHONES = ""


@pytest.mark.asyncio
async def test_whitelist_allows_listed_phone(monkeypatch):
    settings.ALLOWED_PHONES = "5511888880000,5511999990000"
    called = False

    async def fake_set_block(phone):
        nonlocal called
        called = True

    monkeypatch.setattr(consumer.rds, "set_block", fake_set_block)

    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "from_me": True,
        "chat_id": "5511999990000@c.us",
    })
    assert called, "Phone listado deveria passar"

    settings.ALLOWED_PHONES = ""


def test_allowed_phones_set_trims_whitespace():
    settings.ALLOWED_PHONES = " 5511999990000 , 5511888880000 ,"
    assert settings.allowed_phones_set == {"5511999990000", "5511888880000"}
    settings.ALLOWED_PHONES = ""


def test_allowed_phones_set_empty_returns_empty_set():
    settings.ALLOWED_PHONES = ""
    assert settings.allowed_phones_set == set()
