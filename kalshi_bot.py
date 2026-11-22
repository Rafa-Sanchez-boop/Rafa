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
SCAN_SECONDS = max(20, int(os.getenv('SCAN_SECONDS', '30')))
MAX_SPREAD = Decimal(os.getenv('MAX_SPREAD', '0.04'))
MIN_MOVE = Decimal(os.getenv('MIN_MOVE', '0.025'))
MIN_VOLUME_24H = int(os.getenv('MIN_VOLUME_24H', '100'))
MIN_REMAINING_SECONDS = int(os.getenv('MIN_REMAINING_SECONDS', '90'))
ALERT_COOLDOWN_HOURS = max(0, float(os.getenv('ALERT_COOLDOWN_HOURS', '6')))
TOP_ALERTS = max(1, int(os.getenv('TOP_ALERTS', '15')))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('kalshi-btc-monitor')
session = requests.Session()
session.headers.update({'User-Agent': 'KalshiBTC15MMonitor/3.0'})


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
    raise RuntimeError('Kalshi no respondiÃ³ despuÃ©s de varios intentos')


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        log.warning('Telegram sin configurar; alerta solo en logs: %s', message)
        return False
    try:
        response = session.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': CHAT_ID, 'text': message}, timeout=20)
        response.raise_for_status()
        data = response.json()
        if not data.get('ok'):
            log.error('Telegram rechazÃ³ el mensaje: %s', data.get('description', 'sin detalle'))
        return bool(data.get('ok'))
    except (requests.RequestException, ValueError):
        log.exception('No se pudo enviar alerta Telegram')
        return False


def analyze(market, previous_mid):
    bid = dec(market.get('yes_bid_dollars'))
    ask = dec(market.get('yes_ask_dollars'))
    if bid is None or ask is None or not (0 < bid <= ask < 1):
        return None
    spread = ask - bid
    if spread > MAX_SPREAD:
        return None
    volume = market.get('volume_24h')
    try:
        volume = int(volume or 0)
    except (ValueError, TypeError):
        volume = 0
    if volume < MIN_VOLUME_24H:
        return None
    mid = (bid + ask) / 2
    if previous_mid is None:
        return {'mid': mid, 'signal': None}
    change = mid - previous_mid
    if abs(change) < MIN_MOVE:
        return {'mid': mid, 'signal': None}
    # Movement of contract quote, not BTC spot price and not calibrated probability.
    side = 'YES' if change > 0 else 'NO'
    entry = ask if side == 'YES' else Decimal(1) - bid
    return {'mid': mid, 'signal': {'side': side, 'entry': entry, 'change': change, 'spread': spread, 'volume': volume}}


def scan():
    markets = fetch_markets()
    now = time.time()
    sent = 0
    with db_open() as db:
        for market in markets:
            ticker = market.get('ticker', '')
            if not isinstance(ticker, str) or not ticker.startswith(SERIES + '-'):
                continue
            close_at = timestamp(market.get('close_time'))
            open_at = timestamp(market.get('open_time'))
            if close_at is None or close_at - now < MIN_REMAINING_SECONDS or (open_at and open_at > now):
                continue
            if open_at and close_at - open_at > 960:
                continue
            row = db.execute('SELECT midpoint FROM snapshots WHERE ticker=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1', (ticker, now - 60)).fetchone()
            prior = dec(row[0]) if row else None
            bid = dec(market.get('yes_bid_dollars'))
            ask = dec(market.get('yes_ask_dollars'))
            if bid is None or ask is None or not (0 < bid <= ask < 1):
                continue
            midpoint = (bid + ask) / 2
            db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?)', (ticker, now, str(midpoint)))
            result = analyze(market, prior)
            if not result or not result['signal']:
                continue
            if db.execute('SELECT 1 FROM alerts WHERE ticker=?', (ticker,)).fetchone():
                continue
            sig = result['signal']
            minutes = int((close_at - now) // 60)
            message = (f'ð KALSHI BTC 15M â MOVIMIENTO DE COTIZACIÃN\n'
                       f'Contrato: {ticker}\nLado observado: {sig["side"]}\n'
                       f'Precio de referencia: ${sig["entry"]:.3f}\n'
                       f'Cambio en precio YES: {sig["change"]:+.3f}\n'
                       f'Spread: ${sig["spread"]:.3f}\nVolumen 24h: {sig["volume"]}\n'
                       f'Tiempo restante: {minutes} min\n\n'
                       'â ï¸ No es predicciÃ³n validada ni recomendaciÃ³n de compra. Verifica el contrato y el precio actual.')
            if send_telegram(message):
                db.execute('INSERT INTO alerts VALUES (?,?,?,?,?)', (ticker, now, sig['side'], str(sig['entry']), 'contract_quote_momentum'))
                sent += 1
                log.info('Alerta enviada: %s %s', ticker, sig['side'])
                if sent >= TOP_ALERTS:
                    break
        db.execute('DELETE FROM snapshots WHERE observed_at < ?', (now - 48 * 3600,))
        db.execute('DELETE FROM alerts WHERE sent_at < ?', (now - max(48, ALERT_COOLDOWN_HOURS) * 3600,))
        db.commit()
    log.info('Escaneo: %s mercados, %s alertas', len(markets), sent)


def main():
    log.info('Kalshi BTC15M v3 â monitor de cotizaciones, solo alertas')
    while True:
        try:
            scan()
        except Exception:
            log.exception('Error durante el escaneo')
        time.sleep(SCAN_SECONDS)


if __name__ == '__main__':
    main()