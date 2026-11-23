"""Kalshi BTC 15-minute alerts with Kalshi-only confirmation; optional Kalshi order execution.
Signals describe Kalshi YES/NO contract momentum, not BTC/USD price forecasts.
"""
import logging
import os
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
import requests

from kalshi_trading import Trading
from btc_confluence import assess

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = os.getenv("KALSHI_SERIES", "KXBTC15M")
DB_PATH = os.getenv("DB_PATH", "/var/data/kalshi_bot.sqlite3")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_SECONDS = max(10, int(os.getenv("SCAN_SECONDS", "20")))
DELAY = 0
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
    return bid, ask, (bid + ask) / 2

def sample(db, ticker, now, mid):
    db.execute("INSERT OR IGNORE INTO kalshi_only_samples VALUES (?,?,?)",
               (ticker, now, str(mid)))

def confirms_pullback(samples, up):
    """Check an impulse, a Fibonacci pullback, and two continuation quotes."""
    if len(samples) != 5 or any(price is None for price in samples):
        return False
    direction = 1 if up else -1
    start, peak, pullback, first, latest = (direction * price for price in samples)
    impulse = peak - start
    if impulse <= 0:
        return False
    retracement = (peak - pullback) / impulse
    return (Decimal("0.50") <= retracement <= Decimal("0.786")
            and pullback < first < latest)


def signal(db, m, now, close_at, open_at, phase, trading=None):
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
    if change == 0:
        # No observed direction; avoid inventing UP or DOWN.
        return
    up = change > 0
    confluence = assess(http, direction)
    if confluence is None or not confluence["passed"]:
        return
    recent = db.execute("""SELECT yes_mid FROM kalshi_only_samples
        WHERE ticker=? AND observed_at BETWEEN ? AND ?
        ORDER BY observed_at DESC LIMIT 5""", (ticker, now - 120, now)).fetchall()
    if not confirms_pullback([dec(row[0]) for row in reversed(recent)], up):
        return
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
        ("🔺 UP" if up else "🔻 DOWN") + "\n"
        + ("📈 Posible subida" if up else "📉 Posible bajada")
        + f" desde ${mid:.3f} hacia ${projected:.3f}.\n"
        + "🔄 Después el precio puede cambiar de rumbo."
    )
    if notify(message):
        alerted_tickers.add(ticker)
        db.execute("INSERT OR IGNORE INTO kalshi_only_alerts VALUES (?,?,?,?)",
                   (ticker, phase, now, direction))
        log.info("Kalshi-only %s alert: %s %s", phase, ticker, direction)
        if trading is not None:
            db.commit()
            trading.open(m, "yes" if up else "no", entry, standard_fees(m))

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

def standard_fees(market):
    event_ticker = market.get("event_ticker")
    if not event_ticker:
        return False
    series = (get_json("/series/" + SERIES) or {}).get("series", {})
    event = (get_json("/events/" + event_ticker) or {}).get("event", {})
    if not event or event.get("series_ticker") != SERIES:
        return False
    multiplier = dec(event.get("fee_multiplier_override"))
    if multiplier is None or multiplier == 0:
        multiplier = dec(series.get("fee_multiplier"))
    fee_type = event.get("fee_type_override") or series.get("fee_type")
    return (fee_type in {"quadratic", "quadratic_with_maker_fees"}
            and multiplier is not None and Decimal(0) <= multiplier <= Decimal(1))


def scan(trading=None):
    payload = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    if payload is None:
        return
    if trading is not None:
        markets = {m["ticker"]: m for m in payload.get("markets", []) if m.get("ticker")}
        trading.maintain(markets, lambda ticker: (
            get_json("/markets/" + ticker) or {}
        ).get("market"), standard_fees)
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
            if 300 < remaining <= 780:
                signal(db, m, now, closes, opened, "OPEN", trading)
        # Check completed markets infrequently to reduce requests.
        if int(now // 300) != int((now - SCAN_SECONDS) // 300):
            check_results(db, now)
        db.execute("DELETE FROM kalshi_only_samples WHERE observed_at < ?", (now - 48 * 3600,))
        db.commit()

def telegram_test_once():
    """Send one diagnostic message, without fabricating a market signal."""
    marker = os.path.join(os.path.dirname(DB_PATH) or ".", "telegram_test_20261122.ok")
    if os.path.exists(marker):
        return
    log.info("Telegram diagnostic: attempting test delivery")
    if notify("🤖 RAFA — PRUEBA DE TELEGRAM. Conexión funcionando. (No es señal de trading)"):
        log.info("Telegram diagnostic: API accepted test message")
        try:
            os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
            with open(marker, "w", encoding="utf-8") as out:
                out.write("sent\\n")
        except OSError:
            log.exception("Telegram diagnostic marker could not be saved")
    else:
        log.error("Telegram diagnostic: test message failed or was not accepted")

def main():
    log.info("Kalshi BTC15M using Kalshi quotes only and short UP/DOWN messages")
    try:
        trading = Trading.from_environment(http)
    except (OSError, ValueError, RuntimeError, requests.RequestException, sqlite3.Error) as exc:
        log.error("Kalshi order initialization failed (%s). Alerts active.", type(exc).__name__)
        trading = None
    while True:
        try:
            scan(trading)
        except Exception:
            log.exception("Scan failed")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()
