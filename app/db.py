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
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  phone TEXT PRIMARY KEY,
  nome TEXT,
  nome_cadastro TEXT,
  status TEXT,
  modo_mudo INTEGER DEFAULT 0,
  next_follow_up TEXT,
  stage_follow_up INTEGER DEFAULT 0,
  last_customer_message_at TEXT,
  followup_hold_at TEXT,
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

CREATE TABLE IF NOT EXISTS lead_dispatch_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT NOT NULL,
  nome TEXT,
  email TEXT,
  operadora TEXT,
  observacao TEXT,
  vidas TEXT,
  source_phone TEXT,
  raw_block TEXT,
  external_id TEXT,
  status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  scheduled_after TEXT,
  created_at TEXT,
  sent_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ldq_status_sched ON lead_dispatch_queue(status, scheduled_after, created_at);
CREATE INDEX IF NOT EXISTS idx_ldq_phone_created ON lead_dispatch_queue(phone, created_at);
CREATE INDEX IF NOT EXISTS idx_ldq_sent_at ON lead_dispatch_queue(sent_at);
"""


def _ensure_dir() -> None:
    path = settings.SQLITE_PATH
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


# ALTERs idempotentes para bancos ja existentes (CREATE TABLE IF NOT EXISTS nao
# adiciona colunas novas). Cada ADD COLUMN falha com OperationalError se a coluna
# ja existe — engolimos o erro.
_MIGRATIONS = [
    "ALTER TABLE lead_dispatch_queue ADD COLUMN external_id TEXT",
    # Origem declarada pela ponte que entregou o lead (ex.: "META_LEAD_ADS").
    # Decide o roteiro de abertura e a rota semeada no historico — sem ela o
    # lead do formulario da Meta seria tratado como um lead externo qualquer.
    "ALTER TABLE lead_dispatch_queue ADD COLUMN origem TEXT",
    # `leads.nome` passa a guardar SO o nome que o contato informou na conversa.
    # O nome que ele preencheu no cadastro de origem vive aqui, separado —
    # misturar as duas origens no mesmo campo foi o que fez o push_name do
    # WhatsApp virar vocativo (ver app/services/nomes.py).
    "ALTER TABLE leads ADD COLUMN nome_cadastro TEXT",
    # Instante em que a cobranca automatica foi retida por causa de gente de
    # verdade na conversa (atendente respondeu ou operador desativou o
    # assistente). Trava DURAVEL: diferente do bloqueio no Redis — que some se o
    # Redis for limpo — e do `modo_mudo` — que o "Ativar assistente" zera —, esta
    # marca vive no SQLite e so cai quando o LEAD volta a escrever. Enquanto ela
    # for mais recente que a ultima mensagem do lead, nenhum follow-up sai.
    "ALTER TABLE leads ADD COLUMN followup_hold_at TEXT",
]

# Correcoes de DADOS (nao de schema), idempotentes por construcao — rodam junto
# das migracoes a cada boot e, depois da primeira vez, nao atingem linha nenhuma.
_BACKFILLS = [
    # Leads ja mudos quando a coluna nasceu: sem isto, a trava nova so valeria
    # para atendimentos FUTUROS e a base acumulada seguiria protegida apenas
    # pelo `modo_mudo`. `updated_at` e o instante em que o corte foi aplicado.
    """
    UPDATE leads
       SET followup_hold_at = COALESCE(updated_at, datetime('now'))
     WHERE COALESCE(modo_mudo, 0) = 1
       AND followup_hold_at IS NULL
    """,
]

# Predicado compartilhado pelas duas consultas de follow-up (a que semeia e a que
# envia). Fica aqui, num lugar so, para as duas nunca divergirem: um follow-up
# que escapasse por uma delas ja bastaria para cobrar um lead em atendimento.
FOLLOWUP_HOLD_CLAUSE = """
              AND (
                    l.followup_hold_at IS NULL
                 OR (l.last_customer_message_at IS NOT NULL
                     AND l.last_customer_message_at > l.followup_hold_at)
              )
