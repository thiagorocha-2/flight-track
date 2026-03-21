#!/usr/bin/env python3
"""
Coleta precos do Google Flights, compara com historico e envia resumo por DM no Slack.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Diretorio do projeto (para launchd com WorkingDirectory)
ROOT = Path(__file__).resolve().parent
FLIGHTS_PATH = ROOT / "flights.json"
HISTORY_PATH = ROOT / "price_history.json"
LOG_PATH = Path.home() / "Library" / "Logs" / "flight-track.log"

# Precos em BRL plausiveis (evita lixo de texto da pagina)
MIN_PRICE_BRL = 50.0
MAX_PRICE_BRL = 500_000.0

# R$ com espacos unicode / cifrao ASCII ou fullwidth (U+FF04)
# Ex.: R$ 3.487 | R$ 1.234,56 | R $ 1234
BRL_RE = re.compile(
    r"R\s*[\$＄]\s*([\d]{1,3}(?:\.[\d]{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logging.warning("JSON invalido em %s: %s — usando default", path, e)
        return default


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_brl_to_float(raw: str) -> float | None:
    """Converte string brasileira (1.234,56) para float."""
    raw = raw.strip()
    if not raw:
        return None
    if "," in raw:
        whole, frac = raw.rsplit(",", 1)
        whole = whole.replace(".", "")
        try:
            return float(f"{whole}.{frac}")
        except ValueError:
            return None
    # so digitos / milhares com ponto
    cleaned = raw.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_brl_display(value: float) -> str:
    """Formata valor para exibicao tipo brasileira (ex.: 2.345,67)."""
    rounded = round(value, 2)
    whole = int(rounded)
    frac_cents = int(round(abs(rounded - whole) * 100))
    if frac_cents >= 100:
        whole += 1 if whole >= 0 else -1
        frac_cents = 0
    s = str(abs(whole))
    groups: list[str] = []
    while s:
        groups.insert(0, s[-3:])
        s = s[:-3]
    body = ".".join(groups)
    sign = "-" if whole < 0 else ""
    return f"{sign}R$ {body},{frac_cents:02d}"


def normalize_slack_thread_ts(raw: str | None) -> str | None:
    """
    Aceita timestamp Slack (1234567890.123456) ou formato de permalink (p1234567890123456).
    """
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("p") and len(s) > 7 and s[1:].isdigit():
        digits = s[1:]
        return f"{digits[:-6]}.{digits[-6]}"
    return s


def normalize_for_price_scan(text: str) -> str:
    """Unifica espacos invisiveis e compatibilidade unicode (Google usa NBSP etc.)."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    for ch in ("\u00a0", "\u202f", "\u2007", "\u2009", "\u200a", "\u200b"):
        t = t.replace(ch, " ")
    t = re.sub(r"\s+", " ", t)
    return t


def collect_page_price_text(page: Page) -> str:
    """
    Junta texto visivel do body com aria-labels e rotulos curtos de botoes/links.
    O Google Flights costuma expor precos em aria-label mesmo quando o layout
    quebra inner_text do body em headless.
    """
    chunks: list[str] = []
    try:
        chunks.append(page.inner_text("body", timeout=30_000))
    except Exception as e:
        logging.debug("inner_text(body) falhou: %s", e)
    try:
        extra: str = page.evaluate(
            """() => {
                const parts = [];
                document.querySelectorAll('[aria-label]').forEach(el => {
                    const a = el.getAttribute('aria-label');
                    if (a) parts.push(a);
                });
                document.querySelectorAll('[role="button"], [role="link"]').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 220) parts.push(t);
                });
                return parts.join(' ');
            }"""
        )
        if isinstance(extra, str) and extra.strip():
            chunks.append(extra)
    except Exception as e:
        logging.debug("coleta aria-label/role: %s", e)
    return normalize_for_price_scan("\n".join(chunks))


