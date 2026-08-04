"""Testes do leitor de ficha de imovel a partir de link (app/services/imoveis.py).

Nenhum teste aqui bate na rede: o `_get` e substituido por respostas fixas,
para o resultado nao depender do site do parceiro estar no ar.
"""
import json

import pytest

from app.services import imoveis


class FakeResp:
    def __init__(self, text: str = "", payload=None, status_code: int = 200):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("sem json")
        return self._payload


@pytest.fixture(autouse=True)
def _sem_cache(monkeypatch):
    """Desliga o cache Redis — os testes nao sobem Redis."""
    async def _get_none(url):
        return None

    async def _set_noop(url, imovel):
        return None

    monkeypatch.setattr(imoveis, "_cache_get", _get_none)
    monkeypatch.setattr(imoveis, "_cache_set", _set_noop)


@pytest.fixture(autouse=True)
def casa_propria(monkeypatch):
    """Config real da Carmen: todos os portais suportados sao "da casa"."""
    monkeypatch.setattr(imoveis, "_own_sites", lambda: set(imoveis._SITES))


# --------------------------------------------------------------------------
# Parsing de numeros em pt-BR
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entrada,esperado",
    [
        (185000, 185000.0),
        ("185000", 185000.0),
        ("R$ 700.000", 700000.0),          # ponto como separador de milhar
        ("3.150.099,90", 3150099.90),      # milhar + decimal
        ("R$ 8.000,00 IPTU Anual*", 8000.0),
        ("1234.56", 1234.56),              # ponto como decimal (2 casas)
        ("R$ 500", 500.0),
        ("", None),
        (None, None),
        ("sem numero", None),
    ],
)
def test_to_float_pt_br(entrada, esperado):
    assert imoveis._to_float(entrada) == esperado


def test_brl_formata_pt_br():
    assert imoveis._brl(185000) == "R$ 185.000"
    assert imoveis._brl(3150099.90) == "R$ 3.150.099,90"
    assert imoveis._brl(None) == ""


# --------------------------------------------------------------------------
# Deteccao de links
# --------------------------------------------------------------------------

def test_find_property_urls_reconhece_portais_suportados():
    texto = (
        "oi, vi esse https://www.vivareal.com.br/imovel/apto-id-123456/ "
        "e esse aqui https://ba.olx.com.br/imoveis/casa-boa-12345678"
    )
    urls = imoveis.find_property_urls(texto)
    assert len(urls) == 2
    assert "vivareal.com.br" in urls[0]
    assert "olx.com.br" in urls[1]


def test_find_property_urls_ignora_pontuacao_final():
    texto = "olha esse https://www.foxterciaimobiliaria.com.br/imovel/891261."
    assert imoveis.find_property_urls(texto) == [
        "https://www.foxterciaimobiliaria.com.br/imovel/891261"
    ]


def test_find_property_urls_vazio_sem_link():
    assert imoveis.find_property_urls("quero um apto de 2 quartos") == []


def test_find_unsupported_urls_separa_portal_desconhecido():
    texto = "esse https://www.quintoandar.com.br/imovel/123 e esse https://www.vivareal.com.br/imovel/x-id-9999999/"
    assert imoveis.find_unsupported_urls(texto) == ["https://www.quintoandar.com.br/imovel/123"]


def test_subdominio_da_olx_e_reconhecido():
    assert imoveis._site_for("https://rs.olx.com.br/imoveis/casa-1234") is not None


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

_LD_AUXILIADORA = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"RealEstateListing","name":"Apartamento - Centro","identifier":"445247",
  "additionalProperty":[{"@type":"PropertyValue","name":"Su\\u00edtes","value":"1"},
                        {"@type":"PropertyValue","name":"Vagas de Garagem","value":"2"},
                        {"@type":"PropertyValue","name":"Tipo de Neg\\u00f3cio","value":"Venda"}],
  "offers":[{"@type":"Offer","price":185000,
    "itemOffered":{"@type":"Accommodation","accommodationCategory":"Apartamento",
      "numberOfRooms":1,"numberOfBathroomsTotal":1,
      "floorSize":{"value":36},
      "address":{"streetAddress":"Rua Demetrio Ribeiro 654","addressLocality":"Porto Alegre","addressRegion":"RS"}},
    "priceSpecification":[{"name":"Condom\\u00ednio","price":301},{"name":"IPTU","price":45}],
    "seller":{"name":"Carmen Machado"}}]},
 {"@type":"WebPage","name":"Apartamento com 1 quarto em Centro Hist\\u00f3rico, Porto Alegre.",
  "description":"Otimo apartamento semi-mobiliado."}]}
