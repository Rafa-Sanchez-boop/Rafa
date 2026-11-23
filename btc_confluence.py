"""BTC spot confluence filters based on public Coinbase Exchange candles."""

import time
from decimal import Decimal

BASE = "https://api.exchange.coinbase.com"
CACHE_SECONDS = 45
cache = {}


def _candles(session, product, granularity, count):
    key = (product, granularity)
    saved = cache.get(key)
    now = time.time()
    if saved and now - saved[0] < CACHE_SECONDS:
        return saved[1]
    response = session.get(
        f"{BASE}/products/{product}/candles",
        params={"granularity": granularity, "limit": count},
        timeout=8,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or len(rows) < count - 2:
        raise ValueError("Coinbase returned too few BTC candles")
    candles = []
    for row in sorted(rows, key=lambda item: item[0]):
        if len(row) != 6:
            raise ValueError("Invalid Coinbase candle")
        timestamp, low, high, opened, close, volume = row
        candles.append(tuple(Decimal(str(x)) for x in (timestamp, low, high, opened, close, volume)))
    cache[key] = (now, candles)
    return candles


def _ema(values, period):
    if len(values) < period:
        raise ValueError("Insufficient candles for EMA")
    alpha = Decimal(2) / Decimal(period + 1)
    value = sum(values[:period], Decimal(0)) / Decimal(period)
    for price in values[period:]:
        value = alpha * price + (Decimal(1) - alpha) * value
    return value


def _rsi(values, period=14):
    if len(values) < period + 1:
        raise ValueError("Insufficient candles for RSI")
    changes = [right - left for left, right in zip(values[:-1], values[1:])][-period:]
    gains = sum((max(change, Decimal(0)) for change in changes), Decimal(0)) / Decimal(period)
    losses = sum((max(-change, Decimal(0)) for change in changes), Decimal(0)) / Decimal(period)
    if losses == 0:
        return Decimal(100) if gains else Decimal(50)
    return Decimal(100) - Decimal(100) / (Decimal(1) + gains / losses)


def _vwap(candles):
    selected = candles[-60:]
    volume = sum((row[5] for row in selected), Decimal(0))
    if volume <= 0:
        raise ValueError("Coinbase BTC candle volume unavailable")
    weighted = sum((((row[2] + row[1] + row[4]) / Decimal(3)) * row[5] for row in selected), Decimal(0))
    return weighted / volume


def assess(session, direction):
    """Return per-filter status and price only when usable data agree."""
    if direction not in {"UP", "DOWN"}:
        return None
    try:
        one = _candles(session, "BTC-USD", 60, 65)
        five = _candles(session, "BTC-USD", 300, 22)
        fifteen = _candles(session, "BTC-USD", 900, 22)
        closes1 = [row[4] for row in one]
        closes5 = [row[4] for row in five]
        closes15 = [row[4] for row in fifteen]
        spot = closes1[-1]
        ema15 = _ema(closes15, 20)
        m15 = closes15[-1] - closes15[-2]
        m5 = closes5[-1] - closes5[-2]
        rsi1 = _rsi(closes1)
        vwap60 = _vwap(one)

        # The image's Z-score limit is kept as an outlier guard. Compare spot
        # to the hourly mean using population standard deviation of 1m closes.
        window = closes1[-60:]
        mean = sum(window, Decimal(0)) / Decimal(60)
        variance = sum(((price - mean) ** 2 for price in window), Decimal(0)) / Decimal(60)
        if variance <= 0:
            return None
        zscore = (spot - mean) / variance.sqrt()

        up = direction == "UP"
        trend_ok = spot > ema15 if up else spot < ema15
        m15_ok = m15 > 0 if up else m15 < 0
        m5_ok = m5 > 0 if up else m5 < 0
        vwap_ok = spot > vwap60 if up else spot < vwap60
        rsi_ok = Decimal(50) <= rsi1 <= Decimal(75) if up else Decimal(25) <= rsi1 <= Decimal(50)
        filters = {
            "ema20_15m": trend_ok,
            "momentum_15m": m15_ok,
            "rsi14_1m": rsi_ok,
            "momentum_5m": m5_ok,
            "vwap_60m": vwap_ok,
            "zscore_under_2": abs(zscore) < Decimal(2),
        }
        return {
            "passed": all(filters.values()),
            "filters": filters,
            "spot": spot,
            "ema15": ema15,
            "rsi1": rsi1,
            "vwap60": vwap60,
            "zscore": zscore,
        }
    except (ValueError, ArithmeticError, KeyError, TypeError, IndexError):
        return None
