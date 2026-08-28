"""
Templates de follow-up: vocativo vazio, saudacao e precedencia de chaves.

Nome vazio e o caso NORMAL (o push_name do WhatsApp nao pode virar vocativo —
ver app/services/nomes.py). Todo template escrito com "{nome}" precisa sobreviver
a isso sem deixar virgula orfa no texto que o lead recebe.
"""
from datetime import datetime

import pytest

from app.followups import templates as tpl


@pytest.mark.parametrize(
    "cru, esperado",
    [
        ("Oi {nome}, tudo bem?", "Oi, tudo bem?"),
        ("Olá, {nome}! Tudo bem?", "Olá! Tudo bem?"),
        ("Olá, {nome}, bom dia!", "Olá, bom dia!"),
        ("Oi {nome}. Passando aqui.", "Oi. Passando aqui."),
        ("Olá, {nome}: uma novidade", "Olá: uma novidade"),
    ],
)
def test_nome_vazio_nao_deixa_virgula_orfa(monkeypatch, cru, esperado):
    monkeypatch.setattr(tpl, "_overrides", lambda: {"k": cru})
    assert tpl.get("k", nome="") == esperado


def test_com_nome_o_texto_fica_intacto(monkeypatch):
    monkeypatch.setattr(tpl, "_overrides", lambda: {"k": "Olá, {nome}! Tudo bem?"})
    assert tpl.get("k", nome="Marcos") == "Olá, Marcos! Tudo bem?"


def test_paragrafos_sobrevivem_a_limpeza(monkeypatch):
    """Linha em branco separa balao no envio — nao pode ser colapsada."""
    monkeypatch.setattr(tpl, "_overrides", lambda: {"k": "Olá, {nome}!\n\nSegundo balão."})
    assert tpl.get("k", nome="") == "Olá!\n\nSegundo balão."


@pytest.mark.parametrize(
    "hora, esperado",
    [(7, "bom dia"), (11, "bom dia"), (12, "boa tarde"), (17, "boa tarde"), (18, "boa noite")],
)
def test_saudacao_segue_a_hora_do_envio(hora, esperado):
    assert tpl.saudacao(datetime(2026, 8, 28, hora, 30)) == esperado


def test_chave_da_trilha_tem_prioridade_sobre_a_antiga(monkeypatch):
    monkeypatch.setattr(
        tpl, "_overrides",
        lambda: {"no_reply_stage_1": "novo", "reactivation_stage_1": "antigo"},
    )
    assert tpl.get_override_first(["no_reply_stage_1", "reactivation_stage_1"]) == "novo"


def test_cai_na_chave_antiga_quando_a_trilha_nao_tem_texto(monkeypatch):
    monkeypatch.setattr(tpl, "_overrides", lambda: {"reactivation_stage_1": "antigo"})
    assert tpl.get_override_first(["no_reply_stage_1", "reactivation_stage_1"]) == "antigo"


def test_defaults_genericos_nao_contam_como_texto_do_cliente(monkeypatch):
    """Sem override, a reativacao tem que cair na geracao livre pela IA."""
    monkeypatch.setattr(tpl, "_overrides", lambda: {})
    assert tpl.get_override_first(["no_reply_stage_1", "reactivation_stage_1"]) == ""
    # ...mas o DEFAULT continua acessivel por `get()`, para quem o quiser.
    assert tpl.get("reactivation_stage_1", nome="Ana").startswith("Oi Ana")
