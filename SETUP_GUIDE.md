# PLANO PLENO - Guia de Criacao de Novo Projeto

> **Nota:** este template estende o `plano-start-template` com follow-ups
> automaticos, agendamento via Google Calendar e integracoes com sistemas
> externos. Para o fluxo enxuto (apenas IA + Sheets), use o `plano-start-template`.

## Pre-requisitos

- **gh CLI** instalado e autenticado (`gh auth login`)
  - Instalar: https://cli.github.com/
- **Python 3.12+** instalado
- **Git** instalado

## O que ter em maos antes de comecar

| Item | Onde conseguir |
|------|---------------|
| UAZAPI token | Painel UAZAPI > Instancia > Token |
| UAZAPI instancia | Nome da instancia criada no UAZAPI |
| GEMINI API key | https://aistudio.google.com/apikey |
| Google Service Account credentials JSON | Console GCP > Service Account > Keys (mesmo usado para Sheets) |
| Google Sheet ID | URL da planilha (entre /d/ e /edit) |
| **Google Calendar ID** | Config do calendario > "Integrar calendario" (ver secao abaixo) |
| Telefone do dono | Numero com DDI (ex: 5511999990000) |
| Dados do negocio | Nome, endereco, horarios, precos, professores |
| Janela de funcionamento | Usada para IA sugerir slots dentro do horario certo |

---

## Criar novo projeto

```bash
# Na primeira vez, clone o template
gh repo clone gustavocastilho-hub/plano-pleno-template

# Entre no diretorio
cd plano-pleno-template

# Execute o setup
python setup.py
```

O script vai perguntar:
1. **Nome do negocio** - ex: "Academia Muay Thai SP"
2. **Slug** - identificador unico (ex: "muaythai-sp"), gerado automaticamente
3. **Nome da assistente** - ex: "Bia"
4. **Telefone do dono** - para receber alertas
5. **UAZAPI token** - token da instancia WhatsApp
6. **GEMINI API key** - chave da API do Google Gemini
7. **Google Sheet ID** - ID da planilha de leads (pode pular)
8. **Google Calendar ID** - ID do calendario para agendamentos (pode pular)
9. **Google credentials JSON** - caminho do arquivo (pode pular)

### O que o setup faz automaticamente:

| Passo | Acao | Status |
|-------|------|--------|
| 1 | Cria repositorio no GitHub | Automatico |
| 2 | Substitui placeholders nos deploys | Automatico |
| 3 | Gera `.env` e `client.yaml` | Automatico |
| 4 | Commit e push (dispara build) | Automatico |
| 5 | Configura permissoes do GitHub Actions | Automatico |
| 6 | Aguarda build da imagem Docker | Automatico |
| 7 | Torna pacote GHCR publico | Automatico |
| 8 | Cria stack no Portainer + webhook | Automatico |
| 9 | Salva webhook URL como GitHub secret | Automatico |

---

## Preparar Google Calendar (antes do setup ou em seguida)

O plano pleno cria eventos no Google Calendar diretamente quando a IA emite
a flag `[AGENDAR=...]`. Passos:

1. **Criar/escolher o calendario** que sera usado (ex: "Aulas experimentais").
2. **Habilitar a Google Calendar API** no projeto GCP onde esta o Service
   Account: Console GCP > APIs & Services > Library > "Google Calendar API"
   > Enable.
3. **Compartilhar o calendario com o Service Account** (o mesmo email que ja
   e usado para Sheets):
   - Calendario > Configuracoes > Compartilhar com pessoas e grupos
   - Adicionar o email `xxx@yyy.iam.gserviceaccount.com`
   - Permissao: **Fazer mudancas nos eventos**
4. **Pegar o Calendar ID**:
   - Configuracoes > (nome do calendario) > "Integrar calendario"
   - Campo "ID do calendario" (ex: `abc123@group.calendar.google.com`)
5. **Colar** esse ID quando o `setup.py` perguntar.

> Se voce nao usa Google Calendar (ex: cliente que agenda em sistema
> proprio), pode pular esse passo. Configure `appointments.source:
> external_system` no `client.yaml` e preencha o driver em
> `app/services/external_system/`.

---

## Apos o setup

### 1. Preencher client.yaml

Abra o arquivo `client.yaml` no novo repositorio e preencha **todos** os dados do negocio.

#### Secoes do client.yaml

**`business`** - Dados basicos
```yaml
business:
  name: "Academia Muay Thai SP"
  type: "academia de muay thai"
  address: "Rua Exemplo, 123 - Bairro - Cidade/UF"
```

**`assistant`** - Nome e saudacao
```yaml
assistant:
  name: "Bia"
  greeting: "Ola! Sou a Bia, tudo bem?"
```

**`schedule`** - Horarios de aula
```yaml
schedule:
  class_duration: "1 hora"
  weekdays:
    morning: ["7h", "8h", "9h"]
    afternoon: ["14h", "15h"]
    evening: ["18h", "19h", "20h"]
  saturday:
    morning: ["9h", "10h"]
```

