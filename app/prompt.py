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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from app.client_data import load_client_data

DEFAULT_NICHE = "academia"
_SP_TZ = ZoneInfo("America/Sao_Paulo")


def _compute_time_greeting() -> str:
    hour = datetime.now(_SP_TZ).hour
    if 5 <= hour < 12:
        return "bom dia"
    if 12 <= hour < 18:
        return "boa tarde"
    return "boa noite"


def build_prompt() -> str:
    prompts_dir = Path(__file__).parent / "prompts"
    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        keep_trailing_newline=True,
    )
    data = dict(load_client_data())
    assistant = dict(data.get("assistant") or {})
    assistant["greeting"] = _compute_time_greeting()
    data["assistant"] = assistant

    niche = (data.get("niche") or DEFAULT_NICHE).strip()
    template_file = f"{niche}.j2"
    if not (prompts_dir / template_file).exists():
        raise FileNotFoundError(
            f"Prompt do nicho '{niche}' não encontrado em {prompts_dir / template_file}. "
            f"Nichos disponíveis: {[p.stem for p in prompts_dir.glob('*.j2')]}"
        )
    template = env.get_template(template_file)
    return template.render(**data)


def get_system_prompt() -> str:
    """Renderiza o prompt sob demanda (greeting reflete o horário atual)."""
    return build_prompt()
