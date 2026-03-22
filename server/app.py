"""
API HTTP para slash command Slack: gerencia voos no flights.json via GitHub.

Subcomandos:
  /flight-track add Nome https://...       — adiciona voo (com travel_date opcional YYYY-MM-DD)
  /flight-track delete Nome                — remove voo pelo nome (parcial, case-insensitive)
  /flight-track list                       — lista voos ativos
  /flight-track help                       — mostra ajuda
  /flight-track Nome https://...           — atalho para add (compatibilidade)

Configure no Slack: Slash Command apontando para POST /slack/commands
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
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
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

HELP_TEXT = (
    "*Comandos disponíveis:*\n"
    "• `/flight-track add Nome do voo https://... [YYYY-MM-DD]`\n"
    "  Adiciona voo. A data (travel_date) é opcional; se não informada, tenta extrair da URL.\n"
    "• `/flight-track delete Nome do voo`\n"
    "  Remove voo pelo nome (busca parcial, case-insensitive).\n"
    "• `/flight-track list`\n"
    "  Lista todos os voos ativos (com data de viagem).\n"
    "• `/flight-track help`\n"
    "  Mostra esta ajuda.\n\n"
    "_Atalho:_ `/flight-track Nome https://...` equivale a `add`."
)


def _extract_travel_date_from_url(url: str) -> str | None:
    """Extrai a última data YYYY-MM-DD encontrada no parâmetro tfs da URL."""
    dates = DATE_RE.findall(url)
    if not dates:
        return None
    valid: list[str] = []
    for d in dates:
        try:
            date.fromisoformat(d)
            valid.append(d)
        except ValueError:
            continue
    return max(valid) if valid else None


def _parse_add_args(text: str) -> tuple[str, str, str | None] | None:
    """Retorna (name, url, travel_date) ou None se inválido."""
    t = text.strip()
    if not t:
        return None
    m = URL_START.search(t)
    if not m:
        return None
    name = t[: m.start()].strip()
    rest = t[m.start() :].strip()
    parts = rest.split()
    url = parts[0] if parts else ""
    if not url:
        return None
    if not name:
        name = "Voo (sem nome)"

    explicit_date: str | None = None
    if len(parts) > 1:
        candidate = parts[-1]
        if DATE_RE.fullmatch(candidate):
            try:
                date.fromisoformat(candidate)
                explicit_date = candidate
            except ValueError:
                pass

    travel_date = explicit_date or _extract_travel_date_from_url(url)
    return name, url, travel_date


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


def _github_config() -> tuple[str, str, str, str, bool]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    branch = os.environ.get("GITHUB_BRANCH", "main").strip()
    workflow_file = os.environ.get("GITHUB_WORKFLOW_FILE", "flight-track-daily.yml").strip()
    trigger = os.environ.get("TRIGGER_WORKFLOW_AFTER_ADD", "").lower() in ("1", "true", "yes")
    return token, repo, branch, workflow_file, trigger


def _reply(response_url: str, msg: str) -> None:
    _post_response_url(
        response_url,
        {"response_type": "ephemeral", "replace_original": False, "text": msg},
    )


# --------------- Subcomandos ---------------


def _cmd_add(text: str, response_url: str) -> None:
    token, repo, branch, workflow_file, trigger = _github_config()
    reply = lambda msg: _reply(response_url, msg)

    if not token or not repo:
        reply("Servidor sem `GITHUB_TOKEN` ou `GITHUB_REPO`. Configure no provedor.")
        return

    parsed = _parse_add_args(text)
    if not parsed:
        reply(
            "Uso: `/flight-track add Nome do voo https://...google.com/travel/flights/... [YYYY-MM-DD]`\n"
            "O nome fica *antes* da URL."
        )
        return

    name, flight_url, travel_date = parsed

    try:
        flights, sha = get_flights_and_sha(token, repo, branch)
    except Exception as e:
        log.exception("GitHub GET flights.json")
        reply(f"Erro ao ler `flights.json` no GitHub: {e}")
        return

    for row in flights:
        if isinstance(row, dict) and row.get("url") == flight_url:
            reply("Essa URL já está na lista.")
            return

    entry: dict[str, str] = {"name": name, "url": flight_url}
    if travel_date:
        entry["travel_date"] = travel_date

    flights.append(entry)

    try:
        put_flights(token, repo, branch, flights, sha, commit_message=f"flight-track(slack): adiciona {name}")
    except Exception as e:
        log.exception("GitHub PUT flights.json")
        reply(f"Erro ao salvar no GitHub: {e}")
        return

    date_info = f"\nData de viagem: `{travel_date}`" if travel_date else "\n_Sem travel_date — o voo não expira automaticamente._"
    msg = f"Adicionado: *{name}*{date_info}\nO próximo job do GitHub Actions enviará os preços."
    if trigger:
        try:
            trigger_workflow_dispatch(token, repo, workflow_file, branch)
            msg += "\n_Disparei o workflow `flight-track-daily` agora._"
        except Exception as e:
            log.warning("workflow_dispatch: %s", e)
            msg += f"\n_Não consegui disparar o workflow agora ({e}). Rode manual em Actions._"

    reply(msg)


def _cmd_delete(text: str, response_url: str) -> None:
    token, repo, branch, _, _ = _github_config()
    reply = lambda msg: _reply(response_url, msg)

    if not token or not repo:
        reply("Servidor sem `GITHUB_TOKEN` ou `GITHUB_REPO`. Configure no provedor.")
        return

    query = text.strip()
    if not query:
        reply("Uso: `/flight-track delete Nome do voo`")
        return

    try:
        flights, sha = get_flights_and_sha(token, repo, branch)
    except Exception as e:
        log.exception("GitHub GET flights.json")
        reply(f"Erro ao ler `flights.json` no GitHub: {e}")
        return

    query_lower = query.lower()
    matches = [
        (i, f)
        for i, f in enumerate(flights)
        if isinstance(f, dict) and query_lower in str(f.get("name", "")).lower()
    ]

    if not matches:
        reply(f"Nenhum voo encontrado com nome contendo *{query}*.")
        return
    if len(matches) > 1:
        names = "\n".join(f"• {f.get('name', '?')}" for _, f in matches)
        reply(f"Múltiplos voos encontrados. Seja mais específico:\n{names}")
        return

    idx, flight = matches[0]
    removed_name = flight.get("name", "?")
    flights.pop(idx)

    try:
        put_flights(token, repo, branch, flights, sha, commit_message=f"flight-track(slack): remove {removed_name}")
    except Exception as e:
        log.exception("GitHub PUT flights.json")
        reply(f"Erro ao salvar no GitHub: {e}")
        return

    reply(f"Removido: *{removed_name}*\nO voo não será mais rastreado.")


def _cmd_list(response_url: str) -> None:
    token, repo, branch, _, _ = _github_config()
    reply = lambda msg: _reply(response_url, msg)

    if not token or not repo:
        reply("Servidor sem `GITHUB_TOKEN` ou `GITHUB_REPO`. Configure no provedor.")
        return

    try:
        flights, _ = get_flights_and_sha(token, repo, branch)
    except Exception as e:
        log.exception("GitHub GET flights.json")
        reply(f"Erro ao ler `flights.json` no GitHub: {e}")
        return

    if not flights:
        reply("Nenhum voo cadastrado.")
        return

    today = date.today()
    lines = [f"*Voos rastreados ({len(flights)}):*", ""]
    for f in flights:
        if not isinstance(f, dict):
            continue
        name = f.get("name", "?")
        td = str(f.get("travel_date", "")).strip()
        expired = ""
        if td:
            try:
                if date.fromisoformat(td) < today:
                    expired = " :no_entry_sign: _expirado_"
            except ValueError:
                pass
        date_str = f" (`{td}`)" if td else " _(sem data)_"
        lines.append(f"• *{name}*{date_str}{expired}")

    reply("\n".join(lines))


# --------------- Roteamento ---------------


SUBCOMMANDS = {"add", "delete", "remove", "list", "help"}


def _parse_subcommand(text: str) -> tuple[str, str]:
    """Retorna (subcomando, resto). Se não reconhecer, assume 'add'."""
    parts = text.strip().split(None, 1)
    if not parts:
        return "help", ""
    first = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    if first in SUBCOMMANDS:
        if first == "remove":
            first = "delete"
        return first, rest
    return "add", text.strip()


def process_slack_command(text: str, response_url: str) -> None:
    cmd, rest = _parse_subcommand(text)
    if cmd == "help":
        _reply(response_url, HELP_TEXT)
    elif cmd == "list":
        _cmd_list(response_url)
    elif cmd == "delete":
        _cmd_delete(rest, response_url)
    elif cmd == "add":
        _cmd_add(rest, response_url)
    else:
        _reply(response_url, HELP_TEXT)


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
        return _slack_ok_ephemeral("Requisição Slack inválida (sem response_url).")

    if not _user_allowed(user_id):
        return _slack_ok_ephemeral("Você não tem permissão para usar este comando.")

    if not text.strip():
        return _slack_ok_ephemeral(HELP_TEXT)

    background_tasks.add_task(process_slack_command, text, response_url)
    return _slack_ok_ephemeral("Recebido. Processando…")