**`teachers`** - Professores
```yaml
teachers:
  - name: "JOAO SILVA"
    bio: "Descricao do professor."
```

**`plans`** - Planos e valores
```yaml
plans:
  - name: "Plano Mensal"
    price: "R$ 200,00 por mes"
    description: "Acesso livre."
```

**`promotions`** - Promocoes ativas
```yaml
promotions:
  - name: "INAUGURACAO"
    details:
      - "Aula experimental gratuita"
```

**`media`** - Midias (imagens, videos)
```yaml
media:
  "[IMAGEM_ACADEMIA]":
    url: "https://exemplo.com/foto.jpg"
    type: "image"
```

**`appointments`** (PLENO) - Configuracao de agendamento
```yaml
appointments:
  source: "google_calendar"       # ou "external_system" para sistema proprio
  google_calendar:
    calendar_id: "abc123@group.calendar.google.com"
  slot_duration_minutes: 60       # duracao padrao de cada aula
  lead_time_minutes: 60           # antecedencia minima entre agendamento e aula
  business_hours:                 # janela que a IA respeita ao sugerir slots
    mon_fri: ["06:00-22:00"]
    sat: ["09:00-13:00"]
    sun: []
```

> O `calendar_id` tambem pode vir do `.env` (GOOGLE_CALENDAR_ID); o YAML
> tem prioridade se preenchido.

**`followups`** (PLENO) - Follow-ups automaticos
```yaml
followups:
  reactivation:
    enabled: true
    inactive_hours: 24            # dispara apos N horas sem resposta
    max_stages: 3                 # ate N tentativas de reengajamento
    day_of_week: "mon-fri"
    cadence_minutes: 1
  appointment_reminder:
    enabled: true
    hours_before: 3               # envia lembrete X horas antes
    cadence_minutes: 15
  templates:
    reactivation_stage_1: "Oi {nome}, passando pra saber se ainda tem interesse!"
    reactivation_stage_2: "Oi {nome}, consegui um horario especial — quer aproveitar?"
    reactivation_stage_3: "Oi {nome}, ultima chance — posso segurar sua vaga?"
    appointment_reminder: "Lembrete: sua aula e hoje as {horario}. Te esperamos!"
```

> Para desabilitar um follow-up, basta `enabled: false`. Placeholders
> suportados nos templates: `{nome}`, `{horario}`, `{modalidade}`.

Apos preencher, faca o push:
```bash
cd {slug}
git add client.yaml
git commit -m "feat: dados do negocio preenchidos"
git push
```

### 2. Configurar webhook na UAZAPI

No painel UAZAPI, configure o webhook da instancia para:
```
https://webhook-whatsapp.strategicai.com.br/{slug}
```

> Este e o **unico passo manual** necessario.

### 3. Testar

1. Envie uma mensagem para o numero WhatsApp da instancia
2. Acesse o painel: `https://webhook-whatsapp.strategicai.com.br/{slug}/painel`
3. Verifique se a mensagem apareceu e se o bot respondeu
4. **Fluxo de agendamento**: simule uma conversa levando ate a FASE 4. Ao
   confirmar o horario, o painel deve mostrar `[TOOL AGENDAR] Resultado:
   SUCESSO` e um evento deve aparecer no Google Calendar.
5. **Follow-ups**: logs do container `{slug}-scheduler` devem mostrar os
   jobs habilitados em `client.yaml > followups`. Para uma validacao segura
   em producao, setar `FOLLOWUP_DRY_RUN=true` na primeira semana — os
   jobs logam o que enviariam sem chamar a UAZAPI.

### Smoke test local (opcional, sem precisar do deploy completo)

```bash
cd {slug}
pip install -r requirements.txt
python scripts/smoke.py
```

O script valida: init_db, schema do SQLite, parser de `[AGENDAR=...]`,
templates de follow-up e que `appointments:` esta preenchido no YAML.

---

## Atualizando a partir do template

Quando fizer melhorias no template e quiser aplicar nos projetos existentes:

```bash
# No diretorio do projeto do cliente
cd {slug}

# Adicionar o template como remote (so precisa fazer 1 vez)
git remote add template https://github.com/gustavocastilho-hub/plano-pleno-template.git

# Buscar atualizacoes
git fetch template

# Fazer merge das atualizacoes
git merge template/main --allow-unrelated-histories

# Resolver conflitos se houver, depois push
git push
```

---

## Ajustando o template

Para fazer melhorias que valem para todos os futuros projetos:

```bash
cd plano-pleno-template

# Faca suas alteracoes
git add -A
git commit -m "feat: descricao da melhoria"
git push
```

---

## Resumo do fluxo

```
python setup.py
    |
    v
[Tudo automatico: repo, build, Portainer, secrets]
    |
    v
Preencher client.yaml + push
    |
    v
Configurar webhook UAZAPI (unico passo manual)
    |
    v
PRONTO! Bot funcionando.
```
