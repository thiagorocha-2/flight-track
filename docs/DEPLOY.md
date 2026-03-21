# Deploy: GitHub + Railway/Render + Slack `/flight-track`

Siga na ordem. Tempo estimado: **15–25 minutos**.

---

## Parte A — Repositório no GitHub

1. Acesse [github.com/new](https://github.com/new).
2. **Repository name:** `flight-track` (ou outro nome — use o mesmo em `GITHUB_REPO` depois).
3. **Public** ou **Private** (Actions em repo privado tem limite de minutos no plano free).
4. **Não** marque “Add a README” (o projeto já tem arquivos locais).
5. Clique em **Create repository**.

No **Terminal** (na pasta do projeto):

```bash
cd /Users/nuver/Documents/Cursor/flight-track

# Se ainda não configurou identidade Git neste repo:
git config user.name "Seu Nome"
git config user.email "seu-email@exemplo.com"

git remote add origin git@github.com:thiagorocha-2/flight-track.git
git branch -M main
git push -u origin main
```

*(Se preferir HTTPS: `https://github.com/thiagorocha-2/flight-track.git` — o GitHub vai pedir login/token.)*

### Workflow do GitHub Actions

Se subiu ficheiros via API/MCP e **não** existe `.github/workflows/flight-track-daily.yml`, copie o conteúdo de [`docs/flight-track-daily.yml`](flight-track-daily.yml) para **Add file** → `.github/workflows/flight-track-daily.yml` na UI do GitHub (ou faça `git push` a partir do Mac). Tokens sem permissão **Workflows** costumam receber 404 ao tentar criar essa pasta via API.

### Secrets do GitHub Actions (tracker diário)

No GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Valor |
|------|--------|
| `SLACK_BOT_TOKEN` | `xoxb-...` (Bot User OAuth Token) |
| `SLACK_CHANNEL_ID` | `C...` ou `G...` |
| `SLACK_THREAD_TS` | *(opcional)* `1234567890.123456` ou `p...` |

Teste: **Actions** → **Flight track daily** → **Run workflow**.

---

## Parte B — Token do GitHub (PAT) para o servidor Slack

O servidor precisa **commitar** `flights.json` no seu repo.

1. Abra [github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta) (**Fine-grained token**).
2. **Generate new token**.
3. **Token name:** `flight-track-slack`.
4. **Expiration:** o que preferir (90 dias, etc.).
5. **Repository access:** **Only select repositories** → escolha `flight-track`.
6. **Permissions → Repository:**
   - **Contents:** Read and write  
   - **Metadata:** Read  
   - *(Opcional, para rodar o tracker na hora)* **Actions:** Read and write  
7. **Generate token** e **copie** o valor (começa com `github_pat_` ou `ghp_`).

Guarde como `GITHUB_TOKEN` nas variáveis do Railway/Render (Parte C).

---

## Parte C — Hospedar o servidor (escolha **uma** opção)

O app expõe:

- `POST /slack/commands` — usado pelo Slack  
- `GET /health` — health check  

### Opção 1 — Railway (recomendado)

1. Acesse [railway.app](https://railway.app) e faça login (GitHub é o mais simples).
2. **New project** → **Deploy from GitHub repo** (autorize o Railway a ler seus repos se pedir).
3. Selecione o repositório **`flight-track`**.
4. O Railway deve detectar o [**Dockerfile**](../Dockerfile) na raiz (o arquivo [**railway.toml**](../railway.toml) reforça isso).
5. Abra o serviço → **Variables** → adicione:

| Variable | Obrigatório | Exemplo / notas |
|----------|-------------|-----------------|
| `SLACK_SIGNING_SECRET` | Sim | App Slack → **Basic Information** → **Signing Secret** |
| `GITHUB_TOKEN` | Sim | PAT da Parte B |
| `GITHUB_REPO` | Sim | `thiagorocha-2/flight-track` |
| `GITHUB_BRANCH` | Não | `main` (padrão no código se omitir) |
| `TRIGGER_WORKFLOW_AFTER_ADD` | Não | `true` — dispara o workflow após cada `/flight-track` (PAT precisa **Actions: write**) |
| `SLACK_ALLOW_USER_IDS` | Não | `U123,U456` — só esses usuários podem usar o comando |
| `GITHUB_WORKFLOW_FILE` | Não | `flight-track-daily.yml` |

6. **Settings** (do serviço) → **Networking** → **Generate Domain** (ou **Custom Domain**).
7. Copie a URL pública, ex.: `https://flight-track-production-xxxx.up.railway.app`

**URL do Slack:** `https://SUA-URL/slack/commands`

8. Aguarde o deploy ficar **Active**. Teste no navegador: `https://SUA-URL/health` → deve retornar `{"status":"ok"}`.

### Opção 2 — Render

1. Acesse [render.com](https://render.com) e login com GitHub.
2. **New +** → **Web Service** (ou **Blueprint** se usar o [`render.yaml`](../render.yaml) na raiz).
3. Conecte o repo **`flight-track`**.
4. **Runtime:** **Docker**  
   - **Dockerfile path:** `Dockerfile`  
   - **Docker context:** `.` (raiz)
5. **Instance type:** Free (se disponível).
6. **Health check path:** `/health`
7. Em **Environment**, adicione as **mesmas** variáveis da tabela do Railway acima.
8. **Create Web Service**. Copie a URL `https://nome.onrender.com`.

**URL do Slack:** `https://nome.onrender.com/slack/commands`

---

## Parte D — Slash Command no Slack

1. [api.slack.com/apps](https://api.slack.com/apps) → seu app.
2. Menu **Slash Commands** → **Create New Command**.
3. Preencha:
   - **Command:** `/flight-track`
   - **Request URL:** `https://SUA-URL-DO-RAILWAY-OU-RENDER/slack/commands`
   - **Short description:** Adiciona voo ao flight-track
4. **Save**.
5. **Install App** (ou **Reinstall to Workspace**) em **Install your app** se o Slack pedir.

### Teste

No Slack:

```
/flight-track Teste deploy https://www.google.com/travel/flights
```

*(Use uma URL real de voo depois.)*

Você deve ver “Recebido…” e em seguida mensagem de sucesso ou erro do GitHub. Confira no GitHub se `flights.json` ganhou um commit.

---

## Checklist rápido

- [ ] Repo no GitHub com `flights.json` e workflow em `.github/workflows/`
- [ ] Secrets `SLACK_BOT_TOKEN` e `SLACK_CHANNEL_ID` no GitHub Actions
- [ ] PAT com **Contents** no repo `flight-track`
- [ ] Servidor no ar + `/health` OK
- [ ] Variáveis `SLACK_SIGNING_SECRET`, `GITHUB_TOKEN`, `GITHUB_REPO` no Railway/Render
- [ ] **Request URL** do `/flight-track` apontando para `.../slack/commands`

---

## Problemas comuns

| Sintoma | O que fazer |
|---------|-------------|
| Slack: “dispatch_failed” | URL errada, servidor parado, ou não é HTTPS. |
| 401 no servidor | `SLACK_SIGNING_SECRET` errado ou com espaço extra. |
| GitHub: 403/404 no servidor | PAT sem acesso ao repo ou `GITHUB_REPO` errado (`usuario/repo`). |
| Actions não roda | Secrets do repositório faltando; ou workflow desabilitado em **Actions**. |
| `not_in_channel` no tracker | Bot não foi convidado no canal (`/invite @App`). |