"""


def _apply_migrations_sync(con: sqlite3.Connection) -> None:
    for stmt in _MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass
    con.commit()
    for stmt in _BACKFILLS:
        try:
            cur = con.execute(stmt)
            if cur.rowcount:
                logger.info("SQLite backfill: %d linha(s) ajustada(s)", cur.rowcount)
        except sqlite3.OperationalError as exc:
            logger.warning("SQLite backfill falhou: %s", exc)
    con.commit()


def init_db_sync() -> None:
    _ensure_dir()
    con = sqlite3.connect(settings.SQLITE_PATH)
    try:
        con.executescript(SCHEMA)
        con.commit()
        _apply_migrations_sync(con)
        logger.info("SQLite inicializado em %s", settings.SQLITE_PATH)
    finally:
        con.close()


async def init_db() -> None:
    _ensure_dir()
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        for stmt in _MIGRATIONS:
            try:
                await db.execute(stmt)
            except Exception:  # noqa: BLE001 — coluna ja existe
                pass
        await db.commit()
        for stmt in _BACKFILLS:
            try:
                await db.execute(stmt)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite backfill falhou: %s", exc)
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
    """Marca momento da última mensagem do cliente. Insumo para follow-up de reativação.

    O lead respondeu: a conversa reabriu. Zera o ciclo de reativação (estágio e
    agendamento pendente) e tira o 'finalizado' — sem isso, quem já esgotou os
    estágios (ou foi marcado como encerrado) nunca mais seria reativado ao voltar
    a falar. 'agendado' é preservado: quem tem compromisso não é lead frio.
    """
    await upsert_lead(phone, last_customer_message_at=_now_iso())
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        await db.execute(
            """
            UPDATE leads
               SET stage_follow_up = 0,
                   next_follow_up = NULL,
                   status_conversa = CASE
                       WHEN COALESCE(status_conversa, '') = 'finalizado' THEN 'em_andamento'
                       ELSE status_conversa
                   END
             WHERE phone = ?
               AND COALESCE(status_conversa, '') != 'agendado'
            """,
            (phone,),
        )
        await db.commit()


async def set_modo_mudo(phone: str, value: bool = True) -> None:
    await upsert_lead(phone, modo_mudo=1 if value else 0)


async def is_modo_mudo(phone: str) -> bool:
    lead = await get_lead(phone)
    return bool(lead and lead.get("modo_mudo"))


async def mute_followups(phones: Iterable[str], primary: str) -> None:
    """
    Corta o follow-up em DEFINITIVO para o lead (atendente humana assumiu ou o
    operador desativou o assistente no SAI).

    Diferente do bloqueio no Redis — que pode expirar amanha 08:00 e so adia a
    reativacao —, `modo_mudo=1` e permanente: as duas consultas do follow-up
    (`get_followups_due` e o `_seed_inactive_leads` da reativacao) exigem
    `modo_mudo = 0`, entao o lead nunca mais e semeado nem cobrado.

    Zera tambem `next_follow_up`/`stage_follow_up` para nao deixar agendamento
    orfao no banco: o `modo_mudo` sozinho ja barraria o envio, mas a linha
    pendente reapareceria se a flag fosse limpa manualmente algum dia. A fila de
    1o contato pendente tambem e descartada — desativar o assistente nao pode
    deixar um disparo engatilhado.

    Aplica nas duas variantes do numero (com/sem o 9o digito) porque o
    follow-up pode ter sido agendado sob a outra forma — mas nunca cria linha
    para variante que ainda nao existe. Desfeito pelo /reset do lead (que apaga
    a linha inteira) ou por `unmute_followups` ("Ativar assistente" no SAI).
    """
    for variant in phones:
        if variant != primary and not await get_lead(variant):
            continue
        await upsert_lead(
            variant,
            modo_mudo=1,
            next_follow_up=None,
            stage_follow_up=0,
            followup_hold_at=_now_iso(),
        )
        async with aiosqlite.connect(settings.SQLITE_PATH) as db:
            await db.execute(
                """
                UPDATE lead_dispatch_queue
                   SET status = 'skipped', last_error = 'assistente pausado'
                 WHERE phone = ? AND status = 'pending'
                """,
                (variant,),
            )
            await db.commit()


async def mute_followups_bulk(phones: Iterable[str]) -> int:
    """Corta o follow-up de varios leads de uma vez, sem criar linha nova.

    Usado na reconciliacao com o snapshot do SAI: toda conversa que o painel
    lista como pausada (assistente desligado no botao ou atendente humano no
    comando) sai do ciclo de cobranca aqui tambem. So toca em quem ja existe e
    ainda nao esta mudo — a cada poll o custo e uma UPDATE que normalmente nao
    atinge nenhuma linha.

    Fecha o buraco em que o POST /sai/block se perdeu (bot fora do ar, Redis
    limpo): o bloqueio voltava pela reconciliacao, mas o `next_follow_up` seguia
    pendente no SQLite, pronto para disparar assim que o bloqueio saisse.

    Retorna quantas linhas foram cortadas.
    """
    alvos = [p for p in {str(x).strip() for x in phones} if p]
    if not alvos:
        return 0
    # Lotes de 400: o SQLite limita os parametros de uma query (SQLITE_LIMIT_
    # VARIABLE_NUMBER, 999 em builds antigos) e a lista de pausados do SAI pode
    # chegar a milhares de telefones em tenant grande.
    cortados = 0
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        for i in range(0, len(alvos), 400):
            lote = alvos[i : i + 400]
            placeholders = ",".join("?" * len(lote))
            cur = await db.execute(
                f"""
                UPDATE leads
                   SET modo_mudo = 1,
                       next_follow_up = NULL,
                       stage_follow_up = 0,
                       followup_hold_at = ?,
                       updated_at = ?
                 WHERE phone IN ({placeholders})
                   AND COALESCE(modo_mudo, 0) = 0
                """,
                [_now_iso(), _now_iso(), *lote],
            )
            cortados += cur.rowcount or 0
            await db.execute(
                f"""
                UPDATE lead_dispatch_queue
                   SET status = 'skipped', last_error = 'assistente pausado'
                 WHERE phone IN ({placeholders}) AND status = 'pending'
                """,
                lote,
            )
        await db.commit()
        return cortados


async def unmute_followups(phones: Iterable[str]) -> None:
    """Desfaz o `mute_followups` — usado quando o operador clica "Ativar
    assistente" no SAI.

    Nao reagenda nada, mas devolve o lead ao ciclo: o clique e uma ordem
    explicita do operador ("quero a IA de volta nesta conversa"), entao derruba
    junto a trava `followup_hold_at` deixada pelo atendimento humano. E o unico
    caminho que a derruba sem o lead escrever — decisao de produto, para que
    religar o assistente signifique religar o assistente inteiro. Nao cria linha
    para telefone que nao existe na tabela.
    """
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        for variant in phones:
            await db.execute(
                """
                UPDATE leads
                   SET modo_mudo = 0,
                       followup_hold_at = NULL,
                       updated_at = ?
                 WHERE phone = ?
                   AND (COALESCE(modo_mudo, 0) = 1 OR followup_hold_at IS NOT NULL)
                """,
                (_now_iso(), variant),
            )
        await db.commit()


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
    deve ser reativado como lead frio.

    Exclui também quem está sob `followup_hold_at` (atendente humano na
    conversa) sem ter voltado a escrever: o `modo_mudo` sozinho não bastava,
    porque uma linha já agendada ANTES do atendimento humano sobrevivia a
    qualquer limpeza posterior da flag."""
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT l.* FROM leads l
            WHERE l.next_follow_up IS NOT NULL
              AND l.next_follow_up <= ?
              AND COALESCE(l.status_conversa, '') NOT IN ('finalizado', 'agendado')
              AND COALESCE(l.modo_mudo, 0) = 0
              {FOLLOWUP_HOLD_CLAUSE}
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


