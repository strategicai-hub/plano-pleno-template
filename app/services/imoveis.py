"""Leitura da ficha de um imovel a partir do link que o lead envia.

Quando o lead cola a URL de um anuncio no WhatsApp, este modulo busca os dados
e devolve a ficha normalizada. O bot passa a responder ja sabendo de qual
imovel se trata, em vez de perguntar "sobre qual imovel voce fala?".

Estrategia por portal — sempre dado ESTRUTURADO, nunca leitura do visual da
pagina (que quebra a cada redesign do site):

- Auxiliadora Predial: JSON-LD `RealEstateListing` embutido na pagina (o mesmo
  bloco que o site publica para o Google).
- Foxter: `__NEXT_DATA__` -> props.pageProps.product (objeto completo).
- Viva Real / ZAP: API interna `glue-api`, consultada pelo id que ja vem na
  propria URL. As paginas HTML desses dois estao atras de Cloudflare e
  devolvem 404 forjado para qualquer cliente que nao seja navegador de
  verdade; a glue-api responde normalmente com o header `x-domain`.
- OLX: JSON-LD `BuyAction` -> Object (Product).

Sites "da casa" (a imobiliaria da corretora) vem de `client.yaml`:

    realestate:
      own_sites: ["auxiliadorapredial.com.br"]

Link de site da casa vira ficha autoritativa e o bot responde tudo. Link de
terceiro entra apenas como PERFIL (tipo, quartos, bairro, faixa de preco) para
o bot redirecionar bem — sem recitar o anuncio do concorrente.

O resultado fica em cache no Redis: dois leads que mandam o mesmo anuncio
custam uma busca so.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape as html_unescape
from typing import Any, Callable, Optional

import httpx

from app.client_data import load_client_data
from app.config import settings
from app.services.redis_service import get_redis

logger = logging.getLogger(__name__)

# `curl_cffi` imita a impressao digital TLS de um Chrome real. Viva Real, ZAP e
# OLX estao atras de Cloudflare, que identifica o cliente pelo handshake TLS —
# nao pelos cabecalhos. Com httpx puro esses tres devolvem 403 mesmo com
# User-Agent de navegador e todos os headers certos (testado). Auxiliadora e
# Foxter funcionam com qualquer cliente; o fallback httpx cobre eles caso o
# curl_cffi nao esteja instalado (ambiente de teste, por exemplo).
try:  # pragma: no cover - depende do ambiente
    from curl_cffi.requests import AsyncSession as _CurlSession
except ImportError:  # pragma: no cover
    _CurlSession = None
    logger.info("curl_cffi ausente — Viva Real/ZAP/OLX ficarao indisponiveis")

_IMPERSONATE = "chrome"

# Timeout curto: o lead esta esperando. Se o site demorar, seguimos sem a ficha
# em vez de segurar a resposta.
_HTTP_TIMEOUT = 12.0
_CACHE_TTL = 6 * 60 * 60  # 6h — anuncio muda pouco; preco raramente no mesmo dia
_MAX_URLS = 2  # no maximo 2 links por mensagem (evita rajada de requests)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


# --------------------------------------------------------------------------
# Modelo normalizado
# --------------------------------------------------------------------------

@dataclass
class Imovel:
    fonte: str = ""            # nome legivel do site
    url: str = ""
    da_casa: bool = False      # True = anuncio da propria imobiliaria
    indisponivel: bool = False  # anuncio saiu do ar (tipicamente ja vendido)
    codigo: str = ""
    titulo: str = ""
    tipo: str = ""             # Apartamento, Casa, Terreno...
    finalidade: str = ""       # Venda / Aluguel
    endereco: str = ""
    bairro: str = ""
    cidade: str = ""
    uf: str = ""
    preco: Optional[float] = None
    condominio: Optional[float] = None
    iptu: Optional[float] = None
    area: str = ""
    quartos: Optional[int] = None
    suites: Optional[int] = None
    banheiros: Optional[int] = None
    vagas: Optional[int] = None
    descricao: str = ""
    corretor: str = ""
    extras: dict = field(default_factory=dict)

    def is_useful(self) -> bool:
        """Ficha vazia nao vale injetar no prompt (so gastaria token).

        "Anuncio fora do ar" e informacao util por si so — e a diferenca entre
        o bot dizer "esse ja foi vendido" e ficar mudo sobre o link.
        """
        if self.indisponivel:
            return True
        return bool(self.titulo or self.tipo or self.preco or self.endereco)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _brl(value: Optional[float]) -> str:
    if value is None:
        return ""
    try:
        s = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return ""
    # f-string sai en-US ("1,234.56"); troca para pt-BR ("1.234,56").
    s = s.replace(",", "_").replace(".", ",").replace("_", ".")
    if s.endswith(",00"):
        s = s[:-3]
    return f"R$ {s}"


def _to_float(value: Any) -> Optional[float]:
    """Aceita 185000, "185000", "3.150.099,90" e "R$ 1.234,56"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return None
    raw = re.sub(r"[^\d,.\-]", "", raw)
    if not raw:
        return None
    if "," in raw:
        # pt-BR classico: ponto e milhar, virgula e decimal ("3.150.099,90").
        raw = raw.replace(".", "").replace(",", ".")
    elif "." in raw:
        # So ponto: ambiguo. "700.000" e setecentos mil; "1234.56" e decimal.
        # Se todos os grupos depois do primeiro ponto tem exatamente 3 digitos,
        # o ponto e separador de milhar. Sem essa regra, "R$ 700.000" vira 700.
        grupos = raw.split(".")
        if len(grupos) > 1 and all(len(g) == 3 for g in grupos[1:]):
            raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    if isinstance(value, list):
        value = value[0] if value else None
    f = _to_float(value)
    return int(f) if f is not None else None


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url, re.IGNORECASE)
    if not m:
        return ""
    return m.group(1).lower().removeprefix("www.")


