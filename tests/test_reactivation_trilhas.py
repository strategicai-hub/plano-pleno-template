"""
Duas trilhas de reativacao.

- `no_reply`: o lead nunca respondeu. Relogio comeca no ENVIO do 1o contato
  (quem agenda o estagio 1 e o lead_dispatch). Regua da Auxiliadora: 24h, 48h,
  72h — e o ultimo estagio encerra o contato.
- `stalled`: o lead respondeu e parou. Relogio comeca no ultimo retorno dele.
  Uma unica retomada, 48h depois.

O que separa as duas e so `last_customer_message_at`. Sem isso, um lead que
respondeu uma vez levaria os tres toques de "nao respondeu nada" — que e
justamente o texto que nao cabe pra ele.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db_mod
from app.config import settings
from app.followups import reactivation as react
from app.followups import templates as tpl

CFG = {
    "enabled": True,
    "max_per_run": 20,
    "no_reply": {"max_stages": 3, "interval_hours": 24},
    "stalled": {"inactive_hours": 48, "max_stages": 1, "interval_hours": 48},
}

OVERRIDES = {
    "no_reply_stage_1": "sem resposta 1",
    "no_reply_stage_2": "sem resposta 2",
    "no_reply_stage_3": "sem resposta 3",
    "stalled_stage_1": "estagnou 1",
}


@pytest.fixture
def env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "SQLITE_PATH", os.path.join(tmp, "t.db"))
        db_mod.init_db_sync()

        state = {"enviadas": []}

        monkeypatch.setattr(react, "_cfg", lambda: CFG)
        monkeypatch.setattr(tpl, "_overrides", lambda: dict(OVERRIDES))

        async def _false(phone):
            return False

        async def _true(phone, ttl=3600):
            return True

        async def _none(phone):
            return None

        async def fake_vary(phone, base_text, *, nome="", kind=""):
            return ""

        async def fake_send(number, text, delay=None):
            state["enviadas"].append((number, text))

        monkeypatch.setattr(react.rds, "is_blocked", _false)
        monkeypatch.setattr(react.rds, "acquire_followup_lock", _true)
        monkeypatch.setattr(react.rds, "release_followup_lock", _none)
        monkeypatch.setattr(react, "vary_message", fake_vary)
        monkeypatch.setattr(react.uazapi, "send_paragraphs", fake_send)
        yield state


def _atras(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


async def test_lead_que_nunca_respondeu_percorre_os_tres_toques(env):
    phone = "5551900000001"
    await db_mod.schedule_followup(phone, next_follow_up_iso=_atras(minutes=5), stage=1)

    for esperado in ("sem resposta 1", "sem resposta 2", "sem resposta 3"):
        # Cada ciclo reagenda para daqui a 24h; o teste puxa o relogio para tras.
        lead = await db_mod.get_lead(phone)
        if lead and lead.get("next_follow_up"):
            await db_mod.schedule_followup(
                phone,
                next_follow_up_iso=_atras(minutes=5),
                stage=int(lead["stage_follow_up"] or 1),
            )
        await react.run()
        assert env["enviadas"][-1][1] == esperado

    # Terceiro toque encerra: sem proximo agendamento e conversa finalizada.
    lead = await db_mod.get_lead(phone)
    assert lead["status_conversa"] == "finalizado"
    assert lead["next_follow_up"] is None


async def test_intervalo_da_trilha_sem_resposta_e_de_24h(env):
    phone = "5551900000002"
    await db_mod.schedule_followup(phone, next_follow_up_iso=_atras(minutes=5), stage=1)
    await react.run()

    lead = await db_mod.get_lead(phone)
    proximo = datetime.fromisoformat(lead["next_follow_up"])
    horas = (proximo - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 23 <= horas <= 25


async def test_lead_que_respondeu_leva_um_unico_toque(env):
    """Trilha `stalled`: uma retomada so, e ela encerra o ciclo."""
    phone = "5551900000003"
    await db_mod.upsert_lead(phone, last_customer_message_at=_atras(hours=72))
    await db_mod.schedule_followup(phone, next_follow_up_iso=_atras(minutes=5), stage=1)

    await react.run()
    assert [t for _, t in env["enviadas"]] == ["estagnou 1"]

    lead = await db_mod.get_lead(phone)
    assert lead["status_conversa"] == "finalizado"
    assert lead["next_follow_up"] is None


async def test_lead_que_respondeu_ha_pouco_nao_entra_na_regua(env):
    """48h e o corte da trilha `stalled` — 12h atras nao e lead estagnado."""
    phone = "5551900000004"
    await db_mod.upsert_lead(phone, last_customer_message_at=_atras(hours=12))
    await react.run()

    assert env["enviadas"] == []


async def test_lead_estagnado_e_semeado_apos_48h(env):
    """Sem next_follow_up agendado, quem passou de 48h entra sozinho."""
    phone = "5551900000005"
    await db_mod.upsert_lead(phone, last_customer_message_at=_atras(hours=60))
    await react.run()

    assert [t for _, t in env["enviadas"]] == ["estagnou 1"]


async def test_config_antiga_sem_trilhas_continua_funcionando(env, monkeypatch):
    """Cliente que ainda nao separou as trilhas mantem o comportamento de antes."""
    monkeypatch.setattr(
        react, "_cfg",
        lambda: {"enabled": True, "inactive_hours": 24, "max_stages": 2},
    )
    monkeypatch.setattr(
        tpl, "_overrides",
        lambda: {"reactivation_stage_1": "antigo 1", "reactivation_stage_2": "antigo 2"},
    )
    phone = "5551900000006"
    await db_mod.schedule_followup(phone, next_follow_up_iso=_atras(minutes=5), stage=1)
    await react.run()

    assert env["enviadas"][-1][1] == "antigo 1"
    lead = await db_mod.get_lead(phone)
    assert lead["stage_follow_up"] == 2