</script></head><body></body></html>
"""


async def test_parse_auxiliadora_extrai_ficha_completa(monkeypatch):
    async def fake_get(url, headers=None):
        return FakeResp(text=_LD_AUXILIADORA)

    monkeypatch.setattr(imoveis, "_get", fake_get)
    im = await imoveis.fetch_imovel(
        "https://www.auxiliadorapredial.com.br/imovel/venda/445247/apartamento"
    )
    assert im is not None
    assert im.da_casa is True
    assert im.codigo == "445247"
    assert im.tipo == "Apartamento"
    assert im.finalidade == "Venda"
    assert im.preco == 185000
    assert im.condominio == 301
    assert im.iptu == 45
    assert im.area == "36 m²"
    assert im.quartos == 1
    assert im.suites == 1
    assert im.vagas == 2
    assert im.bairro == "Centro Histórico"
    assert im.corretor == "Carmen Machado"


_NEXT_FOXTER = """
<html><body><script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"product":{"code":891261,"h1":"Casa 3 dorms","type":"Casa",
 "saleValue":"3.150.099,90","iptu":"R$ 8.000,00 IPTU Anual*","bedroomsNumber":3,
 "suites":"3 su\\u00edtes","bathrooms":"4 banheiros","parkingSpaces":"5 vagas",
 "areaPrivate":"400","district":"Ch\\u00e1cara das Pedras","city":"Porto Alegre","state":"RS",
 "placeType":"Rua","place":"Professor Ulisses Cabral","placeNumber":"1141",
 "description":"<div><span>Alto Padr\\u00e3o</span></div>"}}}}
</script></body></html>
"""


async def test_parse_foxter_le_next_data(monkeypatch):
    async def fake_get(url, headers=None):
        return FakeResp(text=_NEXT_FOXTER)

    monkeypatch.setattr(imoveis, "_get", fake_get)
    im = await imoveis.fetch_imovel("https://www.foxterciaimobiliaria.com.br/imovel/891261")
    assert im is not None
    assert im.da_casa is True  # a Carmen atende os anuncios da Foxter tambem
    assert im.tipo == "Casa"
    assert im.preco == 3150099.90
    assert im.iptu == 8000.0
    assert im.quartos == 3
    assert im.suites == 3
    assert im.vagas == 5
    assert im.area == "400 m²"
    assert im.endereco == "Rua Professor Ulisses Cabral 1141"
    assert im.descricao == "Alto Padrão"


_GLUE_PAYLOAD = {
    "search": {"result": {"listings": [{
        "listing": {
            "id": "2854455269",
            "title": "Apartamento mobiliado de 2 dormitorios",
            "unitTypes": ["APARTMENT"],
            "usableAreas": ["57"],
            "bedrooms": [2], "bathrooms": [1], "suites": [0], "parkingSpaces": [0],
            "pricingInfos": [{"price": "327000", "monthlyCondoFee": "350",
                              "iptu": "52", "businessType": "SALE"}],
            "address": {"street": "Rua Sape", "streetNumber": "",
                        "neighborhood": "Passo da Areia", "city": "Porto Alegre",
                        "stateAcronym": "RS"},
            "description": "Apartamento mobiliado.",
        },
        "account": {"name": "Imobiliaria X"},
    }]}}
}


async def test_parse_vivareal_usa_id_da_url(monkeypatch):
    chamadas = []

    async def fake_get(url, headers=None):
        chamadas.append((url, headers))
        return FakeResp(payload=_GLUE_PAYLOAD)

    monkeypatch.setattr(imoveis, "_get", fake_get)
    im = await imoveis.fetch_imovel(
        "https://www.vivareal.com.br/imovel/apartamento-2-quartos-id-2854455269/"
    )
    assert im is not None
    # Consultou a API interna com o id extraido da URL e o dominio certo.
    assert "id=2854455269" in chamadas[0][0]
    assert chamadas[0][1]["x-domain"] == "www.vivareal.com.br"
    assert im.fonte == "Viva Real"
    assert im.tipo == "Apartamento"
    assert im.preco == 327000
    assert im.condominio == 350
    assert im.quartos == 2
    assert im.bairro == "Passo da Areia"


async def test_parse_zap_usa_dominio_do_zap(monkeypatch):
    chamadas = []

    async def fake_get(url, headers=None):
        chamadas.append((url, headers))
        return FakeResp(payload=_GLUE_PAYLOAD)

    monkeypatch.setattr(imoveis, "_get", fake_get)
    im = await imoveis.fetch_imovel(
        "https://www.zapimoveis.com.br/imovel/venda-casa-id-2877825573/"
    )
    assert im is not None
    assert chamadas[0][1]["x-domain"] == "www.zapimoveis.com.br"
    assert im.fonte == "ZAP Imóveis"


async def test_url_sem_id_nao_quebra(monkeypatch):
    async def fake_get(url, headers=None):
        raise AssertionError("nao deveria buscar sem id na URL")

    monkeypatch.setattr(imoveis, "_get", fake_get)
    assert await imoveis.fetch_imovel("https://www.vivareal.com.br/venda/rs/") is None


_OLX_HTML = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"BuyAction","identifier":1523533864,
 "Object":{"@type":"Product","name":"OPORTUNIDADE EM BURAQUINHO","description":"Casa duplex."}}
</script></head><body>
&quot;state&quot;:&quot;BA&quot;,&quot;municipality&quot;:&quot;Lauro de Freitas&quot;,&quot;neighbourhood&quot;:&quot;Buraquinho&quot;,&quot;priceValue&quot;:&quot;R$ 700.000&quot;,&quot;properties&quot;:[{&quot;name&quot;:&quot;category&quot;,&quot;value&quot;:&quot;Casas&quot;},{&quot;name&quot;:&quot;real_estate_type&quot;,&quot;value&quot;:&quot;Venda - casa&quot;},{&quot;name&quot;:&quot;condominio&quot;,&quot;value&quot;:&quot;R$ 500&quot;},{&quot;name&quot;:&quot;size&quot;,&quot;value&quot;:&quot;96m²&quot;},{&quot;name&quot;:&quot;rooms&quot;,&quot;value&quot;:&quot;2&quot;},{&quot;name&quot;:&quot;garage_spaces&quot;,&quot;value&quot;:&quot;1&quot;}]
</body></html>
"""