def _own_sites() -> set[str]:
    """Dominios considerados "da casa" (client.yaml > realestate.own_sites)."""
    try:
        data = load_client_data() or {}
    except Exception:
        return set()
    raw = ((data.get("realestate") or {}).get("own_sites")) or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(d).strip().lower().removeprefix("www.") for d in raw if str(d).strip()}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

# Status que significam "o anuncio nao existe mais" — a resposta e devolvida ao
# parser (em vez de virar None) para o bot poder dizer "esse ja foi vendido".
# Auxiliadora e Foxter devolvem 404; a OLX devolve 410 (Gone). Qualquer outro
# status fora do 200 e falha de acesso, nao prova de que o imovel saiu do ar.
_STATUS_REMOVIDO = {404, 410}


async def _get(url: str, headers: Optional[dict] = None) -> Optional[Any]:
    """GET com fingerprint de navegador. Devolve a resposta ou None.

    Devolve a resposta em 200 e tambem nos status de `_STATUS_REMOVIDO`; o
    parser decide o que fazer. A resposta expoe `.text`, `.json()` e
    `.status_code` tanto no curl_cffi quanto no httpx.
    """
    hdrs = {"User-Agent": _UA, "Accept-Language": "pt-BR,pt;q=0.9"}
    if headers:
        hdrs.update(headers)
    try:
        if _CurlSession is not None:
            async with _CurlSession(impersonate=_IMPERSONATE) as session:
                resp = await session.get(url, headers=hdrs, timeout=_HTTP_TIMEOUT)
        else:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT, follow_redirects=True, headers=hdrs
            ) as client:
                resp = await client.get(url)
    except Exception as e:
        logger.warning("Falha ao buscar %s: %s", url, e)
        return None
    if resp.status_code != 200 and resp.status_code not in _STATUS_REMOVIDO:
        logger.info("Site %s respondeu HTTP %s", url, resp.status_code)
        return None
    return resp


def _ld_json_blocks(html: str) -> list[Any]:
    """Todos os blocos <script type="application/ld+json"> ja parseados."""
    out: list[Any] = []
    for raw in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        try:
            out.append(json.loads(raw.strip()))
        except (ValueError, TypeError):
            continue
    return out


def _walk_ld(blocks: list[Any], wanted: set[str]) -> Optional[dict]:
    """Procura, em profundidade, o primeiro no com @type em `wanted`."""
    stack: list[Any] = list(blocks)
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if any(t in wanted for t in types if t):
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


# --------------------------------------------------------------------------
# Parsers por portal
# --------------------------------------------------------------------------

