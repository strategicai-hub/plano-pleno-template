"""Normalizacao de telefone BR — fonte unica das variantes com/sem o 9o digito.

Vive num modulo proprio (sem dependencia de db/client_data) para que camadas
baixas como `redis_service` possam usa-lo sem ciclo de import. `lead_intake`
re-exporta estes nomes por retrocompat.

POR QUE ISTO EXISTE: o mesmo celular circula em DOIS formatos no sistema.
O JID que a UAZAPI entrega costuma vir SEM o 9 (ex.: "556183644341") e e essa
forma que vira chave de tudo no bot (Redis e SQLite). O SAI Comercial, ao
gravar a Conversation, canonicaliza ADICIONANDO o 9 ("5561983644341") — logo o
POST /sai/block chega sempre na forma com 9. Casar so a string exata faz o
bloqueio ser gravado num telefone e consultado em outro: o bot segue mudo no
inbound (o SAI barra o relay) mas os jobs proativos, que consultam pelo phone
do SQLite, nao encontram bloqueio nenhum e disparam follow-up por cima do
"Desativar assistente". Toda leitura/escrita de bloqueio varre as duas formas.
"""
import re


def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def normalize_br_phone(raw: str) -> str:
    """Normaliza para digitos com DDI 55. Retorna "" se nao parecer BR valido."""
    digits = only_digits(raw)
    if not digits:
        return ""
    digits = digits.lstrip("0")  # zero de operadora/DDD (ex.: 021...)
    if digits.startswith("55") and len(digits) in (12, 13):
        return digits
    if len(digits) in (10, 11):  # DDD + assinante, sem DDI
        return "55" + digits
    return ""


def phone_variants(digits: str) -> set[str]:
    """Formas com e sem o 9o digito de um numero BR.

    JIDs do WhatsApp podem omitir o 9 em moveis registrados antes da
    migracao — o lead cadastrado como 5521 9XXXX-XXXX pode responder como
    5521XXXXXXXX. Matching, dedup, bloqueio e seeding de historico usam as
    duas formas.
    """
    variants = {digits}
    if digits.startswith("55"):
        ddd, subscriber = digits[2:4], digits[4:]
        if len(subscriber) == 9 and subscriber.startswith("9"):
            variants.add("55" + ddd + subscriber[1:])
        elif len(subscriber) == 8:
            variants.add("55" + ddd + "9" + subscriber)
    return variants


def block_variants(phone: str) -> list[str]:
    """Variantes a usar em TODA operacao de bloqueio, ordenadas (determinismo).

    Aceita phone em qualquer formato (com "+", com JID, com mascara): extrai os
    digitos e, se for BR reconhecivel, devolve as duas formas. Se nao for BR
    (ex.: numero internacional), devolve so a forma em digitos — melhor que
    perder o bloqueio.
    """
    digits = only_digits(phone)
    if not digits:
        return []
    normalized = normalize_br_phone(digits) or digits
    return sorted(phone_variants(normalized) | {digits})
