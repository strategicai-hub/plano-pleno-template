#!/usr/bin/env python3
"""
Migra historico de chat do bot ANTIGO (formato RedisChatMessageHistory do LangChain)
para o schema do bot NOVO (deste template).

Cenario tipico:
  - Cliente ja tem bot rodando (n8n, langchain, etc) com historico no Redis no
    formato {phone}@s.whatsapp.net--{slug}  -> LIST de mensagens.
  - Vamos cutover para o bot novo, que usa {phone}--{slug}:history.
  - Este script copia os items, extraindo o phone (sem @s.whatsapp.net) e
    aplicando o LTRIM do bot novo.

Uso (do diretorio raiz do projeto):
    python scripts/migrate_legacy_history.py            # dry-run (default)
    python scripts/migrate_legacy_history.py --execute  # aplica de verdade

Configuracao via .env (carregado automaticamente):
    PROJECT_SLUG       (obrigatorio) - usado nos patterns
    REDIS_HOST/PORT/PASSWORD          - destino (= o redis do bot novo)

    LEGACY_REDIS_HOST                 - origem (default = REDIS_HOST)
    LEGACY_REDIS_PORT                 - origem (default = REDIS_PORT)
    LEGACY_REDIS_PASSWORD             - origem (default = REDIS_PASSWORD)
    LEGACY_HISTORY_PATTERN_SUFFIX     - sufixo das chaves antigas
                                        (default = '@s.whatsapp.net--{slug}')

Flags CLI:
    --execute         executa de verdade (default e dry-run)
    --history-limit N aplica LTRIM -N -1 (default = 50, igual app/services/redis_service.py)
    --no-skip-anomalies   nao pular chaves do tipo "BloquearAgente-..." e *-chat-id
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass  # dotenv e opcional; usa os.environ direto

try:
    import redis  # type: ignore
except ImportError:
    print("ERRO: redis-py nao instalado. Rode: pip install redis", file=sys.stderr)
    sys.exit(2)


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def is_anomaly(key: str, suffix: str) -> tuple[bool, str]:
    """Identifica chaves que NAO devem ser migradas (legado quebrado)."""
    if key.startswith('"BloquearAgente-"'):
        return True, "BloquearAgente"
    if key.endswith("-chat-id"):
        return True, "chat-id"
    if key.startswith("alerta_recepcao_"):
        return True, "alerta_recepcao"
    if not key.endswith(suffix):
        return True, "padrao_estranho"
    return False, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="executa de verdade (default e dry-run)")
    parser.add_argument("--history-limit", type=int, default=50, help="LTRIM final (default 50)")
    parser.add_argument("--no-skip-anomalies", action="store_true", help="nao pular chaves anomalas")
    args = parser.parse_args()

    slug = env("PROJECT_SLUG")
    if not slug:
        print("ERRO: PROJECT_SLUG nao definido (.env)", file=sys.stderr)
        return 2

    dest_host = env("REDIS_HOST")
    dest_port = int(env("REDIS_PORT", "6379") or "6379")
    dest_password = env("REDIS_PASSWORD")
    if not dest_host:
        print("ERRO: REDIS_HOST nao definido (.env)", file=sys.stderr)
        return 2

    src_host = env("LEGACY_REDIS_HOST", dest_host)
    src_port = int(env("LEGACY_REDIS_PORT", str(dest_port)) or str(dest_port))
    src_password = env("LEGACY_REDIS_PASSWORD", dest_password)

    suffix = env("LEGACY_HISTORY_PATTERN_SUFFIX", f"@s.whatsapp.net--{slug}") or f"@s.whatsapp.net--{slug}"
    pattern = f"*{suffix}"
    dest_template = "{phone}--" + slug + ":history"

    src = redis.Redis(host=src_host, port=src_port, password=src_password, decode_responses=False)
    dst = redis.Redis(host=dest_host, port=dest_port, password=dest_password, decode_responses=False)

    src.ping()
    dst.ping()

    same_redis = (src_host, src_port) == (dest_host, dest_port)
    print(f"[ok] source: {src_host}:{src_port}")
    print(f"[ok] dest:   {dest_host}:{dest_port}{' (mesmo Redis)' if same_redis else ''}")
    print(f"[modo] {'EXECUCAO REAL' if args.execute else 'DRY-RUN (use --execute para aplicar)'}")
    print(f"[slug] {slug}")
    print(f"[pattern source] {pattern}")
    print(f"[history-limit ] {args.history_limit}")
    print()

    keys = list(src.scan_iter(match=pattern, count=500))
    print(f"[scan] {len(keys)} chaves casando '{pattern}'")
    if not keys:
        print("[fim] nada a migrar.")
        return 0

    migrated = 0
    skipped_anomaly = 0
    skipped_type = 0
    skipped_empty = 0
    truncated = 0
    total_items = 0
    errors: list[tuple[str, str]] = []

    for k_bytes in keys:
        k = k_bytes.decode("utf-8", errors="replace")

        if not args.no_skip_anomalies:
            anom, _why = is_anomaly(k, suffix)
            if anom:
                skipped_anomaly += 1
                continue

        try:
            t = src.type(k_bytes).decode()
        except Exception as e:
            errors.append((k, f"type error: {e}"))
            continue

        if t != "list":
            skipped_type += 1
            continue

        items = src.lrange(k_bytes, 0, -1)
        if not items:
            skipped_empty += 1
            continue

        if not k.endswith(suffix):
            skipped_anomaly += 1
            continue
        phone = k[: -len(suffix)]
        new_key = dest_template.format(phone=phone).encode()

        original_len = len(items)
        will_truncate = original_len > args.history_limit

        if not args.execute:
            migrated += 1
            total_items += min(original_len, args.history_limit)
            if will_truncate:
                truncated += 1
            continue

        try:
            with dst.pipeline(transaction=True) as p:
                p.delete(new_key)
                p.rpush(new_key, *items)
                p.ltrim(new_key, -args.history_limit, -1)
                p.execute()
            migrated += 1
            total_items += min(original_len, args.history_limit)
            if will_truncate:
                truncated += 1
        except Exception as e:
            errors.append((k, f"write error: {e}"))

    print()
    print("=" * 60)
    print("RESUMO")
    print("=" * 60)
    print(f"  chaves migradas       : {migrated}")
    print(f"  truncadas (>{args.history_limit:>3})        : {truncated}")
    print(f"  itens totais gravados : {total_items}")
    print(f"  puladas - anomalia    : {skipped_anomaly}")
    print(f"  puladas - type!=list  : {skipped_type}")
    print(f"  puladas - lista vazia : {skipped_empty}")
    print(f"  erros                 : {len(errors)}")
    if errors:
        print("\n[erros detalhados]")
        for k, msg in errors[:20]:
            print(f"  {k}: {msg}")

    if args.execute:
        dst_count = sum(1 for _ in dst.scan_iter(match=f"*--{slug}:history", count=500))
        print(f"\n[validacao] chaves '*--{slug}:history' no destino: {dst_count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
