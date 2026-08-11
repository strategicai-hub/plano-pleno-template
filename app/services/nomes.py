"""
Origem do nome do contato — trava unica do bot.

REGRA DE NEGOCIO: o bot so pode chamar o contato pelo nome que o PROPRIO
contato forneceu — na conversa (flag [NOME=...]) ou no cadastro que ele mesmo
preencheu na origem externa do lead. O nome do perfil do WhatsApp
(push_name / senderName) NUNCA pode virar vocativo.

Por que o push_name e proibido: ele e o apelido que o dono do chip escolheu
para si, nao o nome dele. Numa base real de 104 leads (erica-vieira, 11/08/2026)
apareceram como "nome do lead": "Tutu", "ZAP 2 tim", "Autoescola Prioridade",
"Deus e fiel", "maurodesasobral", "🖤❤️", "😎". Chamar o contato assim e erro
grave e gerou reclamacao do cliente.

Este modulo e o ponto UNICO por onde qualquer canal (chat reativo, reativacao,
lembrete de agendamento, 1o contato, planilha) resolve o nome. Nenhum canal
deve ler `lead["nome"]` cru — foi exatamente por ler o campo cru que a
reativacao continuou errando depois do primeiro fix.
"""
import re
import unicodedata

# Nome completo brasileiro chega facil a 5 palavras ("Maria Aparecida Bahia de
# Queiroz"). Acima disso e frase, nao nome.
_MAX_PALAVRAS = 5
# Nome proprio de pessoa dificilmente passa de 12 letras numa palavra so
# ("Maximiliano" = 11). Acima disso e quase sempre nome de perfil grudado
# ("maurodesasobral", "aleixoclaudio").
_MAX_LETRAS_POR_PALAVRA = 12

# Pontuacao que nao aparece em nome de pessoa mas e comum em nome de perfil.
_PONTUACAO_PROIBIDA = set("()[]{}<>|/\\*#%$&+=_~^\"")

# Palavras que denunciam perfil comercial/operadora em vez de pessoa.
_TERMOS_NAO_PESSOA = {
    "zap", "whats", "whatsapp", "tim", "vivo", "claro", "oi", "novo", "nova",
    "loja", "lojas", "autoescola", "auto", "escola", "garage", "garagem",
    "oficina", "salao", "studio", "estudio", "atelier", "barbearia", "boutique",
    "adm", "admin", "contato", "comercial", "vendas", "financeiro", "suporte",
    "delivery", "market", "mercado", "distribuidora", "transportes", "servicos",
    "imoveis", "corretor", "corretora", "seguros", "clinica", "consultorio",
    "academia", "pizzaria", "lanchonete", "restaurante", "bar", "petshop",
    "nails", "designer", "desagner", "makeup", "esteticista", "personal",
    "trabalho", "cliente", "amigo", "amiga", "casa", "empresa", "grupo",
    # "deus"/"fiel" barram status de perfil ("Deus e fiel"). "Jesus" NAO entra:
    # e sobrenome comum no Brasil ("Vilma Aparecida de Jesus").
    "deus", "fiel", "abencoado", "abencoada",
}


def sem_acento(txt: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", txt)
        if unicodedata.category(c) != "Mn"
    )


# Alias interno (uso historico do modulo).
_sem_acento = sem_acento


def mesmo_nome(a: str, b: str) -> bool:
    """Compara nomes ignorando acento e caixa ("Laíse" == "Laise")."""
    return bool(a) and bool(b) and sem_acento(a).lower() == sem_acento(b).lower()


def eh_nome_de_pessoa(valor: str) -> bool:
    """True se `valor` tem cara de nome proprio de pessoa.

    Segunda linha de defesa: mesmo que um nome ruim escape da origem (cadastro
    da geradora mal preenchido, registro legado), ele nao vira vocativo.
    """
    txt = " ".join((valor or "").strip().split())
    if len(txt) < 2:
        return False
    if any(ch.isdigit() for ch in txt):
        return False
    low = txt.lower()
    if any(t in low for t in ("http", "www.", "@", ".com", ".br")):
        return False
    if any(ch in _PONTUACAO_PROIBIDA for ch in txt):
        return False
    # Emoji, simbolo grafico ou caractere nao atribuido.
    if any(unicodedata.category(ch) in ("So", "Sk", "Cs", "Co", "Cn") for ch in txt):
        return False

    palavras = txt.split(" ")
    if len(palavras) > _MAX_PALAVRAS:
        return False
    for palavra in palavras:
        limpo = re.sub(r"[^\w'’\-]", "", palavra, flags=re.UNICODE)
        if not limpo or not any(ch.isalpha() for ch in limpo):
            return False
        if _sem_acento(limpo).lower() in _TERMOS_NAO_PESSOA:
            return False
        if len(limpo) > _MAX_LETRAS_POR_PALAVRA and not {"-", "'", "’"} & set(limpo):
            return False
    return True


def primeiro_nome(valor: str) -> str:
    """Primeiro nome normalizado para vocativo ("MARCOS FERNANDO" -> "Marcos")."""
    partes = " ".join((valor or "").strip().split()).split(" ")
    return partes[0].title() if partes and partes[0] else ""


def nome_para_vocativo(nome_confirmado: str = "", nome_cadastro: str = "") -> str:
    """Precedencia unica do nome usavel. Retorna "" quando nao ha nome confiavel.

    1. `nome_confirmado` — o contato escreveu na conversa (flag [NOME=...]).
    2. `nome_cadastro`   — o contato preencheu no formulario de origem do lead.
    3. "" — nao sabemos o nome; o bot nao usa vocativo (e pergunta, se couber).

    O push_name do WhatsApp nao entra em nenhum dos dois — ele nao e persistido.
    """
    for candidato in (nome_confirmado, nome_cadastro):
        if eh_nome_de_pessoa(candidato):
            return primeiro_nome(candidato)
    return ""


def nome_do_lead(lead: dict | None) -> str:
    """Resolve o vocativo a partir do registro do lead (Redis ou SQLite).

    Aceita as duas nomenclaturas porque o hash do Redis usa `name`/`name_cadastro`
    e a tabela SQLite usa `nome`/`nome_cadastro`.
    """
    lead = lead or {}
    return nome_para_vocativo(
        lead.get("nome") or lead.get("name") or "",
        lead.get("nome_cadastro") or lead.get("name_cadastro") or "",
    )
