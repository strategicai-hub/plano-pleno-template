#!/usr/bin/env python3
"""
PLANO START - Setup automatizado de novo projeto.

Uso:
    python setup.py

Fluxo 100% automatizado:
 1. Cria repo no GitHub a partir do template
 2. Substitui placeholders nos arquivos de deploy
 3. Gera .env e client.yaml
 4. Commit e push (dispara GitHub Actions)
 5. Configura permissoes do GitHub Actions
 6. Aguarda build da imagem Docker
 7. Torna pacote GHCR publico
 8. Cria stack no Portainer com auto-update webhook
 9. Salva webhook URL como secret no GitHub
10. Registra cliente no sai-tools (clients.json)

Unico passo manual: configurar webhook na UAZAPI.
"""
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

# ── constantes ───────────────────────────────────────────

GITHUB_OWNER = "gustavocastilho-hub"
TEMPLATE_REPO = f"{GITHUB_OWNER}/plano-pleno-template"
SAI_TOOLS_REPO = f"{GITHUB_OWNER}/sai-tools"
WEBHOOK_DOMAIN = "webhook-whatsapp.strategicai.com.br"

PORTAINER_URL = "https://91.98.64.92:9443"
PORTAINER_ENDPOINT_ID = 1

# ── secrets locais (nunca vao para o git) ────────────────
_SECRETS_FILE = Path(__file__).parent / ".secrets.json"


def _load_secrets() -> dict:
    if _SECRETS_FILE.exists():
        return json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
    return {}


SECRETS = _load_secrets()


def _require_secret(key: str) -> str:
    val = SECRETS.get(key)
    if not val:
        sys.exit(
            f"  ERRO: '{key}' ausente em {_SECRETS_FILE.name}.\n"
            f"  Preencha esse arquivo com as chaves: "
            f"GITHUB_PAT, PORTAINER_TOKEN, REDIS_PASSWORD, RABBITMQ_USER, RABBITMQ_PASS."
        )
    return val


GITHUB_PAT = _require_secret("GITHUB_PAT")
PORTAINER_TOKEN = _require_secret("PORTAINER_TOKEN")
REDIS_PASSWORD = _require_secret("REDIS_PASSWORD")

TEMPLATE_FILES = [
    "docker-compose.yml",
]

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# ── helpers ──────────────────────────────────────────────


def ask(prompt: str, default: str = "") -> str:
    if default:
        text = input(f"  {prompt} [{default}]: ").strip()
        return text if text else default
    while True:
        text = input(f"  {prompt}: ").strip()
        if text:
            return text
        print("    (campo obrigatorio)")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")


def run(cmd: str, cwd: str | None = None, check: bool = True):
    print(f"    $ {cmd}")
    return subprocess.run(
        cmd, shell=True, cwd=cwd, check=check, capture_output=True, text=True
    )


# ── portainer API ────────────────────────────────────────


def portainer_api(method: str, path: str, data=None):
    url = f"{PORTAINER_URL}/api{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"X-API-Key": PORTAINER_TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=_SSL) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"    ERRO Portainer ({e.code}): {e.read().decode()[:300]}")
        return None
    except Exception as e:
        print(f"    ERRO Portainer: {e}")
        return None


def get_swarm_id() -> str | None:
    r = portainer_api("GET", f"/endpoints/{PORTAINER_ENDPOINT_ID}/docker/swarm")
    return r.get("ID") if r else None


def create_portainer_stack(data: dict, env_list: list) -> tuple[str | None, int | None]:
    slug = data["slug"]
    repo_url = f"https://github.com/{GITHUB_OWNER}/{data['repo_name']}"
    webhook_id = str(uuid.uuid4())

    body = {
        "name": slug,
        "repositoryURL": repo_url,
        "repositoryReferenceName": "refs/heads/main",
        "repositoryAuthentication": True,
        "repositoryUsername": "x-access-token",
        "repositoryPassword": GITHUB_PAT,
        "composeFile": "docker-compose.yml",
        "env": env_list,
        "autoUpdate": {"webhook": webhook_id},
    }

    swarm_id = get_swarm_id()
    if swarm_id:
        body["swarmID"] = swarm_id
        endpoint = f"/stacks/create/swarm/repository?endpointId={PORTAINER_ENDPOINT_ID}"
        print(f"    Modo: Swarm (ID: {swarm_id[:12]}...)")
    else:
        endpoint = f"/stacks/create/standalone/repository?endpointId={PORTAINER_ENDPOINT_ID}"
        print("    Modo: Standalone")

    result = portainer_api("POST", endpoint, body)

    if result:
        auto = result.get("AutoUpdate") or {}
        wh = auto.get("Webhook") or webhook_id
        webhook_url = f"{PORTAINER_URL}/api/stacks/webhooks/{wh}"
        stack_id = result.get("Id")
        print(f"    Stack '{slug}' criada! (ID: {stack_id})")
        return webhook_url, stack_id

    return None, None


