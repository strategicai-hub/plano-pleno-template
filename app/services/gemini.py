"""Wrapper do Gemini usando o SDK `google-genai`.

Decisões importantes:
- Usa `google-genai` (novo SDK oficial). Evitar `google-generativeai` (legado).
- `include_thoughts=False` em todas as chamadas: os modelos Gemini Flash geram
  tokens de raciocinio internos por padrao, cobrados como output. Desligar reduz
  drasticamente o custo em bots conversacionais simples.
- `temperature=0.4` no chat (saidas naturais e pouco aleatorias) e 0.2 em
  transcricao/analise de imagem (tarefas deterministicas).
- `max_output_tokens` limita verbosidade (e custo).
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Optional

from google import genai
from google.genai import types as gtypes

from app.client_data import load_client_data
from app.config import settings
from app.prompt import get_system_prompt
from app.services.redis_service import get_chat_history, append_chat_history
from app.services.sai_metrics import log_message_async

logger = logging.getLogger(__name__)

_MODEL = "gemini-3.1-flash-lite"
_client: Optional[genai.Client] = None


_SP_TZ_TC = ZoneInfo("America/Sao_Paulo")
_WEEK_TC = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]


def _temporal_prefix() -> str:
    """Bloco de contexto temporal injetado na user_message a cada turno.

    O system_instruction também recebe a data, mas o modelo às vezes ignora —
    repetir no próprio turno do usuário força a leitura imediata.
    """
    now = datetime.now(_SP_TZ_TC)
    tomorrow = now + timedelta(days=1)
    return (
        f"[CONTEXTO DO SISTEMA — não responda sobre isto, apenas use como referência: "
        f"agora são {now.strftime('%H:%M')} de {_WEEK_TC[now.weekday()]}, {now.strftime('%d/%m/%Y')}. "
        f"Amanhã é {_WEEK_TC[tomorrow.weekday()]}, {tomorrow.strftime('%d/%m/%Y')}. "
        f"REGRA DO NOME: NÃO comece sua resposta com o nome da pessoa e NÃO repita o nome dela. "
        f"Usar o nome em toda mensagem soa robotizado. O nome só pode aparecer DUAS vezes na conversa "
        f"inteira: uma ao recebê-lo (\"Prazer, {{nome}}\") e uma na confirmação do fechamento/agendamento. "
        f"Em TODAS as outras mensagens, não cite o nome — siga o tom do prompt do nicho.]\n\n"
    )



def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


# `_THINKING_OFF` é obrigatório em qualquer chamada com `max_output_tokens` curto
# (followups, summary, transcrição, análise de imagem). Sem `thinking_budget=0`,
# os tokens de pensamento invisíveis consomem o orçamento ANTES da saída visível
# e a resposta sai truncada (ex.: "Oi Gustavo, percebi seu").
# `_THINKING_DYNAMIC` deixa o modelo pensar livremente — só usar no chat principal,
# que não tem teto de output.
_THINKING_OFF = gtypes.ThinkingConfig(thinking_budget=0, include_thoughts=False)
_THINKING_DYNAMIC = gtypes.ThinkingConfig(include_thoughts=False)


def _history_to_contents(history: list[dict]) -> list[gtypes.Content]:
    contents: list[gtypes.Content] = []
    for h in history:
        role = h.get("role")
        text = (h.get("parts") or [{}])[0].get("text", "")
        if not text:
            continue
        contents.append(gtypes.Content(role=role, parts=[gtypes.Part.from_text(text=text)]))
    return contents


def _usage_tokens(response: Any) -> tuple[int, int, int]:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return (0, 0, 0)
    inp = getattr(meta, "prompt_token_count", 0) or 0
    out = getattr(meta, "candidates_token_count", 0) or 0
    total = getattr(meta, "total_token_count", 0) or (inp + out)
    return (inp, out, total)


async def chat(phone: str, user_message: str, lead_name: str = "") -> tuple[str, tuple[int, int, int]]:
    client = _get_client()
    history = await get_chat_history(phone)
    contents = _history_to_contents(history)
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=_temporal_prefix() + user_message)]))

    config = gtypes.GenerateContentConfig(
        system_instruction=get_system_prompt(),
        temperature=0.4,
        thinking_config=_THINKING_DYNAMIC,
    )

    t0 = time.monotonic()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=_MODEL,
        contents=contents,
        config=config,
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    ai_text = (response.text or "").strip()
    tokens = _usage_tokens(response)
    log_message_async(
        lead_phone=phone,
        direction="INBOUND",
        kind="CHAT",
        model=_MODEL,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        latency_ms=latency_ms,
    )

    await append_chat_history(phone, "user", user_message)
    if ai_text:
        await append_chat_history(phone, "model", ai_text)

    return ai_text, tokens


async def transcribe_audio(audio_bytes: bytes, phone: str = "") -> str:
    client = _get_client()
    t0 = time.monotonic()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=_MODEL,
        contents=[
            gtypes.Content(
                role="user",
                parts=[
                    gtypes.Part.from_text(
                        text="Transcreva essa gravacao de audio fielmente. Retorne APENAS o texto transcrito, sem comentarios."
                    ),
                    gtypes.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
                ],
            )
        ],
        config=gtypes.GenerateContentConfig(
            temperature=0.2,
            thinking_config=_THINKING_OFF,
        ),
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    inp, out, _ = _usage_tokens(response)
    log_message_async(
        lead_phone=phone,
        direction="INBOUND",
        kind="TRANSCRIPTION",
        model=_MODEL,
        input_tokens=inp,
        output_tokens=out,
        latency_ms=latency_ms,
    )
    return (response.text or "").strip()


async def generate_summary(phone: str) -> str:
    """Gera um resumo curto da conversa com base no historico recente."""
    history = await get_chat_history(phone)
    if not history:
        return ""

    lines = []
    for entry in history[-10:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = entry.get("parts", [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:200]}")
    if not lines:
        return ""

    client_data = load_client_data()
    business_type = (client_data.get("business", {}) or {}).get("type", "negocio")
    prompt = (
        f"Com base nesse trecho de conversa de {business_type}, "
        "escreva um resumo de 1 a 2 frases em portugues sobre quem e esse lead "
        "e qual o interesse dele. Seja objetivo.\n\n"
        + "\n".join(lines)
    )

    client = _get_client()
    try:
        t0 = time.monotonic()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                temperature=0.4,
                max_output_tokens=150,
                thinking_config=_THINKING_OFF,
            ),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        inp, out, _ = _usage_tokens(response)
        log_message_async(
            lead_phone=phone,
            direction="INBOUND",
            kind="SUMMARY",
            model=_MODEL,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency_ms,
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("Erro ao gerar resumo para %s", phone)
        return ""


async def generate_reactivation_message(
    phone: str,
    nome: str,
    stage: int,
    now_str: str = "",
) -> str:
    """PLENO: gera mensagem personalizada de reativacao a partir do historico do
    lead. `stage` 1..N controla o tom (primeiro contato x ultima chance).
    """
    history = await get_chat_history(phone)

    lines = []
    for entry in history[-12:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = entry.get("parts", [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:240]}")

    client_data = load_client_data()
    business_name = (client_data.get("business") or {}).get("name") or ""
    assistant_name = (client_data.get("assistant") or {}).get("name") or ""
    templates = ((client_data.get("followups") or {}).get("templates") or {})
    hint = templates.get(f"reactivation_stage_{stage}", "")

    tone = {
        1: "empatico, curto, lembrando que a gente ficou no aguardo",
        2: "encorajador, destacando algo especifico que o lead demonstrou interesse",
        3: "ultima chamada, respeitoso, sem pressao",
    }.get(stage, "educado e direto")

    prompt = (
        f"Voce e {assistant_name or 'a assistente'} da {business_name or 'empresa'}.\n"
        f"Data/hora atual: {now_str or '-'}\n"
        f"Nome do lead: {nome or '(desconhecido)'}\n"
        f"Tom desta mensagem: {tone}\n"
        + (f"Referencia (nao copiar literalmente): {hint}\n" if hint else "")
        + "Regras: 1 paragrafo, no maximo 2 frases, SEM asteriscos/markdown, "
          "uma unica pergunta aberta no final convidando o lead a retomar a conversa. "
          "Nao se apresente (ele ja te conhece).\n\n"
        "Trecho da conversa anterior:\n"
        + "\n".join(lines or ["(sem historico)"])
    )

    client = _get_client()
    try:
        t0 = time.monotonic()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                temperature=0.6,
                max_output_tokens=200,
                thinking_config=_THINKING_OFF,
            ),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        inp, out, _ = _usage_tokens(response)
        log_message_async(
            lead_phone=phone,
            direction="OUTBOUND",
            kind="REACTIVATION",
            model=_MODEL,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency_ms,
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("Erro ao gerar mensagem de reativacao para %s", phone)
        return ""


async def generate_first_contact_message(
    phone: str,
    nome: str,
    *,
    observacao: str = "",
) -> str:
    """PLENO: gera o 1o contato para um lead recebido de origem externa (disparo
    ativo, generico — serve qualquer nicho).

    Temperatura alta de proposito: cada mensagem precisa sair com estrutura e
    vocabulario diferentes (anti-ban Meta — texto identico em massa e o
    principal gatilho de bloqueio). Retorna "" em falha; o caller
    (followups/lead_dispatch.py) usa um template estatico de fallback.
    """
    client_data = load_client_data()
    business_name = (client_data.get("business") or {}).get("name") or ""
    assistant_name = ((client_data.get("assistant") or {}).get("name") or "").strip()

    now = datetime.now(_SP_TZ_TC)
    saudacao = "bom dia" if now.hour < 12 else ("boa tarde" if now.hour < 18 else "boa noite")
    primeiro_nome = (nome or "").strip().split(" ")[0].title() if (nome or "").strip() else ""

    prompt = (
        f"Voce e {assistant_name or 'a assistente'}"
        + (f" da {business_name}" if business_name else "")
        + ".\n"
        f"Um lead chamado {nome or '(sem nome)'} demonstrou interesse e deixou o contato "
        "em um canal parceiro, e voce vai iniciar a conversa por WhatsApp.\n"
        f"Horario atual: {now.strftime('%H:%M')} (saudacao adequada: {saudacao}).\n"
        + (f"Observacao do cadastro: {observacao}\n" if observacao else "")
        + "Escreva a PRIMEIRA mensagem iniciando essa conversa.\n"
        "Regras obrigatorias:\n"
        + (f"- Cumprimente pelo primeiro nome ({primeiro_nome}) com a saudacao do horario.\n"
           if primeiro_nome else "- Cumprimente com a saudacao do horario.\n")
        + "- Apresente-se brevemente pelo seu nome"
        + (f" (e diga que fala em nome da {business_name})" if business_name else "")
        + ".\n"
        "- Diga que recebeu o contato/interesse dele.\n"
        "- Termine com UMA pergunta de abertura para comecar a qualificacao.\n"
        "- 2 a 4 frases, tom humano e proximo, SEM markdown, SEM asteriscos, no maximo 1 emoji.\n"
        "- IMPORTANTE (anti-spam): varie a estrutura, a ordem das informacoes e o vocabulario — nunca repita um texto padrao.\n"
        "Responda APENAS com o texto da mensagem."
    )

    # _get_client() dentro do try: se a key estiver ausente/invalida, o
    # construtor levanta — e o caller precisa cair no template de fallback,
    # nao em retry/failed do dispatch.
    try:
        client = _get_client()
        t0 = time.monotonic()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                temperature=0.9,
                max_output_tokens=200,
                thinking_config=_THINKING_OFF,
            ),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        inp, out, _ = _usage_tokens(response)
        log_message_async(
            lead_phone=phone,
            direction="OUTBOUND",
            kind="FIRST_CONTACT",
            model=_MODEL,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency_ms,
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("Erro ao gerar 1o contato para %s", phone)
        return ""


async def analyze_image(image_bytes: bytes, phone: str = "") -> str:
    client = _get_client()
    t0 = time.monotonic()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=_MODEL,
        contents=[
            gtypes.Content(
                role="user",
                parts=[
                    gtypes.Part.from_text(text="Descreva esta imagem em ate 50 palavras, em portugues."),
                    gtypes.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                ],
            )
        ],
        config=gtypes.GenerateContentConfig(
            temperature=0.2,
            thinking_config=_THINKING_OFF,
        ),
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    inp, out, _ = _usage_tokens(response)
    log_message_async(
        lead_phone=phone,
        direction="INBOUND",
        kind="IMAGE_ANALYSIS",
        model=_MODEL,
        input_tokens=inp,
        output_tokens=out,
        latency_ms=latency_ms,
    )
    return (response.text or "").strip()
