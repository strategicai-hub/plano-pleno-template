"""
Testes do SQLite (app/db.py) — usa um arquivo temporario isolado por teste.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db_mod
from app.config import settings


@pytest.fixture
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pleno_test.db")
        monkeypatch.setattr(settings, "SQLITE_PATH", path)
        db_mod.init_db_sync()
        yield path


async def test_upsert_and_get_lead(temp_db):
    await db_mod.upsert_lead("5511999990000", nome="Ana", status="novo")
    lead = await db_mod.get_lead("5511999990000")
    assert lead is not None
    assert lead["nome"] == "Ana"
    assert lead["status"] == "novo"
    assert lead["updated_at"]


async def test_touch_last_message_sets_timestamp(temp_db):
    await db_mod.touch_last_message("5511999990000")
    lead = await db_mod.get_lead("5511999990000")
    assert lead["last_customer_message_at"]


async def test_get_followups_due_respects_status_and_modo_mudo(temp_db):
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()

    await db_mod.schedule_followup("5511000000001", next_follow_up_iso=past, stage=1)
    await db_mod.schedule_followup("5511000000002", next_follow_up_iso=future, stage=1)
    await db_mod.schedule_followup("5511000000003", next_follow_up_iso=past, stage=1)
    await db_mod.set_modo_mudo("5511000000003", True)
    await db_mod.schedule_followup("5511000000004", next_follow_up_iso=past, stage=1)
    await db_mod.mark_finalizado("5511000000004")

    due = await db_mod.get_followups_due(now.isoformat())
    phones = {lead["phone"] for lead in due}
    assert "5511000000001" in phones
    assert "5511000000002" not in phones  # futuro
    assert "5511000000003" not in phones  # modo_mudo
    assert "5511000000004" not in phones  # finalizado


async def test_advance_followup_stage_finalizes(temp_db):
    now = datetime.now(timezone.utc).isoformat()
    await db_mod.schedule_followup("5511000000005", next_follow_up_iso=now, stage=3)
    await db_mod.advance_followup_stage("5511000000005", 3, None, finalize=True)
    lead = await db_mod.get_lead("5511000000005")
    assert lead["status_conversa"] == "finalizado"
    assert lead["next_follow_up"] is None


async def test_schedule_appointment_and_reminder(temp_db):
    now = datetime.now(timezone.utc)
    scheduled = (now + timedelta(hours=1)).isoformat()
    until = (now + timedelta(hours=3)).isoformat()

    appt_id = await db_mod.schedule_appointment(
        phone="5511999990000",
        scheduled_at_iso=scheduled,
        source="google_calendar",
        external_id="evt_abc",
        modalidade="Boxe",
    )
    assert appt_id > 0

    due = await db_mod.get_appointments_for_reminder(until_iso=until, now_iso=now.isoformat())
    assert len(due) == 1
    assert due[0]["external_id"] == "evt_abc"

    await db_mod.mark_reminder_sent(appt_id)

    due_again = await db_mod.get_appointments_for_reminder(until_iso=until, now_iso=now.isoformat())
    assert due_again == []
