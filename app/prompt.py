"""
Gera o SYSTEM_PROMPT a partir do template Jinja2 + dados do client.yaml.

O nicho do negocio (academia, escola_cursos, etc.) e lido de
`client.yaml > niche`. Cada nicho tem um prompt proprio em
`app/prompts/{niche}.j2`. Se `niche` nao estiver definido, usa
"academia" por padrao (retrocompatibilidade).
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.client_data import load_client_data

DEFAULT_NICHE = "academia"


def build_prompt() -> str:
    prompts_dir = Path(__file__).parent / "prompts"
    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        keep_trailing_newline=True,
    )
    data = load_client_data()
    niche = (data.get("niche") or DEFAULT_NICHE).strip()
    template_file = f"{niche}.j2"
    if not (prompts_dir / template_file).exists():
        raise FileNotFoundError(
            f"Prompt do nicho '{niche}' nao encontrado em {prompts_dir / template_file}. "
            f"Nichos disponiveis: {[p.stem for p in prompts_dir.glob('*.j2')]}"
        )
    template = env.get_template(template_file)
    return template.render(**data)


SYSTEM_PROMPT = build_prompt()
