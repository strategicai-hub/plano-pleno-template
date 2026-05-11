# Instruções para o assistente de IA

## Estrutura do projeto

Este repositório (`plano-pleno-template`) é um **template derivado** do `plano-start-template`. Ele adiciona ao start:

- Agendamento direto pela IA (flag `[AGENDAR=<iso-datetime>]`).
- Follow-ups automáticos (reativação e lembrete de agendamento) via APScheduler.
- Persistência durável em SQLite (complementa o Redis efêmero).
- Integração com Google Calendar (padrão) ou sistema externo do cliente (ex.: CloudGym).

Cada cliente do plano pleno tem seu próprio repositório separado, criado a partir deste template.

## Hierarquia de sincronização

A propagação é em cascata:

```
plano-start-template  →  plano-pleno-template  →  clientes do plano pleno
```

- **start → pleno**: commits genéricos do start são cherry-picked aqui pelo `sync-to-derived.sh` do start (este repo está listado lá como derivado).
- **pleno → clientes**: commits genéricos daqui são cherry-picked para os clientes listados abaixo pelo `scripts/sync-to-derived.sh` deste repo.

## SDK Gemini obrigatório: `google-genai` + `thinking_budget=0`

**Este template usa `google-genai` (SDK novo) com thinking tokens desabilitados.** É proibido voltar para `google-generativeai` (legado) ou esquecer o `thinking_config=ThinkingConfig(thinking_budget=0)` em qualquer `GenerateContentConfig`.

**Por quê:** o `gemini-2.5-flash` gera tokens de raciocínio internos cobrados como output. Em bot conversacional simples, isso pode triplicar o custo. Em produção (Seven, AJE DE BOXE) a migração para `google-genai` + thinking desligado cortou ~70% do custo.

**Checklist ao alterar `app/services/gemini.py`:**
- `from google import genai` e `from google.genai import types as gtypes`
- Cliente: `genai.Client(api_key=...)` (singleton)
- Toda chamada via `await asyncio.to_thread(client.models.generate_content, model=..., contents=[...], config=GenerateContentConfig(...))`
- Toda `GenerateContentConfig` inclui `thinking_config=gtypes.ThinkingConfig(thinking_budget=0)`
- `temperature`: 0.4 chat, 0.2 transcrição/imagem, 0.6 reativação
- `max_output_tokens`: 300 chat, 150 resumo, 200 reativação
- Histórico Redis: `ltrim(-10, -1)` (10 mensagens)
- `generate_summary` só em finalização/transferência (nunca a cada turno)

## Regra principal: sincronização cliente → template pleno

Sempre que fizer uma correção ou melhoria em um projeto de cliente do plano pleno, avaliar se a mudança é **genérica** (não depende de dados específicos do cliente) e, se for, aplicar a mesma correção neste template também.

### Como identificar se vai pro template

| Tipo de mudança | Vai pro template? |
|---|---|
| Correção de bug em `app/*.py` (inclusive `followups/`, `db.py`) | Sim |
| Melhoria de regra no `prompt_template.j2` | Sim |
| Novo campo genérico no `client.example.yaml` | Sim |
| Ajuste no `scheduler.py` ou nos jobs de follow-up | Sim |
| Novo driver genérico em `app/services/external_system/` | Sim |
| Remoção de conteúdo hardcoded de outro cliente | Sim |
| Dados específicos do cliente (preços, horários, endereço) | Não |
| Credenciais de sistema externo do cliente (tokens CloudGym etc.) | Não |

### O que é específico do pleno (não propagar para o start)

- Qualquer coisa em `app/db.py`, `app/followups/`, `app/services/calendar*.py`, `app/services/external_system/`
- `scheduler.py` na raiz
- Parser de `[AGENDAR=...]` no `consumer.py`
- FASE 4 do `prompt_template.j2` (agendamento direto)
- Seções `followups:` e `appointments:` do `client.example.yaml`
- Serviço `scheduler` no `docker-compose.yml`

## Projetos derivados diretos

- [luitz-prime](https://github.com/strategicai-hub/luitz-prime) — Luitz Prime Consorcio (nicho: consorcio, assistente: Vic)

> **Importante:** esta lista é a **fonte de verdade** usada por `scripts/sync-to-derived.sh`. Ao adicionar um novo cliente derivado deste template, inclua o link do repo aqui.

## Sincronização template → projetos derivados

Para aplicar um commit genérico deste template em todos os clientes listados acima:

```bash
./scripts/sync-to-derived.sh <commit-sha>
```

O script:
1. Lê a lista de repos derivados desta seção do CLAUDE.md
2. Clona cada um, faz `git cherry-pick -x <commit-sha>` e `git push`
3. Reporta sucessos e falhas ao final

Conflitos de cherry-pick são reportados e o repo é deixado limpo (cherry-pick abortado) — resolva manualmente nesses casos.