def redeploy_stack_with_auth(stack_id: int, env_list: list):
    """Força redeploy passando credenciais ao Docker Swarm (--with-registry-auth).

    O GHCR exige autenticacao mesmo para imagens publicas no protocolo Docker.
    Sem isso, os nos do Swarm nao conseguem fazer pull de imagens novas (rejected).
    """
    payload = {
        "RepositoryReferenceName": "refs/heads/main",
        "PullImage": True,
        "RepositoryAuthentication": True,
        "RepositoryUsername": "x-access-token",
        "RepositoryPassword": GITHUB_PAT,
        "Env": env_list,
    }
    result = portainer_api(
        "PUT", f"/stacks/{stack_id}/git/redeploy?endpointId={PORTAINER_ENDPOINT_ID}", payload
    )
    if result and result.get("Id"):
        print(f"    Redeploy com registry auth - OK")
    else:
        print(f"    AVISO: Redeploy nao confirmado: {result}")

    return None


# ── github automation ────────────────────────────────────


def gh_actions_permissions(repo: str):
    full = f"{GITHUB_OWNER}/{repo}"
    r = run(
        f"gh api repos/{full}/actions/permissions/workflow -X PUT "
        f"-f default_workflow_permissions=write "
        f"-F can_approve_pull_request_reviews=false",
        check=False,
    )
    if r.returncode == 0:
        print("    Permissoes Actions - OK")
    else:
        print(f"    AVISO: {r.stderr[:150]}")


def gh_secret(repo: str, name: str, value: str):
    full = f"{GITHUB_OWNER}/{repo}"
    proc = subprocess.run(
        f"gh secret set {name} -R {full}",
        shell=True, input=value, capture_output=True, text=True,
    )
    print(f"    $ gh secret set {name} -R {full}")
    print(f"    Secret {name} - " + ("OK" if proc.returncode == 0 else f"FALHOU: {proc.stderr[:100]}"))


def gh_package_public(repo: str):
    r = run(
        f"gh api -X PATCH /user/packages/container/{repo} -f visibility=public",
        check=False,
    )
    if r.returncode == 0:
        print("    GHCR publico - OK")
    else:
        print(f"    AVISO: Faca manualmente: https://github.com/users/{GITHUB_OWNER}/packages/container/{repo}/settings")


