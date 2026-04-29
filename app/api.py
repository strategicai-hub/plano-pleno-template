"""
Rotas de observabilidade/logs para painel externo.
Prefixo derivado de settings.WEBHOOK_PATH
"""
import json
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import settings
from app.consumer import _parse_ai_response
from app.services import gemini, redis_keys as keys
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


class ChatTestBody(BaseModel):
    phone: str = "5511999999999"
    message: str = ""


def _test_phone(phone: str) -> str:
    return f"test_{phone}"


@router.post("/chat-test")
async def chat_test_post(body: ChatTestBody):
    """Envia mensagem de teste para a IA. NAO chama UAZAPI."""
    phone = _test_phone(body.phone)
    response_text, tokens = await gemini.chat(phone, body.message)
    parts, finalizado, transferir = _parse_ai_response(response_text)
    return {
        "raw": response_text,
        "parts": parts,
        "finalizado": finalizado,
        "transferir": transferir,
        "tokens": {"input": tokens[0], "output": tokens[1], "total": tokens[2]},
    }


@router.post("/chat-test/reset")
async def chat_test_reset(body: ChatTestBody):
    """Limpa historico, lead e buffer da sessao de teste."""
    r = await get_redis()
    phone = _test_phone(body.phone)
    deleted = 0
    for key in (keys.history_key(phone), keys.lead_key(phone), keys.buffer_key(phone)):
        deleted += await r.delete(key)
    return {"ok": True, "deleted": deleted}


@router.get("/chat-test/history")
async def chat_test_history(phone: str = "5511999999999"):
    """Retorna o historico parseado da sessao de teste."""
    r = await get_redis()
    raw = await r.lrange(keys.history_key(_test_phone(phone)), 0, -1)
    history = []
    for item in raw:
        try:
            entry = json.loads(item)
            role = entry.get("type", "")
            content = entry.get("data", {}).get("content", "")
            if role == "model":
                parts, _, _ = _parse_ai_response(content)
            else:
                parts = [{"type": "text", "content": content}]
            history.append({"role": role, "parts": parts})
        except Exception:
            pass
    return history


