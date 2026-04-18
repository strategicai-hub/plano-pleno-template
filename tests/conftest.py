"""
Fixtures comuns de teste.

Garante que qualquer import de `app.config` dentro dos testes use valores
previsiveis, em vez de depender de um .env real. Os testes que precisam de
Redis/RabbitMQ reais sobem containers e sobrescrevem essas variaveis.
"""
import os
import sys
from pathlib import Path

# Adiciona a raiz do projeto ao sys.path para permitir "from app import ..."
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PROJECT_SLUG", "testslug")
os.environ.setdefault("BUSINESS_NAME", "Teste")
os.environ.setdefault("ASSISTANT_NAME", "Assistente Teste")
os.environ.setdefault("GEMINI_API_KEY", "fake-key")
os.environ.setdefault("UAZAPI_TOKEN", "fake-token")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6399")
os.environ.setdefault("RABBITMQ_HOST", "127.0.0.1")
os.environ.setdefault("RABBITMQ_PORT", "5699")
os.environ.setdefault("RABBITMQ_USER", "guest")
os.environ.setdefault("RABBITMQ_PASS", "guest")
os.environ.setdefault("RABBITMQ_VHOST", "/")
os.environ.setdefault("DEBOUNCE_SECONDS", "0")

# Garante que existe um client.yaml para o prompt carregar.
_client_yaml = ROOT / "client.yaml"
if not _client_yaml.exists():
    import shutil
    shutil.copy(ROOT / "client.example.yaml", _client_yaml)
