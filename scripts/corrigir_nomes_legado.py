#!/usr/bin/env python3
"""
Limpeza retroativa dos nomes contaminados pelo push_name do WhatsApp.

CONTEXTO: ate 10/08/2026 o consumer gravava o push_name (nome do perfil do
WhatsApp) como nome do lead, no Redis e no SQLite. O fix daquele dia fechou a
porta de entrada, mas os registros ja gravados continuaram — e os canais
proativos (reativacao, lembrete) leem exatamente esses registros. Resultado:
em 11/08 a reativacao ainda saiu chamando o lead 5521992728866 de "Tutu"
(nome do perfil) em vez de Marcos (nome do cadastro).

O QUE ESTE SCRIPT FAZ, por lead:
  1. Move o nome do cadastro da geradora (lead_dispatch_queue.nome) para a
     coluna nova `leads.nome_cadastro` e para o campo `name_cadastro` do Redis.
  2. Zera `leads.nome` / `lead:name` — nenhum registro anterior a este fix teve
     o nome confirmado pelo contato na conversa, entao nenhum deles pode ser
     tratado como confirmado. O bot volta a nao usar vocativo (ou usa o do
     cadastro, pela precedencia de app/services/nomes.py) e pergunta o nome
     quando fizer sentido.

  Excecao: se `leads.nome` ja for igual ao primeiro nome do cadastro, o valor
  e mantido — e o mesmo nome que a precedencia devolveria de qualquer forma.

SEGURANCA: roda em SIMULACAO por padrao. Nada e gravado sem `--aplicar`.

USO (dentro do container do worker):
    python scripts/corrigir_nomes_legado.py            # simula e mostra o plano
    python scripts/corrigir_nomes_legado.py --aplicar  # grava
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import redis as redis_sync  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import nomes  # noqa: E402
from app.services import redis_keys as keys  # noqa: E402


def _cadastro_por_telefone(con: sqlite3.Connection) -> dict[str, str]:
    """Nome do cadastro (o que o lead preencheu na geradora) por telefone.

    Considera as duas formas do numero (com e sem o 9o digito), porque o
    disparo pode ter sido cadastrado numa forma e o lead responder na outra.
    """
    mapa: dict[str, str] = {}
    cur = con.execute(
        "SELECT phone, nome FROM lead_dispatch_queue "
        "WHERE COALESCE(nome,'') <> '' ORDER BY created_at"
    )
    for phone, nome in cur.fetchall():
        for variante in _variantes(phone):
            mapa[variante] = nome
    return mapa


def _variantes(phone: str) -> set[str]:
    digitos = "".join(ch for ch in (phone or "") if ch.isdigit())
    out = {digitos}
    if len(digitos) == 13 and digitos[4] == "9":
        out.add(digitos[:4] + digitos[5:])
    elif len(digitos) == 12:
        out.add(digitos[:4] + "9" + digitos[4:])
    return {v for v in out if v}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="grava as mudancas (sem esta flag apenas simula)",
    )
    args = parser.parse_args()

    con = sqlite3.connect(settings.SQLITE_PATH)
    con.row_factory = sqlite3.Row

    # Garante a coluna nova mesmo se o script rodar antes do worker novo subir.
    try:
        con.execute("ALTER TABLE leads ADD COLUMN nome_cadastro TEXT")
        con.commit()
    except sqlite3.OperationalError:
        pass

    cadastros = _cadastro_por_telefone(con)
    leads = con.execute("SELECT phone, nome, nome_cadastro FROM leads").fetchall()

    r = redis_sync.from_url(settings.redis_url, decode_responses=True)

    planos = []
    for row in leads:
        phone = row["phone"]
        nome_atual = (row["nome"] or "").strip()
        cadastro = (cadastros.get(phone) or row["nome_cadastro"] or "").strip()

        # Mantem quando o nome atual ja e o proprio primeiro nome do cadastro
        # (comparacao sem acento, para preservar a grafia melhor: o campo pode
        # ter "Laíse" e o cadastro "Laise").
        mantem = nomes.mesmo_nome(nome_atual, nomes.primeiro_nome(cadastro))
        novo_nome = nome_atual if mantem else ""

        if novo_nome == nome_atual and (row["nome_cadastro"] or "") == cadastro:
            continue

        vocativo = nomes.nome_para_vocativo(novo_nome, cadastro)
        planos.append(
            {
                "phone": phone,
                "nome_antes": nome_atual,
                "nome_depois": novo_nome,
                "cadastro": cadastro,
                "vocativo": vocativo,
            }
        )

    modo = "APLICANDO" if args.aplicar else "SIMULACAO (nada sera gravado)"
    print(f"=== Correcao de nomes legado — {modo} ===")
    print(f"leads na base: {len(leads)} | leads a ajustar: {len(planos)}\n")

    for p in planos:
        print(
            f"  {p['phone']}: nome {p['nome_antes']!r} -> {p['nome_depois']!r} "
            f"| cadastro: {p['cadastro'] or '-'} "
            f"| bot passara a chamar de: {p['vocativo'] or '(sem nome)'}"
        )

    if not args.aplicar:
        print("\nNada foi gravado. Rode de novo com --aplicar para efetivar.")
        return 0

    for p in planos:
        con.execute(
            "UPDATE leads SET nome=?, nome_cadastro=? WHERE phone=?",
            (p["nome_depois"], p["cadastro"], p["phone"]),
        )
        for variante in _variantes(p["phone"]):
            chave = keys.lead_key(variante)
            if r.exists(chave):
                r.hset(
                    chave,
                    mapping={"name": p["nome_depois"], "name_cadastro": p["cadastro"]},
                )
    con.commit()
    con.close()

    print(f"\nOK — {len(planos)} lead(s) corrigido(s) no SQLite e no Redis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
