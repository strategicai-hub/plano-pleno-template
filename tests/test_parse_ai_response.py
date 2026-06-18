from datetime import datetime
from zoneinfo import ZoneInfo

from app.consumer import _parse_ai_response

_SP = ZoneInfo("America/Sao_Paulo")


def test_text_simple():
    parts, fin, trans, agendar, _ = _parse_ai_response("Oi, tudo bem?")
    assert parts == [{"type": "text", "content": "Oi, tudo bem?"}]
    assert fin is False
    assert trans is False
    assert agendar is None


def test_finalizado_flag_true():
    parts, fin, trans, _, _ = _parse_ai_response("Tchau! [FINALIZADO=1]")
    assert parts[0]["content"] == "Tchau!"
    assert fin is True
    assert trans is False


def test_finalizado_flag_false():
    _, fin, *_ = _parse_ai_response("Ainda conversando [FINALIZADO=0]")
    assert fin is False


def test_transferir_flag_true():
    parts, fin, trans, _, _ = _parse_ai_response(
        "Excelente! Vou repassar para a equipe. [TRANSFERIR=1]"
    )
    assert trans is True
    assert fin is False
    assert "[TRANSFERIR=1]" not in parts[0]["content"]


def test_both_flags_together():
    parts, fin, trans, _, _ = _parse_ai_response("Combinado! [TRANSFERIR=1] [FINALIZADO=1]")
    assert fin is True
    assert trans is True
    assert "[TRANSFERIR" not in parts[0]["content"]
    assert "[FINALIZADO" not in parts[0]["content"]


def test_split_by_triple_pipe():
    parts, *_ = _parse_ai_response("Oi!|||Tudo bem?")
    assert len(parts) == 2
    assert parts[0]["content"] == "Oi!"
    assert parts[1]["content"] == "Tudo bem?"


def test_split_by_double_newline():
    parts, *_ = _parse_ai_response("Primeira.\n\nSegunda.")
    assert len(parts) == 2


def test_unknown_tag_is_scrubbed_from_text():
    # Rede de seguranca: tags em colchetes desconhecidas (nao-midia) nunca
    # devem vazar para o lead.
    parts, *_ = _parse_ai_response("Olha isso: [FOTO_INEXISTENTE]")
    assert parts[0]["type"] == "text"
    assert "[FOTO_INEXISTENTE]" not in parts[0]["content"]
    assert parts[0]["content"] == "Olha isso:"


def test_cancelar_agendamento_flag_with_value_does_not_leak():
    # A IA pode contaminar a flag com o padrao =0/=1 das outras flags.
    parts, _, _, _, cancelar = _parse_ai_response(
        "Tudo certo, vou ver isso. [CANCELAR_AGENDAMENTO=0]"
    )
    assert cancelar is True
    assert "CANCELAR_AGENDAMENTO" not in parts[0]["content"]
    assert "[" not in parts[0]["content"]


def test_leftover_flag_is_scrubbed_as_safety_net():
    # Mesmo que uma flag nova/desconhecida apareca, o scrub final a remove.
    parts, *_ = _parse_ai_response("Tudo certo! [FLAG_NOVA=123]")
    assert "[" not in parts[0]["content"]
    assert parts[0]["content"] == "Tudo certo!"


# --- PLENO: flag [AGENDAR=...] ---

def test_agendar_flag_with_modalidade():
    parts, _, _, agendar, _ = _parse_ai_response(
        "Perfeito, ja deixei reservado! [AGENDAR=2025-11-12T19:00|Boxe tradicional]"
    )
    assert agendar is not None
    dt, modalidade = agendar
    # _parse_ai_response interpreta a hora no fuso de São Paulo e retorna tz-aware.
    assert dt == datetime(2025, 11, 12, 19, 0, tzinfo=_SP)
    assert modalidade == "Boxe tradicional"
    assert "[AGENDAR=" not in parts[0]["content"]


def test_agendar_flag_without_modalidade():
    parts, _, _, agendar, _ = _parse_ai_response(
        "Fechado! [AGENDAR=2025-11-12T19:00]"
    )
    assert agendar is not None
    dt, modalidade = agendar
    assert dt == datetime(2025, 11, 12, 19, 0, tzinfo=_SP)
    assert modalidade == ""
    assert "[AGENDAR=" not in parts[0]["content"]


def test_agendar_invalid_iso_is_ignored():
    parts, _, _, agendar, _ = _parse_ai_response(
        "Tentando [AGENDAR=nao-eh-data|Algo]"
    )
    assert agendar is None


def test_no_agendar_returns_none():
    _, _, _, agendar, _ = _parse_ai_response("Oi tudo bem [FINALIZADO=0]")
    assert agendar is None