async def _parse_auxiliadora(url: str) -> Optional[Imovel]:
    resp = await _get(url)
    if resp is None:
        return None
    if resp.status_code in _STATUS_REMOVIDO:
        return Imovel(fonte="Auxiliadora Predial", url=url, indisponivel=True)
    blocks = _ld_json_blocks(resp.text)
    node = _walk_ld(blocks, {"RealEstateListing"})
    if not node:
        return None

    im = Imovel(fonte="Auxiliadora Predial", url=url)
    im.codigo = str(node.get("identifier") or "")
    im.titulo = _strip_html(node.get("name") or "")

    for prop in node.get("additionalProperty") or []:
        if not isinstance(prop, dict):
            continue
        nome = (prop.get("name") or "").strip().lower()
        valor = prop.get("value")
        if nome.startswith("suíte") or nome.startswith("suite"):
            im.suites = _to_int(valor)
        elif "vaga" in nome:
            im.vagas = _to_int(valor)
        elif "negócio" in nome or "negocio" in nome:
            im.finalidade = str(valor or "").strip()

    offer = _first(node.get("offers")) or {}
    if isinstance(offer, dict):
        im.preco = _to_float(offer.get("price"))
        item = offer.get("itemOffered") or {}
        if isinstance(item, dict):
            im.tipo = (item.get("accommodationCategory") or "").strip()
            im.quartos = _to_int(item.get("numberOfRooms"))
            im.banheiros = _to_int(item.get("numberOfBathroomsTotal"))
            floor = item.get("floorSize") or {}
            if isinstance(floor, dict) and floor.get("value") is not None:
                im.area = f"{_to_int(floor.get('value'))} m²"
            addr = item.get("address") or {}
            if isinstance(addr, dict):
                im.endereco = (addr.get("streetAddress") or "").strip()
                im.cidade = (addr.get("addressLocality") or "").strip()
                im.uf = (addr.get("addressRegion") or "").strip()
        for spec in offer.get("priceSpecification") or []:
            if not isinstance(spec, dict):
                continue
            nome = (spec.get("name") or "").strip().lower()
            if "condom" in nome:
                im.condominio = _to_float(spec.get("price"))
            elif "iptu" in nome:
                im.iptu = _to_float(spec.get("price"))
        seller = offer.get("seller") or node.get("provider") or {}
        if isinstance(seller, dict):
            im.corretor = (seller.get("name") or "").strip()

    page = _walk_ld(blocks, {"WebPage"}) or {}
    im.descricao = _clip(_strip_html(page.get("description") or ""), 420)
    # O bairro nao vem em campo proprio; o titulo da pagina traz ("... em X, Y.").
    m = re.search(r"\bem\s+([^,]+),", _strip_html(page.get("name") or im.titulo))
    if m:
        im.bairro = m.group(1).strip()
    return im


async def _parse_foxter(url: str) -> Optional[Imovel]:
    resp = await _get(url)
    if resp is None:
        return None
    if resp.status_code in _STATUS_REMOVIDO:
        return Imovel(fonte="Foxter Cia. Imobiliária", url=url, indisponivel=True)
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    product = (((data.get("props") or {}).get("pageProps") or {}).get("product")) or {}
    if not product:
        return None
    # A Foxter mantem a pagina no ar e marca o imovel como indisponivel.
    if product.get("unavailable"):
        return Imovel(
            fonte="Foxter Cia. Imobiliária", url=url, indisponivel=True,
            codigo=str(product.get("code") or ""),
        )

    im = Imovel(fonte="Foxter Cia. Imobiliária", url=url)
    im.codigo = str(product.get("code") or "")
    im.titulo = _strip_html(product.get("h1") or product.get("title") or "")
    im.tipo = (product.get("type") or "").strip()
    im.finalidade = "Venda" if product.get("saleValue") else ""
    im.preco = _to_float(product.get("saleValue"))
    im.iptu = _to_float(product.get("iptu"))
    im.condominio = _to_float(product.get("condominiumAmountValue"))
    im.quartos = _to_int(product.get("bedroomsNumber"))
    im.suites = _to_int(product.get("suites"))
    im.banheiros = _to_int(product.get("bathrooms"))
    im.vagas = _to_int(product.get("parkingSpaces"))
    area = product.get("areaPrivate") or product.get("areaTotal")
    if area:
        im.area = f"{_to_int(area)} m²"
    im.bairro = (product.get("district") or "").strip()
    im.cidade = (product.get("city") or "").strip()
    im.uf = (product.get("state") or "").strip()
    rua = " ".join(
        p for p in [
            (product.get("placeType") or "").strip(),
            (product.get("place") or "").strip(),
            (product.get("placeNumber") or "").strip(),
        ] if p
    )
    im.endereco = rua
    im.descricao = _clip(_strip_html(product.get("description") or ""), 420)
    return im


