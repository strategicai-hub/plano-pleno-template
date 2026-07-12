"""
Gera o SYSTEM_PROMPT a partir do template Jinja2 + dados do client.yaml.

O nicho do negócio (academia, escola_cursos, etc.) é lido de
`client.yaml > niche`. Cada nicho tem um prompt próprio em
`app/prompts/{niche}.j2`. Se `niche` não estiver definido, usa
"academia" por padrão (retrocompatibilidade).

`assistant.greeting` é injetado dinamicamente em cada render com base
no horário atual de São Paulo ("bom dia" / "boa tarde" / "boa noite"),
ignorando o valor presente no client.yaml.
"""
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from app.client_data import load_client_data
from app.services import sai_sync

DEFAULT_NICHE = "generico"
_SP_TZ = ZoneInfo("America/Sao_Paulo")


def _compute_time_greeting() -> str:
    hour = datetime.now(_SP_TZ).hour
    if 5 <= hour < 12:
        return "bom dia"
    if 12 <= hour < 18:
        return "boa tarde"
    return "boa noite"


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _compute_closed_days_block(horizon_days: int = 90) -> str:
    """Bloco autoritativo de datas FECHADAS (feriados/recessos do painel SAI).

    Le do snapshot em Redis (sai_sync.load_snapshot_sync) e mantem apenas
    intervalos cujo `endDate` >= hoje, dentro de horizonte de `horizon_days`.
    Sem snapshot/holidays -> string vazia (nao polui o prompt).
    """
    snap = sai_sync.load_snapshot_sync()
    if not snap:
        return ""
    holidays = ((snap.get("assistant") or {}).get("holidays") or [])
    if not holidays:
        return ""
    today = datetime.now(_SP_TZ).date()
    horizon = today + timedelta(days=horizon_days)
    lines: list[str] = []
    for h in holidays:
        start = _parse_iso_date(h.get("startDate") or "")
        end = _parse_iso_date(h.get("endDate") or h.get("startDate") or "")
        if start is None or end is None:
            continue
        if end < today or start > horizon:
            continue
        reason = (h.get("reason") or "").strip()
        if start == end:
            label = start.strftime("%d/%m/%Y")
        else:
            label = f"{start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}"
        lines.append(f"  - {label}" + (f" — {reason}" if reason else ""))
    if not lines:
        return ""
    return (
        "\n\n## DATAS FECHADAS - REGRA ABSOLUTA\n"
        "Nas datas listadas abaixo a unidade **NAO abre** (feriado/recesso "
        "cadastrado no painel). PROIBIDO oferecer ou confirmar agendamento de "
        "aula experimental/avaliacao nessas datas — mesmo que a tabela de "
        "horarios normalmente tenha atividade naquele dia da semana. Se o lead "
        "perguntar se vai abrir, responda que estaremos fechados, cite o motivo "
        "se houver, e ofereca o proximo dia util compativel.\n\n"
        + "\n".join(lines)
        + "\n"
    )


def _format_price_cents(cents) -> str | None:
    """Converte priceCents (int) do snapshot do painel em "R$ 1.234,56".

    None/valor invalido -> None (o template renderiza "consulte" nesse caso).
    """
    if cents is None:
        return None
    try:
        reais = int(cents) / 100
    except (TypeError, ValueError):
        return None
    # f-string sai em formato en-US ("1,234.56"); troca para pt-BR ("1.234,56").
    s = f"{reais:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {s}"


def _normalize_snapshot_product(p: dict) -> dict:
    """Molda um produto do snapshot ({name, priceCents, description}) no formato
    que os templates .j2 ja consomem ({name, price, description})."""
    return {
        "name": (p.get("name") or "").strip(),
        "price": _format_price_cents(p.get("priceCents")),
        "description": (p.get("description") or "").strip(),
    }


