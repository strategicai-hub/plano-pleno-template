"""
Cadencia ancorada no 1o contato + janela de envio.

Dois pontos que quebram na pratica e por isso tem teste:

1. o follow-up que cairia de madrugada (abertura as 15h, offset +12h = 03h) tem
   de sair na abertura da janela seguinte, e nao as 03h;
2. o SEGUNDO follow-up conta da MESMA ancora (as 15h do dia seguinte, com
   `offsets_hours: [12, 24]`), nao 12h depois do primeiro — senao cada atraso de
   um estagio empurra o seguinte junto e a regua escorrega dia apos dia.

Cliente que nao declarar `offsets_hours` continua na regua antiga
(`interval_hours` a partir do envio anterior): o ultimo teste e a contraprova.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from zoneinfo import ZoneInfo

import app.db as db_mod
from app.config import settings
from app.followups import cadence
from app.followups import reactivation as react
from app.followups import templates as tpl

TZ = ZoneInfo("America/Sao_Paulo")

CFG = {
    "enabled": True,
    "max_per_run": 20,
    "send_window": {"hours_start": "08:00", "hours_end": "18:00", "spread_minutes": 60},
    "no_reply": {
        "max_stages": 2,
        "offsets_hours": [12, 24],
        "min_gap_hours": 6,
    },
    "stalled": {"inactive_hours": 48, "max_stages": 1, "interval_hours": 48},
}

OVERRIDES = {"no_reply_stage_1": "fup 1", "no_reply_stage_2": "fup 2"}


def _atras(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


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


# --------------------------- a conta pura ---------------------------


def test_madrugada_vira_manha_e_o_segundo_toque_conta_da_ancora():
    abertura = datetime(2026, 9, 9, 15, 0, tzinfo=TZ)
    track = CFG["no_reply"]

    fup1 = cadence.proximo_envio(abertura, 1, track, CFG)
    assert fup1.date() == abertura.date() + timedelta(days=1)
    assert 8 <= fup1.hour < 9

    fup2 = cadence.proximo_envio(
        abertura, 2, track, CFG, piso=cadence.piso_por_gap(fup1, track)
    )
    assert (fup2.date(), fup2.hour, fup2.minute) == (
        abertura.date() + timedelta(days=1), 15, 0,
    )


def test_janela_comprimida_nao_gruda_os_dois_toques():
    """Abertura as 09h: FUP1 cairia as 21h -> manha seguinte; FUP2 (+24h)
    cairia as 09h, uma hora depois do FUP1. `min_gap_hours` afasta os dois."""
    abertura = datetime(2026, 9, 9, 9, 0, tzinfo=TZ)
    track = CFG["no_reply"]

    fup1 = cadence.proximo_envio(abertura, 1, track, CFG)
    fup2 = cadence.proximo_envio(
        abertura, 2, track, CFG, piso=cadence.piso_por_gap(fup1, track)
    )
    assert (fup2 - fup1) >= timedelta(hours=6)
    assert 8 <= fup2.hour <= 18


def test_horario_dentro_da_janela_nao_e_mexido():
    momento = datetime(2026, 9, 9, 14, 37, tzinfo=TZ)
    assert cadence.ajustar_para_janela(momento, CFG) == momento


def test_sem_offsets_a_regua_antiga_continua_valendo():
    """Contraprova: cliente sem `offsets_hours` mantem `interval_hours` a partir
    do envio anterior — o estagio N vale N * interval."""
    track = {"max_stages": 3, "interval_hours": 24}
    assert cadence.offset_horas(track, 1) == 24
    assert cadence.offset_horas(track, 2) == 48
    assert cadence.piso_por_gap(datetime.now(TZ), track) is None


# --------------------------- a regua rodando ---------------------------


async def test_fora_da_janela_o_lead_e_adiado_em_vez_de_cobrado(env, monkeypatch):
    """Inclui o lead que ja estava agendado pela regra antiga, de madrugada."""
    monkeypatch.setattr(
        react, "_cfg",
        lambda: {**CFG, "send_window": {"hours_start": "23:58", "hours_end": "23:59"}},
    )
    phone = "5551900000001"
    await db_mod.schedule_followup(phone, next_follow_up_iso=_atras(minutes=5), stage=1)

    await react.run()

    assert env["enviadas"] == []
    lead = await db_mod.get_lead(phone)
    assert lead["stage_follow_up"] == 1              # nao avancou
    assert lead["next_follow_up"] > _atras(minutes=0)  # foi reagendado para frente


async def test_o_proximo_toque_e_gravado_a_partir_da_ancora(env, monkeypatch):
    """Relogio congelado de proposito: a janela depende da hora do dia, e sem
    congelar este teste passaria ou falharia conforme a hora em que rodasse.

    Cenario: abertura as 15h de 09/09; o FUP 1 saiu as 09h do dia 10 (empurrado
    da madrugada). O FUP 2 tem de ficar marcado para as 15h do dia 10 — +24h da
    ABERTURA —, e nao para as 21h (+12h do envio de agora).
    """
    agora = datetime(2026, 9, 10, 9, 0, tzinfo=TZ)
    monkeypatch.setattr(react, "_now_tz", lambda: agora)

    phone = "5551900000002"
    abertura = datetime(2026, 9, 9, 15, 0, tzinfo=TZ)
    await db_mod.schedule_followup(
        phone,
        next_follow_up_iso=(agora - timedelta(minutes=5)).astimezone(timezone.utc).isoformat(),
        stage=1,
        anchor_iso=abertura.astimezone(timezone.utc).isoformat(),
    )

    await react.run()

    assert [t for _, t in env["enviadas"]] == ["fup 1"]
    lead = await db_mod.get_lead(phone)
    assert lead["stage_follow_up"] == 2
    proximo = datetime.fromisoformat(lead["next_follow_up"]).astimezone(TZ)
    assert proximo == abertura + timedelta(hours=24)
    # A ancora sobrevive ao avanco de estagio.
    assert lead["followup_anchor_at"] is not None
