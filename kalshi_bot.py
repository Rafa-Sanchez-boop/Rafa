
import os
import time
import logging
import sqlite3
from datetime import datetime, timezone

import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH = os.getenv("DB_PATH", "/tmp/btc_alerts.sqlite3")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("BTC15M")

session = requests.Session()
session.headers.update({"User-Agent": "BTC15MAlerts/1.0"})


def database():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent (
            ticker TEXT PRIMARY KEY,
            sent_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def request_markets():
    url = f"{BASE}/markets"
    params = {
        "series_ticker": SERIES,
        "status": "open",
        "limit": 10
    }

    for attempt in range(5):
        response = session.get(
            url, params=params, timeout=20
        )

        if response.status_code == 429:
            delay = min(
                int(response.headers.get("Retry-After", "0") or 0)
                or 5 * (2 ** attempt),
                120
            )
            log.warning("Rate limit. Esperando %s s", delay)
            time.sleep(delay)
            continue

        response.raise_for_status()
        return response.json().get("markets", [])

    raise RuntimeError("Kalshi sigue limitando solicitudes")


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        log.error("Falta configurar Telegram")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        response = session.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=20
        )
        response.raise_for_status()
        return response.json().get("ok", False)
    except requests.RequestException:
        log.exception("Error enviando Telegram")
        return False


def analyze(market):
    yes_bid = number(market.get("yes_bid_dollars"))
    yes_ask = number(market.get("yes_ask_dollars"))

    if yes_bid is None or yes_ask is None:
        return "SIN ENTRADA", None, "Sin cotizaciones"

    if not (0 < yes_bid <= yes_ask < 1):
        return "SIN ENTRADA", None, "Cotizaciones inválidas"

    midpoint = (yes_bid + yes_ask) / 2
    spread = yes_ask - yes_bid

    if spread > 0.08:
        return "SIN ENTRADA", None, "Spread elevado"

    # Señal simple basada en precios del mercado.
    # NO es una predicción validada de Bitcoin.
    if midpoint >= 0.60:
        return "SUBE - YES", yes_ask, "Mercado favorece YES"

    if midpoint <= 0.40:
        return "BAJA - NO", 1 - yes_bid, "Mercado favorece NO"

    return "SIN ENTRADA", None, "Mercado sin dirección clara"


def scan():
    markets = request_markets()
    now = time.time()
    conn = database()

    try:
        for market in markets:
            ticker = market.get("ticker", "")

            if not ticker.startswith(SERIES + "-"):
                continue

            close_ts = parse_time(market.get("close_time"))
            open_ts = parse_time(market.get("open_time"))

            if not close_ts or close_ts <= now:
                continue

            if open_ts and open_ts > now:
                continue

            if open_ts and close_ts - open_ts > 960:
                continue

            existing = conn.execute(
                "SELECT 1 FROM sent WHERE ticker=?",
                (ticker,)
            ).fetchone()

            if existing:
                continue

            direction, entry, reason = analyze(market)
            remaining = max(0, int((close_ts - now) / 60))

            price_text = (
                f"${entry:.2f}"
                if entry is not None
                else "No disponible"
            )

            message = (
                "🚨 KALSHI BTC 15 MIN 🚨\n\n"
                f"📊 SEÑAL: {direction}\n"
                f"💰 Entrada referencia: {price_text}\n"
                f"⏱ Tiempo restante: {remaining} min\n"
                f"📈 Análisis: {reason}\n"
                f"🎯 Contrato: {ticker}\n\n"
                "Señal orientativa, no predicción "
                "garantizada. Revisa el precio actual."
            )

            if send_telegram(message):
                conn.execute(
                    "INSERT INTO sent VALUES (?, ?)",
                    (ticker, now)
                )
                conn.commit()
                log.info("Alerta enviada: %s", ticker)
    finally:
        conn.close()


def main():
    log.info("BTC 15M iniciado. Solo alertas.")

    while True:
        try:
            scan()
        except Exception:
            log.exception("Error en escaneo")

        time.sleep(20)


if __name__ == "__main__":
    main()
