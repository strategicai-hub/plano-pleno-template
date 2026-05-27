"""
Camada SQLite (aiosqlite) para estado durável de leads, follow-ups e
agendamentos. Complementa o Redis (que é efêmero / estado de sessão) com a
camada que precisa sobreviver a reinício de container.

Tabelas:
- leads: next_follow_up, stage_follow_up, status_conversa, modo_mudo,
  last_customer_message_at
- appointments: agendamentos gerados via [AGENDAR=...] (Google Calendar ou
  sistema externo do cliente)
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  phone TEXT PRIMARY KEY,
  nome TEXT,
  status TEXT,
  modo_mudo INTEGER DEFAULT 0,
  next_follow_up TEXT,
  stage_follow_up INTEGER DEFAULT 0,
  last_customer_message_at TEXT,
  status_conversa TEXT DEFAULT 'novo',
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_next_fup ON leads(next_follow_up, status_conversa);
CREATE INDEX IF NOT EXISTS idx_leads_last_msg ON leads(last_customer_message_at);

CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  source TEXT NOT NULL,
  external_id TEXT,
  modalidade TEXT,
  status TEXT DEFAULT 'booked',
  reminder_sent_at TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_appointments_scheduled ON appointments(scheduled_at, status);
CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone);
"""


def _ensure_dir() -> None:
    path = settings.SQLITE_PATH
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def init_db_sync() -> None:
    _ensure_dir()
    con = sqlite3.connect(settings.SQLITE_PATH)
    try:
        con.executescript(SCHEMA)
        con.commit()
        logger.info("SQLite inicializado em %s", settings.SQLITE_PATH)
    finally:
        con.close()


