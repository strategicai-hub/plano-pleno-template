import logging

import google.generativeai as genai

from app.client_data import load_client_data
from app.config import settings
from app.prompt import SYSTEM_PROMPT
from app.services.redis_service import get_chat_history, append_chat_history

logger = logging.getLogger(__name__)

_configured = False


def _ensure_configured() -> None:
    global _configured
    if not _configured:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _configured = True


async def chat(phone: str, user_message: str, lead_name: str = "") -> tuple[str, tuple[int, int, int]]:
    _ensure_configured()

    history = await get_chat_history(phone)

    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=SYSTEM_PROMPT,
    )

    chat_session = model.start_chat(history=history)
    response = chat_session.send_message(user_message)
    ai_text = response.text.strip() if response.text else ""

    tokens = (0, 0, 0)
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        meta = response.usage_metadata
        inp = meta.prompt_token_count or 0
        out = meta.candidates_token_count or 0
        tokens = (inp, out, meta.total_token_count or inp + out)

    await append_chat_history(phone, "user", user_message)
    if ai_text:
        await append_chat_history(phone, "model", ai_text)

    return ai_text, tokens


async def transcribe_audio(audio_bytes: bytes) -> str:
    _ensure_configured()
    model = genai.GenerativeModel("gemini-2.5-flash")

    audio_part = {
        "mime_type": "audio/ogg",
        "data": audio_bytes,
    }
    response = model.generate_content(
        ["Transcreva essa gravacao de audio fielmente. Retorne APENAS o texto transcrito, sem comentarios.", audio_part]
    )
    return response.text.strip() if response.text else ""


async def generate_summary(phone: str) -> str:
    """Gera um resumo curto da conversa com base no historico recente."""
    _ensure_configured()
    history = await get_chat_history(phone)
    if not history:
        return ""

    # Monta texto das ultimas 10 mensagens para resumir
    lines = []
    for entry in history[-10:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = entry.get("parts", [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:200]}")

    if not lines:
        return ""

    model = genai.GenerativeModel("gemini-2.5-flash")
    client = load_client_data()
    business_type = (client.get("business", {}) or {}).get("type", "negocio")
    prompt = (
        f"Com base nesse trecho de conversa de {business_type}, "
        "escreva um resumo de 1 a 2 frases em portugues sobre quem e esse lead "
        "e qual o interesse dele. Seja objetivo.\n\n"
        + "\n".join(lines)
    )
    try:
        response = model.generate_content(prompt)
        return response.text.strip() if response.text else ""
    except Exception:
        return ""


async def generate_reactivation_message(
    phone: str,
    nome: str,
    stage: int,
    now_str: str = "",
) -> str:
    """
    PLENO: gera mensagem personalizada de reativacao a partir do historico do
    lead. `stage` 1..N controla o tom (primeiro contato x ultima chance).
    Se o cliente preencheu `followups.templates.reactivation_stage_<N>` no
    client.yaml, o template pode servir de base — aqui deixamos a geracao
    livre (Gemini redige a partir do contexto da conversa).
    """
    _ensure_configured()
    history = await get_chat_history(phone)

    lines = []
    for entry in history[-12:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = entry.get("parts", [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:240]}")

    client = load_client_data()
    business_name = (client.get("business") or {}).get("name") or ""
    assistant_name = (client.get("assistant") or {}).get("name") or ""
    templates = ((client.get("followups") or {}).get("templates") or {})
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

    model = genai.GenerativeModel("gemini-2.5-flash")
    try:
        response = model.generate_content(prompt)
        return response.text.strip() if response.text else ""
    except Exception:
        logger.exception("Erro ao gerar mensagem de reativacao para %s", phone)
        return ""


async def analyze_image(image_bytes: bytes) -> str:
    _ensure_configured()
    model = genai.GenerativeModel("gemini-2.5-flash")

    image_part = {
        "mime_type": "image/jpeg",
        "data": image_bytes,
    }
    response = model.generate_content(
        ["Descreva esta imagem em ate 50 palavras, em portugues.", image_part]
    )
    return response.text.strip() if response.text else ""
