"""
API HTTP para slash command Slack: adiciona voo ao flights.json no GitHub.

Configure no Slack: Slash Command apontando para POST /slack/commands
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import parse_qs

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from server.github_flights import get_flights_and_sha, put_flights, trigger_workflow_dispatch
from server.slack_verify import verify_slack_signature

load_dotenv()

app = FastAPI(title="Flight Track Slack")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("flight-track-slack")

URL_START = re.compile(r"https?://", re.IGNORECASE)


def _parse_name_and_url(text: str) -> tuple[str, str] | None:
    t = text.strip()
    if not t:
        return None
    m = URL_START.search(t)
    if not m:
        return None
    name = t[: m.start()].strip()
    url = t[m.start() :].strip()
    url = url.split()[0] if url else ""
    if not url:
        return None
    if not name:
        name = "Voo (sem nome)"
    return name, url


def _slack_ok_ephemeral(msg: str) -> JSONResponse:
    return JSONResponse({"response_type": "ephemeral", "text": msg})


def _post_response_url(url: str, payload: dict[str, Any]) -> None:
    try:
        r = httpx.post(url, json=payload, timeout=30.0)
        r.raise_for_status()
    except Exception as e:
        log.exception("Falha ao POST response_url: %s", e)


def _user_allowed(user_id: str) -> bool:
    allow = os.environ.get("SLACK_ALLOW_USER_IDS", "").strip()
    if not allow:
        return True
    allowed = {x.strip() for x in allow.split(",") if x.strip()}
    return user_id in allowed


def process_slack_command(text: str, response_url: str, user_id: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    branch = os.environ.get("GITHUB_BRANCH", "main").strip()
    workflow_file = os.environ.get("GITHUB_WORKFLOW_FILE", "flight-track-daily.yml").strip()
    trigger = os.environ.get("TRIGGER_WORKFLOW_AFTER_ADD", "").lower() in (
        "1",
        "true",
        "yes",
    )

    def reply(msg: str, ok: bool = True) -> None:
        _post_response_url(
            response_url,
            {
                "response_type": "ephemeral",
                "replace_original": False,
                "text": msg,
            },
        )

    if not token or not repo:
        reply(
            "Servidor sem `GITHUB_TOKEN` ou `GITHUB_REPO`. "
            "Configure as variaveis no provedor (Railway, etc.).",
            ok=False,
        )
        return

    parsed = _parse_name_and_url(text)
    if not parsed:
        reply(
            "Uso: `/flight-track Nome do voo https://www.google.com/travel/flights/...`\n"
            "O nome fica *antes* da URL (com espaco entre eles).",
            ok=False,
        )
        return

    name, flight_url = parsed

    try:
        flights, sha = get_flights_and_sha(token, repo, branch)
    except Exception as e:
        log.exception("GitHub GET flights.json")
        reply(f"Erro ao ler `flights.json` no GitHub: {e}", ok=False)
        return

    for row in flights:
        if isinstance(row, dict) and row.get("url") == flight_url:
            reply("Essa URL ja esta na lista.", ok=False)
            return

    flights.append({"name": name, "url": flight_url})

    try:
        put_flights(
            token,
            repo,
            branch,
            flights,
            sha,
            commit_message=f"flight-track(slack): adiciona {name}",
        )
    except Exception as e:
        log.exception("GitHub PUT flights.json")
        reply(f"Erro ao salvar no GitHub: {e}", ok=False)
        return

    msg = f"Adicionado: *{name}*\nURL registrada. O proximo job do GitHub Actions enviara os precos."
    if trigger:
        try:
            trigger_workflow_dispatch(token, repo, workflow_file, branch)
            msg += "\n_Disparei o workflow `flight-track-daily` agora._"
        except Exception as e:
            log.warning("workflow_dispatch: %s", e)
            msg += f"\n_Nao consegui disparar o workflow agora ({e}). Rode manual em Actions._"

    reply(msg, ok=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/slack/commands")
async def slack_commands(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    body = await request.body()
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    verify_slack_signature(request, body, signing_secret)

    form = parse_qs(body.decode("utf-8"))
    text = (form.get("text") or [""])[0]
    response_url = (form.get("response_url") or [""])[0]
    user_id = (form.get("user_id") or [""])[0]

    if not response_url:
        return _slack_ok_ephemeral("Requisicao Slack invalida (sem response_url).")

    if not _user_allowed(user_id):
        return _slack_ok_ephemeral("Voce nao tem permissao para usar este comando.")

    if not text.strip():
        return _slack_ok_ephemeral(
            "Uso: `/flight-track Nome do voo https://...`\n"
            "Ex.: `/flight-track Voo SP Dez https://www.google.com/travel/flights/...`"
        )

    background_tasks.add_task(process_slack_command, text, response_url, user_id)
    return _slack_ok_ephemeral("Recebido. Atualizando `flights.json` no GitHub…")