def _merge_sai_snapshot(data: dict) -> dict:
    """Funde o snapshot do Painel IA WhatsApp (SAI, via Redis) sobre o client.yaml.

    Em producao o painel e a fonte de verdade:
      - assistant.name          <- displayName cadastrado no painel
      - assistant.business_hours <- horario de funcionamento do painel
      - products                <- catalogo do painel. No nicho corretor de
        imoveis, cada empreendimento ativo entra aqui como um item de produto
        (nome "Empreendimento: X" + ficha rotulada na description).

    client.yaml continua como fallback quando o Redis esta vazio (bot recem-subido
    ou falha de sync). So sobrescreve quando o snapshot traz o dado nao-vazio, para
    nao apagar o que veio do client.yaml.
    """
    snap = sai_sync.load_snapshot_sync()
    if not snap:
        return data
    assistant = dict(data.get("assistant") or {})
    snap_assistant = snap.get("assistant") or {}
    display_name = (snap_assistant.get("displayName") or "").strip()
    if display_name:
        assistant["name"] = display_name
    business_hours = snap_assistant.get("businessHours")
    if business_hours:
        assistant["business_hours"] = business_hours
    data["assistant"] = assistant
    products = snap.get("products")
    if products:
        data["products"] = [_normalize_snapshot_product(p) for p in products if p]
    return data


def _compute_time_context_block() -> str:
    """Bloco autoritativo de data/hora atual em Sao Paulo.

    Injetado no FINAL do prompt (modelos seguem melhor instrucoes no final).
    Inclui hoje + ontem + amanha ja computados para evitar erros de calculo.
    """
    week = [
        "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sábado", "domingo",
    ]
    now = datetime.now(_SP_TZ)
    yesterday = now - timedelta(days=1)
    tomorrow = now + timedelta(days=1)
    return (
        "\n\n---\n\n## DATA E HORA ATUAIS - REGRA ABSOLUTA\n"
        "Estas informações são AUTORITATIVAS. Substituem qualquer suposição sua. "
        "Use-as sempre que for falar de dia, data, hoje, ontem, amanhã, semana ou horário:\n\n"
        f"- AGORA (America/Sao_Paulo): {now.strftime('%d/%m/%Y %H:%M')}\n"
        f"- HOJE é {week[now.weekday()]} ({now.strftime('%d/%m/%Y')}).\n"
        f"- ONTEM foi {week[yesterday.weekday()]} ({yesterday.strftime('%d/%m/%Y')}).\n"
        f"- AMANHÃ será {week[tomorrow.weekday()]} ({tomorrow.strftime('%d/%m/%Y')}).\n\n"
        "PROIBIDO inventar outro dia da semana. Se for mencionar \"amanhã\", "
        f"obrigatoriamente é {week[tomorrow.weekday()]}.\n"
    )


def build_prompt() -> str:
    prompts_dir = Path(__file__).parent / "prompts"
    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        keep_trailing_newline=True,
    )
    data = dict(load_client_data())
    data = _merge_sai_snapshot(data)
    assistant = dict(data.get("assistant") or {})
    assistant["greeting"] = _compute_time_greeting()
    data["assistant"] = assistant

    niche = (data.get("niche") or DEFAULT_NICHE).strip()
    template_file = f"{niche}.j2"
    if not (prompts_dir / template_file).exists():
        # Nicho sem prompt dedicado: NUNCA quebrar o boot. Cai no prompt
        # generico defensivo (funciona com qualquer client.yaml minimo) em vez
        # de levantar excecao ou mascarar para "academia".
        import logging
        logging.getLogger(__name__).warning(
            "Prompt do nicho '%s' nao encontrado; usando '%s.j2' (fallback generico). "
            "Disponiveis: %s",
            niche, DEFAULT_NICHE, [p.stem for p in prompts_dir.glob("*.j2")],
        )
        template_file = f"{DEFAULT_NICHE}.j2"
    template = env.get_template(template_file)
    return (
        template.render(**data)
        + _compute_time_context_block()
        + _compute_closed_days_block()
    )


def get_system_prompt() -> str:
    """Renderiza o prompt sob demanda (greeting reflete o horário atual)."""
    return build_prompt()