async def test_parse_olx_le_ficha_escapada(monkeypatch):
    async def fake_get(url, headers=None):
        return FakeResp(text=_OLX_HTML)

    monkeypatch.setattr(imoveis, "_get", fake_get)
    im = await imoveis.fetch_imovel(
        "https://ba.olx.com.br/grande-salvador/imoveis/oportunidade-1523533864"
    )
    assert im is not None
    assert im.tipo == "Casa"
    assert im.finalidade == "Venda"
    assert im.preco == 700000.0     # nao pode virar 700
    assert im.condominio == 500
    assert im.area == "96 m²"
    assert im.quartos == 2
    assert im.vagas == 1
    assert im.cidade == "Lauro de Freitas"
    assert im.bairro == "Buraquinho"


# --------------------------------------------------------------------------
# Anuncio removido (imovel ja vendido) — um caso por portal
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,status,fonte",
    [
        ("https://www.auxiliadorapredial.com.br/imovel/venda/999999/x", 404,
         "Auxiliadora Predial"),
        ("https://www.foxterciaimobiliaria.com.br/imovel/100000", 404,
         "Foxter Cia. Imobiliária"),
        ("https://ba.olx.com.br/imoveis/teste-1111111111", 410, "OLX"),
    ],
)
async def test_status_de_removido_marca_indisponivel(monkeypatch, url, status, fonte):
    async def fake_get(u, headers=None):
        return FakeResp(text="<html></html>", status_code=status)

    monkeypatch.setattr(imoveis, "_get", fake_get)
    im = await imoveis.fetch_imovel(url)
    assert im is not None
    assert im.indisponivel is True
    assert im.fonte == fonte


async def test_foxter_com_flag_unavailable(monkeypatch):
    html = _NEXT_FOXTER.replace('"code":891261', '"unavailable":true,"code":891261')

    async def fake_get(url, headers=None):
        return FakeResp(text=html)

    monkeypatch.setattr(imoveis, "_get", fake_get)
    im = await imoveis.fetch_imovel("https://www.foxterciaimobiliaria.com.br/imovel/891261")
    assert im is not None
    assert im.indisponivel is True
    assert im.codigo == "891261"
    # Nao pode vazar ficha de um imovel que saiu do ar.
    assert im.preco is None


async def test_glue_sem_resultado_marca_indisponivel(monkeypatch):
    async def fake_get(url, headers=None):
        return FakeResp(payload={"search": {"totalCount": 0, "result": {"listings": []}}})

    monkeypatch.setattr(imoveis, "_get", fake_get)
    im = await imoveis.fetch_imovel(
        "https://www.vivareal.com.br/imovel/apartamento-id-2854455269/"
    )
    assert im is not None
    assert im.indisponivel is True
    assert im.codigo == "2854455269"


