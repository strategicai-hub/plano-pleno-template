"""
Templates default para mensagens dos jobs de follow-up.

O cliente pode sobrescrever via client.yaml > followups.templates.<chave>.
Placeholders suportados: {nome}, {saudacao}, {horario}, {modalidade}.

Chaves de reativacao:
- `no_reply_stage_N`  — lead que nunca respondeu (conta do envio do 1o contato)
- `stalled_stage_N`   — lead que respondeu e estagnou (conta do ultimo retorno)
- `reactivation_stage_N` — formato antigo, trilha unica. Continua valendo como
  fallback para os clientes que ainda nao separaram as duas trilhas.
"""
import re
from datetime import datetime

from app.client_data import load_client_data

DEFAULTS = {
    "reactivation_stage_1": "Oi {nome}, passando pra saber se ainda tem interesse!",
    "reactivation_stage_2": "Oi {nome}, consegui um horario especial pra voce — quer aproveitar?",
    "reactivation_stage_3": "Oi {nome}, ultima chance — posso segurar sua vaga?",
    "appointment_reminder": "Lembrete: sua aula e hoje as {horario}. Te esperamos!",
}


def saudacao(now: datetime) -> str:
    """"bom dia" / "boa tarde" / "boa noite" conforme a hora do envio.

    Mensagem proativa com saudacao fixa no texto sai errada o dia inteiro: um
    "bom dia" escrito pelo cliente e disparado as 15h denuncia automacao.
    """
    if now.hour < 12:
        return "bom dia"
    if now.hour < 18:
        return "boa tarde"
    return "boa noite"


def limpar_vocativo_vazio(texto: str) -> str:
    """Remove a virgula orfa quando {nome} veio vazio.

    Nome vazio e o caso normal desde que o push_name do WhatsApp deixou de ser
    usado como nome — o template precisa sobreviver a isso sem sair torto:

        "Oi , tudo bem"        -> "Oi, tudo bem"
        "Olá, , bom dia!"      -> "Olá, bom dia!"
        "Olá, ! Tudo bem?"     -> "Olá! Tudo bem?"
        "Oi . Passando aqui."  -> "Oi. Passando aqui."
    """
    # Espaco antes de pontuacao nao existe em portugues — quando aparece, e o
    # buraco deixado por um {nome} vazio.
    texto = re.sub(r"[ \t]+([,!?.:;])", r"\1", texto)
    texto = re.sub(r",\s*,", ",", texto)
    # Virgula encostada na pontuacao que fecha a frase: sobra de "Olá, {nome}!".
    texto = re.sub(r",\s*([!?.:;])", r"\1", texto)
    # Preserva as linhas em branco (cada bloco vira um balao); colapsa so os
    # espacos dentro de cada linha.
    linhas = [re.sub(r"[ \t]{2,}", " ", linha).rstrip() for linha in texto.split("\n")]
    return "\n".join(linhas).strip()


# Alias historico (uso interno anterior a esta versao).
_limpar_vocativo_vazio = limpar_vocativo_vazio


def _overrides() -> dict:
    data = load_client_data() or {}
    return (data.get("followups") or {}).get("templates") or {}


def get(key: str, **placeholders) -> str:
    overrides = _overrides()
    raw = overrides.get(key) or DEFAULTS.get(key, "")
    try:
        return limpar_vocativo_vazio(raw.format(**placeholders))
    except (KeyError, IndexError):
        return raw


def get_override_first(keys: list[str], **placeholders) -> str:
    """Primeiro template ESCRITO PELO CLIENTE entre `keys`, na ordem.

    Ignora os DEFAULTS de proposito: eles sao placeholders genericos ("Oi
    {nome}, passando pra saber se ainda tem interesse!") e existem so para o
    caso de alguem ligar o job sem configurar nada. Se contassem aqui, todo
    cliente que nao escreveu texto proprio perderia a reativacao personalizada
    que a IA monta lendo a conversa — que e bem melhor.

    Usado pela reativacao para tentar a chave da trilha nova
    (`no_reply_stage_1`) antes de cair na chave antiga (`reactivation_stage_1`).
    Retorna "" quando o cliente nao escreveu nenhuma das duas.
    """
    overrides = _overrides()
    for key in keys:
        raw = overrides.get(key)
        if isinstance(raw, str) and raw.strip():
            return get(key, **placeholders)
    return ""
