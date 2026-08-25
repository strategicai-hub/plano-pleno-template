from datetime import datetime
from zoneinfo import ZoneInfo

from app import consumer
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


# --- REGRESSAO: vazamento de marcador no WhatsApp (25/08/2026) ---
# Um cliente derivado deste template entregou "[ORIGEM=]" e "[IMAGEM_FOTOS_1]"
# a um lead real. A auditoria do template achou tres buracos que continuavam
# abertos aqui, cobertos pelos testes abaixo.

def _all_text(parts):
    return " ".join(p["content"] for p in parts if p["type"] == "text")


def test_resposta_so_com_marcador_nao_cai_no_fallback():
    """O fallback `parts = [texto cru]` mandava a resposta inteira ao lead.

    Quando todo o conteudo era marcador, cada balao ficava vazio, a lista saia
    vazia e o fallback devolvia o texto ORIGINAL — com os marcadores.
    """
    parts, *_ = _parse_ai_response("[FLAG_X=1]")
    assert parts == []


def test_flag_vazia_nunca_vaza():
    for flag in ("[FINALIZADO=]", "[TRANSFERIR=]", "[AGENDAR=]", "[NOME=]"):
        parts, *_ = _parse_ai_response(f"Bom dia! {flag}")
        assert _all_text(parts) == "Bom dia!", f"{flag} vazou"


def test_eco_do_contexto_do_sistema_nao_vaza():
    """O bloco injetado a cada turno tem espacos e acento — escapava do scrub."""
    parts, *_ = _parse_ai_response(
        "[CONTEXTO DO SISTEMA — não responda sobre isto: agora são 14:16]\n\nBom dia!"
    )
    assert _all_text(parts) == "Bom dia!"


def test_texto_colado_na_tag_de_midia_e_preservado(monkeypatch):
    """Antes, o texto no mesmo balao da tag era descartado junto com ela."""
    monkeypatch.setitem(consumer.MEDIA_DICT, "[FOTO_1]", {"url": "https://x/1.jpg", "type": "image"})
    parts, *_ = _parse_ai_response("Olha só o studio: [FOTO_1] bonito, né?")
    assert [p["type"] for p in parts] == ["text", "image", "text"]
    assert parts[0]["content"] == "Olha só o studio:"
    assert parts[2]["content"] == "bonito, né?"


def test_varias_tags_no_mesmo_bloco_viram_varias_midias(monkeypatch):
    """Antes so a primeira tag do bloco virava envio; as outras sumiam."""
    monkeypatch.setitem(consumer.MEDIA_DICT, "[FOTO_1]", {"url": "https://x/1.jpg", "type": "image"})
    monkeypatch.setitem(consumer.MEDIA_DICT, "[FOTO_2]", {"url": "https://x/2.jpg", "type": "image"})
    parts, *_ = _parse_ai_response("[FOTO_1] [FOTO_2]")
    assert [p["content"] for p in parts] == ["https://x/1.jpg", "https://x/2.jpg"]


def test_tag_de_midia_com_digito_e_reconhecida(monkeypatch):
    """Cliente numera as proprias tags; `[A-Z_]+` nao casava nenhuma delas."""
    monkeypatch.setitem(
        consumer.MEDIA_DICT, "[IMAGEM_FOTOS_1]", {"url": "https://x/f1.jpg", "type": "image"}
    )
    parts, *_ = _parse_ai_response("Conheça o espaço:\n\n[IMAGEM_FOTOS_1]\n\nQuer visitar?")
    assert [p["type"] for p in parts] == ["text", "image", "text"]
    assert "[IMAGEM" not in _all_text(parts)


def test_no_agendar_returns_none():
    _, _, _, agendar, _ = _parse_ai_response("Oi tudo bem [FINALIZADO=0]")
    assert agendar is None
