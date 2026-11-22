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
TARGET_TICKS = 35
TICK = Decimal("0.01")
MAX_SPREAD = Decimal(os.getenv("MAX_SPREAD", "0.04"))
MIN_MOVE = Decimal(os.getenv("MIN_MOVE", "0.025"))
MIN_VOLUME = Decimal(os.getenv("MIN_VOLUME_24H", "100"))
MIN_REMAINING = max(121, int(os.getenv("MIN_REMAINING_SECONDS", "121")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-only")
http = requests.Session()
http.headers["User-Agent"] = "KalshiOnlyBTC15M/4.0"

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
    if db.execute("SELECT 1 FROM kalshi_only_alerts WHERE ticker=? AND phase=?",
                  (ticker, phase)).fetchone():
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
        # Compare current contract pricing with samples observed in its first
        # 20-60 seconds; do not pretend this is BTC spot momentum.
        rows = db.execute("""SELECT yes_mid FROM kalshi_only_samples
            WHERE ticker=? AND observed_at>=? AND observed_at<=?
            ORDER BY observed_at LIMIT 1""",
            (ticker, open_at, open_at + 60)).fetchone()
        if not rows:
            log.info("No early Kalshi sample for %s; no signal", ticker)
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
    target = entry + TICK * TARGET_TICKS
    if target > Decimal("0.99"):
        log.info("Target outside allowed price range: %s", ticker)
        return
    remaining = max(0, int(close_at - now))
    phase_name = "DESPUÉS DE 2 MINUTOS" if phase == "OPEN" else "ÚLTIMOS 3 MINUTOS"
    message = (
        "📊 BTC KALSHI — " + phase_name + "\\n"
        + ("🔺 UP" if up else "🔻 DOWN") + " | SEÑAL EXPERIMENTAL\\n"
        + "━━━━━━━━━━━━━━━━━━\\n"
        + f"📈 Precio YES anterior (Kalshi): ${prior:.3f}\\n"
        + f"📈 Precio YES actual (Kalshi): ${mid:.3f}\\n"
        + f"📏 Cambio en cotización YES: {change:+.3f}\\n"
        + f"🎟️ Entrada indicativa {direction}: ${entry:.2f}\\n"
        + f"🎯 Objetivo de venta: ${target:.2f} (+{TARGET_TICKS} ticks brutos)\\n"
        + f"⏳ Tiempo restante: {remaining//60:02d}:{remaining%60:02d}\\n"
        + f"🧾 Contrato: {ticker}\\n"
        + "⚠️ Solo datos de Kalshi. El cambio de cotización no predice "
          "necesariamente el precio BTC ni el resultado final. "
          "Objetivo no garantizado; comisiones y liquidez pueden impedir beneficio. "
          "Alerta informativa, no operación automática."
    )
    if notify(message):
        db.execute("INSERT INTO kalshi_only_alerts VALUES (?,?,?,?)",
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
            notify(f"📊 KALSHI — RESULTADO OFICIAL\\n🧾 {ticker}\\n"
                   f"🔮 Pronóstico: {predicted[0]}\\n"
                   f"🏁 Resultado Kalshi: {result.upper()}\\n"
                   f"{'✅ ACERTÓ' if correct else '❌ FALLÓ'}\\n"
                   "⚠️ Acertar dirección no confirma que se vendiera al objetivo de 35 ticks.")

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
            if now - opened >= DELAY and remaining >= MIN_REMAINING:
                if 720 <= remaining <= 780:
                    signal(db, m, now, closes, opened, "OPEN")
                if MIN_REMAINING <= remaining <= 180:
                    signal(db, m, now, closes, opened, "FINAL")
        # Check completed markets infrequently to reduce requests.
        if int(now // 300) != int((now - SCAN_SECONDS) // 300):
            check_results(db, now)
        db.execute("DELETE FROM kalshi_only_samples WHERE observed_at < ?", (now - 48 * 3600,))
        db.commit()

def main():
    log.info("Kalshi-only BTC15M v4.0: observe first 120s; alerts only; no Kraken")
    while True:
        try:
            scan()
        except Exception:
            log.exception("Scan failed")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()