async def init_db() -> None:
    _ensure_dir()
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    logger.info("SQLite inicializado em %s", settings.SQLITE_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def upsert_lead(phone: str, **fields) -> None:
    if not phone:
        return
    fields["updated_at"] = _now_iso()
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        cur = await db.execute("SELECT phone FROM leads WHERE phone=?", (phone,))
        row = await cur.fetchone()
        if row is None:
            cols = ["phone"] + list(fields.keys())
            vals = [phone] + list(fields.values())
            placeholders = ",".join(["?"] * len(cols))
            await db.execute(
                f"INSERT INTO leads ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
        elif fields:
            assigns = ",".join(f"{k}=?" for k in fields.keys())
            await db.execute(
                f"UPDATE leads SET {assigns} WHERE phone=?",
                list(fields.values()) + [phone],
            )
        await db.commit()


async def get_lead(phone: str) -> Optional[dict]:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM leads WHERE phone=?", (phone,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def touch_last_message(phone: str) -> None:
    """Marca momento da última mensagem do cliente. Insumo para follow-up de reativação."""
    await upsert_lead(phone, last_customer_message_at=_now_iso())


async def set_modo_mudo(phone: str, value: bool = True) -> None:
    await upsert_lead(phone, modo_mudo=1 if value else 0)


async def is_modo_mudo(phone: str) -> bool:
    lead = await get_lead(phone)
    return bool(lead and lead.get("modo_mudo"))


async def schedule_followup(phone: str, next_follow_up_iso: str, stage: int = 1) -> None:
    await upsert_lead(
        phone,
        next_follow_up=next_follow_up_iso,
        stage_follow_up=stage,
        status_conversa="em_andamento",
    )


async def get_followups_due(now_iso: str) -> list[dict]:
    """Leads devidos para reativação. Exclui leads com appointment ativo
    (booked/reminded com scheduled_at >= now) — quem já tem aula marcada não
    deve ser reativado como lead frio."""
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT l.* FROM leads l
            WHERE l.next_follow_up IS NOT NULL
              AND l.next_follow_up <= ?
              AND COALESCE(l.status_conversa, '') NOT IN ('finalizado', 'agendado')
              AND COALESCE(l.modo_mudo, 0) = 0
              AND NOT EXISTS (
                  SELECT 1 FROM appointments a
                  WHERE a.phone = l.phone
                    AND a.scheduled_at >= ?
                    AND a.status IN ('booked', 'reminded')
              )
            """,
            (now_iso, now_iso),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def has_active_appointment(phone: str, now_iso: str) -> bool:
    """True se o lead tem appointment futuro/atual ainda válido."""
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        cur = await db.execute(
            """
            SELECT 1 FROM appointments
            WHERE phone = ?
              AND scheduled_at >= ?
              AND status IN ('booked', 'reminded')
            LIMIT 1
            """,
            (phone, now_iso),
        )
        row = await cur.fetchone()
        return row is not None


async def advance_followup_stage(
    phone: str,
    new_stage: int,
    next_iso: Optional[str],
    finalize: bool,
) -> None:
    fields = {"stage_follow_up": new_stage, "next_follow_up": next_iso}
    if finalize:
        fields["status_conversa"] = "finalizado"
        fields["next_follow_up"] = None
    await upsert_lead(phone, **fields)


async def mark_finalizado(phone: str) -> None:
    await upsert_lead(phone, status_conversa="finalizado", next_follow_up=None)


async def delete_lead(phone: str) -> None:
    """Remove a row inteira do lead (e seus appointments) — usado pelo /reset."""
    if not phone:
        return
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        await db.execute("DELETE FROM appointments WHERE phone=?", (phone,))
        await db.execute("DELETE FROM leads WHERE phone=?", (phone,))
        await db.commit()


async def schedule_appointment(
    phone: str,
    scheduled_at_iso: str,
    source: str,
    external_id: Optional[str] = None,
    modalidade: Optional[str] = None,
) -> tuple[int, bool]:
    """Registra um agendamento de forma idempotente.

    Se já existe um appointment ativo (status booked/reminded) para o mesmo
    `phone` num horário a ±5 min do solicitado, NÃO cria outra linha — atualiza
    a existente (modalidade/external_id) e a retorna. Isso evita linhas
    duplicadas que geram múltiplos lembretes idênticos quando a IA chama o
    handler de agendamento / emite `[AGENDAR=...]` em mais de uma tentativa/turno.

    Retorna (appointment_id, created) — `created=False` quando reaproveitou uma
    linha existente, permitindo ao chamador não disparar alerta de equipe 2x.
    `source` = 'google_calendar' | 'external_system' | etc.
    """
    from datetime import datetime as _dt, timedelta as _td

    # Janela de ±5 min para considerar "mesmo agendamento" (tolera segundos /
    # pequenas variações de parsing da hora informada pela IA).
    lo_iso = scheduled_at_iso
    hi_iso = scheduled_at_iso
    try:
        base = _dt.fromisoformat(scheduled_at_iso)
        lo_iso = (base - _td(minutes=5)).isoformat()
        hi_iso = (base + _td(minutes=5)).isoformat()
    except ValueError:
        pass

    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        cur = await db.execute(
            """
            SELECT id FROM appointments
            WHERE phone = ?
              AND status IN ('booked', 'reminded')
              AND scheduled_at BETWEEN ? AND ?
            ORDER BY id LIMIT 1
            """,
            (phone, lo_iso, hi_iso),
        )
        existing = await cur.fetchone()
        if existing:
            appt_id = existing[0]
            await db.execute(
                """
                UPDATE appointments
                SET modalidade = COALESCE(?, modalidade),
                    external_id = COALESCE(?, external_id)
                WHERE id = ?
                """,
                (modalidade, external_id, appt_id),
            )
            await db.commit()
            logger.info(
                "schedule_appointment: reaproveitando appointment_id=%s para %s (dedup)",
                appt_id, phone,
            )
            return appt_id, False

        cur = await db.execute(
            """
            INSERT INTO appointments
                (phone, scheduled_at, source, external_id, modalidade, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'booked', ?)
            """,
            (phone, scheduled_at_iso, source, external_id, modalidade, _now_iso()),
        )
        await db.commit()
        return cur.lastrowid or 0, True


async def get_appointments_for_reminder(
    until_iso: str,
    now_iso: Optional[str] = None,
) -> list[dict]:
    """Agendamentos com scheduled_at entre now e until, ainda não lembrados."""
    now_iso = now_iso or _now_iso()
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM appointments
            WHERE scheduled_at BETWEEN ? AND ?
              AND status = 'booked'
              AND reminder_sent_at IS NULL
            """,
            (now_iso, until_iso),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def mark_reminder_sent(appointment_id: int) -> None:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        await db.execute(
            "UPDATE appointments SET reminder_sent_at=?, status='reminded' WHERE id=?",
            (_now_iso(), appointment_id),
        )
        await db.commit()


async def list_all_leads() -> list[dict]:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM leads ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
