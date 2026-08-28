"""
Disparo de 1o contato por ORIGEM do lead.

Lead do formulario de Lead Ads da Meta chega pelo POST /sai/leads com
`origin: "META_LEAD_ADS"`. A partir dai o disparo tem que:

1. usar o roteiro de abertura que o cliente escreveu para aquela origem
   (`lead_dispatch.templates_by_origin`), e nao o 1o contato generico da IA;
2. mandar o texto em baloes (a abertura tem varios paragrafos);
3. semear no historico o marcador de contato ATIVO com a ROTA da origem — sem
   ele o prompt trata a conversa como passiva e refaz a apresentacao do zero.

Lead sem origem declarada continua no caminho antigo (1o contato gerado pela IA).
"""
import os
import tempfile

import pytest

import app.db as db_mod
from app.config import settings
from app.followups import lead_dispatch as ld

ABERTURA = (
    "Olá, {nome}! Tudo bem? Meu nome é Luiza, sou assistente da Auxiliadora Predial.\n\n"
    "Vimos que você demonstrou interesse em um dos nossos imóveis.\n\n"
    "1) Você busca um imóvel para moradia da família ou o foco é investimento e rentabilidade?"
)

CFG = {
    "enabled": True,
    "days": "mon-sun",
    "hours_start": "00:00",
    "hours_end": "23:59",
    "daily_cap": 100,
    "followup_after_hours": 24,
    "callback_enabled": False,
    "templates_by_origin": {"META_LEAD_ADS": ABERTURA},
}


@pytest.fixture
def env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "SQLITE_PATH", os.path.join(tmp, "t.db"))
        monkeypatch.setattr(settings, "FOLLOWUP_DRY_RUN", False)
        db_mod.init_db_sync()

        state = {"paragrafos": [], "historico": [], "gerou_ia": 0}

        monkeypatch.setattr(ld, "_cfg", lambda: CFG)

        async def _false():
            return False

        async def _lock(phone, ttl=600):
            return True

        async def _none(*a, **kw):
            return None

        async def fake_has_history(phone):
            return False

        async def fake_get_lead(phone):
            return None

        async def fake_send_paragraphs(number, text, delay=None):
            state["paragrafos"].append((number, text))

        async def fake_append(phone, role, content):
            state["historico"].append((phone, role, content))

        async def fake_vary(phone, base_text, *, nome="", kind=""):
            return ""  # variacao desligada: o teste checa o texto do cliente

        async def fake_first_contact(phone, nome, *, observacao=""):
            state["gerou_ia"] += 1
            return "primeiro contato gerado pela IA"

        monkeypatch.setattr(ld.rds, "is_dispatch_gated", _false)
        monkeypatch.setattr(ld.rds, "is_blocked", lambda p: _false())
        monkeypatch.setattr(ld.rds, "acquire_followup_lock", _lock)
        monkeypatch.setattr(ld.rds, "release_followup_lock", _none)
        monkeypatch.setattr(ld.rds, "has_chat_history", fake_has_history)
        monkeypatch.setattr(ld.rds, "get_lead", fake_get_lead)
        monkeypatch.setattr(ld.rds, "create_lead", _none)
        monkeypatch.setattr(ld.rds, "update_lead", _none)
        monkeypatch.setattr(ld.rds, "append_chat_history", fake_append)
        monkeypatch.setattr(ld.rds, "set_dispatch_gate", _none)
        monkeypatch.setattr(ld.uazapi, "send_presence", _none)
        monkeypatch.setattr(ld.uazapi, "send_paragraphs", fake_send_paragraphs)
        monkeypatch.setattr(ld, "vary_message", fake_vary)
        monkeypatch.setattr(ld, "generate_first_contact_message", fake_first_contact)
        yield state


async def test_lead_da_meta_recebe_a_abertura_do_cliente(env):
    await db_mod.enqueue_lead_dispatch(
        phone="5551900000010", nome="Marcos Fernando", origem="META_LEAD_ADS",
    )
    await ld.run()

    assert env["gerou_ia"] == 0, "abertura da Meta nao pode cair no 1o contato generico"
    _numero, texto = env["paragrafos"][0]
    assert texto.startswith("Olá, Marcos!")
    assert "1) Você busca um imóvel" in texto
    # Preserva os paragrafos: cada um vira um balao no envio.
    assert texto.count("\n\n") == 2


async def test_lead_da_meta_semeia_a_rota_no_historico(env):
    await db_mod.enqueue_lead_dispatch(
        phone="5551900000011", nome="Ana", origem="META_LEAD_ADS",
    )
    await ld.run()

    contextos = [c for _p, role, c in env["historico"] if role == "user"]
    assert contextos, "nada semeado no historico"
    assert "contato ATIVO" in contextos[0]
    assert "Rota: META_FORM" in contextos[0]


async def test_lead_sem_origem_continua_no_primeiro_contato_da_ia(env):
    await db_mod.enqueue_lead_dispatch(phone="5551900000012", nome="Joana")
    await ld.run()

    assert env["gerou_ia"] == 1
    assert env["paragrafos"][0][1] == "primeiro contato gerado pela IA"
    contextos = [c for _p, role, c in env["historico"] if role == "user"]
    assert "origem externa" in contextos[0]
    assert "contato ATIVO" not in contextos[0]


async def test_nome_de_cadastro_invalido_nao_vira_vocativo(env):
    """Mesma trava de nomes do resto do bot, agora no roteiro por origem."""
    await db_mod.enqueue_lead_dispatch(
        phone="5551900000013", nome="ZAP 2 tim", origem="META_LEAD_ADS",
    )
    await ld.run()

    texto = env["paragrafos"][0][1]
    assert "ZAP" not in texto
    assert texto.startswith("Olá! Tudo bem?")


async def test_agenda_o_primeiro_follow_up_24h_depois_do_envio(env):
    phone = "5551900000014"
    await db_mod.enqueue_lead_dispatch(phone=phone, nome="Bruno", origem="META_LEAD_ADS")
    await ld.run()

    lead = await db_mod.get_lead(phone)
    assert lead["next_follow_up"] is not None
    assert lead["stage_follow_up"] == 1
    # Nunca respondeu: fica na trilha `no_reply` da reativacao.
    assert not lead["last_customer_message_at"]