_GLUE_FIELDS = (
    "search(result(listings(listing("
    "id,title,description,unitTypes,usableAreas,totalAreas,bedrooms,bathrooms,"
    "suites,parkingSpaces,pricingInfos,address),account(name))))"
)

_UNIT_TYPES = {
    "APARTMENT": "Apartamento",
    "HOME": "Casa",
    "CONDOMINIUM": "Casa de condomínio",
    "PENTHOUSE": "Cobertura",
    "KITNET": "Kitnet",
    "FLAT": "Flat",
    "BUSINESS": "Comercial",
    "OFFICE": "Sala comercial",
    "ALLOTMENT_LAND": "Terreno",
    "FARM": "Sítio/Chácara",
}


async def _parse_glue(url: str, fonte: str, x_domain: str) -> Optional[Imovel]:
    """Viva Real e ZAP: consulta a API interna pelo id que vem na URL.

    A pagina HTML desses portais fica atras de Cloudflare e devolve 404 forjado
    para cliente que nao e navegador — por isso NAO tentamos ler o HTML aqui.
    """
    m = re.search(r"id-(\d{5,})", url) or re.search(r"/(\d{7,})/?$", url)
    if not m:
        return None
    listing_id = m.group(1)
    api = (
        "https://glue-api.vivareal.com/v2/listings"
        f"?id={listing_id}&includeFields={_GLUE_FIELDS}"
    )
    resp = await _get(api, headers={"x-domain": x_domain})
    if resp is None:
        return None
    try:
        payload = resp.json()
        entries = payload["search"]["result"]["listings"]
    except (ValueError, KeyError, TypeError):
        return None
    if not entries:
        # O id veio da propria URL do anuncio: nenhum registro significa que o
        # anuncio saiu do ar (tipicamente vendido/alugado), nao erro de leitura.
        return Imovel(fonte=fonte, url=url, indisponivel=True, codigo=listing_id)

    entry = entries[0]
    listing = entry.get("listing") or {}
    im = Imovel(fonte=fonte, url=url)
    im.codigo = str(listing.get("id") or listing_id)
    im.titulo = _strip_html(listing.get("title") or "")
    unit = _first(listing.get("unitTypes"))
    im.tipo = _UNIT_TYPES.get(str(unit or "").upper(), str(unit or "").title())
    im.quartos = _to_int(listing.get("bedrooms"))
    im.banheiros = _to_int(listing.get("bathrooms"))
    im.suites = _to_int(listing.get("suites"))
    im.vagas = _to_int(listing.get("parkingSpaces"))
    area = _first(listing.get("usableAreas")) or _first(listing.get("totalAreas"))
    if area:
        im.area = f"{_to_int(area)} m²"

    pricing = _first(listing.get("pricingInfos")) or {}
    if isinstance(pricing, dict):
        im.preco = _to_float(pricing.get("price"))
        im.condominio = _to_float(pricing.get("monthlyCondoFee"))
        im.iptu = _to_float(pricing.get("iptu"))
        im.finalidade = "Venda" if pricing.get("businessType") == "SALE" else "Aluguel"

    addr = listing.get("address") or {}
    if isinstance(addr, dict):
        rua = " ".join(
            p for p in [
                (addr.get("street") or "").strip(),
                (addr.get("streetNumber") or "").strip(),
            ] if p
        )
        im.endereco = rua
        im.bairro = (addr.get("neighborhood") or "").strip()
        im.cidade = (addr.get("city") or "").strip()
        im.uf = (addr.get("stateAcronym") or addr.get("state") or "").strip()

    account = entry.get("account") or {}
    if isinstance(account, dict):
        im.corretor = (account.get("name") or "").strip()
    im.descricao = _clip(_strip_html(listing.get("description") or ""), 420)
    return im


