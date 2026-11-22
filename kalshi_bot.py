"""Kalshi BTC 15-minute market monitor. Alerts only; no trading.

Market prices are NOT independent predictions of BTC direction.
"""
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

BASE = 'https://external-api.kalshi.com/trade-api/v2'
SERIES = os.getenv('KALSHI_SERIES', 'KXBTC15M')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DB_PATH = os.getenv('DB_PATH', '/var/data/kalshi_bot.sqlite3')
SCAN_SECONDS = max(15, int(os.getenv('SCAN_SECONDS', '20')))
MULTI_HORIZONS = (2, 3, 5, 10)
MIN_HORIZON_EVALUATIONS = 50
OPEN_MIN_FREQUENCY = min(0.95, max(0.50, float(os.getenv('OPEN_MIN_FREQUENCY', '0.65'))))
OPEN_MIN_MOVE_USD = max(0.0, float(os.getenv('OPEN_MIN_MOVE_USD', '5')))
MAX_SPREAD = Decimal(os.getenv('MAX_SPREAD', '0.04'))
MIN_MOVE = Decimal(os.getenv('MIN_MOVE', '0.025'))
MIN_VOLUME_24H = Decimal(os.getenv('MIN_VOLUME_24H', '100'))
MIN_REMAINING_SECONDS = max(121, int(os.getenv('MIN_REMAINING_SECONDS', '121')))
OPEN_SIGNAL_DELAY_SECONDS = 20
PROFIT_TARGET_TICKS = 1
KALSHI_TICK = Decimal('0.01')  # Standard cent-priced contracts; verify market tick size before execution.
ALERT_COOLDOWN_HOURS = max(0, float(os.getenv('ALERT_COOLDOWN_HOURS', '6')))
TOP_ALERTS = max(1, int(os.getenv('TOP_ALERTS', '15')))
TELEGRAM_TEST_ON_START = os.getenv('TELEGRAM_TEST_ON_START', 'false').lower() in ('1', 'true', 'yes')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('kalshi-btc-monitor')
session = requests.Session()
session.headers.update({'User-Agent': 'KalshiBTC15MMonitor/3.6'})


