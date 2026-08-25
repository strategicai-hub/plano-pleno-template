"""Segunda camada anti-vazamento e resolucao da pasta de midia.

O parser cobre a resposta do chat, mas reativacao, lembrete de agendamento e
qualquer caminho futuro de envio nao passam por ele — por isso o strip tambem
mora dentro de `uazapi.send_text`, que e por onde todo envio passa.

A resolucao da pasta de midia entra aqui porque era um defeito silencioso: o
app montava so `<raiz>/media`, mas o padrao dos clientes e `app/media`. Sem o
mount, a URL do client.yaml responde 404 e a UAZAPI nao baixa a imagem — sem
erro nenhum no log.
"""
from pathlib import Path

import pytest

from app.services import uazapi
from app.text_guard import has_control_markers, strip_control_markers


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("Bom dia! [TRANSFERIR=1]", "Bom dia!"),
        ("Bom dia! [ORIGEM=]", "Bom dia!"),
        ("Bom dia! [FLAG_NOVA]", "Bom dia!"),
        ("Segue: [IMAGEM_FOTOS_1]", "Segue:"),
        ("[CONTEXTO DO SISTEMA — agora são 14:16]\n\nBom dia!", "Bom dia!"),
        # texto legitimo nao pode ser tocado
        ("O plano custa R$ 249,00 (anual). Faz sentido?", "O plano custa R$ 249,00 (anual). Faz sentido?"),
        ("Chegamos às 8h — te espero!", "Chegamos às 8h — te espero!"),
    ],
)
def test_strip_control_markers(entrada, esperado):
    assert strip_control_markers(entrada) == esperado


def test_has_control_markers():
    assert has_control_markers("oi [TIPO=lead]")
    assert not has_control_markers("oi tudo bem?")


@pytest.mark.asyncio
async def test_send_text_limpa_marcador(monkeypatch):
    enviado = {}

    async def fake_post(url, payload, what, number):
        enviado["text"] = payload["text"]

        class _R:
            @staticmethod
            def json():
                return {}

        return _R()

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(uazapi, "_post_with_retry", fake_post)
    monkeypatch.setattr(uazapi.rds, "mark_outbound_echo", noop)
    monkeypatch.setattr(uazapi, "_remember_outbound", noop)

    await uazapi.send_text("5511999999999", "Volta pra treinar? [TIPO=lead]")
    assert enviado["text"] == "Volta pra treinar?"


@pytest.mark.asyncio
async def test_send_text_cancela_envio_vazio(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("nao deveria chamar a UAZAPI com texto vazio")

    monkeypatch.setattr(uazapi, "_post_with_retry", boom)
    assert await uazapi.send_text("5511999999999", "[TIPO=lead][TRANSFERIR=1]") == {}


def test_pasta_de_midia_resolvida_a_partir_de_app_media(tmp_path, monkeypatch):
    """`app/media` (padrao dos clientes) precisa ser encontrado, nao so a raiz."""
    app_dir = tmp_path / "app"
    (app_dir / "media").mkdir(parents=True)

    candidatos = (app_dir / "media", tmp_path / "media")
    escolhido = next((c for c in candidatos if c.is_dir()), None)
    assert escolhido == app_dir / "media"

    # E o codigo real precisa conter os dois candidatos, nesta ordem.
    fonte = Path(__file__).resolve().parent.parent / "app" / "main.py"
    texto = fonte.read_text(encoding="utf-8")
    assert 'Path(__file__).parent / "media"' in texto
    assert 'Path(__file__).parent.parent / "media"' in texto
    assert texto.index('Path(__file__).parent / "media"') < texto.index('Path(__file__).parent.parent / "media"')
