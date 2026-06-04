#!/usr/bin/env python3
"""
Onboarding de cliente — esteira completa (chatbot + SAI Comercial).

Recebe o YAML de negocio + slug + email do cliente e executa:
 1. Cria a GEMINI API KEY (gcloud, 1 projeto GCP por cliente)
 2. Cria pasta + repo (template plano-pleno) + .env + client.yaml
 3. Push + build (GitHub Actions) + GHCR publico
 4. Cria a stack no Portainer (1a passada, sem token/tenant ainda)
 5. Registra o chatbot no catalogo do SAI Comercial -> chatbotId
 6. POST /api/admin/onboard: cria/linka instancia (UAZAPI/SAIZAP), provisiona
    tenant + usuario admin + bot + ponte, configura o webhook unico, devolve
    tenantId, ingestSecret, token da instancia e QR/paircode
 7. 2a passada: injeta UAZAPI_TOKEN + SAI_TENANT_ID + SAI_INGEST_SECRET no stack
 8. Resumo: QR/paircode, webhook, credenciais de login do cliente

Reaproveita as funcoes do setup.py (mesma pasta).

Uso:
  python onboard.py --yaml negocio.yaml --slug meu-cliente --admin-email dono@cliente.com \\
      [--provider UAZAPI|SAIZAP] [--instance-mode CREATE|LINK] [--external-id <id|name>] \\
      [--instance-name nome] [--name "Nome do Negocio"] [--alert-phone 5511999990000] \\
      [--tier START|PLENO|PREMIUM] [--has-crm] [--has-disparador] \\
      [--gemini-key <chave>] [--skip-gcp] [--gcp-billing <billing-id>]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import yaml

import setup  # reaproveita o setup.py do template (mesma pasta)


# ── helpers ──────────────────────────────────────────────


def _rand_suffix(n: int = 4) -> str:
    return os.urandom(8).hex()[:n]


def ask(prompt: str, default: str = "", required: bool = True) -> str:
    if default:
        v = input(f"  {prompt} [{default}]: ").strip()
        return v or default
    while True:
        v = input(f"  {prompt}{': ' if required else ' [Enter para pular]: '}").strip()
        if v or not required:
            return v
        print("    (campo obrigatorio)")


# ── YAML -> SAI assistantConfig ──────────────────────────

_WEEKMAP = {"mon_fri": [1, 2, 3, 4, 5], "sat": [6], "sun": [0]}  # SAI: dom=0..sab=6
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def yaml_to_business_hours(ydoc: dict) -> list:
    """Mapeia appointments.business_hours ({mon_fri,sat,sun}) -> BusinessHoursSchema."""
    bh = (((ydoc or {}).get("appointments") or {}).get("business_hours")) or {}
    by_weekday = {w: [] for w in range(7)}
    for key, weekdays in _WEEKMAP.items():
        for rng in (bh.get(key) or []):
            start, _, end = str(rng).partition("-")
            start, end = start.strip(), end.strip()
            if _HHMM.match(start) and _HHMM.match(end):
                for w in weekdays:
                    by_weekday[w].append({"start": start, "end": end})
    return [
        {"weekday": w, "windows": wins[:4]}
        for w, wins in sorted(by_weekday.items())
        if wins
    ]


def parse_price_to_cents(price) -> int | None:
    """'R$ 1.200,00 por mes' -> 120000. Sem valor confiavel -> None."""
    if price is None:
        return None
    s = str(price)
    m = re.search(r"(\d[\d.\s]*)(?:,(\d{2}))?", s)
    if not m:
        return None
    reais = re.sub(r"[.\s]", "", m.group(1))
    cents = m.group(2) or "00"
    if not reais.isdigit():
        return None
    try:
        return int(reais) * 100 + int(cents)
    except ValueError:
        return None


def yaml_to_products(ydoc: dict) -> list:
    out = []
    for p in (ydoc or {}).get("plans") or []:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name[:120],
            "priceCents": parse_price_to_cents(p.get("price")),
            "description": (str(p.get("description") or "").strip() or None),
        })
    return out[:500]


# ── Gemini key via gcloud (projeto por cliente) ──────────


def create_gemini_key(slug: str, billing_id: str | None) -> str:
    if shutil.which("gcloud") is None:
        raise RuntimeError(
            "gcloud nao encontrado. Instale o Google Cloud SDK e rode `gcloud auth login`, "
            "ou passe --gemini-key <chave> / --skip-gcp."
        )

    base = re.sub(r"[^a-z0-9-]", "", slug.lower()).strip("-")[:18] or "cliente"
    project_id = f"sai-{base}-{_rand_suffix()}"[:30].strip("-")

    def g(*args, check=True):
        cmd = ["gcloud", *args]
        print(f"    $ {' '.join(cmd)}")
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

    print(f"  Criando projeto GCP {project_id}...")
    r = g("projects", "create", project_id, "--name", f"SAI {slug}", check=False)
    if r.returncode != 0 and "already exists" not in (r.stderr + r.stdout).lower():
        raise RuntimeError(f"gcloud projects create falhou: {r.stderr[:300]}")

    if billing_id:
        g("billing", "projects", "link", project_id, f"--billing-account={billing_id}", check=False)

    g("services", "enable", "generativelanguage.googleapis.com", f"--project={project_id}")

    r = g(
        "services", "api-keys", "create",
        f"--project={project_id}",
        "--display-name", f"gemini-{slug}",
        "--api-target=service=generativelanguage.googleapis.com",
        "--format=json",
    )
    key = ""
    try:
        data = json.loads(r.stdout or "{}")
        key = (
            data.get("keyString")
            or (data.get("response") or {}).get("keyString")
            or ""
        )
        key_name = data.get("name") or (data.get("response") or {}).get("name") or ""
    except json.JSONDecodeError:
        key_name = ""
    if not key and key_name:
        # fallback: buscar a keyString pelo nome do recurso
        rk = g("services", "api-keys", "get-key-string", key_name,
               "--format=value(keyString)", check=False)
        key = rk.stdout.strip()
    if not key:
        raise RuntimeError("gcloud criou o projeto/chave mas nao retornou a keyString.")
    print("  Gemini API key criada - OK")
    return key


# ── chamada ao endpoint de onboarding do SAI Comercial ───


def call_onboard(payload: dict) -> dict:
    token = setup.SECRETS.get("ONBOARD_TOKEN")
    if not token:
        sys.exit("  ERRO: ONBOARD_TOKEN ausente em ~/.claude/.env (mesmo valor do env do service sai-comercial_web).")
    url = f"{setup.SAI_COMERCIAL_URL}/api/admin/onboard"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-onboard-token": token},
    )
    try:
        with urllib.request.urlopen(req, context=setup._SSL, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        sys.exit(f"  ERRO onboard ({e.code}): {detail}")
    except Exception as e:
        sys.exit(f"  ERRO onboard: {e}")


# ── main ─────────────────────────────────────────────────


def parse_args():
    p = argparse.ArgumentParser(description="Onboarding completo de cliente (chatbot + SAI Comercial).")
    p.add_argument("--yaml", required=True, help="Caminho do client.yaml de negocio preenchido")
    p.add_argument("--slug", required=True)
    p.add_argument("--admin-email", required=True)
    p.add_argument("--name", default="", help="Nome do negocio (default: business.name do YAML)")
    p.add_argument("--alert-phone", default="", help="Telefone do dono (ALERT_PHONE)")
    p.add_argument("--provider", choices=["UAZAPI", "SAIZAP"], default=None)
    p.add_argument("--instance-mode", choices=["CREATE", "LINK"], default=None)
    p.add_argument("--external-id", default=None, help="LINK: id (UAZAPI) ou name (SAIZAP) da instancia")
    p.add_argument("--instance-name", default=None)
    p.add_argument("--tier", choices=["START", "PLENO", "PREMIUM"], default="START")
    p.add_argument("--has-crm", action="store_true")
    p.add_argument("--has-disparador", action="store_true")
    p.add_argument("--gemini-key", default=None, help="Pula gcloud e usa esta chave")
    p.add_argument("--skip-gcp", action="store_true", help="Pergunta a chave Gemini manualmente")
    p.add_argument("--gcp-billing", default=None, help="Billing account id (default: GCP_BILLING_ID do .env)")
    return p.parse_args()


def main():
    args = parse_args()

    slug = setup.slugify(args.slug)
    if slug != args.slug:
        print(f"  slug normalizado: {args.slug} -> {slug}")

    yaml_path = Path(args.yaml)
    if not yaml_path.exists():
        sys.exit(f"  ERRO: YAML nao encontrado: {yaml_path}")
    ydoc = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    raw_yaml = yaml_path.read_text(encoding="utf-8")

    business_name = args.name or str((ydoc.get("business") or {}).get("name") or slug)
    assistant_name = str((ydoc.get("assistant") or {}).get("name") or "Assistente")
    niche = str(ydoc.get("niche") or "academia")
    calendar_id = str(((ydoc.get("appointments") or {}).get("google_calendar") or {}).get("calendar_id") or "")

    # provider / modo
    provider = args.provider
    if not provider:
        provider = ask("Provider WhatsApp (UAZAPI/SAIZAP)", "UAZAPI").upper()
        if provider not in ("UAZAPI", "SAIZAP"):
            sys.exit("  ERRO: provider invalido.")
    if provider == "SAIZAP":
        instance_mode = "LINK"
    else:
        instance_mode = args.instance_mode or ask("Modo da instancia (CREATE/LINK)", "CREATE").upper()
    external_id = args.external_id
    if instance_mode == "LINK" and not external_id:
        external_id = ask("externalId da instancia (id UAZAPI / name SAIZAP)")

    check = setup.check_prereqs  # valida gh/gh auth
    check()

    print("\n" + "=" * 60)
    print(f"  ONBOARDING: {business_name}  (slug: {slug})")
    print(f"  Provider: {provider} / {instance_mode}   Tier: {args.tier}")
    print("=" * 60)

    # ── [1] Gemini key ──────────────────────────────────────
    print("\n  [1/8] Gemini API key...")
    if args.gemini_key:
        gemini_key = args.gemini_key.strip()
        print("    usando --gemini-key")
    elif args.skip_gcp:
        gemini_key = ask("Cole a GEMINI API KEY (AI Studio)")
    else:
        billing = args.gcp_billing or setup.SECRETS.get("GCP_BILLING_ID")
        try:
            gemini_key = create_gemini_key(slug, billing)
        except Exception as e:
            print(f"    AVISO: gcloud falhou ({e}).")
            gemini_key = ask("Cole a GEMINI API KEY manualmente (ou Enter p/ preencher depois)", required=False)

    data = {
        "business_name": business_name,
        "slug": slug,
        "assistant_name": assistant_name,
        "alert_phone": args.alert_phone,
        "uazapi_token": "",            # 2a passada (vem do onboard)
        "gemini_key": gemini_key,
        "sheet_id": "",
        "google_creds": (
            json.dumps(setup.SECRETS["GOOGLE_CREDENTIALS_JSON"])
            if isinstance(setup.SECRETS.get("GOOGLE_CREDENTIALS_JSON"), dict)
            else str(setup.SECRETS.get("GOOGLE_CREDENTIALS_JSON", ""))
        ),
        "calendar_id": calendar_id,
        "repo_name": slug,
        "niche": niche,
        "sai_tenant_id": "",           # 2a passada
        "sai_ingest_secret": "",       # 2a passada
    }

    # ── [2] repo + env + client.yaml ────────────────────────
    print("\n  [2/8] Repo + .env + client.yaml...")
    repo_dir = setup.create_repo(data)
    setup.replace_placeholders(repo_dir, data)
    setup.generate_env(repo_dir, data)
    # client.yaml do negocio sobrescreve o gerado heuristicamente
    shutil.copyfile(yaml_path, repo_dir / "client.yaml")
    print("    client.yaml do negocio copiado - OK")

    # ── [3] push + build + GHCR ─────────────────────────────
    print("\n  [3/8] Push + build...")
    setup.commit_push(repo_dir, data)
    setup.gh_actions_permissions(data["repo_name"])
    build_ok = setup.wait_build(data["repo_name"])
    if build_ok:
        setup.gh_package_public(data["repo_name"])

    # ── [4] stack Portainer (1a passada) ────────────────────
    print("\n  [4/8] Stack Portainer (1a passada)...")
    env_list = setup.build_env_list(data)
    webhook_url, stack_id = setup.create_portainer_stack(data, env_list)
    if webhook_url:
        setup.gh_secret(data["repo_name"], "PORTAINER_WEBHOOK_URL", webhook_url)
    if stack_id:
        setup.redeploy_stack_with_auth(stack_id, env_list)

    # ── [5] registra chatbot -> chatbotId ───────────────────
    print("\n  [5/8] Registrando chatbot no SAI Comercial...")
    chatbot_id = setup.register_chatbot_comercial(data)
    if not chatbot_id:
        print("    AVISO: chatbotId nao obtido; o tenant sera criado sem vinculo de chatbot.")

    # ── [6] onboard (tenant + instancia + bot + webhook) ────
    print("\n  [6/8] Provisionando tenant + instancia (POST /api/admin/onboard)...")
    payload = {
        "slug": slug,
        "name": business_name,
        "adminEmail": args.admin_email,
        "provider": provider,
        "instanceMode": instance_mode,
        "assistenteIATier": args.tier,
        "hasCRM": bool(args.has_crm),
        "hasDisparador": bool(args.has_disparador),
        "assistantConfig": {
            "displayName": assistant_name,
            "businessHours": yaml_to_business_hours(ydoc),
            "products": yaml_to_products(ydoc),
            "sourceYaml": raw_yaml,
        },
    }
    if chatbot_id:
        payload["chatbotId"] = chatbot_id
    if external_id:
        payload["externalId"] = external_id
    if args.instance_name:
        payload["instanceName"] = args.instance_name

    res = call_onboard(payload)
    tenant_id = res.get("tenantId")
    admin_password = res.get("adminPassword")
    bot = res.get("bot") or {}
    inst = res.get("instance") or {}
    wh = res.get("webhook") or {}
    ingest_secret = bot.get("ingestSecret", "")
    inst_token = inst.get("token", "")
    print(f"    tenant {slug} criado (id {tenant_id}) - OK")

    # ── [7] 2a passada: UAZAPI_TOKEN + SAI_TENANT_ID/SECRET ─
    print("\n  [7/8] 2a passada (UAZAPI_TOKEN + SAI_TENANT_ID/SECRET no stack)...")
    data["uazapi_token"] = inst_token or data["uazapi_token"]
    data["sai_tenant_id"] = tenant_id or ""
    data["sai_ingest_secret"] = ingest_secret
    if stack_id:
        env_list2 = setup.build_env_list(data)
        setup.update_stack_env(stack_id, env_list2)
        # atualiza tambem o .env do repo (referencia) e versiona
        setup.generate_env(repo_dir, data)
        setup.run("git add -A", cwd=str(repo_dir), check=False)
        setup.run('git commit -m "chore: env do SAI Comercial (tenant/ingest/token)"',
                  cwd=str(repo_dir), check=False)
        setup.run("git push origin main", cwd=str(repo_dir), check=False)
    else:
        print("    AVISO: stack nao criada — rode novamente os envs manualmente no Portainer:")
        print(f"      UAZAPI_TOKEN, SAI_TENANT_ID={tenant_id}, SAI_INGEST_SECRET=<ingest>")

    setup.register_sai_tools_client(data)

    # ── [8] resumo ──────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  ONBOARDING CONCLUIDO!")
    print("=" * 60)
    print(f"""
  Negocio : {business_name}
  Slug    : {slug}
  Repo    : https://github.com/{setup.GITHUB_OWNER}/{slug}
  Bot URL : https://{setup.WEBHOOK_DOMAIN}/{slug}

  LOGIN DO CLIENTE (painel SAI Comercial):
    URL   : {setup.SAI_COMERCIAL_URL}/login
    Email : {res.get('adminEmail')}
    Senha : {admin_password}

  INSTANCIA ({inst.get('provider')}):
    status: {inst.get('status')}   externalId: {inst.get('externalId')}""")
    if inst.get("paircode"):
        print(f"    PAIRCODE (digite no WhatsApp): {inst.get('paircode')}")
    if inst.get("qrcode"):
        print("    QR CODE retornado (escaneie). Tambem disponivel na tela do Disparador no painel.")
    if not inst.get("paircode") and not inst.get("qrcode") and inst.get("status") != "CONNECTED":
        print("    Conecte o numero escaneando o QR na tela do Disparador (painel).")

    print(f"""
  WEBHOOK (unico, automatico):
    ok     : {wh.get('ok')}
    url    : {wh.get('url')}
    eventos: {', '.join(wh.get('events') or [])}
    excluir: {', '.join(wh.get('exclude') or [])}""")
    if not wh.get("ok"):
        print("    AVISO: webhook nao confirmado. Configure manualmente na instancia:")
        print(f"      URL: {setup.SAI_COMERCIAL_URL}/api/webhooks/uazapi/{slug}")
        print("      Eventos: connection, messages, messages_update | Excluir: isgroupyes")
    print("")


if __name__ == "__main__":
    main()
