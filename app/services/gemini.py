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
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Optional

from google import genai
from google.genai import types as gtypes

from app.client_data import load_client_data
from app.config import settings
from app.prompt import get_system_prompt
from app.services import imoveis
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


def _temporal_prefix(lead_name: str = "") -> str:
    """Bloco de contexto temporal injetado na user_message a cada turno.

    O system_instruction também recebe a data, mas o modelo às vezes ignora —
    repetir no próprio turno do usuário força a leitura imediata.

    `lead_name` já vem resolvido por `nomes.nome_do_lead()`: é o nome que o
    próprio contato forneceu (na conversa ou no cadastro que ele preencheu),
    nunca o nome do perfil do WhatsApp. Vazio = não sabemos o nome dele.
    """
    now = datetime.now(_SP_TZ_TC)
    tomorrow = now + timedelta(days=1)
    nome_confirmado = (lead_name or "").strip()
    return (
        f"[CONTEXTO DO SISTEMA — não responda sobre isto, apenas use como referência: "
        f"agora são {now.strftime('%H:%M')} de {_WEEK_TC[now.weekday()]}, {now.strftime('%d/%m/%Y')}. "
        f"Amanhã é {_WEEK_TC[tomorrow.weekday()]}, {tomorrow.strftime('%d/%m/%Y')}. "
        f"NOME CONFIRMADO DO CONTATO: {nome_confirmado if nome_confirmado else '(vazio — ainda não informado)'}. "
        f"Esse campo acima é a ÚNICA fonte de nome que você pode usar. "
        f"PROIBIDO chamar o contato por qualquer nome que não esteja nesse campo — em especial o nome "
        f"do perfil/agenda do WhatsApp, e qualquer nome que apareça em assinatura, encaminhamento ou "
        f"anexo. Esses nomes costumam ser de outra pessoa e chamar o contato assim é erro grave. "
        f"Se o contato disser nesta conversa que se chama outra coisa, o nome novo substitui o antigo "
        f"imediatamente e o antigo nunca mais é usado. "
        f"Quando o contato informar o nome, emita [NOME=PrimeiroNome] no fim da resposta. "
        f"Se o campo acima estiver vazio, você NÃO sabe o nome dele — não invente, não chute e não use "
        f"vocativo nenhum; se fizer sentido, pergunte uma única vez como pode chamá-lo. "
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

    # Link de anuncio na mensagem -> le a ficha do imovel e injeta como
    # contexto. Fica AQUI (e nao no consumer) porque `chat` e o funil unico de
    # todas as entradas — WhatsApp, simulador e follow-ups. Falha nunca pode
    # derrubar o atendimento: segue sem a ficha.
    try:
        bloco_imovel = await imoveis.build_context_block(user_message)
    except Exception:
        logger.exception("Erro ao montar contexto de imovel para %s", phone)
        bloco_imovel = ""
    if bloco_imovel:
        # Vai para o historico junto com a mensagem: nos turnos seguintes o
        # lead pergunta "e o condominio?" e a ficha ainda esta no contexto.
        user_message = bloco_imovel + user_message

    history = await get_chat_history(phone)
    contents = _history_to_contents(history)
    contents.append(
        gtypes.Content(
            role="user",
            parts=[gtypes.Part.from_text(text=_temporal_prefix(lead_name) + user_message)],
        )
    )

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

    if ai_text:
        # Grava a fala do usuario so quando ha resposta: evita duplicar no historico
        # durante o loop de retry por resposta vazia. Happy-path fica identico.
        await append_chat_history(phone, "user", user_message)
        await append_chat_history(phone, "model", ai_text)

    return ai_text, tokens


async def transcribe_audio(audio_bytes: bytes, phone: str = "", mime_type: str = "audio/ogg") -> str:
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
                    gtypes.Part.from_bytes(data=audio_bytes, mime_type=mime_type or "audio/ogg"),
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


async def generate_handoff_summary(phone: str) -> str:
    """Briefing para a equipe humana no handoff [TRANSFERIR=1]: resumo da
    qualificacao do lead + motivo provavel do atendimento humano, a partir do
    historico recente. Retorna "" em falha (o caller usa um fallback)."""
    history = await get_chat_history(phone)
    if not history:
        return ""

    lines = []
    for entry in history[-12:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = entry.get("parts", [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:240]}")
    if not lines:
        return ""

    prompt = (
        "Voce faz o briefing para a equipe humana que vai assumir este "
        "atendimento de WhatsApp. Com base na conversa abaixo, escreva EXATAMENTE "
        "neste formato (duas linhas):\n"
        "Resumo da qualificacao: <1 a 2 frases sobre quem e o lead, o que ele "
        "quer e os dados ja coletados>\n"
        "Motivo do atendimento humano: <1 frase com o porque de o atendimento "
        "humano ter sido acionado agora>\n\n"
        "Seja objetivo, em portugues, sem markdown e sem asteriscos.\n\n"
        "Conversa:\n" + "\n".join(lines)
    )

    client = _get_client()
    try:
        t0 = time.monotonic()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=200,
                thinking_config=_THINKING_OFF,
            ),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        inp, out, _ = _usage_tokens(response)
        log_message_async(
            lead_phone=phone,
            direction="INBOUND",
            kind="HANDOFF_SUMMARY",
            model=_MODEL,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency_ms,
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("Erro ao gerar briefing de handoff para %s", phone)
        return ""


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


# Numerais por extenso que denotam quantidade. "um"/"uma" ficam de fora de
# proposito: sao artigos indefinidos comuns ("um dos nossos imoveis") e barra-los
# reprovaria toda variacao.
_NUMERAIS_EXTENSO = {
    "dois", "duas", "tres", "três", "quatro", "cinco", "seis", "sete", "oito",
    "nove", "dez", "onze", "doze", "ambas", "ambos",
}


def _quantidades(texto: str) -> set[str]:
    """Numeros (algarismo ou extenso) presentes no texto, normalizados."""
    low = (texto or "").lower()
    numeros = set(re.findall(r"\d+", low))
    palavras = set(re.findall(r"[a-zà-ÿ]+", low))
    return numeros | (palavras & _NUMERAIS_EXTENSO)


async def vary_message(
    phone: str,
    base_text: str,
    *,
    nome: str = "",
    kind: str = "VARIATION",
) -> str:
    """Reescreve com outras palavras um texto escrito pelo cliente, preservando
    intencao, fatos, estrutura de baloes e tamanho.

    Por que existe: mensagens proativas (abertura do disparo, follow-ups) sao o
    MESMO texto para todo lead — e texto identico enviado em massa e o principal
    gatilho de bloqueio do WhatsApp. Variar as palavras mantendo o conteudo
    protege o numero sem tirar o roteiro das maos do cliente.

    Retorna "" quando falha ou quando a saida nao respeita a estrutura do texto
    base — nesses casos o caller envia o literal, que sempre e melhor do que uma
    reescrita que perdeu um paragrafo ou inventou informacao.
    """
    base = (base_text or "").strip()
    if not base:
        return ""

    base_paras = _paragraphs(base)
    client_data = load_client_data()
    business_name = (client_data.get("business") or {}).get("name") or ""
    assistant_name = ((client_data.get("assistant") or {}).get("name") or "").strip()

    if nome:
        regra_nome = (
            f"O contato se chama {nome} — este e o UNICO nome permitido no vocativo. "
            "Mantenha o vocativo onde ele ja esta no texto base."
        )
    else:
        regra_nome = (
            "Voce NAO sabe o nome deste contato. Se o texto base tiver um vocativo vazio "
            "ou uma virgula orfa, ajuste a frase para fluir sem nome. PROIBIDO inventar nome."
        )

    prompt = (
        f"Voce e {assistant_name or 'a assistente'}"
        + (f" da {business_name}" if business_name else "")
        + ".\n"
        "Reescreva a MENSAGEM BASE abaixo com outras palavras. Nao e uma mensagem nova: "
        "e a MESMA mensagem, dita de um jeito um pouco diferente.\n\n"
        f"PODE variar: saudacao e abertura; sinonimos e conectivos; ordem das frases dentro "
        f"de um paragrafo; a forma da pergunta final; contracoes (para/pra); presenca de no "
        f"maximo 1 emoji.\n"
        "NAO PODE mudar: a intencao e o pedido final; nenhum fato, nome proprio, numero, valor "
        "ou oferta citada; a quantidade de paragrafos; o tamanho aproximado de cada paragrafo; "
        "a ordem dos assuntos. PROIBIDO acrescentar informacao, promessa ou oferta que nao "
        "esteja na mensagem base. PROIBIDO usar asteriscos, markdown ou qualquer formatacao.\n"
        "PROIBIDO INVENTAR QUANTIDADE (trava absoluta): se a mensagem base diz 'algumas "
        "perguntas', 'algumas opcoes' ou qualquer quantidade vaga, a reescrita TEM que "
        "continuar vaga. Nunca troque uma quantidade vaga por um numero ('algumas' -> '3'), "
        "nem invente numero, prazo, valor ou contagem que nao esteja escrito na mensagem base. "
        "Numero errado no texto vira promessa quebrada com o cliente.\n"
        f"REGRA DO NOME (prioritaria): {regra_nome}\n"
        f"ESTRUTURA OBRIGATORIA: a resposta tem que ter exatamente {len(base_paras)} paragrafo(s), "
        "separados por UMA linha em branco (cada paragrafo vira um balao no WhatsApp).\n"
        "Responda APENAS com o texto reescrito, sem comentario nenhum.\n\n"
        "MENSAGEM BASE:\n"
        f"{base}"
    )

    try:
        client = _get_client()
        t0 = time.monotonic()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                # Alta de proposito: o objetivo da chamada e justamente que duas
                # execucoes do mesmo texto base saiam diferentes.
                temperature=0.9,
                # Teto folgado sobre o base: sem isso a reescrita de um texto de
                # 5 paragrafos sai cortada no meio.
                max_output_tokens=max(300, len(base) // 2),
                thinking_config=_THINKING_OFF,
            ),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        inp, out, _ = _usage_tokens(response)
        log_message_async(
            lead_phone=phone,
            direction="OUTBOUND",
            kind=kind,
            model=_MODEL,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency_ms,
        )
        variacao = (response.text or "").strip()
    except Exception:
        logger.exception("Erro ao variar mensagem para %s (kind=%s)", phone, kind)
        return ""

    if not variacao:
        return ""
    # Guarda-costas da estrutura: o modelo as vezes junta tudo num paragrafo so
    # ou inventa um "P.S.". Perder um balao ou ganhar um estraga a mensagem que
    # o cliente escreveu — nesses casos o literal e a escolha certa.
    novos = _paragraphs(variacao)
    if len(novos) != len(base_paras):
        logger.warning(
            "vary_message: variacao descartada para %s (%d paragrafos, esperado %d)",
            phone, len(novos), len(base_paras),
        )
        return ""
    if "*" in variacao or "#" in variacao:
        logger.warning("vary_message: variacao descartada para %s (markdown na saida)", phone)
        return ""
    # Quantidade inventada: "algumas perguntinhas" virou "3 perguntinhas" numa
    # abertura cujo roteiro tem 4 perguntas. O modelo concretiza quantidade vaga
    # se deixarem, e um numero errado no 1o contato e promessa quebrada — a
    # instrucao no prompt sozinha nao basta.
    extras = _quantidades(variacao) - _quantidades(base)
    if extras:
        logger.warning(
            "vary_message: variacao descartada para %s (quantidade inventada: %s)",
            phone, sorted(extras),
        )
        return ""
    if not (0.6 <= len(variacao) / max(len(base), 1) <= 1.6):
        logger.warning(
            "vary_message: variacao descartada para %s (tamanho %d vs base %d)",
            phone, len(variacao), len(base),
        )
        return ""
    return variacao


async def generate_reactivation_message(
    phone: str,
    nome: str,
    stage: int,
    now_str: str = "",
) -> str:
    """PLENO: gera mensagem personalizada de reativacao a partir do historico do
    lead. `stage` 1..N controla o tom (primeiro contato x ultima chance).

    `nome` DEVE vir de `nomes.nome_do_lead()` — nunca do campo cru do banco.
    Este canal ja disparou "Oi Tutu" (nome do perfil do WhatsApp) porque lia o
    nome direto do SQLite, sem a trava que o chat reativo tinha.
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

    # Trava de nome: o unico vocativo permitido e o `nome` recebido aqui. Sem
    # isso o modelo pesca qualquer nome que apareca no trecho da conversa
    # (assinatura, nome de terceiro citado, nome antigo ja corrigido).
    if nome:
        regra_nome = (
            f"Chame o lead de {nome} — este e o unico nome permitido. "
            "PROIBIDO usar qualquer outro nome que apareca no trecho da conversa abaixo."
        )
    else:
        regra_nome = (
            "Voce NAO sabe o nome deste lead. Escreva a mensagem SEM vocativo e SEM nome "
            "(ex.: comece com 'Oi, tudo bem?'). PROIBIDO inventar um nome ou pescar um nome "
            "do trecho da conversa abaixo."
        )

    prompt = (
        f"Voce e {assistant_name or 'a assistente'} da {business_name or 'empresa'}.\n"
        f"Data/hora atual: {now_str or '-'}\n"
        f"Nome do lead: {nome or '(desconhecido — nao use nome)'}\n"
        f"Tom desta mensagem: {tone}\n"
        + (f"Referencia (nao copiar literalmente): {hint}\n" if hint else "")
        + f"REGRA DO NOME (prioritaria): {regra_nome}\n"
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


async def analyze_image(image_bytes: bytes, phone: str = "", mime_type: str = "image/jpeg") -> str:
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
                    gtypes.Part.from_bytes(data=image_bytes, mime_type=mime_type or "image/jpeg"),
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
