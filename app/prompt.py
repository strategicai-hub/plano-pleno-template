"""
Gera o SYSTEM_PROMPT a partir do template Jinja2 + dados do client.yaml.
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.client_data import load_client_data


def build_prompt() -> str:
    template_dir = Path(__file__).parent
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        keep_trailing_newline=True,
    )
    template = env.get_template("prompt_template.j2")
    data = load_client_data()
    return template.render(**data)


SYSTEM_PROMPT = build_prompt()
