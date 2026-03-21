"""Verificacao de assinatura Slack (requisitos de seguranca Slack)."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, Request


def verify_slack_signature(request: Request, body: bytes, signing_secret: str) -> None:
    if not signing_secret:
        raise HTTPException(status_code=500, detail="SLACK_SIGNING_SECRET nao configurado")

    signature = request.headers.get("X-Slack-Signature", "")
    ts_header = request.headers.get("X-Slack-Request-Timestamp", "")
    if not signature or not ts_header:
        raise HTTPException(status_code=401, detail="Cabecalhos Slack ausentes")

    try:
        ts = int(ts_header)
    except ValueError as e:
        raise HTTPException(status_code=401, detail="Timestamp invalido") from e

    if abs(int(time.time()) - ts) > 60 * 5:
        raise HTTPException(status_code=401, detail="Requisicao muito antiga")

    basestring = f"v0:{ts_header}:{body.decode('utf-8')}"
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Assinatura invalida")