@router.get("/chat-test", response_class=HTMLResponse)
async def chat_test_ui():
    """Painel de teste estilo WhatsApp. Conversa com a IA sem chamar UAZAPI."""
    bname = settings.BUSINESS_NAME
    aname = settings.ASSISTANT_NAME
    wpath = settings.WEBHOOK_PATH
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{bname} — Teste do bot</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #e5ddd5; height: 100vh; display: flex; flex-direction: column; }}
  header {{ background: #075e54; color: #fff; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 1px 3px rgba(0,0,0,.2); }}
  header .info h1 {{ font-size: 16px; font-weight: 500; }}
  header .info small {{ font-size: 12px; opacity: .8; }}
  header .controls {{ display: flex; gap: 8px; align-items: center; font-size: 12px; }}
  header input {{ background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.3); color: #fff; padding: 4px 8px; border-radius: 4px; font-size: 12px; width: 140px; }}
  header input::placeholder {{ color: rgba(255,255,255,.6); }}
  header button {{ background: #25d366; color: #fff; border: none; padding: 5px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }}
  header button:hover {{ background: #1da851; }}
  header button.reset {{ background: #c0392b; }}
  header button.reset:hover {{ background: #a32d22; }}
  #chat {{ flex: 1; overflow-y: auto; padding: 16px; }}
  .msg {{ max-width: 70%; margin-bottom: 8px; padding: 8px 12px; border-radius: 8px; word-wrap: break-word; box-shadow: 0 1px 2px rgba(0,0,0,.1); position: relative; clear: both; }}
  .msg.user {{ background: #dcf8c6; float: right; border-bottom-right-radius: 2px; }}
  .msg.bot {{ background: #fff; float: left; border-bottom-left-radius: 2px; }}
  .msg p {{ white-space: pre-wrap; line-height: 1.4; font-size: 14px; }}
  .msg img {{ max-width: 100%; border-radius: 4px; display: block; }}
  .msg .ts {{ font-size: 10px; color: #999; text-align: right; margin-top: 4px; }}
  .clearfix {{ clear: both; }}
  #typing {{ display: none; float: left; background: #fff; padding: 8px 14px; border-radius: 8px; margin-bottom: 8px; clear: both; }}
  #typing.on {{ display: block; }}
  #typing span {{ display: inline-block; width: 6px; height: 6px; background: #aaa; border-radius: 50%; margin: 0 2px; animation: blink 1.4s infinite both; }}
  #typing span:nth-child(2) {{ animation-delay: .2s; }}
  #typing span:nth-child(3) {{ animation-delay: .4s; }}
  @keyframes blink {{ 0%, 80%, 100% {{ opacity: .2; }} 40% {{ opacity: 1; }} }}
  footer {{ background: #f0f0f0; padding: 10px; display: flex; gap: 8px; }}
  #input {{ flex: 1; padding: 10px 14px; border: 1px solid #ccc; border-radius: 20px; font-size: 14px; outline: none; background: #fff; }}
  #send {{ background: #075e54; color: #fff; border: none; width: 44px; height: 44px; border-radius: 50%; cursor: pointer; font-size: 18px; }}
  #send:disabled {{ background: #999; cursor: not-allowed; }}
  .system {{ text-align: center; color: #555; font-size: 11px; padding: 6px; clear: both; background: rgba(255,255,255,.6); border-radius: 6px; margin: 8px auto; max-width: 60%; }}
</style>
</head>
<body>
<header>
  <div class="info">
    <h1>🧪 {aname} — modo teste</h1>
    <small>{bname} · sem WhatsApp · sem UAZAPI</small>
  </div>
  <div class="controls">
    <span>phone:</span>
    <input id="phone" value="5511999999999" placeholder="5511999999999">
    <button class="reset" onclick="resetChat()">🗑️ Resetar</button>
  </div>
</header>
<div id="chat"></div>
<footer>
  <input id="input" placeholder="Digite sua mensagem..." autocomplete="off">
  <button id="send" onclick="sendMsg()">➤</button>
</footer>
<script>
const WPATH = '{wpath}';
const chatEl = document.getElementById('chat');
const phoneEl = document.getElementById('phone');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');

function escapeHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function addBubble(role, parts, opts) {{
  opts = opts || {{}};
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  for (const p of parts) {{
    if (p.type === 'image') {{
      const img = document.createElement('img');
      img.src = p.content;
      wrap.appendChild(img);
    }} else if (p.type === 'document' || p.type === 'video') {{
      const a = document.createElement('a');
      a.href = p.content; a.target = '_blank'; a.textContent = '📎 ' + p.type + ' (clique)';
      wrap.appendChild(a);
    }} else {{
      const para = document.createElement('p');
      para.innerHTML = escapeHtml(p.content);
      wrap.appendChild(para);
    }}
  }}
  if (opts.flags) {{
    const meta = document.createElement('div');
    meta.className = 'ts';
    meta.textContent = opts.flags;
    wrap.appendChild(meta);
  }}
  chatEl.appendChild(wrap);
  const clr = document.createElement('div'); clr.className = 'clearfix'; chatEl.appendChild(clr);
  chatEl.scrollTop = chatEl.scrollHeight;
}}

function addSystem(text) {{
  const d = document.createElement('div');
  d.className = 'system';
  d.textContent = text;
  chatEl.appendChild(d);
  chatEl.scrollTop = chatEl.scrollHeight;
}}

function showTyping(on) {{
  let el = document.getElementById('typing');
  if (!el) {{
    el = document.createElement('div');
    el.id = 'typing';
    el.innerHTML = '<span></span><span></span><span></span>';
    chatEl.appendChild(el);
  }}
  el.className = on ? 'on' : '';
  if (on) chatEl.scrollTop = chatEl.scrollHeight;
}}

async function sendMsg() {{
  const msg = inputEl.value.trim();
  if (!msg) return;
  inputEl.value = '';
  sendBtn.disabled = true;
  addBubble('user', [{{type: 'text', content: msg}}]);
  showTyping(true);
  try {{
    const r = await fetch(WPATH + '/chat-test', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{phone: phoneEl.value, message: msg}}),
    }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    showTyping(false);
    let flags = '';
    if (data.finalizado) flags += '[FINALIZADO] ';
    if (data.transferir) flags += '[TRANSFERIR EQUIPE] ';
    if (data.tokens) flags += `tokens: ${{data.tokens.total}}`;
    addBubble('bot', data.parts, {{flags}});
  }} catch (e) {{
    showTyping(false);
    addSystem('Erro: ' + e.message);
  }}
  sendBtn.disabled = false;
  inputEl.focus();
}}

async function resetChat() {{
  if (!confirm('Limpar historico desta sessao de teste?')) return;
  try {{
    await fetch(WPATH + '/chat-test/reset', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{phone: phoneEl.value, message: ''}}),
    }});
    chatEl.innerHTML = '';
    addSystem('Historico resetado.');
  }} catch (e) {{ addSystem('Erro ao resetar: ' + e.message); }}
}}

async function loadHistory() {{
  try {{
    const r = await fetch(WPATH + '/chat-test/history?phone=' + encodeURIComponent(phoneEl.value));
    const hist = await r.json();
    chatEl.innerHTML = '';
    if (!hist.length) {{
      addSystem('Conversa nova. Digite uma mensagem para comecar.');
      return;
    }}
    for (const h of hist) {{
      addBubble(h.role === 'user' ? 'user' : 'bot', h.parts);
    }}
  }} catch (e) {{ addSystem('Erro ao carregar: ' + e.message); }}
}}

inputEl.addEventListener('keypress', (e) => {{ if (e.key === 'Enter') sendMsg(); }});
phoneEl.addEventListener('change', loadHistory);
loadHistory();
</script>
</body>
</html>"""
    return html


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
