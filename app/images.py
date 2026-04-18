# Dicionario de imagens/midias que a IA pode referenciar via tags.
# Carregado automaticamente da secao "media" do client.yaml.
# Formato no YAML: "[TAG]": {url: "https://...", type: "image|document|video"}

from app.client_data import load_client_data

_data = load_client_data()
MEDIA_DICT: dict[str, dict] = _data.get("media", {}) or {}
