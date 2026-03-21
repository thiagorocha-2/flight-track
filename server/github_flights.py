"""Leitura e escrita de flights.json via GitHub Contents API."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _auth_headers(token: str) -> dict[str, str]:
    return {**GITHUB_HEADERS_BASE, "Authorization": f"Bearer {token}"}


def get_flights_and_sha(
    token: str,
    repo: str,
    branch: str,
) -> tuple[list[dict[str, Any]], str]:
    """Retorna (lista de voos, sha do blob atual)."""
    owner, name = repo.split("/", 1)
    url = f"{GITHUB_API}/repos/{owner}/{name}/contents/flights.json"
    r = httpx.get(
        url,
        params={"ref": branch},
        headers=_auth_headers(token),
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("type") != "file" or "content" not in data or "sha" not in data:
        raise ValueError("Resposta inesperada da API do GitHub para flights.json")
    raw = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("flights.json deve ser um array JSON")
    return parsed, str(data["sha"])


def put_flights(
    token: str,
    repo: str,
    branch: str,
    flights: list[dict[str, Any]],
    sha: str,
    commit_message: str,
) -> None:
    owner, name = repo.split("/", 1)
    body_json = json.dumps(flights, ensure_ascii=False, indent=2) + "\n"
    content_b64 = base64.b64encode(body_json.encode("utf-8")).decode("ascii")
    url = f"{GITHUB_API}/repos/{owner}/{name}/contents/flights.json"
    payload = {
        "message": commit_message,
        "content": content_b64,
        "sha": sha,
        "branch": branch,
    }
    r = httpx.put(url, json=payload, headers=_auth_headers(token), timeout=30.0)
    if r.status_code == 409:
        raise RuntimeError("Conflito ao salvar (409). Tente o comando de novo.")
    r.raise_for_status()


def trigger_workflow_dispatch(
    token: str,
    repo: str,
    workflow_file: str,
    branch: str,
) -> None:
    """Dispara workflow_dispatch (precisa escopo actions:write no token)."""
    owner, name = repo.split("/", 1)
    url = (
        f"{GITHUB_API}/repos/{owner}/{name}/actions/workflows/"
        f"{workflow_file}/dispatches"
    )
    r = httpx.post(
        url,
        json={"ref": f"refs/heads/{branch}"},
        headers=_auth_headers(token),
        timeout=30.0,
    )
    if r.status_code == 204:
        return
    logging.warning("workflow_dispatch falhou: %s %s", r.status_code, r.text)
    r.raise_for_status()
