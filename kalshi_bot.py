"""
KALSHI AI BOT v2 — Cloud alerts-only scanner
- Public Kalshi market data only; no trading endpoints or API keys.
- Stores snapshots and sent-alert deduplication in SQLite.
- Optional Telegram notifications.
- Rule-based market-momentum monitor, NOT a calibrated event-outcome predictor.
"""
import os, time, sqlite3, logging, math
from datetime import datetime, timezone
from typing import Any
import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
DB_PATH = os.getenv("DB_PATH", "/var/data/kalshi_bot.sqlite3")
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "300"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME_24H", "100"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.04"))
MIN_MOVE = float(os.getenv("MIN_MOVE", "0.025"))
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "6"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
TOP_ALERTS = int(os.getenv("TOP_ALERTS", "15"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-ai")

http = requests.Session()
http.headers.update({"User-Agent": "KalshiAIAlerts/0.2"})


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        ticker TEXT NOT NULL, seen_at REAL NOT NULL, midpoint REAL NOT NULL,
        PRIMARY KEY(ticker, seen_at))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
        ticker TEXT NOT NULL, side TEXT NOT NULL, sent_at REAL NOT NULL,
        PRIMARY KEY(ticker, side))""")
    conn.commit()
    return conn


def fnum(v: Any):
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def get_markets():
    all_markets, cursor, seen = [], None, set()
    for _ in range(MAX_PAGES):
        params = {"status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = http.get(f"{BASE}/markets", params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
        for m in payload.get("markets", []):
            ticker = m.get("ticker")
            if ticker and ticker not in seen:
                all_markets.append(m)
                seen.add(ticker)
        cursor = payload.get("cursor")
        if not cursor:
            break
    return all_markets


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = http.post(url, json={"chat_id": TELEGRAM_CHAT,
                                 "text": message, "disable_web_page_preview": True},
                      timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        log.exception("Telegram notification failed")


def previous_midpoint(conn, ticker):
    row = conn.execute(
        "SELECT midpoint FROM snapshots WHERE ticker=? ORDER BY seen_at DESC LIMIT 1",
        (ticker,)).fetchone()
    return row[0] if row else None


def was_alerted_recently(conn, ticker, side, now):
    row = conn.execute("SELECT sent_at FROM alerts WHERE ticker=? AND side=?",
                       (ticker, side)).fetchone()
    return bool(row and now - row[0] < ALERT_COOLDOWN_HOURS * 3600)


def scan_once():
    now = time.time()
    markets = get_markets()
    conn = db()
    candidates = []

    for m in markets:
        ticker = m.get("ticker", "")
        bid = fnum(m.get("yes_bid_dollars"))
        ask = fnum(m.get("yes_ask_dollars"))
        vol = fnum(m.get("volume_24h_fp")) or 0.0

        if not ticker or bid is None or ask is None:
            continue
        if not (0 < bid <= ask < 1):
            continue
        spread = ask - bid
        if vol < MIN_VOLUME or spread > MAX_SPREAD:
            continue

        mid = (bid + ask) / 2
        old_mid = previous_midpoint(conn, ticker)
        # Save every valid observation; a new deployment starts its own baseline.
        conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                     (ticker, now, mid))

        if old_mid is None:
            continue
        move = mid - old_mid
        if abs(move) < MIN_MOVE:
            continue

        side = "YES" if move > 0 else "NO"
        if was_alerted_recently(conn, ticker, side, now):
            continue

        # Executable-side reference prices. These are approximate; inspect the book
        # and current quotes before acting.
        entry = ask if side == "YES" else (1 - bid)
        # Momentum score is deliberately NOT represented as a probability.
        score = min(100, abs(move) / max(MIN_MOVE, 0.001) * 50)
        title = m.get("title") or m.get("subtitle") or ticker
        candidates.append({
            "ticker": ticker, "title": title, "side": side, "entry": entry,
            "mid": mid, "move": move, "spread": spread, "volume": vol,
            "score": score, "close": m.get("close_time", ""),
            "category": m.get("category", "Unspecified")
        })

    conn.commit()
    candidates.sort(key=lambda x: (x["score"], x["volume"]), reverse=True)
    candidates = candidates[:TOP_ALERTS]

    for c in candidates:
        msg = (
            f"🔎 KALSHI AI — WATCH {c['side']}\n"
            f"{c['title']}\n"
            f"Ticker: {c['ticker']}\n"
            f"Precio de referencia de entrada: ${c['entry']:.2f}\n"
            f"Movimiento desde escaneo anterior: {c['move']:+.1%}\n"
            f"Spread YES: {c['spread']:.1%} | Volumen 24h: {c['volume']:.0f}\n"
            f"Cierre: {c['close'] or 'No indicado'}\n"
            "No es una probabilidad predictiva ni una orden. Verifica reglas, noticias, "
            "liquidez, comisiones y precio actual antes de decidir."
        )
        log.info("%s | %s | move=%+.3f | spread=%.3f | vol=%.0f",
                 c["ticker"], c["side"], c["move"], c["spread"], c["volume"])
        telegram(msg)
        conn.execute("INSERT OR REPLACE INTO alerts VALUES (?, ?, ?)",
                     (c["ticker"], c["side"], now))

    conn.commit()
    conn.close()
    log.info("Escaneados %d mercados; alertas nuevas: %d", len(markets), len(candidates))


def main():
    log.info("KALSHI AI BOT v2 iniciado. Solo alertas; sin trading.")
    while True:
        try:
            scan_once()
        except Exception:
            log.exception("Falló el escaneo; se reintentará en el próximo ciclo.")
        time.sleep(max(60, SCAN_SECONDS))


if __name__ == "__main__":
    main()