async def _parse_vivareal(url: str) -> Optional[Imovel]:
    return await _parse_glue(url, "Viva Real", "www.vivareal.com.br")


async def _parse_zap(url: str) -> Optional[Imovel]:
    return await _parse_glue(url, "ZAP Imóveis", "www.zapimoveis.com.br")


def _json_array_at(text: str, start: int) -> Optional[list]:
    """Fatia o array JSON que comeca em `start` contando colchetes."""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return None
    return None


_OLX_TIPOS = {"casas": "Casa", "apartamentos": "Apartamento", "terrenos": "Terreno",
              "sítios": "Sítio/Chácara", "sitios": "Sítio/Chácara"}


async def _parse_olx(url: str) -> Optional[Imovel]:
    resp = await _get(url)
    if resp is None:
        return None
    # A OLX devolve 410 Gone quando o anuncio foi encerrado pelo anunciante.
    if resp.status_code in _STATUS_REMOVIDO:
        return Imovel(fonte="OLX", url=url, indisponivel=True)

    im = Imovel(fonte="OLX", url=url)
    blocks = _ld_json_blocks(resp.text)
    node = _walk_ld(blocks, {"Product"}) or {}
    if not node:
        action = _walk_ld(blocks, {"BuyAction"}) or {}
        node = action.get("Object") or action.get("object") or {}
    if isinstance(node, dict):
        im.titulo = _strip_html(node.get("name") or "")
        im.descricao = _clip(_strip_html(node.get("description") or ""), 420)

    m = re.search(r"-(\d{8,})/?$", url)
    if m:
        im.codigo = m.group(1)

    # O restante da ficha vem do estado da pagina, embutido com entidades HTML
    # (&quot;). Depois do unescape vira JSON legivel por regex/slice.
    raw = html_unescape(resp.text)

    mp = re.search(r'"priceValue"\s*:\s*"([^"]+)"', raw)
    if mp:
        im.preco = _to_float(mp.group(1))
    if im.preco is None:
        mp2 = re.search(r'"price"\s*:\s*"?(\d{4,})"?', raw)
        if mp2:
            im.preco = _to_float(mp2.group(1))

    mb = re.search(r'"neighbourhood"\s*:\s*"([^"]*)"', raw)
    if mb:
        im.bairro = mb.group(1).strip()
    mc = re.search(r'"municipality"\s*:\s*"([^"]*)"', raw)
    if mc:
        im.cidade = mc.group(1).strip()
    mu = re.search(r'"state"\s*:\s*"([A-Z]{2})"', raw)
    if mu:
        im.uf = mu.group(1)

    idx = raw.find('"properties":[')
    props = _json_array_at(raw, raw.index("[", idx)) if idx >= 0 else None
    for prop in props or []:
        if not isinstance(prop, dict):
            continue
        nome = (prop.get("name") or "").strip().lower()
        valor = prop.get("value")
        if nome == "category":
            im.tipo = _OLX_TIPOS.get(str(valor or "").strip().lower(), str(valor or "").strip())
        elif nome == "real_estate_type":
            texto = str(valor or "")
            if re.search(r"alug|loca", texto, re.I):
                im.finalidade = "Aluguel"
            elif re.search(r"venda", texto, re.I):
                im.finalidade = "Venda"
        elif nome == "condominio":
            im.condominio = _to_float(valor)
        elif nome == "iptu":
            im.iptu = _to_float(valor)
        elif nome == "size":
            area = _to_int(valor)
            if area:
                im.area = f"{area} m²"
        elif nome == "rooms":
            im.quartos = _to_int(valor)
        elif nome == "bathrooms":
            im.banheiros = _to_int(valor)
        elif nome in ("suites", "suítes"):
            im.suites = _to_int(valor)
        elif nome == "garage_spaces":
            im.vagas = _to_int(valor)
        elif nome == "re_features" and valor:
            im.extras["Detalhes"] = _clip(str(valor), 200)
    return im