def dec(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else None
    except (ValueError, AttributeError):
        return None


def db_open():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS snapshots (ticker TEXT NOT NULL, observed_at REAL NOT NULL, midpoint TEXT NOT NULL, PRIMARY KEY(ticker, observed_at))')
    db.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_time ON snapshots(ticker, observed_at)')
    db.execute('CREATE TABLE IF NOT EXISTS alerts (ticker TEXT PRIMARY KEY, sent_at REAL NOT NULL, side TEXT NOT NULL, entry_price TEXT NOT NULL, reason TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS opening_alerts (ticker TEXT PRIMARY KEY, sent_at REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS final_alerts (ticker TEXT PRIMARY KEY, sent_at REAL NOT NULL)')
    db.execute("""CREATE TABLE IF NOT EXISTS opening_predictions (
        ticker TEXT PRIMARY KEY, predicted_at REAL NOT NULL, initial_price REAL NOT NULL,
        direction TEXT NOT NULL, target_price REAL NOT NULL, horizon_seconds INTEGER NOT NULL,
        model_version TEXT NOT NULL, evaluated_at REAL, observed_price REAL,
        direction_correct INTEGER, target_error_usd REAL, status TEXT NOT NULL DEFAULT 'pending'
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS opening_price_samples (
        ticker TEXT NOT NULL, observed_at REAL NOT NULL, price REAL NOT NULL,
        PRIMARY KEY(ticker, observed_at)
    )""")
    db.execute('CREATE INDEX IF NOT EXISTS idx_open_samples ON opening_price_samples(ticker, observed_at)')
    db.execute("""CREATE TABLE IF NOT EXISTS horizon_evaluations (
        ticker TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
        due_at REAL NOT NULL, evaluated_at REAL, observed_price REAL,
        direction_correct INTEGER, target_error_usd REAL,
        status TEXT NOT NULL DEFAULT 'pending',
        PRIMARY KEY(ticker, horizon_minutes)
    )""")
    db.commit()
    return db


def fetch_markets():
    for attempt in range(5):
        try:
            response = session.get(f'{BASE}/markets', params={'series_ticker': SERIES, 'status': 'open', 'limit': 100}, timeout=20)
            if response.status_code == 429 or response.status_code >= 500:
                retry = response.headers.get('Retry-After', '')
                delay = min(120, int(retry) if retry.isdigit() else 2 ** (attempt + 1))
                log.warning('HTTP %s; reintento en %ss', response.status_code, delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            return payload.get('markets', [])
        except (requests.RequestException, ValueError) as exc:
            if attempt == 4:
                raise
            log.warning('Consulta fallida: %s', exc)
            time.sleep(min(2 ** (attempt + 1), 30))
    raise RuntimeError('Kalshi no respondió después de varios intentos')


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        log.warning('Telegram sin configurar; alerta solo en logs: %s', message)
        return False
    try:
        response = session.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': CHAT_ID, 'text': message}, timeout=20)
        response.raise_for_status()
        data = response.json()
        if not data.get('ok'):
            log.error('Telegram rechazó el mensaje: %s', data.get('description', 'sin detalle'))
        return bool(data.get('ok'))
    except (requests.RequestException, ValueError):
        log.exception('No se pudo enviar alerta Telegram')
        return False


def analyze(market, previous_mid):
    bid = dec(market.get('yes_bid_dollars'))
    ask = dec(market.get('yes_ask_dollars'))
    if bid is None or ask is None or not (0 < bid <= ask < 1):
        return None, 'cotizacion_invalida'
    spread = ask - bid
    if spread > MAX_SPREAD:
        return None, 'spread_alto'
    # Prefer the fixed-point volume field; older API responses may provide volume_24h.
    # Missing volume is unknown, not zero. Never silently bypass the filter.
    volume = dec(market.get('volume_24h_fp'))
    if volume is None:
        volume = dec(market.get('volume_24h'))
    if volume is None:
        return None, 'volumen_no_disponible'
    if volume < MIN_VOLUME_24H:
        return None, 'volumen_bajo'
    mid = (bid + ask) / 2
    if previous_mid is None:
        return None, 'sin_historial_60s'
    change = mid - previous_mid
    if abs(change) < MIN_MOVE:
        return None, 'movimiento_insuficiente'
    side = 'YES' if change > 0 else 'NO'
    entry = ask if side == 'YES' else Decimal(1) - bid
    return {'side': side, 'entry': entry, 'change': change,
            'spread': spread, 'volume': volume}, 'candidato'


_btc_cache = {'at': 0, 'bars': []}


def btc_bars():
    """Recent closed one-minute BTC/USD bars from Kraken; cached 60 seconds."""
    now = time.time()
    if now - _btc_cache['at'] < 60:
        return _btc_cache['bars']
    try:
        response = session.get('https://api.kraken.com/0/public/OHLC',
                               params={'pair': 'XBTUSD', 'interval': 1}, timeout=12)
        response.raise_for_status()
        data = response.json()
        if data.get('error'):
            raise ValueError(str(data['error']))
        key = next(k for k in data['result'] if k != 'last')
        bars = [(int(x[0]), float(x[4])) for x in data['result'][key][:-1]]
        if len(bars) < 180 or now - bars[-1][0] > 180:
            raise ValueError('Insufficient or stale BTC candles')
        _btc_cache.update(at=now, bars=bars)
        return bars
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning('BTC candles unavailable: %s', exc)
        _btc_cache['at'] = now
        return _btc_cache['bars'] if _btc_cache['bars'] and now - _btc_cache['bars'][-1][0] < 180 else []


_spot_cache = {'at': 0, 'price': None}


def btc_spot():
    """Recent Kraken BTC last trade; not the Kalshi settlement index."""
    now = time.time()
    if now - _spot_cache['at'] < 12:
        return _spot_cache['price']
    try:
        response = session.get('https://api.kraken.com/0/public/Ticker',
                               params={'pair': 'XBTUSD'}, timeout=8)
        response.raise_for_status()
        data = response.json()
        if data.get('error'):
            raise ValueError(str(data['error']))
        key = next(iter(data['result']))
        price = float(data['result'][key]['c'][0])
        if not (1000 < price < 10000000):
            raise ValueError('Invalid BTC spot')
        _spot_cache.update(at=now, price=price)
        return price
    except (requests.RequestException, ValueError, KeyError, TypeError, StopIteration) as exc:
        log.warning('BTC spot unavailable: %s', exc)
        return None


_trade_cache = {'at': 0, 'result': None}


def recent_trade_pressure():
    """Recent aggressive buy/sell volume from Kraken public trade prints."""
    now = time.time()
    if now - _trade_cache['at'] < 20:
        return _trade_cache['result']
    _trade_cache.update(at=now, result=None)
    try:
        response = session.get('https://api.kraken.com/0/public/Trades',
                               params={'pair': 'XBTUSD', 'count': 1000}, timeout=8)
        response.raise_for_status()
        payload = response.json()
        if payload.get('error'):
            raise ValueError(str(payload['error']))
        key = next(k for k in payload['result'] if k != 'last')
        trades = payload['result'][key]
        recent = [t for t in trades if len(t) >= 4 and now - 90 <= float(t[2]) <= now + 5]
        buy = sum(float(t[1]) for t in recent if t[3] == 'b')
        sell = sum(float(t[1]) for t in recent if t[3] == 's')
        if len(recent) < 20 or buy + sell <= 0:
            return None
        pressure = (buy - sell) / (buy + sell)
        result = (pressure, len(recent))
        _trade_cache['result'] = result
        return result
    except (requests.RequestException, ValueError, KeyError, TypeError, StopIteration) as exc:
        log.warning('Kraken recent trade pressure unavailable: %s', exc)
        return None


def empirical_probability(bars, horizon, strike=None):
    """Experimental analog estimator. Not calibrated or validated for Kalshi."""
    closes = [price for _, price in bars]
    if len(closes) < 180 or horizon < 1:
        return None
    momentum = closes[-1] / closes[-6] - 1
    samples = []
    for i in range(6, len(closes) - horizon):
        past_momentum = closes[i] / closes[i - 5] - 1
        samples.append((abs(past_momentum - momentum), closes[i + horizon] / closes[i] - 1))
    samples.sort(key=lambda x: x[0])
    selected = [ret for _, ret in samples[:80]]
    if len(selected) < 50:
        return None
    target = 0.0 if strike is None else strike / closes[-1] - 1
    # Laplace smoothing. Descriptive historical analog frequency, not calibrated odds.
    probability = (sum(ret > target for ret in selected) + 1) / (len(selected) + 2)
    return probability, len(selected), closes[-1]



def initial_move_target(bars, spot, window=2):
    """Two-minute historical close-to-close analogs, not exact intraminute extremes."""
    closes = [p for _, p in bars]
    if len(closes) < 180 or min(closes) <= 0 or spot <= 0:
        return None
    momentum = closes[-1] / closes[-6] - 1
    analogs = []
    for i in range(6, len(closes) - window):
        past = closes[i] / closes[i - 5] - 1
        move = closes[i + window] / closes[i] - 1
        analogs.append((abs(past - momentum), i, move))
    analogs.sort(key=lambda x: x[0])
    chosen = []
    for _, index, ret in analogs:
        if all(abs(index - previous) > window for previous, _ in chosen):
            chosen.append((index, ret))
        if len(chosen) == 80:
            break
    returns = [ret for _, ret in chosen]
    if len(returns) < 50:
        return None
    up_count = sum(x > 0 for x in returns)
    down_count = sum(x < 0 for x in returns)
    frequency = max(up_count, down_count) / len(returns)
    # Always report a direction, but label weak evidence clearly.
    direction = 'UP' if up_count > down_count else 'DOWN'
    matching = sorted(abs(x) for x in returns
                      if (x > 0 if direction == 'UP' else x < 0))
    if not matching:
        return None
    median_move = matching[len(matching) // 2]
    target = spot * (1 + median_move if direction == 'UP' else 1 - median_move)
    strong = frequency >= OPEN_MIN_FREQUENCY and abs(target - spot) >= OPEN_MIN_MOVE_USD and up_count != down_count
    return direction, target, len(returns), frequency, strong


def strike_price(market):
    # Never substitute contract bid/ask for BTC settlement strike.
    for field in ('floor_strike', 'strike_price'):
        value = dec(market.get(field))
        if value is not None and value > 1000:
            return float(value)
    return None



def selected_horizon(db):
    """Use a proven horizon; until enough evidence, retain the 2-minute baseline."""
    rows = db.execute(
        "SELECT horizon_minutes,COUNT(*),AVG(direction_correct) "
        "FROM horizon_evaluations WHERE status='evaluated' "
        "GROUP BY horizon_minutes"
    ).fetchall()
    eligible = [(accuracy, -minutes, minutes) for minutes, n, accuracy in rows
                if n >= MIN_HORIZON_EVALUATIONS and accuracy is not None]
    return max(eligible)[2] if len(eligible) == len(MULTI_HORIZONS) else 2


def evaluate_horizons(db, now):
    rows = db.execute(
        "SELECT h.ticker,h.horizon_minutes,h.due_at,p.initial_price,p.direction,p.target_price "
        "FROM horizon_evaluations h JOIN opening_predictions p ON p.ticker=h.ticker "
        "WHERE h.status='pending' AND h.due_at<=?", (now,)
    ).fetchall()
    if not rows:
        return
    price = btc_spot()
    for ticker, minutes, due, initial, direction, target in rows:
        if now - due > max(45, SCAN_SECONDS * 2):
            db.execute("UPDATE horizon_evaluations SET status='missing_observation' "
                       "WHERE ticker=? AND horizon_minutes=?", (ticker, minutes))
            continue
        if price is None:
            continue
        correct = int(price > initial if direction == 'UP' else price < initial)
        db.execute("UPDATE horizon_evaluations SET status='evaluated',evaluated_at=?,"
                   "observed_price=?,direction_correct=?,target_error_usd=? "
                   "WHERE ticker=? AND horizon_minutes=?",
                   (now, price, correct, abs(price-target), ticker, minutes))
        log.info('HORIZON evaluated ticker=%s minutes=%d correct=%d price=%.2f',
                 ticker, minutes, correct, price)


def opening_message(market, now, close_at, horizon=2):
    """Independent opening move alert; never implies Kalshi settlement or profit."""
    remaining = int(close_at - now)
    if not (780 <= remaining <= 900 - OPEN_SIGNAL_DELAY_SECONDS):
        return None
    bars = btc_bars()
    if not bars:
        log.info('OPEN report: BTC history unavailable')
        return '📊 BTC KALSHI — PRIMEROS 2 MINUTOS\n⚪ SIN PRONÓSTICO: historial BTC no disponible'
    spot = btc_spot()
    if spot is None:
        log.info('OPEN report: fresh BTC spot unavailable')
        return '📊 BTC KALSHI — PRIMEROS 2 MINUTOS\n⚪ SIN PRONÓSTICO: precio BTC no disponible'
    if abs(spot / bars[-1][1] - 1) > 0.02:
        log.warning('OPEN report: Kraken spot diverges from last closed candle')
        return '📊 BTC KALSHI — PRIMEROS 2 MINUTOS\n⚪ SIN PRONÓSTICO: precios inconsistentes'
    quick = initial_move_target(bars, spot, window=horizon)
    if quick is None:
        log.info('OPEN report: insufficient historical direction evidence')
        return '📊 BTC KALSHI — PRIMEROS 2 MINUTOS\n⚪ SIN PRONÓSTICO: muestras insuficientes'
    side, target, samples, frequency, strong = quick
    pressure = recent_trade_pressure()
    pressure_text = '⚖️ Flujo de operaciones: no disponible\\n'
    if pressure is not None:
        imbalance, trade_count = pressure
        agrees = (imbalance > 0 and side == 'UP') or (imbalance < 0 and side == 'DOWN')
        pressure_text = (f'⚖️ Presión compradora-vendedora (90 s): {imbalance:+.1%} '
                         f'({trade_count} operaciones); '
                         f'{"confirma dirección" if agrees else "no confirma dirección"}\\n')
        strong = strong and agrees
    else:
        strong = False
    # Indicative one-tick gross profit target for the Kalshi contract, not BTC/USD.
    bid = dec(market.get('yes_bid_dollars'))
    ask = dec(market.get('yes_ask_dollars'))
    if bid is None or ask is None or not (0 < bid <= ask < 1) or ask - bid > MAX_SPREAD:
        return None
    entry = ask if side == 'UP' else Decimal(1) - bid
    exit_target = entry + KALSHI_TICK * PROFIT_TARGET_TICKS
    if exit_target > Decimal('0.99'):
        return None
    delta = target - spot
    direction = 'bajada' if side == 'DOWN' else 'subida'
    return (
        '📊 BTC KALSHI — ENTRADA INICIAL\n'
        f'{"🔻 DOWN" if side == "DOWN" else "🔺 UP"} | {"SEÑAL EXPERIMENTAL" if strong else "PRONÓSTICO DÉBIL — NO OPERAR"}\n'
        '━━━━━━━━━━━━━━━━━━\n'
        f'💰 Precio al detectar señal (Kraken): ${spot:,.2f}\n'
        f'🎯 Objetivo estimado: ${target:,.2f}\n'
        f'📏 Movimiento esperado: {delta:+,.2f} USD\n'
        f'🎟️ Entrada indicativa Kalshi: ${entry:.2f} | Objetivo de venta: ${exit_target:.2f} (+1 tick bruto)\n'
        '⏱️ Horizonte aproximado: 120 segundos; no se puede estimar el segundo exacto con velas de 1 minuto\n'
        f'📊 Frecuencia histórica de dirección: {frequency:.1%} ({samples} muestras)\n'
        f'📝 Interpretación: posible {direction} desde ${spot:,.2f} hacia ${target:,.2f}; '
        'después el precio puede cambiar de rumbo.\n'
        f'⏳ Tiempo restante del contrato: {remaining // 60:02d}:{remaining % 60:02d}\n'
        f'🧾 Contrato: {market.get("ticker", "")}\n'
        '⚠️ Estimación experimental con cierres de velas de 1 minuto. '
        'No es la apertura oficial de Kalshi ni un máximo/mínimo garantizado. '
        'No implica beneficio al vender en Kalshi.'
    )

def prediction_message(market, now, close_at, phase):
    remaining = int(close_at - now)
    if phase == 'OPEN' and not (810 <= remaining <= 900 - OPEN_SIGNAL_DELAY_SECONDS):
        return None
    if phase == 'FINAL' and not (MIN_REMAINING_SECONDS <= remaining <= 180):
        return None
    bars = btc_bars()
    if not bars:
        return None
    strike = strike_price(market)
    if strike is None:
        log.info('%s skipped: no verified BTC strike for %s', phase, market.get('ticker'))
        return None
    horizon = max(1, min(15, round(remaining / 60)))
    estimate = empirical_probability(bars, horizon, strike)
    if estimate is None:
        return None
    probability, count, spot = estimate
    side = 'UP ▲' if probability > 0.5 else 'DOWN ▼'
    confidence = max(probability, 1 - probability)
    if confidence < 0.65:
        log.info('%s skipped: low historical confidence %.3f', phase, confidence)
        return None
    bid = dec(market.get('yes_bid_dollars'))
    ask = dec(market.get('yes_ask_dollars'))
    if bid is None or ask is None or not (0 < bid <= ask < 1):
        return None
    if ask - bid > MAX_SPREAD:
        return None
    entry = ask if probability > 0.5 else 1 - bid
    exit_target = Decimal(str(entry)) + KALSHI_TICK * PROFIT_TARGET_TICKS
    if exit_target > Decimal('0.99'):
        return None
    # Only flag positive theoretical edge versus contract ask.
    if float(entry) + 0.08 >= confidence:
        log.info('%s skipped: insufficient hypothetical edge', phase)
        return None
    phase_name = 'APERTURA' if phase == 'OPEN' else 'ÚLTIMOS 3 MINUTOS'
    quick_note = ''
    if phase == 'OPEN':
        quick = initial_move_target(bars, spot)
        if quick:
            quick_side, quick_target, quick_samples, quick_frequency, quick_strong = quick
            delta = quick_target - spot
            quick_note = (
                '📊 BTC KALSHI — ENTRADA INICIAL\n'
                f'{"🔻 DOWN" if quick_side == "DOWN" else "🔺 UP"}\n'
                '━━━━━━━━━━━━━━━━━━\n'
                f'💰 Precio inicial: ${spot:,.2f}\n'
                f'🎯 Objetivo estimado: ${quick_target:,.2f}\n'
                f'📏 Movimiento esperado: {delta:+,.2f} USD\n'
                '⏱️ Ventana de análisis: Primeros 2 minutos\n'
                f'📊 Frecuencia histórica de dirección: {quick_frequency:.1%} ({quick_samples} muestras)\n'
                f'📝 Interpretación: posible movimiento desde ${spot:,.2f} hacia ${quick_target:,.2f}, '
                'sin asumir que continuará en esa dirección después.\n'
                '⚠️ Estimación experimental con velas de 1 minuto, no predicción exacta.\n'
                '━━━━━━━━━━━━━━━━━━\n'
            )
        else:
            quick_note = '⚡ KALSHI BITCOIN | PRIMEROS 2 MINUTOS: SIN SEÑAL\n━━━━━━━━━━━━━━━━━━\n'

    distance = spot - strike
    distance_label = 'por encima' if distance > 0 else ('por debajo' if distance < 0 else 'igual')
    up_price = float(ask)
    down_price = float(Decimal(1) - bid)
    return (
        f'🔔 SEÑALES BTC 15M | {phase_name}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'🚨 ALERTA DE CIERRE: {side}\n'
        f'{quick_note}'
        f'📊 UP histórico comparable: {probability:.1%}\n'
        f'📉 DOWN histórico comparable: {1 - probability:.1%}\n'
        f'🔎 Muestras comparables: {count}\n'
        f'💰 Precio BTC (Kraken): ${spot:,.2f}\n'
        f'🎯 Referencia del contrato: ${strike:,.2f}\n'
        f'📏 Distancia: ${abs(distance):,.2f} {distance_label}\n'
        f'🎟️ Entrada indicativa: ${float(entry):.2f} | Objetivo de venta: ${exit_target:.2f} (+1 tick bruto)\n'
        f'🟢 Precio UP estimado: ${up_price:.3f}\n'
        f'🔴 Precio DOWN estimado: ${down_price:.3f}\n'
        f'⏳ Tiempo restante: {remaining // 60:02d}:{remaining % 60:02d}\n'
        f'🧾 Contrato: {market.get("ticker", "")}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        '⚠️ Señal experimental: frecuencias históricas no calibradas, '
        'NO probabilidades fiables. Cotizaciones indicativas, no garantizadas. '
        'Kraken puede diferir del índice de liquidación de Kalshi. '
        'No es una recomendación de apuesta.'
    )


def record_opening_prices(db, now):
    """Sample every active two-minute prediction on each scan; not tick-by-tick data."""
    active = db.execute(
        "SELECT ticker,predicted_at,horizon_seconds FROM opening_predictions "
        "WHERE status='pending' AND predicted_at<=? AND predicted_at+horizon_seconds>=?",
        (now, now)
    ).fetchall()
    if not active:
        return
    price = btc_spot()
    if price is None:
        log.warning('OPEN price sample unavailable for %d pending predictions', len(active))
        return
    for ticker, started, horizon in active:
        db.execute(
            'INSERT OR IGNORE INTO opening_price_samples VALUES (?,?,?)',
            (ticker, now, price)
        )
        log.info('OPEN sample ticker=%s elapsed=%.1fs spot=%.2f',
                 ticker, now-started, price)


def evaluate_opening_predictions(db, now):
    """Evaluate observed endpoint only, not intrawindow target touches."""
    rows = db.execute(
        "SELECT ticker,predicted_at,initial_price,direction,target_price,horizon_seconds "
        "FROM opening_predictions WHERE status='pending' AND predicted_at+horizon_seconds<=?",
        (now,)
    ).fetchall()
    if not rows:
        return
    spot = btc_spot()
    for ticker, at, initial, direction, target, horizon in rows:
        delay = now - (at + horizon)
        if delay > max(45, SCAN_SECONDS * 2):
            db.execute("UPDATE opening_predictions SET status='missing_observation' WHERE ticker=?", (ticker,))
            log.warning('OPEN evaluation missed timely observation: %s', ticker)
            continue
        if spot is None:
            continue
        correct = int((spot > initial) if direction == 'UP' else (spot < initial))
        error = abs(spot - target)
        db.execute(
            "UPDATE opening_predictions SET evaluated_at=?,observed_price=?,"
            "direction_correct=?,target_error_usd=?,status='evaluated' WHERE ticker=?",
            (now, spot, correct, error, ticker)
        )
        samples = db.execute(
            'SELECT observed_at,price FROM opening_price_samples '
            'WHERE ticker=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at',
            (ticker, at, at+horizon)
        ).fetchall()
        values = [initial] + [p for _, p in samples] + [spot]
        low, high = min(values), max(values)
        touched = (low <= target if direction == 'DOWN' else high >= target)
        rises = sum(b > a for a,b in zip(values,values[1:]))
        falls = sum(b < a for a,b in zip(values,values[1:]))
        log.info('OPEN path ticker=%s sampled_points=%d low=%.2f high=%.2f '
                 'target_touched_in_samples=%s rises=%d falls=%d '
                 'sampling_interval_approx=%ds (not tick data)',
                 ticker, len(samples), low, high, int(touched), rises, falls, SCAN_SECONDS)
        log.info('OPEN evaluated ticker=%s correct=%s initial=%.2f observed=%.2f '
                 'target=%.2f endpoint_error=%.2f observation_delay=%.1fs',
                 ticker, correct, initial, spot, target, error, delay)
        outcome = '✅ ACERTÓ DIRECCIÓN' if correct else '❌ FALLÓ DIRECCIÓN'
        move = spot - initial
        target_status = 'Sí (en muestras)' if touched else 'No observado en muestras'
        message = (
            f'📊 BTC KALSHI — RESULTADO A LOS 2 MINUTOS\n'
            f'{outcome}\n'
            f'🧾 Contrato: {ticker}\n'
            f'🔮 Pronóstico: {direction}\n'
            f'💰 Precio inicial: ${initial:,.2f}\n'
            f'🎯 Objetivo estimado: ${target:,.2f}\n'
            f'📍 Precio observado: ${spot:,.2f}\n'
            f'📏 Movimiento real: {move:+,.2f} USD\n'
            f'📐 Error frente al objetivo: ${error:,.2f}\n'
            f'📉 Mínimo muestreado: ${low:,.2f}\n'
            f'📈 Máximo muestreado: ${high:,.2f}\n'
            f'🎯 Objetivo alcanzado: {target_status}\n'
            f'⏱️ Evaluación realizada {delay:.0f} s después del horizonte.\n'
            f'⚠️ Evaluación aproximada con Kraken; no representa el resultado oficial de Kalshi.'
        )
        if not send_telegram(message):
            log.error('OPEN result notification failed ticker=%s', ticker)
    count, hits, avg_error = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(direction_correct),0),AVG(target_error_usd) "
        "FROM opening_predictions WHERE status='evaluated'"
    ).fetchone()
    if count:
        log.info('OPEN metrics evaluated=%d directional_accuracy=%.1f%% mean_endpoint_error=%.2f USD',
                 count, hits * 100 / count, avg_error)


def scan():
    markets = fetch_markets()
    now = time.time()
    sent = 0
    reasons = {}

    def count(reason):
        reasons[reason] = reasons.get(reason, 0) + 1

    with db_open() as db:
        record_opening_prices(db, now)
        evaluate_opening_predictions(db, now)
        evaluate_horizons(db, now)
        for market in markets:
            ticker = market.get('ticker', '')
            if not isinstance(ticker, str) or not ticker.startswith(SERIES + '-'):
                count('serie_distinta')
                continue
            close_at = timestamp(market.get('close_time'))
            open_at = timestamp(market.get('open_time'))
            if close_at is None:
                count('sin_cierre')
                continue
            if close_at - now < MIN_REMAINING_SECONDS:
                count('poco_tiempo')
                continue
            if open_at and open_at > now:
                count('todavia_no_abierto')
                continue
            # Observe the market immediately, but never send opening alerts before 20s.
            opening_ready = open_at is not None and now - open_at >= OPEN_SIGNAL_DELAY_SECONDS
            if open_at and close_at - open_at > 960:
                count('duracion_invalida')
                continue
            for phase, table in (('OPEN', 'opening_alerts'), ('FINAL', 'final_alerts')):
                if phase == 'OPEN' and not opening_ready:
                    continue
                if db.execute(f'SELECT 1 FROM {table} WHERE ticker=?', (ticker,)).fetchone():
                    continue
                message = (opening_message(market, now, close_at, selected_horizon(db)) if phase == 'OPEN'
                           else prediction_message(market, now, close_at, phase))
                if message and send_telegram(message):
                    if phase == 'OPEN' and '🎯 Objetivo estimado:' in message:
                        bars = btc_bars()
                        spot = btc_spot()
                        horizon = selected_horizon(db)
                        quick = initial_move_target(bars, spot, window=horizon) if bars and spot else None
                        if quick:
                            direction, target, _, _, _ = quick
                            db.execute(
                                "INSERT OR IGNORE INTO opening_predictions "
                                "(ticker,predicted_at,initial_price,direction,target_price,"
                                "horizon_seconds,model_version) VALUES (?,?,?,?,?,?,?)",
                                (ticker, now, spot, direction, target, horizon * 60, '3.11')
                            )
                            for minutes in MULTI_HORIZONS:
                                db.execute(
                                    "INSERT OR IGNORE INTO horizon_evaluations "
                                    "(ticker,horizon_minutes,due_at) VALUES (?,?,?)",
                                    (ticker, minutes, now + minutes * 60)
                                )
                    db.execute(f'INSERT INTO {table} VALUES (?,?)', (ticker, now))
                    sent += 1
                    log.info('Experimental %s signal sent: %s', phase, ticker)
            bid = dec(market.get('yes_bid_dollars'))
            ask = dec(market.get('yes_ask_dollars'))
            if bid is None or ask is None or not (0 < bid <= ask < 1):
                count('cotizacion_invalida')
                continue
            row = db.execute(
                'SELECT midpoint FROM snapshots WHERE ticker=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1',
                (ticker, now - 60)
            ).fetchone()
            prior = dec(row[0]) if row else None
            midpoint = (bid + ask) / 2
            db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?)',
                       (ticker, now, str(midpoint)))
            count('historial_actualizado')
        db.execute('DELETE FROM snapshots WHERE observed_at < ?', (now - 48 * 3600,))
        db.execute('DELETE FROM opening_price_samples WHERE observed_at < ?', (now - 30 * 86400,))
        # BTC 15m tickers are unique. Keep alert history so a restart never
        # re-sends an old contract; only snapshot history is pruned.
        db.commit()
    details = ', '.join(f'{key}={value}' for key, value in sorted(reasons.items())) or 'sin_detalles'
    log.info('Escaneo: %s mercados, %s alertas | motivos: %s', len(markets), sent, details)


def main():
    log.info('Kalshi BTC15M v3.11 multi-horizon evaluation; no trading')
    if TELEGRAM_TEST_ON_START:
        if send_telegram('🧪 Prueba de conexión Kalshi Bot v3.3: Telegram funciona. No es una señal de mercado.'):
            log.info('Prueba Telegram enviada correctamente')
        else:
            log.error('Prueba Telegram fallida')
    while True:
        try:
            scan()
        except Exception:
            log.exception('Error durante el escaneo')
        time.sleep(SCAN_SECONDS)


if __name__ == '__main__':
    main()