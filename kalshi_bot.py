"""Kalshi-only BTC 15-minute alerts. No Kraken, no automatic trading.
Signals describe Kalshi YES/NO contract momentum, not BTC/USD price forecasts.
"""
import logging
import os
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = os.getenv("KALSHI_SERIES", "KXBTC15M")
DB_PATH = os.getenv("DB_PATH", "/var/data/kalshi_bot.sqlite3")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_SECONDS = max(10, int(os.getenv("SCAN_SECONDS", "20")))
DELAY = 120
MAX_SPREAD = Decimal(os.getenv("MAX_SPREAD", "0.04"))
MIN_MOVE = Decimal(os.getenv("MIN_MOVE", "0.025"))
MIN_VOLUME = Decimal(os.getenv("MIN_VOLUME_24H", "100"))
MIN_REMAINING = max(300, int(os.getenv("MIN_REMAINING_SECONDS", "300")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-only")
http = requests.Session()
http.headers["User-Agent"] = "KalshiOnlyBTC15M/4.5"
# Extra in-process protection against opposite alerts during a worker lifetime.
alerted_tickers = set()

def dec(x):
    try:
        v = Decimal(str(x))
        return v if v.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None

def ts(x):
    try:
        return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp() if x else None
    except (ValueError, AttributeError):
        return None

def db_open():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_samples
               (ticker TEXT, observed_at REAL, yes_mid TEXT,
                PRIMARY KEY(ticker, observed_at))""")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_alerts
               (ticker TEXT, phase TEXT, sent_at REAL, direction TEXT,
                PRIMARY KEY(ticker,phase))""")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_results
               (ticker TEXT PRIMARY KEY, result TEXT, observed_at REAL)""")
    db.commit()
    return db

def get_json(path, params=None):
    for attempt in range(4):
        try:
            r = http.get(BASE + path, params=params, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(30, 2 ** (attempt + 1)))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Kalshi request failed: %s", exc)
            if attempt == 3:
                return None
            time.sleep(2 ** attempt)
    return None

def notify(msg):
    if not TOKEN or not CHAT_ID:
        log.warning("Telegram not configured: %s", msg)
        return False
    try:
        r = http.post("https://api.telegram.org/bot" + TOKEN + "/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg}, timeout=15)
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except (requests.RequestException, ValueError):
        log.exception("Telegram send failed")
        return False

def quote(m):
    bid, ask = dec(m.get("yes_bid_dollars")), dec(m.get("yes_ask_dollars"))
    if bid is None or ask is None or not (Decimal(0) < bid <= ask < Decimal(1)):
        return None
    if ask - bid > MAX_SPREAD:
        return None
    return bid, ask, (bid + ask) / 2

def sample(db, ticker, now, mid):
    db.execute("INSERT OR IGNORE INTO kalshi_only_samples VALUES (?,?,?)",
               (ticker, now, str(mid)))

def signal(db, m, now, close_at, open_at, phase):
    ticker = m["ticker"]
    if ticker in alerted_tickers:
        return
    if db.execute("SELECT 1 FROM kalshi_only_alerts WHERE ticker=?",
                  (ticker,)).fetchone():
        alerted_tickers.add(ticker)
        return
    q = quote(m)
    if q is None:
        return
    bid, ask, mid = q
    volume = dec(m.get("volume_24h_fp"))
    if volume is None:
        volume = dec(m.get("volume_24h"))
    if volume is None or volume < MIN_VOLUME:
        return
    if phase == "OPEN":
        # Use the first actually observed Kalshi quote as baseline. The
        # market's open_time can predate the 15-minute trading window, and
        # a worker restart may miss the first minute. Never fabricate a quote.
        rows = db.execute("""SELECT yes_mid, observed_at FROM kalshi_only_samples
            WHERE ticker=? AND observed_at<=?
            ORDER BY observed_at ASC LIMIT 1""",
            (ticker, now - 60)).fetchone()
        if not rows:
            log.info("Waiting for 60s of observed Kalshi quotes for %s", ticker)
            return
        prior = dec(rows[0])
    else:
        rows = db.execute("""SELECT yes_mid FROM kalshi_only_samples
            WHERE ticker=? AND observed_at<=?
            ORDER BY observed_at DESC LIMIT 1""",
            (ticker, now - 60)).fetchone()
        prior = dec(rows[0]) if rows else None
    if prior is None:
        return
    change = mid - prior
    if abs(change) < MIN_MOVE:
        # No strong Kalshi direction; retry on subsequent scans.
        return
    up = change > 0
    direction = "UP" if up else "DOWN"
    entry = ask if up else Decimal(1) - bid
    remaining = max(0, int(close_at - now))
    phase_name = "OPORTUNIDAD A TIEMPO"
    # Projection is for the Kalshi YES contract quote, NOT BTC/USD.
    # A one-minute momentum extrapolation is illustrative, not a target order.
    projected = min(Decimal("0.99"), max(Decimal("0.01"), mid + change))
    interpretation = (f"📉 Interpretación: posible bajada de la cotización YES de Kalshi "
                      f"desde ${mid:.3f} hacia ${projected:.3f}; después el precio puede cambiar de rumbo."
                      if not up else
                      f"📈 Interpretación: posible subida de la cotización YES de Kalshi "
                      f"desde ${mid:.3f} hacia ${projected:.3f}; después el precio puede cambiar de rumbo.")
    message = (
        "📊 BTC KALSHI — " + phase_name + "\n"
        + ("🔺 UP" if up else "🔻 DOWN") + " | SEÑAL EXPERIMENTAL\n"
        + "━━━━━━━━━━━━━━━━━━\n"
        + f"📈 Precio YES anterior (Kalshi): ${prior:.3f}\n"
        + f"📈 Precio YES actual (Kalshi): ${mid:.3f}\n"
        + f"📏 Cambio en cotización YES: {change:+.3f}\n"
        + f"🎟️ Entrada indicativa {direction}: ${entry:.2f}\n"
        + f"⏳ Tiempo restante: {remaining//60:02d}:{remaining%60:02d}\n"
        + f"🧾 Contrato: {ticker}\n"
        + interpretation + "\n"
        + "🎯 Proyección ilustrativa de cotización YES, no precio BTC/USD ni objetivo de compra/venta.\n"
        + "⚠️ Solo datos de Kalshi. El cambio de cotización no predice "
          "necesariamente el precio BTC ni el resultado final. "
          "No se fija objetivo de venta; comisiones y liquidez pueden impedir beneficio. "
          "Alerta informativa, no operación automática."
    )
    if notify(message):
        alerted_tickers.add(ticker)
        db.execute("INSERT OR IGNORE INTO kalshi_only_alerts VALUES (?,?,?,?)",
                   (ticker, phase, now, direction))
        log.info("Kalshi-only %s alert: %s %s", phase, ticker, direction)

def check_results(db, now):
    # Use Kalshi's official result field, never Kraken, for completed markets.
    rows = db.execute("""SELECT DISTINCT ticker FROM kalshi_only_alerts
        WHERE ticker NOT IN (SELECT ticker FROM kalshi_only_results)""").fetchall()
    for (ticker,) in rows[-30:]:
        payload = get_json("/markets/" + ticker)
        market = payload.get("market", {}) if payload else {}
        result = str(market.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        db.execute("INSERT OR IGNORE INTO kalshi_only_results VALUES (?,?,?)",
                   (ticker, result, now))
        predicted = db.execute("""SELECT direction FROM kalshi_only_alerts
            WHERE ticker=? AND phase='OPEN'""", (ticker,)).fetchone()
        if predicted:
            correct = (predicted[0] == "UP" and result == "yes") or (
                predicted[0] == "DOWN" and result == "no")
            notify(f"📊 KALSHI — RESULTADO OFICIAL\n🧾 {ticker}\n"
                   f"📈 Dirección de momentum observada: {predicted[0]}\n"
                   f"🏁 Resultado final Kalshi: {result.upper()}\n"
                   f"{'Coincidió' if correct else 'No coincidió'} con el resultado final.\n"
                   "⚠️ La señal mide momentum de cotización, no predice "
                   "el resultado final ni confirma beneficio de compraventa.")

def scan():
    payload = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    if payload is None:
        return
    now = time.time()
    with db_open() as db:
        for m in payload.get("markets", []):
            ticker = m.get("ticker", "")
            if not isinstance(ticker, str) or not ticker.startswith(SERIES + "-"):
                continue
            opened, closes = ts(m.get("open_time")), ts(m.get("close_time"))
            if opened is None or closes is None or not (opened <= now < closes):
                continue
            if closes - opened > 960:
                continue
            q = quote(m)
            if q:
                sample(db, ticker, now, q[2])
            remaining = closes - now
            # Scan continuously after initial analysis, until five minutes remain.
            # OPEN phase is used for one alert per contract.
            if now - opened >= DELAY and remaining >= MIN_REMAINING:
                signal(db, m, now, closes, opened, "OPEN")
        # Check completed markets infrequently to reduce requests.
        if int(now // 300) != int((now - SCAN_SECONDS) // 300):
            check_results(db, now)
        db.execute("DELETE FROM kalshi_only_samples WHERE observed_at < ?", (now - 48 * 3600,))
        db.commit()

def main():
    log.info("Kalshi-only BTC15M v4.5: continuous opportunities after 120s; no late alerts; no Kraken")
    while True:
        try:
            scan()
        except Exception:
            log.exception("Scan failed")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()
