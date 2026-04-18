"""
Rotas de observabilidade/logs para painel externo.
Prefixo derivado de settings.WEBHOOK_PATH
"""
import json
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings
from app.services import redis_keys as keys
from app.services.redis_service import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(prefix=settings.WEBHOOK_PATH)


@router.get("/logs/leads")
async def logs_leads():
    """Retorna todos os leads com dados de CRM."""
    r = await get_redis()

    lead_keys = await r.keys(keys.lead_scan_pattern())
    history_keys = await r.keys(keys.history_scan_pattern())

    phones: set[str] = set()
    for k in lead_keys:
        phones.add(keys.phone_from_lead_key(k))
    for k in history_keys:
        phones.add(keys.phone_from_history_key(k))

    leads = []
    for phone in sorted(phones):
        crm = await r.hgetall(keys.lead_key(phone))
        msg_count = await r.llen(keys.history_key(phone))
        has_followup = await r.exists(keys.followup_active_key(phone)) == 1
        leads.append({
            "phone": phone,
            "nome": crm.get("name", ""),
            "nicho": crm.get("nicho", ""),
            "resumo": crm.get("resumo", ""),
            "event_id": crm.get("event_id", ""),
            "msg_count": msg_count,
            "has_followup": has_followup,
        })

    leads.sort(key=lambda x: x["msg_count"], reverse=True)
    return leads


@router.get("/logs/history/{phone}")
async def logs_history(phone: str):
    """Retorna o historico de mensagens de um lead."""
    r = await get_redis()

    raw = await r.lrange(keys.history_key(phone), 0, -1)
    messages = []
    for item in raw:
        try:
            entry = json.loads(item)
            messages.append({
                "role": entry.get("type", ""),
                "content": entry.get("data", {}).get("content", ""),
            })
        except Exception:
            pass
    return messages


@router.get("/logs/events")
async def logs_events(limit: int = 100):
    """Retorna os ultimos N eventos de execucao do worker."""
    r = await get_redis()

    raw = await r.lrange(keys.session_log_key(), 0, limit - 1)
    events = []
    for item in raw:
        try:
            events.append(json.loads(item))
        except Exception:
            pass
    return events


@router.get("/painel", response_class=HTMLResponse)
async def painel():
    """Painel de logs em tempo real."""
    bname = settings.BUSINESS_NAME
    wpath = settings.WEBHOOK_PATH
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>{bname} — Painel</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #111; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 20px; }}
  h1 {{ color: #e67e22; font-size: 18px; margin-bottom: 4px; }}
  #status {{ font-size: 11px; color: #666; margin-bottom: 16px; }}
  .event {{ border: 1px solid #2a2a2a; border-radius: 6px; padding: 10px 14px; margin-bottom: 10px; background: #1a1a1a; }}
  .event-header {{ color: #555; font-size: 11px; margin-bottom: 8px; border-bottom: 1px solid #2a2a2a; padding-bottom: 5px; }}
  .event-header .phone {{ color: #3498db; font-weight: bold; }}
  .log-line {{ margin: 3px 0; font-size: 12px; line-height: 1.5; }}
  .new-badge {{ display: inline-block; background: #27ae60; color: #fff; font-size: 10px; padding: 1px 5px; border-radius: 3px; margin-left: 8px; }}
</style>
</head>
<body>
<h1>{bname} — Execucoes</h1>
<div id="status">Carregando...</div>
<div id="events"></div>
<script>
let lastTs = null;

function fmt(ts) {{
  return new Date(ts * 1000).toLocaleString('pt-BR', {{ timeZone: 'America/Sao_Paulo' }});
}}

async function refresh() {{
  try {{
    const res = await fetch('{wpath}/logs/events?limit=50');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const events = await res.json();
    const container = document.getElementById('events');
    const status = document.getElementById('status');

    if (!events.length) {{
      status.textContent = 'Nenhuma execucao registrada ainda.';
      return;
    }}

    const newest = events[0].ts;
    const isNew = newest !== lastTs;

    if (isNew) {{
      container.innerHTML = '';
      for (let i = 0; i < events.length; i++) {{
        const ev = events[i];
        const div = document.createElement('div');
        div.className = 'event';

        const header = document.createElement('div');
        header.className = 'event-header';
        header.innerHTML = fmt(ev.ts) + ' &nbsp;—&nbsp; <span class="phone">' + (ev.phone || '') + '</span>'
          + (i === 0 && lastTs !== null ? '<span class="new-badge">NOVO</span>' : '');
        div.appendChild(header);

        for (const line of (ev.lines || [])) {{
          const p = document.createElement('p');
          p.className = 'log-line';
          p.innerHTML = line;
          div.appendChild(p);
        }}
        container.appendChild(div);
      }}
      lastTs = newest;
    }}

    const now = new Date().toLocaleTimeString('pt-BR');
    status.textContent = 'Atualizado: ' + now + ' · ' + events.length + ' execucao(oes)';
  }} catch (e) {{
    document.getElementById('status').textContent = 'Erro: ' + e.message;
  }}
}}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
    return html