def extract_lowest_brl_price(page_text: str) -> float | None:
    """Extrai o menor valor em R$ plausivel do texto da pagina."""
    candidates: list[float] = []
    for m in BRL_RE.finditer(page_text):
        val = parse_brl_to_float(m.group(1))
        if val is not None and MIN_PRICE_BRL <= val <= MAX_PRICE_BRL:
            candidates.append(val)
    if not candidates:
        return None
    return min(candidates)


@dataclass
class FlightResult:
    name: str
    url: str
    price: float | None
    error: str | None = None


def scrape_flight_price(url: str, headless: bool, timeout_ms: int) -> tuple[float | None, str | None]:
    if not url or not url.strip():
        return None, "URL vazia — preencha em flights.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="pt-BR",
                timezone_id="America/Sao_Paulo",
                viewport={"width": 1365, "height": 900},
                device_scale_factor=1,
            )
            context.add_init_script(
                "try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}"
            )
            page = context.new_page()
            try:
                page.goto(url.strip(), wait_until="load", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                return None, "Timeout ao carregar a pagina"

            # Varias tentativas: GF demora a hidratar precos, sobretudo em headless.
            per_attempt_wait = min(12_000, max(5_000, timeout_ms // 4))
            max_attempts = 3
            price: float | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    page.wait_for_selector("text=R$", timeout=per_attempt_wait)
                except PlaywrightTimeoutError:
                    logging.info(
                        "Tentativa %s/%s: seletor R$ nao apareceu a tempo; seguindo",
                        attempt,
                        max_attempts,
                    )

                page.wait_for_timeout(2_000)
                # Scroll para forcar listas / cards de precos (ex.: "Menor preço total")
                for _ in range(5):
                    page.evaluate("window.scrollBy(0, 500)")
                    page.wait_for_timeout(350)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(400)

                blob = collect_page_price_text(page)
                price = extract_lowest_brl_price(blob)
                if price is not None:
                    break
                page.wait_for_timeout(4_000)

            if price is None:
                return None, "Nao foi possivel encontrar preco em R$ na pagina"
            return price, None
        finally:
            browser.close()


def format_price_line(
    name: str,
    price: float | None,
    prev: float | None,
    error: str | None,
    flight_url: str,
) -> str:
    if error:
        return f"• *{name}*: _{error}_"
    assert price is not None
    formatted = format_brl_display(price)
    if prev is None:
        delta = " (novo — sem historico)"
    elif price < prev - 0.01:
        diff = prev - price
        delta = f" (baixou {format_brl_display(diff)})"
    elif price > prev + 0.01:
        diff = price - prev
        delta = f" (subiu {format_brl_display(diff)})"
    else:
        delta = " (sem mudanca)"
    link = f"<{flight_url}|abrir no Google Flights>" if flight_url.strip() else ""
    suffix = f" — {link}" if link else ""
    return f"• *{name}*: {formatted}{delta}{suffix}"


def build_slack_message(results: list[FlightResult], history: dict[str, Any]) -> str:
    today = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y")
    lines = [f"*Flight Tracker* — {today}", ""]
    for r in results:
        prev_data = history.get(r.name)
        prev_price: float | None = None
        if isinstance(prev_data, dict) and "last_price" in prev_data:
            try:
                prev_price = float(prev_data["last_price"])
            except (TypeError, ValueError):
                prev_price = None
        lines.append(format_price_line(r.name, r.price, prev_price, r.error, r.url))
    lines.append("")
    lines.append("_Fonte: Google Flights (scraping local). Precos podem variar na hora da compra._")
    return "\n".join(lines)


def split_slack_message(text: str, max_len: int = 3500) -> list[str]:
    """Divide mensagem para caber no limite do Slack (margem para mrkdwn)."""
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > max_len and current:
            parts.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        parts.append("\n".join(current))
    return parts


def send_slack_notification(
    token: str,
    text: str,
    *,
    channel_id: str | None,
    thread_ts: str | None,
    dm_user_id: str | None,
) -> None:
    """
    Envia mensagem no Slack:
    - Se `channel_id` estiver definido: posta no canal / grupo / mpim (use o ID C.../G...).
    - Senao: abre DM com `dm_user_id` e posta la.
    - `thread_ts` (opcional): responde dentro da thread dessa conversa (mensagem pai).
    """
    client = WebClient(token=token)
    target_channel: str

    if channel_id:
        target_channel = channel_id
        logging.info("Enviando para conversa Slack: %s", channel_id)
    elif dm_user_id:
        try:
            conv = client.conversations_open(users=dm_user_id)
            target_channel = conv["channel"]["id"]
        except SlackApiError as e:
            logging.error("Falha ao abrir DM: %s", e.response.get("error", e))
            raise
        logging.info("Enviando DM para usuario: %s", dm_user_id)
    else:
        raise ValueError("Defina SLACK_CHANNEL_ID (canal/grupo) ou SLACK_USER_ID (DM) no .env")

    post_kwargs: dict[str, Any] = {"channel": target_channel, "mrkdwn": True}
    if thread_ts:
        post_kwargs["thread_ts"] = thread_ts

    chunks = split_slack_message(text)
    for i, chunk in enumerate(chunks):
        try:
            client.chat_postMessage(text=chunk, **post_kwargs)
        except SlackApiError as e:
            logging.error("Falha ao enviar mensagem Slack: %s", e.response.get("error", e))
            raise
        if i < len(chunks) - 1:
            logging.info("Enviado chunk %s/%s", i + 1, len(chunks))


def update_history(history: dict[str, Any], results: list[FlightResult]) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    for r in results:
        if r.error or r.price is None:
            continue
        history[r.name] = {
            "last_price": r.price,
            "currency": "BRL",
            "last_seen": now,
            "url": r.url,
        }


def main() -> int:
    load_dotenv(ROOT / ".env")
    setup_logging()

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    slack_channel_id = os.environ.get("SLACK_CHANNEL_ID", "").strip()
    slack_thread_ts = normalize_slack_thread_ts(os.environ.get("SLACK_THREAD_TS", "").strip())
    user_id = os.environ.get("SLACK_USER_ID", "").strip()
    headless = os.environ.get("HEADLESS", "1").lower() not in ("0", "false", "no")
    timeout_ms = int(os.environ.get("PAGE_TIMEOUT_MS", "45000"))
    skip_slack = os.environ.get("SKIP_SLACK", "").lower() in ("1", "true", "yes")

    if not skip_slack and not token:
        logging.error("Defina SLACK_BOT_TOKEN no arquivo .env (ou use SKIP_SLACK=1 para testar)")
        return 1

    if not skip_slack and not slack_channel_id and not user_id:
        logging.error(
            "Defina SLACK_CHANNEL_ID (canal ou conversa em grupo) ou SLACK_USER_ID (DM) no .env"
        )
        return 1

    if not skip_slack and slack_thread_ts and not slack_channel_id:
        logging.error("SLACK_THREAD_TS so funciona junto com SLACK_CHANNEL_ID (mesma conversa)")
        return 1

    flights_raw = load_json(FLIGHTS_PATH, [])
    if not isinstance(flights_raw, list):
        logging.error("flights.json deve ser uma lista de objetos")
        return 1

    results: list[FlightResult] = []
    for item in flights_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "Sem nome")).strip()
        url = str(item.get("url", "")).strip()
        logging.info("Processando: %s", name)
        price, err = scrape_flight_price(url, headless=headless, timeout_ms=timeout_ms)
        results.append(FlightResult(name=name, url=url, price=price, error=err))

    history: dict[str, Any] = load_json(HISTORY_PATH, {})
    if not isinstance(history, dict):
        history = {}

    message = build_slack_message(results, history)
    if skip_slack:
        logging.warning("SKIP_SLACK=1 — mensagem nao enviada ao Slack")
        logging.info("Preview da mensagem:\n%s", message)
    else:
        try:
            send_slack_notification(
                token,
                message,
                channel_id=slack_channel_id or None,
                thread_ts=slack_thread_ts,
                dm_user_id=user_id or None,
            )
        except (SlackApiError, ValueError):
            return 1

    update_history(history, results)
    save_json(HISTORY_PATH, history)
    logging.info("Concluido. Historico atualizado em %s", HISTORY_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
