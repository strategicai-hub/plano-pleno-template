"""Wrapper do Gemini usando o SDK `google-genai`.

Decisões importantes:
- Usa `google-genai` (novo SDK oficial). Evitar `google-generativeai` (legado).
- `thinking_budget=0` em todas as chamadas: o gemini-2.5-flash gera tokens
  de raciocinio internos por padrao, cobrados como output. Desligar reduz
  drasticamente o custo em bots conversacionais simples.
- `temperature=0.4` no chat (saidas naturais e pouco aleatorias) e 0.2 em
  transcricao/analise de imagem (tarefas deterministicas).
- `max_output_tokens` limita verbosidade (e custo).
"""
import asyncio
import logging
from typing import Any, Optional

from google import genai
from google.genai import types as gtypes

from app.client_data import load_client_data
from app.config import settings
from app.prompt import get_system_prompt
from app.services.redis_service import get_chat_history, append_chat_history

logger = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


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
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=user_message)]))

    config = gtypes.GenerateContentConfig(
        system_instruction=get_system_prompt(),
        temperature=0.4,
        max_output_tokens=300,
        thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
    )

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=_MODEL,
        contents=contents,
        config=config,
    )

    ai_text = (response.text or "").strip()
    tokens = _usage_tokens(response)

    await append_chat_history(phone, "user", user_message)
    if ai_text:
        await append_chat_history(phone, "model", ai_text)

    return ai_text, tokens


async def transcribe_audio(audio_bytes: bytes) -> str:
    client = _get_client()
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
            thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
        ),
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
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                temperature=0.4,
                max_output_tokens=150,
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
            ),
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
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                temperature=0.6,
                max_output_tokens=200,
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
            ),
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("Erro ao gerar mensagem de reativacao para %s", phone)
        return ""


async def analyze_image(image_bytes: bytes) -> str:
    client = _get_client()
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
            thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
        ),
    )
    return (response.text or "").strip()
