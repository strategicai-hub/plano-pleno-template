"""
Trava de regressao do canal que efetivamente falhou em 11/08/2026 (erica-vieira).

A reativacao lia `lead["nome"]` cru do SQLite e disparou "Oi Tutu" — "Tutu" era
o nome do perfil do WhatsApp do lead, cujo cadastro dizia "MARCOS FERNANDO DE
TURETTA". O fix anterior so havia blindado o chat reativo; este canal ficou de
fora e continuou errando por mais um dia.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db_mod
from app.config import settings
from app.followups import reactivation as react


@pytest.fixture
def env(monkeypatch):
    """SQLite isolado + todos os efeitos externos da reativacao neutralizados."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "SQLITE_PATH", os.path.join(tmp, "t.db"))
        db_mod.init_db_sync()

        state = {"nomes_gerados": [], "enviadas": []}

        monkeypatch.setattr(
            react, "_cfg",
            lambda: {"enabled": True, "inactive_hours": 24, "max_stages": 3},
        )

        async def fake_blocked(phone):
            return False

        async def fake_acquire(phone, ttl=3600):
            return True

        async def fake_release(phone):
            return None

        monkeypatch.setattr(react.rds, "is_blocked", fake_blocked)
        monkeypatch.setattr(react.rds, "acquire_followup_lock", fake_acquire)
        monkeypatch.setattr(react.rds, "release_followup_lock", fake_release)

        async def fake_generate(phone, nome, stage, now_str=""):
            state["nomes_gerados"].append(nome)
            return f"Oi {nome}, ainda tem interesse?" if nome else "Oi, ainda tem interesse?"

        async def fake_send(number, text, delay=None):
            state["enviadas"].append((number, text))

        monkeypatch.setattr(react, "generate_reactivation_message", fake_generate)
        monkeypatch.setattr(react.uazapi, "send_text", fake_send)
        yield state


async def _agenda(phone: str, **campos):
    passado = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await db_mod.upsert_lead(phone, **campos)
    await db_mod.schedule_followup(phone, next_follow_up_iso=passado, stage=1)


async def test_usa_o_nome_do_cadastro_e_nunca_o_do_perfil(env):
    """Caso real: campo de nome confirmado vazio, cadastro preenchido."""
    await _agenda("5521992728866", nome="", nome_cadastro="MARCOS FERNANDO DE TURETTA")
    await react.run()

    assert env["nomes_gerados"] == ["Marcos"]
    assert "Tutu" not in env["enviadas"][0][1]


async def test_nome_confirmado_na_conversa_tem_prioridade(env):
    await _agenda("5521973658531", nome="Laíse", nome_cadastro="Laise Souza")
    await react.run()

    assert env["nomes_gerados"] == ["Laíse"]


async def test_registro_legado_com_nome_de_perfil_sai_sem_vocativo(env):
    """Se um push_name antigo escapar da limpeza, ele nao vira vocativo."""
    await _agenda("5521964603429", nome="Wagner Garage")
    await react.run()

    assert env["nomes_gerados"] == [""]
    assert "Garage" not in env["enviadas"][0][1]


async def test_lead_sem_nome_nenhum_sai_sem_vocativo(env):
    await _agenda("5521900000000")
    await react.run()

    assert env["nomes_gerados"] == [""]
    assert env["enviadas"][0][1] == "Oi, ainda tem interesse?"