# dominio -> (nome legivel, parser)
_SITES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "auxiliadorapredial.com.br": ("Auxiliadora Predial", _parse_auxiliadora),
    "foxterciaimobiliaria.com.br": ("Foxter Cia. Imobiliária", _parse_foxter),
    "vivareal.com.br": ("Viva Real", _parse_vivareal),
    "zapimoveis.com.br": ("ZAP Imóveis", _parse_zap),
    "olx.com.br": ("OLX", _parse_olx),
}


def _site_for(url: str) -> Optional[tuple[str, Callable[[str], Any]]]:
    dom = _domain(url)
    if not dom:
        return None
    for known, entry in _SITES.items():
        # Cobre subdominios (ex.: ba.olx.com.br, www.vivareal.com.br).
        if dom == known or dom.endswith("." + known):
            return entry
    return None


# --------------------------------------------------------------------------
# API publica
# --------------------------------------------------------------------------

def find_property_urls(text: str) -> list[str]:
    """URLs de anuncio (dos portais suportados) presentes na mensagem."""
    seen: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:)]}»\"'")
        if _site_for(url) and url not in seen:
            seen.append(url)
        if len(seen) >= _MAX_URLS:
            break
    return seen


def find_unsupported_urls(text: str) -> list[str]:
    """URLs que parecem anuncio de imovel mas de site que nao sabemos ler.

    Serve para o bot avisar ("nao consigo abrir esse link") em vez de ignorar
    o link em silencio e perguntar algo que o lead ja respondeu mandando a URL.
    """
    out: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:)]}»\"'")
        if _site_for(url):
            continue
        dom = _domain(url)
        if dom and url not in out:
            out.append(url)
    return out[:_MAX_URLS]


