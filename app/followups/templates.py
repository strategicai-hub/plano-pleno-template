"""
Templates default para mensagens dos jobs de follow-up.

O cliente pode sobrescrever via client.yaml > followups.templates.<chave>.
Placeholders suportados: {nome}, {horario}, {modalidade}.
"""
import re

from app.client_data import load_client_data

DEFAULTS = {
    "reactivation_stage_1": "Oi {nome}, passando pra saber se ainda tem interesse!",
    "reactivation_stage_2": "Oi {nome}, consegui um horario especial pra voce — quer aproveitar?",
    "reactivation_stage_3": "Oi {nome}, ultima chance — posso segurar sua vaga?",
    "appointment_reminder": "Lembrete: sua aula e hoje as {horario}. Te esperamos!",
}


def _limpar_vocativo_vazio(texto: str) -> str:
    """Remove a virgula orfa quando {nome} veio vazio ("Oi , tudo bem" -> "Oi, tudo bem").

    Nome vazio e o caso normal desde que o push_name do WhatsApp deixou de ser
    usado como nome — o template precisa sobreviver a isso sem sair torto.
    """
    texto = re.sub(r"\s+,", ",", texto)
    texto = re.sub(r",\s*,", ",", texto)
    return re.sub(r"\s{2,}", " ", texto).strip()


def get(key: str, **placeholders) -> str:
    data = load_client_data() or {}
    overrides = (data.get("followups") or {}).get("templates") or {}
    raw = overrides.get(key) or DEFAULTS.get(key, "")
    try:
        return _limpar_vocativo_vazio(raw.format(**placeholders))
    except (KeyError, IndexError):
        return raw
