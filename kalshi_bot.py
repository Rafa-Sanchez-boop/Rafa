"""BTC 15-minute Kalshi contract signals with optional Kraken BTC/USD context; no trading."""
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
MIN_REMAINING = 0  # Allow alerts until contract closes, including final 2-3 minutes.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-only")
http = requests.Session()
http.headers["User-Agent"] = "KalshiBTC15M/5.1"
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
    db.execute("""CREATE TABLE IF NOT EXISTS btc_reference_samples
               (observed_at REAL PRIMARY KEY, price TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_results
               (ticker TEXT PRIMARY KEY, result TEXT, observed_at REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS signal_features
               (ticker TEXT PRIMARY KEY, direction TEXT, move REAL,
                remaining_seconds INTEGER, recorded_at REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS learning_audit
               (ticker TEXT PRIMARY KEY, correct INTEGER, assessed_at REAL)""")
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
        accepted = bool(r.json().get("ok"))
        if accepted:
            log.info("Telegram API accepted sendMessage")
        else:
            log.error("Telegram API rejected sendMessage")
        return accepted
    except (requests.RequestException, ValueError):
        log.exception("Telegram send failed")
        return False

def kraken_reference(db, now):
    """Optional BTC/USD spot context, never used to determine Kalshi settlement."""
    try:
        r = http.get("https://api.kraken.com/0/public/Ticker",
                     params={"pair": "XBTUSD"}, timeout=8)
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            return "📍 BTC/USD Kraken: no disponible (API)."
        ticker = next(iter(payload["result"].values()))
        price = dec(ticker["c"][0])
        if price is None or price <= 0:
            return "📍 BTC/USD Kraken: dato no válido."
        old = db.execute("""SELECT price FROM btc_reference_samples
            WHERE observed_at <= ? ORDER BY observed_at DESC LIMIT 1""",
                         (now - 60,)).fetchone()
        previous = dec(old[0]) if old else None
        db.execute("INSERT OR REPLACE INTO btc_reference_samples VALUES (?,?)",
                   (now, str(price)))
        if previous is None:
            return f"📍 BTC/USD Kraken: ${price:,.2f}; sin referencia previa suficiente."
        delta = price - previous
        trend = "subió" if delta > 0 else "bajó" if delta < 0 else "sin cambio"
        return (f"📍 BTC/USD Kraken: ${price:,.2f}; "
                f"{trend} ${abs(delta):,.2f} frente a muestra previa (≥60 s).")
    except (requests.RequestException, ValueError, KeyError, TypeError, StopIteration):
        log.warning("Kraken reference unavailable", exc_info=True)
        return "📍 BTC/USD Kraken: no disponible; señal de Kalshi sin confirmar."

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

def performance(db, direction=None):
    """Evaluate only settled, previously emitted alerts; no look-ahead."""
    where = "AND a.direction=?" if direction else ""
    params = (direction,) if direction else ()
    rows = db.execute("""SELECT a.direction, r.result
        FROM kalshi_only_alerts a JOIN kalshi_only_results r ON r.ticker=a.ticker
        WHERE a.phase='OPEN' AND r.result IN ('yes','no') """ + where, params).fetchall()
    n = len(rows)
    hits = sum((d == "UP" and result == "yes") or
               (d == "DOWN" and result == "no") for d, result in rows)
    return n, hits


def learning_summary(db, direction):
    """Conservative empirical reliability with a 10-signal minimum."""
    n, hits = performance(db, direction)
    if n < 10:
        return f"🧠 Aprendizaje {direction}: {n}/10 resultados mínimos; sin ajuste aún."
    rate = hits / n
    # A smoothed historical agreement rate is NOT a validated probability.
    smoothed = (hits + 2) / (n + 4)
    note = ("historial favorable" if smoothed >= 0.60 else
            "historial débil; revisar señales" if smoothed < 0.45 else
            "historial sin ventaja clara")
    return (f"🧠 Aprendizaje {direction}: {hits}/{n} coincidencias "
            f"({rate:.1%}); evaluación suavizada {smoothed:.1%}, {note}. "
            "No es probabilidad de ganar.")


