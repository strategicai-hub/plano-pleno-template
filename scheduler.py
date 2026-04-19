"""
Scheduler: processo separado que executa os jobs de follow-up via APScheduler.

Jobs:
- reactivation: lead inativo por N horas -> mensagem de reativacao em ate 3
  estagios. Cadencia configuravel em client.yaml > followups.reactivation.
- appointment_reminder: lembrete X horas antes de cada agendamento. Cadencia
  configuravel em client.yaml > followups.appointment_reminder.

Flag FOLLOWUP_DRY_RUN=true loga o que seria enviado sem chamar UAZAPI.
Habilitacao individual de cada job via followups.<job>.enabled no client.yaml.
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.client_data import load_client_data
from app.config import settings
from app.db import init_db_sync
from app.followups import appointment_reminder, reactivation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler")


def _followups_cfg() -> dict:
    data = load_client_data() or {}
    return data.get("followups") or {}


async def main() -> None:
    init_db_sync()

    cfg = _followups_cfg()
    tz = settings.SCHEDULER_TZ
    scheduler = AsyncIOScheduler(timezone=tz)

    r_cfg = cfg.get("reactivation") or {}
    if r_cfg.get("enabled", False):
        minute = r_cfg.get("cadence_minutes", 1)
        minute_expr = f"*/{int(minute)}" if int(minute) > 1 else "*"
        scheduler.add_job(
            reactivation.run,
            CronTrigger(
                day_of_week=r_cfg.get("day_of_week", "mon-fri"),
                minute=minute_expr,
                timezone=tz,
            ),
            id="reactivation",
            max_instances=1,
            coalesce=True,
        )
        logger.info("job reactivation: cadencia %s min, dias %s",
                    minute_expr, r_cfg.get("day_of_week", "mon-fri"))

    ar_cfg = cfg.get("appointment_reminder") or {}
    if ar_cfg.get("enabled", False):
        minute = int(ar_cfg.get("cadence_minutes", 15))
        minute_expr = f"*/{minute}" if minute > 1 else "*"
        scheduler.add_job(
            appointment_reminder.run,
            CronTrigger(minute=minute_expr, timezone=tz),
            id="appointment_reminder",
            max_instances=1,
            coalesce=True,
        )
        logger.info("job appointment_reminder: cadencia %d min", minute)

    if not scheduler.get_jobs():
        logger.warning("Nenhum job habilitado em client.yaml > followups. Scheduler ocioso.")

    scheduler.start()
    logger.info("Scheduler iniciado (tz=%s). Jobs: %s",
                tz, [j.id for j in scheduler.get_jobs()])

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
