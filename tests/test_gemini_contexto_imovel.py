"""A ficha do imovel tem que entrar no contexto em TODAS as entradas.

Regressao real: a leitura do link vivia no consumer, entao o simulador (que
chama gemini.chat direto) nunca recebia a ficha — a Lucia devolvia o link do
proprio lead e transferia por um dado que estava na ficha. A montagem do bloco
passou para dentro de gemini.chat, que e o funil unico de WhatsApp, simulador
e follow-ups.
"""
import pytest

from app.services import gemini


class _FakeUsage:
    prompt_token_count = 10
    candidates_token_count = 5
    total_token_count = 15


class _FakeResponse:
    text = "Resposta da IA"
    usage_metadata = _FakeUsage()


class _FakeModels:
    def __init__(self):
        self.contents = None

    def generate_content(self, model=None, contents=None, config=None):
        self.contents = contents
        return _FakeResponse()


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


@pytest.fixture
def gemini_isolado(monkeypatch):
    """Isola o chat: sem Redis, sem rede, sem telemetria."""
    client = _FakeClient()
    gravado: list[tuple[str, str]] = []

    async def _history(phone):
        return []

    async def _append(phone, role, text):
        gravado.append((role, text))

    monkeypatch.setattr(gemini, "_get_client", lambda: client)
    monkeypatch.setattr(gemini, "get_chat_history", _history)
    monkeypatch.setattr(gemini, "append_chat_history", _append)
    monkeypatch.setattr(gemini, "log_message_async", lambda **kwargs: None)
    monkeypatch.setattr(gemini, "get_system_prompt", lambda: "PROMPT")
    return client, gravado


async def test_ficha_do_imovel_entra_no_contexto(monkeypatch, gemini_isolado):
    client, gravado = gemini_isolado

    async def _bloco(texto):
        assert "auxiliadorapredial" in texto
        return "[CONTEXTO DO SISTEMA — ficha do imovel]\n\n"

    monkeypatch.setattr(gemini.imoveis, "build_context_block", _bloco)

    await gemini.chat("5551999999999", "olha esse https://www.auxiliadorapredial.com.br/imovel/venda/445247/x")

    enviado = client.models.contents[-1].parts[0].text
    assert "[CONTEXTO DO SISTEMA — ficha do imovel]" in enviado

    # A ficha tambem precisa ficar no historico: no turno seguinte o lead
    # pergunta "e o condominio?" e o dado tem que continuar no contexto.
    user_msgs = [texto for role, texto in gravado if role == "user"]
    assert user_msgs and "[CONTEXTO DO SISTEMA — ficha do imovel]" in user_msgs[0]


async def test_mensagem_sem_link_nao_ganha_bloco(monkeypatch, gemini_isolado):
    client, gravado = gemini_isolado

    async def _bloco(texto):
        return ""

    monkeypatch.setattr(gemini.imoveis, "build_context_block", _bloco)

    await gemini.chat("5551999999999", "quero um apto de 2 quartos")

    user_msgs = [texto for role, texto in gravado if role == "user"]
    assert user_msgs == ["quero um apto de 2 quartos"]


async def test_falha_na_leitura_nao_derruba_o_chat(monkeypatch, gemini_isolado):
    client, gravado = gemini_isolado

    async def _explode(texto):
        raise RuntimeError("portal fora do ar")

    monkeypatch.setattr(gemini.imoveis, "build_context_block", _explode)

    resposta, tokens = await gemini.chat("5551999999999", "olha https://www.vivareal.com.br/imovel/x-id-123456/")

    # O atendimento segue normalmente, apenas sem a ficha.
    assert resposta == "Resposta da IA"
    assert tokens == (10, 5, 15)