def accuracy_report(db):
    n, hits = performance(db)
    up_n, up_hits = performance(db, "UP")
    down_n, down_hits = performance(db, "DOWN")
    if not n:
        return "📚 Precisión histórica: sin contratos alertados y liquidados todavía."
    return (f"📚 Precisión histórica (coincidencia con liquidación): "
            f"{hits}/{n} = {hits/n:.1%}\\n"
            f"🔺 UP: {up_hits}/{up_n} | 🔻 DOWN: {down_hits}/{down_n}\\n"
            "⚠️ No equivale a rentabilidad ni precisión predictiva validada.")

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
    kraken_line = kraken_reference(db, now)
    learned = learning_summary(db, direction)
    historical = accuracy_report(db)
    interpretation = (
        "\n🧠 INTERPRETACIÓN — cotizaciones de Kalshi\n"
        + f"📊 Probabilidad implícita YES aproximada: {mid * 100:.1f}%\n"
        + f"📈 Posible subida: YES podría superar ${mid:.3f} si continúa la presión compradora; sin objetivo cuantificado.\n"
        + f"📉 Posible bajada: YES podría caer por debajo de ${mid:.3f} si domina la presión vendedora; sin objetivo cuantificado.\n"
        + f"🔄 Referencia de cambio de rumbo: YES ${prior:.3f} (cotización observada hace al menos 60 segundos).\n"
        + f"🔎 Interpretación: YES {'subió' if up else 'bajó'} {abs(change) * 100:.1f} centavos respecto a esa referencia.\n"
        + "⚠️ Estas cifras son del contrato, NO del precio BTC/USD. No hay base para calcular objetivos BTC en dólares ni una confianza estadística validada.\n"
    )
    phase_name = "OPORTUNIDAD A TIEMPO"
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
        + kraken_line + "\n"
        + interpretation
        + learned + "\n" + historical + "\n"
        +  "⚠️ Señal basada exclusivamente en cotizaciones de Kalshi. "
          "El movimiento de cotización no garantiza el resultado final. "
          "No se fija objetivo de venta; comisiones y liquidez pueden impedir beneficio. "
          "Referencia BTC/USD de Kraken solo informativa. "
          "Alerta informativa, no operación automática."
    )
    if notify(message):
        alerted_tickers.add(ticker)
        db.execute("INSERT OR IGNORE INTO signal_features VALUES (?,?,?,?,?)",
                   (ticker, direction, float(change), remaining, now))
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
            db.execute("INSERT OR IGNORE INTO learning_audit VALUES (?,?,?)",
                       (ticker, int(correct), now))
            notify(f"📊 KALSHI — RESULTADO OFICIAL\n🧾 {ticker}\n"
                   f"📈 Dirección de momentum observada: {predicted[0]}\n"
                   f"🏁 Resultado final Kalshi: {result.upper()}\n"
                   f"{'Coincidió' if correct else 'No coincidió'} con el resultado final.\n"
                   "⚠️ La señal mide momentum de cotización, no predice "
                   "el resultado final ni confirma beneficio de compraventa.\n"
                   + accuracy_report(db))

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
            # Scan continuously after initial analysis, until the contract closes.
            # OPEN phase is used for one alert per contract.
            if now - opened >= DELAY and remaining >= MIN_REMAINING:
                signal(db, m, now, closes, opened, "OPEN")
        # Check completed markets infrequently to reduce requests.
        if int(now // 300) != int((now - SCAN_SECONDS) // 300):
            check_results(db, now)
        db.execute("DELETE FROM kalshi_only_samples WHERE observed_at < ?", (now - 48 * 3600,))
        db.execute("DELETE FROM btc_reference_samples WHERE observed_at < ?", (now - 48 * 3600,))
        db.commit()

def main():
    log.info("Kalshi BTC15M v5.1: Kraken, interpretation and historical learning")
    while True:
        try:
            scan()
        except Exception:
            log.exception("Scan failed")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()
