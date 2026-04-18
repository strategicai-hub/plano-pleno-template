# Deploy — GitHub + GHCR + Portainer (redeploy automático)

Guia reutilizável para publicar este tipo de stack em novos clientes. O fluxo é:

```
push na main  →  GitHub Actions builda imagem  →  publica no GHCR  →  chama webhook do Portainer  →  stack puxa a nova imagem e reinicia
```

Tempo de setup para um cliente novo: ~10 minutos.

---

## 1. Pré-requisitos do cliente

- Servidor com Docker Swarm ativo (`docker swarm init` se ainda não estiver).
- Portainer CE/BE instalado e acessível.
- Rede externa `network_public` já criada no Swarm (ou ajuste o nome no `docker-compose.yml`).
- Traefik rodando e escutando na mesma rede, com `letsencryptresolver` configurado (caso use as labels deste projeto).
- Domínio apontando para o servidor (para as labels do Traefik).

---

## 2. Estrutura que precisa existir no repositório

Três arquivos fazem o fluxo funcionar:

1. **`Dockerfile`** — como a imagem é construída.
2. **`.github/workflows/docker-publish.yml`** — builda, publica no GHCR e dispara o webhook do Portainer.
3. **`docker-compose.yml`** — em formato Swarm (`deploy:` em vez de `restart:`), consumindo a imagem do GHCR por tag `:latest`.

Ao duplicar o projeto para um cliente novo, troque em todos os arquivos:

- Nome da imagem no GHCR: `ghcr.io/<owner>/<repo>:latest`
- Nome do serviço no compose (`aje-bot`)
- Hostname do Traefik (`webhook-whatsapp.strategicai.com.br`)
- Variáveis de ambiente específicas do cliente

---

## 3. Configurar o GitHub Container Registry (GHCR)

1. O workflow usa `secrets.GITHUB_TOKEN` (já existe automaticamente), então **não precisa criar PAT**.
2. Garanta que o repositório tenha permissão de escrita em packages:
   - Repo → Settings → Actions → General → Workflow permissions → **Read and write permissions**.
3. No primeiro push, a imagem é criada como **privada** por padrão. Para o Portainer conseguir puxar sem login:
   - GitHub → seu perfil/org → Packages → `<repo>` → Package settings → **Change visibility → Public**.
   - Alternativa (se precisar manter privada): configurar um Registry no Portainer com um PAT com escopo `read:packages` e linkar à stack.

---

## 4. Criar a stack no Portainer via Git

No Portainer:

1. **Stacks → Add stack**.
2. Nome: o do cliente (ex: `aje-de-boxe`).
3. Build method: **Repository**.
4. Repository URL: URL do repositório GitHub.
5. Repository reference: `refs/heads/main`.
6. Compose path: `docker-compose.yml`.
7. **Automatic updates** → ative:
   - **Webhook** (deixe desativado o polling — o webhook é mais rápido e não consome API).
   - Portainer mostrará uma **URL de webhook**. Copie — é o valor que vai no secret do GitHub.
8. Em **Environment variables**, preencha TODAS as variáveis que o `docker-compose.yml` referencia (`${VAR}`). Veja `.env.example` como checklist.
9. **Deploy the stack**.

> A URL do webhook tem o formato `https://portainer.seu-dominio/api/stacks/webhooks/<uuid>`. Tratar como segredo — quem tiver a URL pode forçar redeploys.

---

## 5. Adicionar o secret no GitHub

1. Repo → Settings → Secrets and variables → Actions → **New repository secret**.
2. Name: `PORTAINER_WEBHOOK_URL`
3. Value: a URL copiada do Portainer no passo anterior.
4. Save.

Pronto. O step `Trigger Portainer redeploy` no workflow vai ler esse secret e chamar a URL após cada build bem-sucedido.

---

## 6. Testando o fluxo end-to-end

1. Faça um commit bobo na `main` (ex: mexer num comentário).
2. `git push origin main`.
3. Acompanhe em **Actions** no GitHub — o workflow deve:
   - Buildar a imagem.
   - Publicar `:latest` e `:<sha>` no GHCR.
   - Chamar o webhook do Portainer (step "Trigger Portainer redeploy").
4. No Portainer, abra a stack → aba **Activity**. Deve aparecer um redeploy iniciado pelo webhook.
5. `docker service ps <stack>_<service>` no servidor mostra a task nova substituindo a antiga.

Se o step do webhook for pulado com a mensagem `PORTAINER_WEBHOOK_URL not set, skipping redeploy`, é porque o secret não foi criado/não está visível para o workflow.

---

## 7. Duplicando para um cliente novo (checklist)

- [ ] Criar novo repo no GitHub (ou fork/clone deste).
- [ ] Substituir `ghcr.io/<owner>/<repo>` no `docker-compose.yml` e no workflow.
- [ ] Ajustar nome do serviço, host do Traefik e env vars do cliente.
- [ ] Ativar Read/write permissions em Actions.
- [ ] Push inicial → confirmar imagem no GHCR → tornar pública (ou configurar registry privado no Portainer).
- [ ] Criar stack no Portainer via Git com webhook ativado.
- [ ] Copiar URL do webhook → criar secret `PORTAINER_WEBHOOK_URL` no GitHub.
- [ ] Push de teste → validar redeploy automático.

---

## 8. Troubleshooting rápido

| Sintoma | Causa provável | Fix |
|---|---|---|
| Actions falha no `Build and push` com `denied` | Permissões de package insuficientes | Settings → Actions → Workflow permissions → Read and write |
| Portainer não puxa imagem (`pull access denied`) | Imagem privada sem registry configurado | Tornar o package público ou linkar registry com PAT no Portainer |
| Webhook retorna 404 | URL do webhook copiada errada, ou stack recriada (URL muda) | Abrir stack no Portainer, copiar URL nova, atualizar o secret |
| Stack redeploy, mas container roda código antigo | Tag `:latest` não foi atualizada, ou o Swarm está cacheando | Confirmar no GHCR que o `:latest` aponta para o SHA novo; forçar `--force` update se necessário |
| Step do webhook é pulado | Secret `PORTAINER_WEBHOOK_URL` ausente | Criar o secret no repo |
| `curl: (60) SSL certificate problem` no step do webhook | Portainer acessado por IP ou com cert self-signed | O workflow já usa `curl -k`; se preferir verificação estrita, coloque o Portainer atrás de um domínio com cert válido |
