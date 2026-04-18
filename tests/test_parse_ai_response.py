from datetime import datetime

from app.consumer import _parse_ai_response


def test_text_simple():
    parts, fin, trans, agendar = _parse_ai_response("Oi, tudo bem?")
    assert parts == [{"type": "text", "content": "Oi, tudo bem?"}]
    assert fin is False
    assert trans is False
    assert agendar is None


def test_finalizado_flag_true():
    parts, fin, trans, _ = _parse_ai_response("Tchau! [FINALIZADO=1]")
    assert parts[0]["content"] == "Tchau!"
    assert fin is True
    assert trans is False


def test_finalizado_flag_false():
    _, fin, *_ = _parse_ai_response("Ainda conversando [FINALIZADO=0]")
    assert fin is False


def test_transferir_flag_true():
    parts, fin, trans, _ = _parse_ai_response(
        "Excelente! Vou repassar para a equipe. [TRANSFERIR=1]"
    )
    assert trans is True
    assert fin is False
    assert "[TRANSFERIR=1]" not in parts[0]["content"]


def test_both_flags_together():
    parts, fin, trans, _ = _parse_ai_response("Combinado! [TRANSFERIR=1] [FINALIZADO=1]")
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


def test_unknown_tag_falls_through_as_text():
    parts, *_ = _parse_ai_response("Olha isso: [FOTO_INEXISTENTE]")
    assert parts[0]["type"] == "text"
    assert "[FOTO_INEXISTENTE]" in parts[0]["content"]


# --- PLENO: flag [AGENDAR=...] ---

def test_agendar_flag_with_modalidade():
    parts, _, _, agendar = _parse_ai_response(
        "Perfeito, ja deixei reservado! [AGENDAR=2025-11-12T19:00|Boxe tradicional]"
    )
    assert agendar is not None
    dt, modalidade = agendar
    assert dt == datetime(2025, 11, 12, 19, 0)
    assert modalidade == "Boxe tradicional"
    assert "[AGENDAR=" not in parts[0]["content"]


def test_agendar_flag_without_modalidade():
    parts, _, _, agendar = _parse_ai_response(
        "Fechado! [AGENDAR=2025-11-12T19:00]"
    )
    assert agendar is not None
    dt, modalidade = agendar
    assert dt == datetime(2025, 11, 12, 19, 0)
    assert modalidade == ""
    assert "[AGENDAR=" not in parts[0]["content"]


def test_agendar_invalid_iso_is_ignored():
    parts, _, _, agendar = _parse_ai_response(
        "Tentando [AGENDAR=nao-eh-data|Algo]"
    )
    assert agendar is None


def test_no_agendar_returns_none():
    _, _, _, agendar = _parse_ai_response("Oi tudo bem [FINALIZADO=0]")
    assert agendar is None
