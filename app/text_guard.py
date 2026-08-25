"""Rede de seguranca contra vazamento de marcador de controle para o lead.

O prompt manda a IA emitir marcadores como [TRANSFERIR=1], [NOME=Aline] ou
[FOTO_1]. Eles sao instrucoes para o sistema — o lead NUNCA pode ve-los. Em
25/08/2026 dois escaparam num atendimento real de um cliente derivado deste
template ("[ORIGEM=]" e "[IMAGEM_FOTOS_1]"), e a auditoria mostrou tres
buracos que continuavam abertos aqui:

- resposta composta so por marcador caia no fallback `parts = [texto cru]` e
  ia inteira para o WhatsApp;
- o bloco "[CONTEXTO DO SISTEMA ...]" injetado a cada turno tem espacos e
  acento, entao escapava do scrub por caixa alta;
- texto colado numa tag de midia era descartado junto com a tag, e a segunda
  tag do mesmo bloco nunca virava envio.

Por isso a limpeza acontece em duas camadas: `_parse_ai_response` trata cada
marcador conhecido (e converte a tag de midia em envio real), e
`strip_control_markers` varre o que sobrou — inclusive marcador novo que o
modelo tenha inventado ou escrito de forma malformada.
"""
import re

# [NOME], [NOME=valor] e [NOME=], com nome em CAIXA ALTA, digitos e underscore.
# Texto normal do bot nunca usa colchete com palavra em caixa alta, entao o
# risco de apagar conteudo legitimo e nulo. O valor nao cruza quebra de linha
# para nao engolir um paragrafo inteiro quando o colchete ficou sem fechar.
_CONTROL_MARKER_RE = re.compile(r"\[\s*[A-Z][A-Z0-9_]+\s*(?:=[^\]\n]*)?\]")

# Bloco de contexto temporal injetado em cada turno (app/services/gemini.py).
# Tem espacos e acento, entao escapa do regex acima — se o modelo ecoar o
# bloco, o lead veria a instrucao interna inteira.
_SYSTEM_CONTEXT_RE = re.compile(r"\[\s*CONTEXTO DO SISTEMA[^\]]*\]", re.IGNORECASE)


def strip_control_markers(text: str) -> str:
    """Remove marcadores de controle e normaliza o espaco que sobrou."""
    if not text:
        return ""
    cleaned = _SYSTEM_CONTEXT_RE.sub("", text)
    cleaned = _CONTROL_MARKER_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def has_control_markers(text: str) -> bool:
    """True se ainda restar marcador no texto — usado para alertar no log."""
    if not text:
        return False
    return bool(_CONTROL_MARKER_RE.search(text) or _SYSTEM_CONTEXT_RE.search(text))
