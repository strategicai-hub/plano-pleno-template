"""
Smoke test local do plano pleno.

Valida sem precisar de Redis/RabbitMQ/UAZAPI/deploy:
1. client.yaml esta presente e tem os blocos do pleno (appointments, followups)
2. SQLite inicializa, schema de leads + appointments cria sem erro
3. Parser de [AGENDAR=...] extrai a data corretamente
4. Templates de follow-up fazem format com placeholders

Uso:
    python scripts/smoke.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Env minimo para importar o app sem depender de .env real
os.environ.setdefault("PROJECT_SLUG", "smoke")
os.environ.setdefault("GEMINI_API_KEY", "fake")
os.environ.setdefault("UAZAPI_TOKEN", "fake")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6399")
os.environ.setdefault("RABBITMQ_HOST", "127.0.0.1")
os.environ.setdefault("RABBITMQ_PORT", "5699")

_FAIL = False


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _fail(msg: str) -> None:
    global _FAIL
    _FAIL = True
    print(f"  [FAIL] {msg}")


def check_client_yaml() -> None:
    print("\n[1/4] client.yaml")
    path = ROOT / "client.yaml"
    if not path.exists():
        _fail(f"{path} nao existe — copie client.example.yaml e preencha")
        return

    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    appts = data.get("appointments")
    if not appts:
        _fail("bloco `appointments:` ausente no client.yaml")
    else:
        src = appts.get("source")
        if src not in ("google_calendar", "external_system"):
            _fail(f"appointments.source invalido: {src!r}")
        else:
            _ok(f"appointments.source = {src}")
        bh = appts.get("business_hours") or {}
        if not (bh.get("mon_fri") or bh.get("sat") or bh.get("sun")):
            _fail("appointments.business_hours vazio — IA nao saberá sugerir slots")
        else:
            _ok("appointments.business_hours preenchido")

    fups = data.get("followups")
    if not fups:
        _fail("bloco `followups:` ausente no client.yaml")
    else:
        for job in ("reactivation", "appointment_reminder"):
            cfg = fups.get(job) or {}
            state = "enabled" if cfg.get("enabled") else "disabled"
            _ok(f"followup.{job}: {state}")


async def check_sqlite() -> None:
    print("\n[2/4] SQLite (leads + appointments)")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "smoke.db")
        from app.config import settings
        settings.SQLITE_PATH = db_path

        from app import db
        db.init_db_sync()
        _ok(f"init_db em {db_path}")

        await db.upsert_lead("5511999990000", nome="Smoke", status="novo")
        lead = await db.get_lead("5511999990000")
        if lead and lead.get("nome") == "Smoke":
            _ok("upsert_lead + get_lead")
        else:
            _fail(f"upsert_lead/get_lead inconsistente: {lead!r}")

        now = datetime.now(timezone.utc)
        appt_id = await db.schedule_appointment(
            phone="5511999990000",
            scheduled_at_iso=(now + timedelta(hours=1)).isoformat(),
            source="google_calendar",
            external_id="evt_smoke",
            modalidade="Smoke",
        )
        if appt_id > 0:
            _ok(f"schedule_appointment id={appt_id}")
        else:
            _fail("schedule_appointment nao retornou id")

        due = await db.get_appointments_for_reminder(
            until_iso=(now + timedelta(hours=3)).isoformat(),
            now_iso=now.isoformat(),
        )
        if len(due) == 1:
            _ok("get_appointments_for_reminder retorna o agendamento")
        else:
            _fail(f"esperava 1 agendamento, veio {len(due)}")


def check_parser() -> None:
    print("\n[3/4] Parser [AGENDAR=...]")
    from app.consumer import _parse_ai_response

    parts, fin, trans, agendar = _parse_ai_response(
        "Perfeito! [AGENDAR=2026-05-14T19:00|Boxe tradicional]"
    )
    if agendar is None:
        _fail("parser nao extraiu a flag AGENDAR")
        return
    dt, mod = agendar
    if dt == datetime(2026, 5, 14, 19, 0) and mod == "Boxe tradicional":
        _ok(f"AGENDAR extraido: {dt.isoformat()} / {mod}")
    else:
        _fail(f"AGENDAR inesperado: {dt}, {mod!r}")

    if "[AGENDAR=" in parts[0]["content"]:
        _fail("flag vazou no conteudo enviado ao lead")
    else:
        _ok("flag removida do conteudo enviado")


def check_templates() -> None:
    print("\n[4/4] Templates de follow-up")
    from app.followups import templates

    reminder = templates.get("appointment_reminder", nome="Ana", horario="19:00", modalidade="Boxe")
    if reminder and "19:00" in reminder:
        _ok(f"appointment_reminder: {reminder}")
    else:
        _fail(f"appointment_reminder inesperado: {reminder!r}")

    for stage in (1, 2, 3):
        msg = templates.get(f"reactivation_stage_{stage}", nome="Ana")
        if msg:
            _ok(f"reactivation_stage_{stage} ok")
        else:
            _fail(f"reactivation_stage_{stage} vazio")


def main() -> int:
    print("=" * 60)
    print(" SMOKE TEST - plano-pleno-template")
    print("=" * 60)

    check_client_yaml()
    asyncio.run(check_sqlite())
    check_parser()
    check_templates()

    print("\n" + "=" * 60)
    if _FAIL:
        print(" RESULTADO: FALHOU (veja [FAIL] acima)")
        print("=" * 60)
        return 1
    print(" RESULTADO: OK")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
