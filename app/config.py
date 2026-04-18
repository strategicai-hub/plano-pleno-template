from urllib.parse import quote

from pydantic_settings import BaseSettings
from pydantic import model_validator


class Settings(BaseSettings):
    # Identificador unico do projeto (deriva queue, redis prefix, webhook path, etc.)
    PROJECT_SLUG: str = "template"

    # Dados do negocio (exibidos no painel e no FastAPI)
    BUSINESS_NAME: str = "Empresa"
    ASSISTANT_NAME: str = "Assistente"

    # RabbitMQ
    RABBITMQ_HOST: str = "91.98.64.92"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USER: str = "guest"
    RABBITMQ_PASS: str = "guest"
    RABBITMQ_VHOST: str = "default"
    RABBITMQ_QUEUE: str = ""

    # Redis
    REDIS_HOST: str = "91.98.64.92"
    REDIS_PORT: int = 6380
    REDIS_PASSWORD: str = ""

    # Google Gemini
    GEMINI_API_KEY: str = ""

    # UAZAPI
    UAZAPI_BASE_URL: str = "https://strategicai.uazapi.com"
    UAZAPI_TOKEN: str = ""
    UAZAPI_INSTANCE: str = ""

    # Google Sheets
    GOOGLE_CREDENTIALS_JSON: str = ""
    GOOGLE_SHEET_ID: str = ""

    # App
    WEBHOOK_PATH: str = ""
    DEBOUNCE_SECONDS: int = 30
    BLOCK_TTL_SECONDS: int = 3600

    # Alerta de atendimento humano
    # Formato: somente digitos, com DDI (ex: 5511999990000)
    ALERT_PHONE: str = ""

    # Numeros que ignoram debounce (comma-separated, ex: "5511999990000,5511888880000")
    DEBOUNCE_BYPASS_PHONES: str = ""

    # Whitelist de numeros que a IA pode responder (comma-separated).
    # Se vazio, responde para todos. Util em ambientes de homologacao/piloto.
    ALLOWED_PHONES: str = ""

    # CORS (comma-separated, use "*" para liberar todas as origens)
    CORS_ORIGINS: str = "*"

    @model_validator(mode="after")
    def _fill_defaults_from_slug(self) -> "Settings":
        if not self.RABBITMQ_QUEUE:
            self.RABBITMQ_QUEUE = self.PROJECT_SLUG
        if not self.UAZAPI_INSTANCE:
            self.UAZAPI_INSTANCE = self.PROJECT_SLUG
        if not self.WEBHOOK_PATH:
            self.WEBHOOK_PATH = f"/{self.PROJECT_SLUG}"
        return self

    @property
    def debounce_bypass_set(self) -> set[str]:
        if not self.DEBOUNCE_BYPASS_PHONES:
            return set()
        return {p.strip() for p in self.DEBOUNCE_BYPASS_PHONES.split(",") if p.strip()}

    @property
    def allowed_phones_set(self) -> set[str]:
        if not self.ALLOWED_PHONES:
            return set()
        return {p.strip() for p in self.ALLOWED_PHONES.split(",") if p.strip()}

    @property
    def cors_origins(self) -> list[str]:
        raw = (self.CORS_ORIGINS or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def rabbitmq_url(self) -> str:
        user = quote(self.RABBITMQ_USER, safe="")
        password = quote(self.RABBITMQ_PASS, safe="")
        vhost = quote(self.RABBITMQ_VHOST, safe="")
        return (
            f"amqp://{user}:{password}"
            f"@{self.RABBITMQ_HOST}:{self.RABBITMQ_PORT}/{vhost}"
        )

    @property
    def redis_url(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
