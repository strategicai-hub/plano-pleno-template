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

from dateutil.easter import easter
from jinja2 import Environment, FileSystemLoader

from app.client_data import load_client_data
from app.services import sai_sync

DEFAULT_NICHE = "generico"
_SP_TZ = ZoneInfo("America/Sao_Paulo")

_WEEKDAY_NAMES = (
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
)

# Feriados nacionais de data fixa (Lei 662/1949, 6.802/1980, 10.607/2002 e
# 14.759/2024, que tornou a Consciencia Negra feriado nacional).
_FIXED_HOLIDAYS: tuple[tuple[tuple[int, int], str], ...] = (
    ((1, 1), "Confraternização Universal (Ano Novo)"),
    ((4, 21), "Tiradentes"),
    ((5, 1), "Dia do Trabalho"),
    ((9, 7), "Independência do Brasil"),
    ((10, 12), "Nossa Senhora Aparecida"),
    ((11, 2), "Finados"),
    ((11, 15), "Proclamação da República"),
    ((11, 20), "Dia Nacional de Zumbi e da Consciência Negra"),
    ((12, 25), "Natal"),
)

# Feriados/pontos facultativos moveis, contados a partir do domingo de Pascoa.
_EASTER_OFFSETS: tuple[tuple[int, str], ...] = (
    (-48, "Carnaval (segunda-feira)"),
    (-47, "Carnaval (terça-feira)"),
    (-46, "Quarta-feira de Cinzas"),
    (-2, "Sexta-feira Santa (Paixão de Cristo)"),
    (0, "Domingo de Páscoa"),
    (60, "Corpus Christi"),
)


def _brazil_holidays(year: int) -> dict[date, str]:
    """Feriados nacionais do Brasil no ano (fixos + moveis pela Pascoa)."""
    holidays = {date(year, m, d): name for (m, d), name in _FIXED_HOLIDAYS}
    easter_sunday = easter(year)
    for offset, name in _EASTER_OFFSETS:
        holidays[easter_sunday + timedelta(days=offset)] = name
    return holidays


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


def _compute_no_invention_block() -> str:
    """Trava anti-invencao, valida para TODO nicho.

    Motivada por incidente real (Portal Fitbox, 07/09/2026): em feriado, o bot
    afirmou a um contato perdido na rua que um colaborador estava a caminho da
    entrada para recebe-lo. Nada disso existia - a pessoa ficou esperando.
    """
    return (
        "\n\n---\n\n## NUNCA PROMETA AÇÃO DE PESSOAS - TRAVA ABSOLUTA\n"
        "Você é um sistema de mensagens. Você NÃO enxerga o local, NÃO sabe quem "
        "está lá, NÃO consegue fazer ninguém sair do lugar e NÃO sabe quanto tempo "
        "alguém demora. PROIBIDO afirmar o que uma pessoa está fazendo ou vai "
        "fazer no mundo físico.\n\n"
        "- PROIBIDO dizer que alguém (colaborador, atendente, recepção, corretor, "
        "professor, responsável) \"está a caminho\", \"já está indo\", \"vai te "
        "receber na entrada\", \"está te esperando\", \"saiu para te buscar\" ou "
        "\"vai até aí\".\n"
        "- PROIBIDO prometer que alguém vai até a rua, portão, portaria, "
        "estacionamento, recepção ou qualquer ponto de encontro.\n"
        "- PROIBIDO dizer \"acionei a equipe agora\", \"mandei alguém\", \"já pedi "
        "para alguém ir\" como se fosse uma ação física executada por você.\n"
        "- PROIBIDO afirmar que tem alguém disponível neste instante, que estão "
        "atendendo agora ou que o retorno será imediato.\n"
        "- VOCABULÁRIO DO ENCAMINHAMENTO: a equipe responde POR MENSAGEM, não vai "
        "ao encontro de ninguém. Termine em \"te orientar por aqui\", \"te responder "
        "por aqui\" ou equivalente. PROIBIDO emendar verbo de deslocamento ou "
        "encontro presencial:\n"
        "    ERRADO: \"a equipe te orienta e te encontra\" / \"vão te achar aí\" / "
        "\"alguém vai até você\"\n"
        "    CERTO: \"a equipe te orienta por aqui\" / \"a equipe te responde por aqui\"\n"
        "- PROIBIDO descrever a posição do local em relação a onde o contato diz "
        "estar (\"um pouco mais adiante\", \"logo depois\", \"do outro lado da rua\", "
        "\"a duas quadras\", \"é só seguir reto\"). Você não sabe onde ele está nem "
        "o trajeto — isso é invenção. Repita o endereço EXATO da base e pare aí.\n"
        "- FORA DO HORÁRIO DE FUNCIONAMENTO: PROIBIDO dizer que a equipe responde "
        "\"agora\", \"em instantes\" ou \"já já\". Informe que no momento não há "
        "expediente e que a equipe retorna no próximo horário.\n"
        "- O que você PODE fazer: passar o que está escrito na base (endereço, "
        "horário, referência) e registrar o contato para a equipe humana, deixando "
        "claro que quem responde é a equipe.\n"
    )


