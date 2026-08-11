"""
Trava de regressao da origem do nome do contato.

Caso real que originou estes testes (11/08/2026): o lead 5521992728866 tinha
"MARCOS FERNANDO DE TURETTA" no cadastro da geradora e "Tutu" como nome do
perfil do WhatsApp. O push_name venceu, foi persistido como nome do lead, e a
mensagem de reativacao saiu como "Oi Tutu".
"""
import pytest

from app.consumer import _extract_nome_flag
from app.services import nomes


@pytest.mark.parametrize(
    "valor",
    [
        "Marcos",
        "Maria Aparecida",
        "Ana Cristina Silva",
        "José",
        "Luiz-Felipe",
        "D'Angelo",
        "MARCOS FERNANDO DE TURETTA",          # nome completo em caixa alta
        "Maria Aparecida Bahia de Queiroz",    # 5 palavras
        "Vilma Aparecida de Jesus",            # "Jesus" e sobrenome, nao termo comercial
    ],
)
def test_aceita_nome_de_pessoa(valor):
    assert nomes.eh_nome_de_pessoa(valor) is True


@pytest.mark.parametrize(
    "valor",
    [
        "",
        " ",
        "A",
        "🖤❤️",                       # perfil so com emoji
        "😎",
        "ZAP 2 tim",                  # tem digito e termo de operadora
        "Autoescola Prioridade",      # perfil comercial
        "Wagner Garage",
        "ana Cristina nails desagner",  # 4 palavras + termos de negocio
        "Deus é fiel",
        "maurodesasobral",            # nome de perfil grudado
        "aleixoclaudio aleixo",
        "Alberto(Beto Jesus)",        # pontuacao de perfil
        "contato@empresa.com",
        "quero saber quanto custa o plano",  # frase, nao nome
    ],
)
def test_rejeita_o_que_nao_e_nome_de_pessoa(valor):
    assert nomes.eh_nome_de_pessoa(valor) is False


def test_mesmo_nome_ignora_acento_e_caixa():
    assert nomes.mesmo_nome("Laíse", "Laise") is True
    assert nomes.mesmo_nome("CÁTIA", "catia") is True
    assert nomes.mesmo_nome("Laíse", "Marcos") is False
    assert nomes.mesmo_nome("", "Laise") is False


def test_primeiro_nome_normaliza():
    assert nomes.primeiro_nome("MARCOS FERNANDO DE TURETTA") == "Marcos"
    assert nomes.primeiro_nome("  maria  aparecida ") == "Maria"
    assert nomes.primeiro_nome("") == ""


def test_precedencia_confirmado_vence_cadastro():
    assert nomes.nome_para_vocativo("Laise", "MARCOS FERNANDO") == "Laise"


def test_precedencia_cai_para_cadastro_quando_nao_ha_confirmado():
    assert nomes.nome_para_vocativo("", "MARCOS FERNANDO DE TURETTA") == "Marcos"


def test_precedencia_ignora_lixo_e_devolve_vazio():
    assert nomes.nome_para_vocativo("🖤❤️", "ZAP 2 tim") == ""


def test_caso_real_marcos_usa_o_nome_do_cadastro():
    """Estado do lead 5521992728866 depois da separacao de campos.

    "Tutu" (push_name) deixou de ser persistido, entao o campo de nome
    confirmado fica vazio e o vocativo vem do cadastro que o proprio lead
    preencheu na geradora.
    """
    lead_sqlite = {"nome": "", "nome_cadastro": "MARCOS FERNANDO DE TURETTA"}
    assert nomes.nome_do_lead(lead_sqlite) == "Marcos"

    lead_redis = {"name": "", "name_cadastro": "MARCOS FERNANDO DE TURETTA"}
    assert nomes.nome_do_lead(lead_redis) == "Marcos"


def test_nome_confirmado_na_conversa_sobrepoe_o_cadastro():
    """Lead cadastrado como "Laise Souza" que se apresenta como outra coisa."""
    lead = {"nome": "Laíse", "nome_cadastro": "Laise Souza"}
    assert nomes.nome_do_lead(lead) == "Laíse"


def test_lead_sem_nenhum_nome_confiavel_fica_sem_vocativo():
    assert nomes.nome_do_lead({"nome": "Wagner Garage"}) == ""
    assert nomes.nome_do_lead({}) == ""
    assert nomes.nome_do_lead(None) == ""


def test_flag_nome_valida_grava_primeiro_nome():
    texto, nome = _extract_nome_flag("Prazer, Laise! [NOME=Laise Souza]")
    assert nome == "Laise"
    assert "[NOME=" not in texto


def test_flag_nome_invalida_e_descartada():
    _, nome_emoji = _extract_nome_flag("Ok [NOME=🖤❤️]")
    assert nome_emoji == ""

    _, nome_perfil = _extract_nome_flag("Ok [NOME=Autoescola Prioridade]")
    assert nome_perfil == ""


def test_resposta_sem_flag_fica_intacta():
    texto, nome = _extract_nome_flag("Bom dia! Como posso ajudar?")
    assert nome == ""
    assert texto == "Bom dia! Como posso ajudar?"
