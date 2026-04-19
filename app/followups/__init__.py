"""
Jobs de follow-up automaticos (executados pelo scheduler.py).

Cada modulo expoe uma funcao `run()` assincrona que o APScheduler chama
conforme o cron configurado. Cada `run` e idempotente e pode ser executado
em DRY_RUN via settings.FOLLOWUP_DRY_RUN.
"""