def _compute_holidays_block(holiday_hours: str, horizon_days: int = 365) -> str:
    """Bloco autoritativo de feriados nacionais + horario especial de feriado.

    Sem `schedule.holiday_hours` no client.yaml -> string vazia (nao polui o
    prompt e nao inventa horario para cliente que nao configurou a regra).
    """
    holiday_hours = (holiday_hours or "").strip()
    if not holiday_hours:
        return ""

    today = datetime.now(_SP_TZ).date()
    horizon = today + timedelta(days=horizon_days)
    calendar: dict[date, str] = {}
    for year in range(today.year, horizon.year + 1):
        calendar.update(_brazil_holidays(year))

    today_name = calendar.get(today)
    if today_name:
        today_line = (
            f"- ATENÇÃO: HOJE ({today.strftime('%d/%m/%Y')}) É FERIADO — "
            f"{today_name}. Hoje o atendimento é SOMENTE das {holiday_hours}."
        )
    else:
        today_line = (
            f"- HOJE ({today.strftime('%d/%m/%Y')}) NÃO é feriado nacional. "
            "Vale o horário normal informado na base."
        )

    upcoming = [
        f"  - {d.strftime('%d/%m/%Y')} ({_WEEKDAY_NAMES[d.weekday()]}) — {name}"
        for d, name in sorted(calendar.items())
        if today <= d <= horizon
    ]

    return (
        "\n\n---\n\n## FERIADOS - REGRA ABSOLUTA\n"
        f"Em feriado o atendimento é em HORÁRIO ESPECIAL: {holiday_hours}. "
        "Não é o horário normal de dia útil, e também NÃO é dia fechado.\n\n"
        f"{today_line}\n"
        "- Fora dessa janela, no feriado, NÃO há expediente. PROIBIDO dizer que "
        "a equipe vai atender \"agora\", que tem alguém no local ou que alguém vai "
        "receber o contato. Informe que no momento não há expediente e que a "
        "equipe responde no próximo horário de funcionamento.\n"
        "- A agenda/grade normal NÃO vale em feriado. Se perguntarem se algo "
        "específico acontece no feriado, você NÃO sabe: encaminhe para a equipe "
        "com [TRANSFERIR=1]. PROIBIDO confirmar ou negar por conta própria.\n"
        "- Se a data também aparecer na seção ## DATAS FECHADAS, aquela seção tem "
        "PRECEDÊNCIA: naquele dia não há funcionamento nem em horário especial.\n"
        "- PROIBIDO inventar feriado que não esteja na lista abaixo e PROIBIDO "
        "dizer que uma data é feriado sem conferir aqui.\n\n"
        "Feriados nacionais nos próximos 12 meses:\n"
        + "\n".join(upcoming)
        + "\n"
    )


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


def _as_list(value) -> list[str]:
    """Normaliza um campo do client.yaml que pode vir como lista ou string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.splitlines() if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _compute_mission_block(data: dict) -> str:
    """Bloco da MISSAO da assistente (client.yaml > mission), valido para TODO nicho.

    Vem das 3 ultimas perguntas do onboarding no SAI Comercial:
      - mission.questions -> perguntas que a assistente deve fazer
      - mission.documents -> documentos que ela deve pedir (opcional)
      - mission.goal      -> criterio de conclusao do atendimento

    Injetado no FINAL do prompt (modelos seguem melhor instrucoes no final).
    Sem bloco `mission` no client.yaml -> string vazia (nao polui o prompt).
    """
    mission = data.get("mission") or {}
    if not isinstance(mission, dict):
        return ""
    questions = _as_list(mission.get("questions"))
    documents = _as_list(mission.get("documents"))
    goal = (mission.get("goal") or "").strip() if isinstance(mission.get("goal"), str) else ""
    if not (questions or documents or goal):
        return ""

    out = [
        "\n\n---\n\n## MISSAO DA ASSISTENTE - REGRA ABSOLUTA",
        "Este bloco foi definido pelo dono do negocio e tem prioridade sobre o "
        "roteiro generico das fases acima. Conduza a conversa ate cumpri-lo.",
    ]
    if questions:
        out.append(
            "\n### Perguntas obrigatorias\n"
            "Faca UMA pergunta por mensagem, na ordem abaixo, de forma natural "
            "(nao leia como formulario). Nunca repita uma pergunta ja respondida "
            "espontaneamente pelo contato. Se ele desviar do assunto, responda a "
            "duvida dele e depois retome de onde parou:\n"
            + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
        )
    if documents:
        out.append(
            "\n### Documentos a solicitar\n"
            "Peca um documento por vez, explicando em uma linha para que serve. "
            "Aceite foto/arquivo enviado pelo WhatsApp e confirme o recebimento "
            "('Recebi, obrigada.'). Se o contato nao tiver agora, siga em frente e "
            "retome depois - nunca insista mais de uma vez seguida. PROIBIDO pedir "
            "senha, codigo de acesso, cartao ou dado bancario:\n"
            + "\n".join(f"- {d}" for d in documents)
        )
    if goal:
        out.append(
            "\n### Objetivo final\n"
            f"{goal}\n\n"
            "Enquanto esse objetivo nao for atingido, mantenha a conversa avancando. "
            "Assim que for atingido, faca um resumo curto do que foi coletado, avise "
            "que a equipe vai assumir e emita [TRANSFERIR=1]."
        )
    return "\n".join(out) + "\n"


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
    holiday_hours = (data.get("schedule") or {}).get("holiday_hours") or ""
    return (
        template.render(**data)
        + _compute_mission_block(data)
        + _compute_no_invention_block()
        + _compute_time_context_block()
        + _compute_holidays_block(holiday_hours)
        + _compute_closed_days_block()
    )


def get_system_prompt() -> str:
    """Renderiza o prompt sob demanda (greeting reflete o horário atual)."""
    return build_prompt()
