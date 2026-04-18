"""
Carrega os dados do negocio a partir do arquivo client.yaml.
"""
import yaml
from pathlib import Path
from functools import lru_cache


@lru_cache
def load_client_data() -> dict:
    path = Path(__file__).parent.parent / "client.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Arquivo client.yaml nao encontrado em {path}. "
            "Copie client.example.yaml para client.yaml e preencha os dados."
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
