#!/usr/bin/env python3
"""
OFFBOARDING completo de cliente (espelho do onboard.py).

Desfaz TUDO que o onboarding criou para um slug:
  1. SAI Comercial : assinatura+customer ASAAS, instancia UAZAPI (no servidor),
                     tenant (cascade), usuarios, registro Chatbot
  2. Portainer     : stack {slug} (api/worker/scheduler) + volume {slug}_{slug}_data
  3. GHCR          : pacote ghcr.io/strategicai-hub/{slug}
  4. GitHub        : repo strategicai-hub/{slug}
  5. GCP           : projeto "SAI {slug}" (sai-{slug}-XXXX) — recuperavel por 30 dias
  6. sai-tools     : entrada no clients.json
  7. Runtime       : chaves Redis ({slug}:* e *--{slug}:*) + fila RabbitMQ {slug}
  8. Local         : pasta clientes/{slug}

Uso:
    python offboard.py --slug corretor-fulano --dry-run     # so relata
    python offboard.py --slug corretor-fulano               # pede confirmacao
    python offboard.py --slug corretor-fulano --yes         # sem confirmacao

Cada etapa e best-effort/idempotente: recurso ja inexistente vira "nao encontrado",
nunca aborta as demais. Rode quantas vezes precisar ate zerar tudo.
"""
import argparse
import json
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import setup  # noqa: E402  (constantes, SECRETS, portainer, run)

SSH_HOST = "root@91.98.64.92"
SSH_KEY = str(Path.home() / ".ssh" / "sai01")
GITHUB_API = "https://api.github.com"


# ── helpers ──────────────────────────────────────────────