def _cache_key(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    return f"{settings.PROJECT_SLUG}:imovel:{digest}"


async def _cache_get(url: str) -> Optional[Imovel]:
    try:
        r = await get_redis()
        raw = await r.get(_cache_key(url))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return Imovel(**json.loads(raw))
    except (ValueError, TypeError):
        return None


async def _cache_set(url: str, imovel: Imovel) -> None:
    try:
        r = await get_redis()
        await r.set(
            _cache_key(url),
            json.dumps(imovel.__dict__, ensure_ascii=False),
            ex=_CACHE_TTL,
        )
    except Exception:
        logger.debug("Falha ao cachear ficha de %s", url, exc_info=True)


async def fetch_imovel(url: str) -> Optional[Imovel]:
    """Busca a ficha de um anuncio. None se o site nao e suportado ou falhou."""
    site = _site_for(url)
    if not site:
        return None
    cached = await _cache_get(url)
    if cached is not None:
        return cached if cached.is_useful() else None

    _fonte, parser = site
    try:
        imovel = await parser(url)
    except Exception:
        logger.exception("Erro ao ler ficha do imovel em %s", url)
        return None
    if imovel is None or not imovel.is_useful():
        return None

    dom = _domain(url)
    proprios = _own_sites()
    imovel.da_casa = dom in proprios or any(dom.endswith("." + d) for d in proprios)
    await _cache_set(url, imovel)
    return imovel


def _render_ficha(im: Imovel) -> str:
    linhas: list[str] = []

    def add(rotulo: str, valor: Any) -> None:
        if valor in (None, "", 0):
            return
        linhas.append(f"- {rotulo}: {valor}")

    add("Tipo", im.tipo)
    add("Operação", im.finalidade)
    add("Código do anúncio", im.codigo)
    local = ", ".join(p for p in [im.endereco, im.bairro, im.cidade, im.uf] if p)
    add("Endereço", local)
    add("Valor", _brl(im.preco))
    add("Condomínio", _brl(im.condominio))
    add("IPTU", _brl(im.iptu))
    add("Área", im.area)
    add("Quartos", im.quartos)
    add("Suítes", im.suites)
    add("Banheiros", im.banheiros)
    add("Vagas de garagem", im.vagas)
    if im.descricao:
        add("Descrição do anúncio", im.descricao)
    return "\n".join(linhas)


def _render_perfil(im: Imovel) -> str:
    """Versao reduzida, para anuncio de terceiro: so o perfil de busca."""
    partes: list[str] = []
    if im.tipo:
        partes.append(im.tipo)
    if im.quartos:
        partes.append(f"{im.quartos} quarto(s)")
    if im.area:
        partes.append(im.area)
    local = ", ".join(p for p in [im.bairro, im.cidade] if p)
    if local:
        partes.append(f"em {local}")
    if im.preco:
        partes.append(f"faixa de {_brl(im.preco)}")
    if im.finalidade:
        partes.append(f"para {im.finalidade.lower()}")
    return " | ".join(partes)


def build_block(
    imoveis: list[Imovel], nao_suportados: list[str] | None = None
) -> str:
    """Monta o bloco de contexto injetado antes da mensagem do lead."""
    if not imoveis and not nao_suportados:
        return ""

    secoes: list[str] = []
    for im in imoveis:
        if im.indisponivel:
            ref = f" (código {im.codigo})" if im.codigo else ""
            secoes.append(
                f"ANÚNCIO FORA DO AR — o imóvel deste link{ref} não está mais "
                f"anunciado em {im.fonte}. Na prática isso quase sempre significa "
                "que JÁ FOI VENDIDO. Avise o lead disso com naturalidade (sem "
                "lamentar demais) e, no mesmo turno, pergunte as características "
                "do que ele procura (tipo, bairro, quantos quartos e faixa de "
                "valor) para você oferecer opções. É PROIBIDO descrever este "
                "imóvel, citar valor ou inventar qualquer dado sobre ele."
            )
        elif im.da_casa:
            secoes.append(
                "IMÓVEL IDENTIFICADO PELO LINK — ANÚNCIO DA NOSSA IMOBILIÁRIA "
                f"({im.fonte}). Esta ficha é AUTORITATIVA: responda as dúvidas do "
                "lead sobre este imóvel usando SOMENTE os dados abaixo, seguindo o "
                "SCRIPT — IMÓVEL JÁ CADASTRADO. NÃO pergunte de qual imóvel ele "
                "fala — você já sabe.\n" + _render_ficha(im)
            )
        else:
            secoes.append(
                f"LINK DE ANÚNCIO DE OUTRA IMOBILIÁRIA ({im.fonte}) — USO INTERNO, "
                "NÃO RECITE ESTA FICHA. Não detalhe este imóvel, não cite valor, "
                "endereço exato, corretor nem o nome do concorrente. Use apenas o "
                "perfil abaixo para dizer que não trabalhamos com esse anúncio e "
                "oferecer imóveis parecidos da nossa carteira.\n"
                f"- Perfil que o lead procura: {_render_perfil(im)}"
            )

    for url in nao_suportados or []:
        secoes.append(
            "LINK QUE NÃO CONSEGUIMOS ABRIR — avise ao lead que não conseguiu "
            "acessar esse link e peça, em UMA pergunta só, as características do "
            "imóvel que ele procura (tipo, bairro, quantos quartos e faixa de "
            "valor). NÃO peça o código do anúncio e NÃO invente nenhum dado "
            "sobre esse imóvel. Se mesmo assim o lead insistir em detalhes DESTE "
            "imóvel específico, não improvise: encaminhe para o atendimento "
            "humano ([TRANSFERIR=1]).\n"
            f"- Link enviado: {url}"
        )

    corpo = "\n\n".join(secoes)
    return (
        "[CONTEXTO DO SISTEMA — o lead enviou um link. Não responda sobre este "
        "bloco nem mencione que recebeu um \"contexto\"; apenas use as "
        f"informações.\n\n{corpo}]\n\n"
    )


async def build_context_block(text: str) -> str:
    """Pipeline completo: acha links na mensagem e devolve o bloco pronto.

    Devolve "" quando nao ha link algum — custo zero no caminho comum.
    """
    urls = find_property_urls(text)
    outros = find_unsupported_urls(text)
    if not urls and not outros:
        return ""

    imoveis: list[Imovel] = []
    if urls:
        results = await asyncio.gather(
            *(fetch_imovel(u) for u in urls), return_exceptions=True
        )
        for res in results:
            if isinstance(res, Imovel):
                imoveis.append(res)
            elif isinstance(res, BaseException):
                logger.warning("Leitura de imovel falhou: %s", res)

    # Link de portal suportado que falhou na leitura tambem vira "nao consegui abrir".
    lidos = {im.url for im in imoveis}
    falhos = [u for u in urls if u not in lidos]
    return build_block(imoveis, outros + falhos)