def test_bloco_de_indisponivel_pede_perfil_e_nao_descreve():
    im = imoveis.Imovel(fonte="Viva Real", url="https://x", indisponivel=True,
                        codigo="2854455269", da_casa=True)
    bloco = imoveis.build_block([im])
    assert "FORA DO AR" in bloco
    assert "JÁ FOI VENDIDO" in bloco
    assert "2854455269" in bloco
    # Nao pode instruir a apresentar ficha de imovel vendido.
    assert "AUTORITATIVA" not in bloco


async def test_indisponivel_tem_prioridade_sobre_da_casa(monkeypatch):
    """Anuncio removido de site proprio nao pode virar bloco de ficha."""
    async def fake_get(url, headers=None):
        return FakeResp(text="<html></html>", status_code=404)

    monkeypatch.setattr(imoveis, "_get", fake_get)
    bloco = await imoveis.build_context_block(
        "olha https://www.auxiliadorapredial.com.br/imovel/venda/999999/x"
    )
    assert "FORA DO AR" in bloco
    assert "NOSSA IMOBILIÁRIA" not in bloco


def test_bloco_de_link_nao_suportado_pede_caracteristicas_e_nao_codigo():
    bloco = imoveis.build_block([], ["https://www.quintoandar.com.br/imovel/1"])
    assert "características" in bloco
    assert "NÃO peça o código" in bloco
    assert "[TRANSFERIR=1]" in bloco


async def test_site_fora_do_ar_devolve_none(monkeypatch):
    async def fake_get(url, headers=None):
        return None

    monkeypatch.setattr(imoveis, "_get", fake_get)
    assert await imoveis.fetch_imovel(
        "https://www.auxiliadorapredial.com.br/imovel/venda/1/x"
    ) is None


async def test_excecao_no_parser_nao_propaga(monkeypatch):
    async def explode(url):
        raise RuntimeError("boom")

    monkeypatch.setitem(imoveis._SITES, "auxiliadorapredial.com.br",
                        ("Auxiliadora Predial", explode))
    assert await imoveis.fetch_imovel(
        "https://www.auxiliadorapredial.com.br/imovel/venda/1/x"
    ) is None


# --------------------------------------------------------------------------
# Montagem do bloco de contexto
# --------------------------------------------------------------------------

def test_bloco_da_casa_e_autoritativo():
    im = imoveis.Imovel(fonte="Auxiliadora Predial", da_casa=True, tipo="Apartamento",
                        preco=185000, bairro="Centro", quartos=1)
    bloco = imoveis.build_block([im])
    assert "ANÚNCIO DA NOSSA IMOBILIÁRIA" in bloco
    assert "R$ 185.000" in bloco
    assert "CONTEXTO DO SISTEMA" in bloco


def test_bloco_de_terceiro_nao_expoe_ficha():
    im = imoveis.Imovel(fonte="Viva Real", da_casa=False, tipo="Apartamento",
                        preco=327000, condominio=350, bairro="Passo da Areia",
                        quartos=2, endereco="Rua Sape 100", corretor="Imobiliaria X",
                        descricao="Texto do anuncio do concorrente")
    bloco = imoveis.build_block([im])
    assert "OUTRA IMOBILIÁRIA" in bloco
    assert "NÃO RECITE" in bloco
    # Dados sensiveis do concorrente ficam de fora do bloco.
    assert "Rua Sape 100" not in bloco
    assert "Imobiliaria X" not in bloco
    assert "Texto do anuncio do concorrente" not in bloco
    assert "R$ 350" not in bloco
    # O perfil de busca continua disponivel para o redirecionamento.
    assert "Passo da Areia" in bloco
    assert "2 quarto(s)" in bloco


def test_bloco_de_link_nao_suportado_pede_codigo():
    bloco = imoveis.build_block([], ["https://www.quintoandar.com.br/imovel/1"])
    assert "NÃO CONSEGUIMOS ABRIR" in bloco
    assert "quintoandar.com.br" in bloco


def test_sem_link_nao_gera_bloco():
    assert imoveis.build_block([], []) == ""


async def test_build_context_block_sem_url_nao_busca_nada(monkeypatch):
    async def fake_get(url, headers=None):
        raise AssertionError("nao deveria fazer request sem link")

    monkeypatch.setattr(imoveis, "_get", fake_get)
    assert await imoveis.build_context_block("quero um apto de 2 quartos") == ""


async def test_build_context_block_portal_que_falhou_vira_aviso(monkeypatch):
    async def fake_get(url, headers=None):
        return None

    monkeypatch.setattr(imoveis, "_get", fake_get)
    bloco = await imoveis.build_context_block(
        "olha https://www.foxterciaimobiliaria.com.br/imovel/891261"
    )
    assert "NÃO CONSEGUIMOS ABRIR" in bloco