def wait_build(repo: str, timeout: int = 300) -> bool:
    full = f"{GITHUB_OWNER}/{repo}"
    t0 = time.time()
    time.sleep(5)

    while time.time() - t0 < timeout:
        r = run(
            f"gh run list -R {full} --limit 1 --json status,conclusion",
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            runs = json.loads(r.stdout)
            if runs:
                st = runs[0].get("status", "")
                co = runs[0].get("conclusion", "")
                if st == "completed":
                    ok = co == "success"
                    print(f"    Build {'OK!' if ok else 'FALHOU: ' + co}")
                    return ok
                elapsed = int(time.time() - t0)
                sys.stdout.write(f"    [{elapsed}s] {st}...          \r")
                sys.stdout.flush()
        time.sleep(15)

    print("\n    Timeout.")
    return False


# ── project creation ─────────────────────────────────────


def check_prereqs():
    try:
        r = run("gh --version", check=False)
        if r.returncode != 0:
            sys.exit("  ERRO: gh CLI nao encontrado.")
    except FileNotFoundError:
        sys.exit("  ERRO: gh CLI nao encontrado.")
    if run("gh auth status", check=False).returncode != 0:
        sys.exit("  ERRO: gh nao autenticado. Execute: gh auth login")


def list_available_niches() -> list[str]:
    """Lista nichos disponiveis a partir de app/prompts/*.j2."""
    prompts_dir = Path(__file__).parent / "app" / "prompts"
    if not prompts_dir.exists():
        return ["academia"]
    niches = sorted(p.stem for p in prompts_dir.glob("*.j2"))
    return niches or ["academia"]


def ask_niche() -> str:
    niches = list_available_niches()
    if len(niches) == 1:
        return niches[0]
    default = "academia" if "academia" in niches else niches[0]
    print("\n  Nichos disponiveis:")
    for i, n in enumerate(niches, 1):
        print(f"    {i}) {n}")
    while True:
        raw = input(f"  Escolha o nicho [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(niches):
            return niches[int(raw) - 1]
        if raw in niches:
            return raw
        print(f"    Opcao invalida. Use numero (1-{len(niches)}) ou o nome exato.")


def collect_inputs() -> dict:
    print("\n" + "=" * 60)
    print("  PLANO PLENO - Novo projeto")
    print("=" * 60)

    print("\n  --- DADOS DO NEGOCIO ---")
    bname = ask("Nome do negocio (ex: Academia Muay Thai SP)")
    slug = ask("Slug", slugify(bname))
    aname = ask("Nome da assistente", "Assistente")
    phone = ask("Telefone do dono (ex: 5511999990000)")

    print("\n  --- NICHO DO NEGOCIO ---")
    niche = ask_niche()

    print("\n  --- TOKENS ---")
    uaz = ask("UAZAPI token")
    gem = ask("GEMINI API key")
    sid = ask("Google Sheet ID (ou 'pular')", "pular")
    gcal = ask("Google Calendar ID (ou 'pular')", "pular")

    gcreds = ""
    if sid != "pular":
        # Se ja tem as credenciais no .secrets.json, usa direto sem perguntar
        if "GOOGLE_CREDENTIALS_JSON" in SECRETS:
            creds_val = SECRETS["GOOGLE_CREDENTIALS_JSON"]
            if isinstance(creds_val, dict):
                gcreds = json.dumps(creds_val)
            else:
                gcreds = str(creds_val)
            print("    Google credentials: carregado de .secrets.json")
        else:
            default_creds = SECRETS.get("GOOGLE_CREDENTIALS_JSON_PATH", "pular")
            cp = ask("Google credentials JSON path (ou 'pular')", default_creds)
            if cp != "pular" and Path(cp).exists():
                gcreds = Path(cp).read_text(encoding="utf-8").strip()

    print(f"\n  --- RESUMO ---")
    print(f"  Negocio:  {bname}")
    print(f"  Nicho:    {niche}")
    print(f"  Slug:     {slug}")
    print(f"  URL:      https://{WEBHOOK_DOMAIN}/{slug}")

    if input("\n  Confirma? (s/n) [s]: ").strip().lower() not in ("", "s"):
        sys.exit("  Cancelado.")

    return {
        "business_name": bname, "slug": slug, "assistant_name": aname,
        "alert_phone": phone, "uazapi_token": uaz, "gemini_key": gem,
        "sheet_id": sid if sid != "pular" else "", "google_creds": gcreds,
        "calendar_id": gcal if gcal != "pular" else "",
        "repo_name": slug, "niche": niche,
    }


def create_repo(data: dict) -> Path:
    repo = data["repo_name"]
    full = f"{GITHUB_OWNER}/{repo}"
    parent = Path(__file__).parent.parent
    repo_dir = parent / repo

    print(f"\n  [1/8] Criando repo {full}...")
    if repo_dir.exists():
        print(f"    Ja existe. Usando existente.")
        return repo_dir

    r = run(f"gh repo create {full} --private --template {TEMPLATE_REPO} --clone",
            cwd=str(parent), check=False)
    if r.returncode != 0:
        if "already exists" in r.stderr:
            print("    Repo ja existe. Clonando...")
            run(f"gh repo clone {full}", cwd=str(parent))
        else:
            sys.exit(f"  ERRO: {r.stderr}")
    return repo_dir


def replace_placeholders(repo_dir: Path, data: dict):
    print("\n  [2/8] Placeholders...")
    reps = {"{{SLUG}}": data["slug"], "{{GITHUB_OWNER}}": GITHUB_OWNER, "{{REPO_NAME}}": data["repo_name"]}
    for fp in TEMPLATE_FILES:
        p = repo_dir / fp
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8")
        for k, v in reps.items():
            txt = txt.replace(k, v)
        p.write_text(txt, encoding="utf-8")
        print(f"    {fp} - OK")


def generate_env(repo_dir: Path, data: dict):
    print("\n  [3/8] Gerando .env e client.yaml...")
    env = f"""PROJECT_SLUG={data['slug']}
BUSINESS_NAME={data['business_name']}
ASSISTANT_NAME={data['assistant_name']}
RABBITMQ_HOST=91.98.64.92
RABBITMQ_PORT=5672
RABBITMQ_USER={SECRETS.get('RABBITMQ_USER', 'guest')}
RABBITMQ_PASS={SECRETS.get('RABBITMQ_PASS', 'guest')}
RABBITMQ_VHOST=default
REDIS_HOST=91.98.64.92
REDIS_PORT=6380
REDIS_PASSWORD={REDIS_PASSWORD}
GEMINI_API_KEY={data['gemini_key']}
UAZAPI_BASE_URL=https://strategicai.uazapi.com
UAZAPI_TOKEN={data['uazapi_token']}
GOOGLE_CREDENTIALS_JSON={data['google_creds']}
GOOGLE_SHEET_ID={data['sheet_id']}
GOOGLE_CALENDAR_ID={data['calendar_id']}
SQLITE_PATH=/data/pleno.db
SCHEDULER_TZ=America/Sao_Paulo
FOLLOWUP_DRY_RUN=false
DEBOUNCE_SECONDS=30
BLOCK_TTL_SECONDS=3600
ALERT_PHONE={data['alert_phone']}
DEBOUNCE_BYPASS_PHONES={data['alert_phone']}
ALLOWED_PHONES=
"""
    (repo_dir / ".env").write_text(env, encoding="utf-8")
    print("    .env - OK")

    example = repo_dir / "client.example.yaml"
    client = repo_dir / "client.yaml"
    if example.exists() and not client.exists():
        c = example.read_text(encoding="utf-8")
        c = c.replace('niche: "academia"', f'niche: "{data["niche"]}"')
        c = c.replace('"AJE DE BOXE"', f'"{data["business_name"]}"')
        c = c.replace('"academia de boxe"', '"[PREENCHER]"')
        c = c.replace('"RUA FREI MAURO, 31 - ADRIANOPOLIS"', '"[PREENCHER]"')
        c = c.replace('"Vic"', f'"{data["assistant_name"]}"')
        c = c.replace('"Ola! Sou a Vic, tudo bem?"',
                       f'"Ola! Sou a {data["assistant_name"]}, tudo bem?"')
        client.write_text(c, encoding="utf-8")
        print("    client.yaml - OK")

    # O template mantem client.yaml no .gitignore (protege dev tests da conftest),
    # mas no projeto derivado ele PRECISA ser versionado: o container do
    # Portainer faz COPY . . no build, entao sem client.yaml o Jinja falha no
    # startup. Remove essa linha do .gitignore do derivado.
    gi = repo_dir / ".gitignore"
    if gi.exists():
        lines = gi.read_text(encoding="utf-8").splitlines(keepends=False)
        new_lines = [ln for ln in lines if ln.strip() != "client.yaml"]
        if len(new_lines) != len(lines):
            gi.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            print("    .gitignore ajustado (client.yaml rastreado) - OK")


def commit_push(repo_dir: Path, data: dict):
    print("\n  [4/8] Push...")
    run("git add -A", cwd=str(repo_dir))
    run(f'git commit -m "feat: setup {data["business_name"]}"', cwd=str(repo_dir), check=False)
    r = run("git push origin main", cwd=str(repo_dir), check=False)
    if r.returncode != 0:
        run("git branch -M main", cwd=str(repo_dir), check=False)
        run("git push -u origin main", cwd=str(repo_dir), check=False)
    print("    OK")


def build_env_list(data: dict) -> list[dict]:
    return [
        {"name": "PROJECT_SLUG", "value": data["slug"]},
        {"name": "BUSINESS_NAME", "value": data["business_name"]},
        {"name": "ASSISTANT_NAME", "value": data["assistant_name"]},
        {"name": "RABBITMQ_HOST", "value": "91.98.64.92"},
        {"name": "RABBITMQ_PORT", "value": "5672"},
        {"name": "RABBITMQ_USER", "value": SECRETS.get("RABBITMQ_USER", "guest")},
        {"name": "RABBITMQ_PASS", "value": SECRETS.get("RABBITMQ_PASS", "guest")},
        {"name": "RABBITMQ_VHOST", "value": "default"},
        {"name": "REDIS_HOST", "value": "91.98.64.92"},
        {"name": "REDIS_PORT", "value": "6380"},
        {"name": "REDIS_PASSWORD", "value": REDIS_PASSWORD},
        {"name": "GEMINI_API_KEY", "value": data["gemini_key"]},
        {"name": "UAZAPI_BASE_URL", "value": "https://strategicai.uazapi.com"},
        {"name": "UAZAPI_TOKEN", "value": data["uazapi_token"]},
        {"name": "GOOGLE_CREDENTIALS_JSON", "value": data["google_creds"]},
        {"name": "GOOGLE_SHEET_ID", "value": data["sheet_id"]},
        {"name": "GOOGLE_CALENDAR_ID", "value": data["calendar_id"]},
        {"name": "SQLITE_PATH", "value": "/data/pleno.db"},
        {"name": "SCHEDULER_TZ", "value": "America/Sao_Paulo"},
        {"name": "FOLLOWUP_DRY_RUN", "value": "false"},
        {"name": "DEBOUNCE_SECONDS", "value": "30"},
        {"name": "BLOCK_TTL_SECONDS", "value": "3600"},
        {"name": "ALERT_PHONE", "value": data["alert_phone"]},
        {"name": "DEBOUNCE_BYPASS_PHONES", "value": data["alert_phone"]},
        {"name": "ALLOWED_PHONES", "value": ""},
    ]


def register_sai_tools_client(data: dict) -> None:
    """Adiciona o cliente no clients.json do repo sai-tools."""
    import base64

    r = run(f"gh api repos/{SAI_TOOLS_REPO}/contents/clients.json", check=False)
    if r.returncode != 0:
        print(f"    AVISO: Nao foi possivel acessar sai-tools: {r.stderr[:150]}")
        return

    file_info = json.loads(r.stdout)
    current_content = base64.b64decode(
        file_info["content"].replace("\n", "")
    ).decode("utf-8")
    clients = json.loads(current_content)

    new_url = f"https://{WEBHOOK_DOMAIN}/{data['slug']}"
    if any(c.get("url") == new_url for c in clients):
        print(f"    Ja registrado.")
        return

    clients.append({"name": data["business_name"], "url": new_url})
    new_content_b64 = base64.b64encode(
        json.dumps(clients, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    payload = json.dumps({
        "message": f"feat: adicionar cliente {data['business_name']}",
        "content": new_content_b64,
        "sha": file_info["sha"],
    })
    proc = subprocess.run(
        f"gh api repos/{SAI_TOOLS_REPO}/contents/clients.json -X PUT --input -",
        shell=True, input=payload, capture_output=True, text=True,
    )
    if proc.returncode == 0:
        print(f"    '{data['business_name']}' registrado no sai-tools - OK")
    else:
        print(f"    AVISO: Falha ao registrar no sai-tools: {proc.stderr[:150]}")


def print_done(data: dict, webhook_url: str | None):
    slug = data["slug"]
    repo = f"{GITHUB_OWNER}/{data['repo_name']}"
    print("\n" + "=" * 60)
    print("  PROJETO CRIADO!")
    print("=" * 60)
    print(f"""
  Repo:   https://github.com/{repo}
  URL:    https://{WEBHOOK_DOMAIN}/{slug}
  Painel: https://{WEBHOOK_DOMAIN}/{slug}/painel

  -------------------------------------------------------
  UNICO PASSO MANUAL:

  >> Configurar webhook na UAZAPI para:
     https://{WEBHOOK_DOMAIN}/{slug}
  -------------------------------------------------------
""")
    if not webhook_url:
        print("  AVISO: Stack Portainer nao criada automaticamente.")
        print(f'  gh secret set PORTAINER_WEBHOOK_URL -R {repo} --body "URL"')


# ── main ─────────────────────────────────────────────────


def main():
    check_prereqs()
    data = collect_inputs()

    repo_dir = create_repo(data)
    replace_placeholders(repo_dir, data)
    generate_env(repo_dir, data)
    commit_push(repo_dir, data)

    print("\n  [5/8] Permissoes GitHub Actions...")
    gh_actions_permissions(data["repo_name"])

    print("\n  [6/8] Aguardando build...")
    build_ok = wait_build(data["repo_name"])

    if build_ok:
        print("\n  [7/8] GHCR publico...")
        gh_package_public(data["repo_name"])
    else:
        print("\n  [7/8] Build nao completou. GHCR manual depois.")

    print("\n  [8/8] Stack Portainer...")
    env_list = build_env_list(data)
    webhook_url, stack_id = create_portainer_stack(data, env_list)

    if webhook_url:
        print("\n  Salvando webhook secret...")
        gh_secret(data["repo_name"], "PORTAINER_WEBHOOK_URL", webhook_url)

    if stack_id:
        print("\n  Redeploy com registry auth (GHCR requer autenticacao no Docker)...")
        redeploy_stack_with_auth(stack_id, env_list)

    print("\n  [10/10] Registrando no sai-tools...")
    register_sai_tools_client(data)

    print_done(data, webhook_url)


if __name__ == "__main__":
    main()