def github_api(method: str, path: str, token: str, data: dict | None = None):
    """Chamada direta a API do GitHub (retorna (status, json|None))."""
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=json.dumps(data).encode() if data else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "sai-offboard",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def portainer_delete(path: str) -> int:
    """DELETE no Portainer tolerando corpo vazio (204). Retorna status HTTP."""
    req = urllib.request.Request(
        f"{setup.PORTAINER_URL}/api{path}",
        method="DELETE",
        headers={"X-API-Key": setup.PORTAINER_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, context=setup._SSL, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def ssh(remote_cmd: str, timeout: int = 90):
    return subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=10", SSH_HOST, remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def call_offboard(payload: dict) -> dict | None:
    token = setup.SECRETS.get("ONBOARD_TOKEN")
    if not token:
        sys.exit("  ERRO: ONBOARD_TOKEN ausente em ~/.claude/.env")
    req = urllib.request.Request(
        f"{setup.SAI_COMERCIAL_URL}/api/admin/offboard",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "x-onboard-token": token},
    )
    try:
        with urllib.request.urlopen(req, context=setup._SSL, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        if e.code == 404:
            return None  # nada no SAI para este slug
        print(f"    ERRO offboard SAI ({e.code}): {detail}")
        return None
    except Exception as e:
        print(f"    ERRO offboard SAI: {e}")
        return None


def _rm_readonly(func, path, _exc):
    Path(path).chmod(stat.S_IWRITE)
    func(path)


# ── descoberta (dry-run) ─────────────────────────────────


def discover(slug: str, asaas_email: str | None, asaas_client_ids: list[str]) -> dict:
    found: dict = {}

    # SAI Comercial (tenant, instancias, chatbot, asaas)
    payload = {"slug": slug, "dryRun": True}
    if asaas_email:
        payload["asaasEmail"] = asaas_email
    if asaas_client_ids:
        payload["asaasClientIds"] = asaas_client_ids
    found["sai"] = call_offboard(payload)

    # Portainer stack
    stacks = setup.portainer_api("GET", "/stacks") or []
    found["stack"] = next((s for s in stacks if s.get("Name") == slug), None)

    # GitHub repo + pacote GHCR
    pat = setup.GITHUB_PAT
    st, _ = github_api("GET", f"/repos/{setup.GITHUB_OWNER}/{slug}", pat)
    found["repo"] = st == 200
    pkg_token = setup.SECRETS.get("GHCR_PAT") or pat
    st, _ = github_api("GET", f"/orgs/{setup.GITHUB_OWNER}/packages/container/{slug}", pkg_token)
    found["package"] = st == 200

    # GCP (nome do projeto = "SAI {slug}", id sai-{base}-XXXX)
    found["gcp"] = []
    if shutil.which("gcloud"):
        r = subprocess.run("gcloud projects list --format=json", shell=True,
                           capture_output=True, text=True)
        if r.returncode == 0:
            try:
                for p in json.loads(r.stdout or "[]"):
                    if p.get("name") == f"SAI {slug}" and p.get("lifecycleState") == "ACTIVE":
                        found["gcp"].append(p.get("projectId"))
            except json.JSONDecodeError:
                pass

    # sai-tools clients.json
    found["sai_tools"] = False
    r = setup.run(f"gh api repos/{setup.SAI_TOOLS_REPO}/contents/clients.json", check=False)
    if r.returncode == 0:
        import base64
        info = json.loads(r.stdout)
        clients = json.loads(base64.b64decode(info["content"].replace("\n", "")).decode())
        url = f"https://{setup.WEBHOOK_DOMAIN}/{slug}"
        found["sai_tools"] = any(c.get("url") == url for c in clients)
        found["_sai_tools_info"] = info
        found["_sai_tools_clients"] = clients

    # pasta local
    local_dir = Path(__file__).parent.parent / slug
    found["local"] = local_dir if local_dir.exists() else None

    return found


def print_report(slug: str, found: dict):
    print("\n" + "=" * 60)
    print(f"  OFFBOARDING (levantamento): {slug}")
    print("=" * 60)

    sai = found.get("sai")
    if sai and sai.get("tenant"):
        t = sai["tenant"]
        c = t.get("counts", {})
        print(f"  Tenant SAI    : {t['name']} (id {t['id']})")
        print(f"    usuarios    : {', '.join(t.get('users') or []) or '-'}")
        print(f"    dados       : {c.get('contacts', 0)} contatos, {c.get('deals', 0)} deals, "
              f"{c.get('campaigns', 0)} campanhas, {c.get('aiMessageLogs', 0)} logs IA")
        for i in sai.get("instances", []):
            print(f"    instancia   : {i['provider']} {i.get('name')} ({i.get('status')}) "
                  f"externalId={i.get('externalId')}")
    else:
        print("  Tenant SAI    : nao encontrado")
    if sai and sai.get("chatbot"):
        cb = sai["chatbot"]
        extra = " [EM USO POR OUTROS TENANTS - sera mantido]" if cb.get("inUseByOtherTenants") else ""
        print(f"  Chatbot SAI   : {cb['baseUrl']}{extra}")
    else:
        print("  Chatbot SAI   : nao encontrado")
    for a in (sai or {}).get("asaas", []):
        print(f"  ASAAS         : {a['name']} <{a.get('email')}> (CRM {a['crmTenantSlug']}, status {a['status']})")
        print(f"    customer    : {a.get('asaasCustomerId') or '-'}   assinatura: {a.get('asaasSubscriptionId') or '-'}")
    if not (sai or {}).get("asaas"):
        print("  ASAAS         : nenhum Client do CRM com assinatura encontrado")

    stack = found.get("stack")
    print(f"  Stack Portainer: {'id ' + str(stack['Id']) if stack else 'nao encontrada'}")
    print(f"  Repo GitHub   : {'existe' if found.get('repo') else 'nao encontrado'} "
          f"({setup.GITHUB_OWNER}/{slug})")
    print(f"  Pacote GHCR   : {'existe' if found.get('package') else 'nao encontrado'}")
    print(f"  Projeto GCP   : {', '.join(found.get('gcp') or []) or 'nao encontrado'}")
    print(f"  sai-tools     : {'registrado' if found.get('sai_tools') else 'nao registrado'}")
    print(f"  Pasta local   : {found.get('local') or 'nao encontrada'}")
    print("=" * 60)


# ── execucao ─────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description="Offboarding completo de cliente (espelho do onboard.py).")
    p.add_argument("--slug", required=True)
    p.add_argument("--dry-run", action="store_true", help="So levanta e relata, nao deleta nada")
    p.add_argument("--yes", action="store_true", help="Pula a confirmacao interativa")
    p.add_argument("--keep-gcp", action="store_true", help="Nao deletar o projeto GCP")
    p.add_argument("--keep-repo", action="store_true", help="Nao deletar repo GitHub nem pacote GHCR")
    p.add_argument("--keep-local", action="store_true", help="Nao apagar a pasta clientes/{slug}")
    p.add_argument("--skip-runtime", action="store_true", help="Pular limpeza Redis/RabbitMQ/volume via SSH")
    p.add_argument("--skip-asaas", action="store_true", help="Nao mexer no ASAAS")
    p.add_argument("--asaas-email", default=None, help="E-mail para localizar o Client do CRM com assinatura")
    p.add_argument("--asaas-client-id", action="append", default=[], help="Id explicito de Client do CRM (repetivel)")
    args = p.parse_args()

    slug = setup.slugify(args.slug)

    print(f"\n  Levantando recursos de '{slug}'...")
    found = discover(slug, args.asaas_email, args.asaas_client_id)
    print_report(slug, found)

    if args.dry_run:
        print("\n  (dry-run: nada foi deletado)\n")
        return

    if not args.yes:
        typed = input(f"\n  Digite o slug '{slug}' para confirmar a DELECAO de tudo acima: ").strip()
        if typed != slug:
            sys.exit("  Abortado (confirmacao nao confere).")

    results: list[str] = []

    # ── [1/7] SAI Comercial (ASAAS + instancia UAZAPI + tenant + chatbot) ──
    print("\n  [1/7] SAI Comercial (ASAAS + instancia + tenant + chatbot)...")
    sai_found = found.get("sai") and (found["sai"].get("tenant") or found["sai"].get("chatbot")
                                      or found["sai"].get("asaas"))
    if sai_found:
        payload = {"slug": slug, "deleteAsaas": not args.skip_asaas}
        if args.asaas_email:
            payload["asaasEmail"] = args.asaas_email
        if args.asaas_client_id:
            payload["asaasClientIds"] = args.asaas_client_id
        res = call_offboard(payload)
        if res and res.get("ok"):
            r = res.get("results", {})
            print(f"    tenant : {r.get('tenant')}")
            print(f"    users  : {', '.join(r.get('usersDeleted') or []) or '-'}")
            for i in r.get("instances", []):
                print(f"    instancia {i.get('provider')} {i.get('externalId')}: {i.get('remote')}")
            print(f"    chatbot: {r.get('chatbot')}")
            asaas_r = r.get("asaas")
            if isinstance(asaas_r, list):
                for a in asaas_r:
                    print(f"    asaas  : client {a['crmClientId']} assinatura={a['subscription']} customer={a['customer']}")
            else:
                print(f"    asaas  : {asaas_r}")
            results.append("SAI: OK")
        else:
            results.append("SAI: FALHOU (ver acima)")
    else:
        print("    nada no SAI para este slug - pulando")
        results.append("SAI: nada a fazer")

    # ── [2/7] stack Portainer ───────────────────────────────
    print("\n  [2/7] Stack Portainer...")
    stack = found.get("stack")
    if stack:
        st = portainer_delete(f"/stacks/{stack['Id']}?endpointId={setup.PORTAINER_ENDPOINT_ID}")
        ok = st in (200, 204)
        print(f"    DELETE stack {stack['Id']} -> HTTP {st}")
        results.append(f"Stack: {'OK' if ok else f'FALHOU (HTTP {st})'}")
    else:
        print("    stack nao encontrada - pulando")
        results.append("Stack: nada a fazer")

    # ── [3/7] runtime na VPS (volume, Redis, RabbitMQ) ──────
    print("\n  [3/7] Runtime na VPS (volume, Redis, RabbitMQ)...")
    if args.skip_runtime:
        print("    --skip-runtime: pulando")
        results.append("Runtime: pulado")
    else:
        try:
            # volume da stack (Swarm prefixa com o nome da stack) — precisa dos
            # containers terem sido removidos; tenta algumas vezes.
            vol = f"{slug}_{slug}_data"
            vol_ok = False
            for _ in range(3):
                r = ssh(f"docker volume rm {vol} 2>&1 || true")
                out = (r.stdout + r.stderr).strip()
                if vol in out and "in use" not in out.lower():
                    vol_ok = True
                    break
                if "no such volume" in out.lower():
                    vol_ok = True
                    out = "ja nao existia"
                    break
                time.sleep(5)
            print(f"    volume {vol}: {'OK' if vol_ok else 'nao removido (em uso?)'}")

            # chaves Redis do cliente ({slug}:* e *--{slug}:*)
            pw = setup.REDIS_PASSWORD
            deleted = 0
            for pat in (f"{slug}:*", f"*--{slug}:*"):
                cmd = (
                    "docker exec $(docker ps -qf name=redis_redis | head -n1) sh -c "
                    f"\"redis-cli --no-auth-warning -a '{pw}' --scan --pattern '{pat}' "
                    f"| xargs -r -n 100 redis-cli --no-auth-warning -a '{pw}' del\""
                )
                r = ssh(cmd)
                for line in (r.stdout or "").splitlines():
                    if line.strip().isdigit():
                        deleted += int(line.strip())
            print(f"    Redis: {deleted} chave(s) removida(s)")

            # fila RabbitMQ (nome = slug)
            r = ssh("docker exec $(docker ps -qf name=rabbitmq_rabbitmq | head -n1) "
                    f"rabbitmqctl delete_queue {slug} 2>&1 || true")
            out = (r.stdout + r.stderr).strip().splitlines()
            print(f"    RabbitMQ: {out[-1] if out else 'sem resposta'}")
            results.append("Runtime: OK (best-effort)")
        except Exception as e:
            print(f"    AVISO: limpeza runtime falhou ({e}) - nao critico")
            results.append("Runtime: FALHOU (nao critico)")

    # ── [4/7] pacote GHCR + repo GitHub ─────────────────────
    print("\n  [4/7] Pacote GHCR + repo GitHub...")
    if args.keep_repo:
        print("    --keep-repo: pulando")
        results.append("Repo/GHCR: pulado")
    else:
        if found.get("package"):
            pkg_token = setup.SECRETS.get("GHCR_PAT") or setup.GITHUB_PAT
            st, _ = github_api("DELETE", f"/orgs/{setup.GITHUB_OWNER}/packages/container/{slug}", pkg_token)
            ok = st in (200, 204)
            print(f"    pacote GHCR: {'OK' if ok else f'FALHOU (HTTP {st})'}")
            results.append(f"GHCR: {'OK' if ok else f'FALHOU (HTTP {st})'}")
        else:
            print("    pacote GHCR nao encontrado - pulando")
            results.append("GHCR: nada a fazer")

        if found.get("repo"):
            st, _ = github_api("DELETE", f"/repos/{setup.GITHUB_OWNER}/{slug}", setup.GITHUB_PAT)
            if st in (200, 204):
                print("    repo: OK")
                results.append("Repo: OK")
            else:
                r = setup.run(f"gh repo delete {setup.GITHUB_OWNER}/{slug} --yes", check=False)
                if r.returncode == 0:
                    print("    repo: OK (via gh)")
                    results.append("Repo: OK")
                else:
                    print(f"    repo: FALHOU (token sem escopo delete_repo?). Delete manual:")
                    print(f"      https://github.com/{setup.GITHUB_OWNER}/{slug}/settings")
                    results.append("Repo: FALHOU (deletar manualmente)")
        else:
            print("    repo nao encontrado - pulando")
            results.append("Repo: nada a fazer")

    # ── [5/7] projeto GCP ───────────────────────────────────
    print("\n  [5/7] Projeto GCP...")
    if args.keep_gcp:
        print("    --keep-gcp: pulando")
        results.append("GCP: pulado")
    elif found.get("gcp"):
        for pid in found["gcp"]:
            r = subprocess.run(f"gcloud projects delete {pid} --quiet", shell=True,
                               capture_output=True, text=True)
            ok = r.returncode == 0
            print(f"    {pid}: {'OK (recuperavel por 30 dias)' if ok else 'FALHOU: ' + r.stderr[:200]}")
            results.append(f"GCP {pid}: {'OK' if ok else 'FALHOU'}")
    else:
        print("    nenhum projeto ativo encontrado - pulando")
        results.append("GCP: nada a fazer")

    # ── [6/7] sai-tools clients.json ────────────────────────
    print("\n  [6/7] sai-tools (clients.json)...")
    if found.get("sai_tools"):
        import base64
        info = found["_sai_tools_info"]
        clients = found["_sai_tools_clients"]
        url = f"https://{setup.WEBHOOK_DOMAIN}/{slug}"
        new_clients = [c for c in clients if c.get("url") != url]
        payload = json.dumps({
            "message": f"chore: remover cliente {slug} (offboarding)",
            "content": base64.b64encode(
                json.dumps(new_clients, ensure_ascii=False, indent=2).encode()
            ).decode(),
            "sha": info["sha"],
        })
        proc = subprocess.run(
            f"gh api repos/{setup.SAI_TOOLS_REPO}/contents/clients.json -X PUT --input -",
            shell=True, input=payload, capture_output=True, text=True,
        )
        ok = proc.returncode == 0
        print(f"    remocao: {'OK' if ok else 'FALHOU: ' + proc.stderr[:150]}")
        results.append(f"sai-tools: {'OK' if ok else 'FALHOU'}")
    else:
        print("    nao registrado - pulando")
        results.append("sai-tools: nada a fazer")

    # ── [7/7] pasta local ───────────────────────────────────
    print("\n  [7/7] Pasta local...")
    local_dir = found.get("local")
    if args.keep_local:
        print("    --keep-local: pulando")
        results.append("Local: pulado")
    elif local_dir:
        try:
            shutil.rmtree(local_dir, onerror=_rm_readonly)
            print(f"    {local_dir}: OK")
            results.append("Local: OK")
        except Exception as e:
            print(f"    FALHOU ({e}) - feche editores/OneDrive e apague manualmente")
            results.append("Local: FALHOU (apagar manualmente)")
    else:
        print("    pasta nao encontrada - pulando")
        results.append("Local: nada a fazer")

    # ── resumo ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  OFFBOARDING CONCLUIDO: {slug}")
    print("=" * 60)
    for line in results:
        print(f"  - {line}")
    print("\n  Rode novamente com --dry-run para conferir que nada sobrou.\n")


if __name__ == "__main__":
    main()