async def cancel_appointment(phone: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Cancela o agendamento ativo mais recente do `phone`.
    Retorna (cancelou?, external_id, source) — usado para tambem cancelar no
    backend de calendar e/ou no SAI Comercial.
    """
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, external_id, source FROM appointments
            WHERE phone = ? AND status IN ('booked', 'reminded')
            ORDER BY scheduled_at DESC LIMIT 1
            """,
            (phone,),
        )
        row = await cur.fetchone()
        if not row:
            return False, None, None
        now = _now_iso()
        await db.execute(
            "UPDATE appointments SET status='canceled', reminder_sent_at=? WHERE id=?",
            (now, row["id"]),
        )
        await db.commit()
        logger.info("cancel_appointment: id=%s phone=%s source=%s ext=%s", row["id"], phone, row["source"], row["external_id"])
        return True, row["external_id"], row["source"]


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


# --------------- fila de disparo de 1o contato (leads externos) ---------------

async def enqueue_lead_dispatch(
    phone: str,
    nome: str = "",
    email: str = "",
    operadora: str = "",
    observacao: str = "",
    vidas: str = "",
    source_phone: str = "",
    raw_block: str = "",
    external_id: str = "",
    origem: str = "",
    dedup_hours: int = 72,
    variants: Optional[set[str]] = None,
) -> tuple[int, bool]:
    """Enfileira um lead para o 1o contato. Retorna (id, created).

    Dedup: se ja existe row pending/sent para o mesmo telefone (qualquer
    variante com/sem 9o digito) criada dentro de `dedup_hours`, nao insere —
    a mesma origem pode reenviar o mesmo lead e a UAZAPI pode reentregar o evento.
    """
    all_phones = set(variants or ()) | {phone}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=dedup_hours)).isoformat()
    placeholders = ",".join("?" * len(all_phones))
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        cur = await db.execute(
            f"""
            SELECT id FROM lead_dispatch_queue
            WHERE phone IN ({placeholders})
              AND status IN ('pending', 'sent')
              AND created_at >= ?
            LIMIT 1
            """,
            list(all_phones) + [cutoff],
        )
        row = await cur.fetchone()
        if row:
            return row[0], False
        cur = await db.execute(
            """
            INSERT INTO lead_dispatch_queue
                (phone, nome, email, operadora, observacao, vidas,
                 source_phone, raw_block, external_id, origem, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (phone, nome, email, operadora, observacao, vidas,
             source_phone, raw_block, external_id, origem, _now_iso()),
        )
        await db.commit()
        return cur.lastrowid or 0, True


async def get_pending_dispatches(now_iso: str, limit: int = 10) -> list[dict]:
    """Leads pendentes de 1o contato, FIFO. Respeita scheduled_after (retry)."""
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM lead_dispatch_queue
            WHERE status = 'pending'
              AND (scheduled_after IS NULL OR scheduled_after <= ?)
            ORDER BY created_at, id
            LIMIT ?
            """,
            (now_iso, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def count_dispatches_sent_since(since_iso: str) -> int:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM lead_dispatch_queue WHERE status='sent' AND sent_at >= ?",
            (since_iso,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def mark_dispatch_sent(dispatch_id: int) -> None:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        await db.execute(
            "UPDATE lead_dispatch_queue SET status='sent', sent_at=?, last_error=NULL WHERE id=?",
            (_now_iso(), dispatch_id),
        )
        await db.commit()


async def mark_dispatch_skipped(dispatch_id: int, reason: str) -> None:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        await db.execute(
            "UPDATE lead_dispatch_queue SET status='skipped', last_error=? WHERE id=?",
            (reason, dispatch_id),
        )
        await db.commit()


async def mark_dispatch_failed(
    dispatch_id: int,
    error: str,
    retry_after_iso: Optional[str] = None,
    max_attempts: int = 3,
) -> None:
    """Registra falha de envio. Mantem pending com backoff (scheduled_after)
    ate `max_attempts`; depois marca failed definitivo."""
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        cur = await db.execute(
            "SELECT attempts FROM lead_dispatch_queue WHERE id=?", (dispatch_id,)
        )
        row = await cur.fetchone()
        attempts = (row[0] if row and row[0] else 0) + 1
        if attempts >= max_attempts or not retry_after_iso:
            await db.execute(
                "UPDATE lead_dispatch_queue SET status='failed', attempts=?, last_error=? WHERE id=?",
                (attempts, (error or "")[:500], dispatch_id),
            )
        else:
            await db.execute(
                "UPDATE lead_dispatch_queue SET attempts=?, last_error=?, scheduled_after=? WHERE id=?",
                (attempts, (error or "")[:500], retry_after_iso, dispatch_id),
            )
        await db.commit()
