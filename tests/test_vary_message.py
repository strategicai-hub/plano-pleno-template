"""
Guarda-costas da variacao de mensagem (`gemini.vary_message`).

A variacao existe para que dois leads nunca recebam a string identica (texto
igual em massa e o principal gatilho de bloqueio do WhatsApp). O risco do
mecanismo e o modelo "melhorar" o texto do cliente — e foi o que aconteceu em
producao: a abertura da Auxiliadora dizia "vou fazer algumas perguntinhas
rapidas" e saiu "vou fazer 3 perguntinhas rapidas", num roteiro de 4 perguntas.

Quando a saida nao respeita o texto base, o certo e descartar e enviar o literal.
"""
import pytest

from app.services import gemini


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.usage_metadata = None


class _FakeModels:
    def generate_content(self, **kw):  # nunca chamado: to_thread e substituido
        raise AssertionError("generate_content nao deveria ser chamado direto")


class _FakeClient:
    models = _FakeModels()


@pytest.fixture
def variar(monkeypatch):
    """Chama vary_message com a saida do modelo controlada pelo teste."""
    monkeypatch.setattr(gemini, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(gemini, "log_message_async", lambda **kw: None)
    monkeypatch.setattr(
        gemini, "load_client_data",
        lambda: {"business": {"name": "Imob X"}, "assistant": {"name": "Luiza"}},
    )

    async def _run(base: str, saida: str, **kw):
        async def fake_to_thread(fn, *a, **k):
            return _FakeResponse(saida)
        monkeypatch.setattr(gemini.asyncio, "to_thread", fake_to_thread)
        return await gemini.vary_message("5511900000000", base, **kw)

    return _run


ABERTURA = (
    "Olá, Marcos! Tudo bem? Meu nome é Luiza, sou assistente da Auxiliadora Predial.\n\n"
    "Para selecionarmos o melhor material para você, vou fazer algumas perguntinhas rápidas, tudo bem?\n\n"
    "1) Você busca um imóvel para moradia da família ou o foco é investimento?"
)


async def test_descarta_quando_inventa_numero_de_perguntas(variar):
    """O caso real: "algumas perguntinhas" virou "3 perguntinhas"."""
    saida = ABERTURA.replace("algumas perguntinhas", "3 perguntinhas")
    assert await variar(ABERTURA, saida) == ""


async def test_descarta_numero_por_extenso(variar):
    saida = ABERTURA.replace("algumas perguntinhas", "três perguntinhas")
    assert await variar(ABERTURA, saida) == ""


async def test_aceita_variacao_que_respeita_o_texto(variar):
    saida = (
        "Oi, Marcos! Bom dia. Aqui é a Luiza, da Auxiliadora Predial.\n\n"
        "Para eu separar o material certo pra você, posso fazer algumas perguntinhas rápidas?\n\n"
        "1) Você procura um imóvel para a família morar ou pensa em investimento?"
    )
    assert await variar(ABERTURA, saida) == saida


async def test_numero_que_ja_estava_no_texto_base_passa(variar):
    """O "1)" da pergunta e o "dois minutinhos" do follow-up sao do cliente."""
    base = "Oi, Marcos!\n\nQuando tiver dois minutinhos, dá uma olhada e me retorna."
    saida = "Oi, Marcos!\n\nAssim que sobrarem dois minutinhos, dá uma olhada e me responde."
    assert await variar(base, saida) == saida


async def test_artigo_indefinido_nao_conta_como_quantidade(variar):
    """"um"/"uma" sao artigos comuns — barra-los reprovaria toda variacao."""
    base = "Oi!\n\nVimos que você demonstrou interesse."
    saida = "Oi!\n\nVimos que você teve interesse em um dos nossos imóveis."
    assert await variar(base, saida) == saida


async def test_descarta_quando_perde_um_balao(variar):
    saida = "Olá, Marcos! Tudo bem? Vou fazer algumas perguntinhas rápidas. Você busca imóvel?"
    assert await variar(ABERTURA, saida) == ""


async def test_descarta_markdown(variar):
    saida = ABERTURA.replace("1)", "*1)*")
    assert await variar(ABERTURA, saida) == ""


async def test_descarta_quando_encolhe_demais(variar):
    saida = "Oi!\n\nPosso perguntar?\n\nÉ pra morar?"
    assert await variar(ABERTURA, saida) == ""


async def test_texto_base_vazio_nao_chama_o_modelo(variar):
    assert await variar("", "qualquer coisa") == ""
