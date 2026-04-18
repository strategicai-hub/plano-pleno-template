# PLANO PLENO - Guia de Criacao de Novo Projeto

> **Nota:** este template estende o `plano-pleno-template` com follow-ups
> automaticos, agendamento via Google Calendar e integracoes com sistemas
> externos. Para o fluxo enxuto (apenas IA + Sheets), use o `plano-pleno-template`.

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
| Google Sheets credentials JSON | Console GCP > Service Account > Keys |
| Google Sheet ID | URL da planilha (entre /d/ e /edit) |
| Telefone do dono | Numero com DDI (ex: 5511999990000) |
| Dados do negocio | Nome, endereco, horarios, precos, professores |

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
8. **Google credentials JSON** - caminho do arquivo (pode pular)

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
