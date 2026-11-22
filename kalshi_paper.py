"""Conservative Kalshi paper-trading gate. Does not submit real orders."""
import logging
import os
import sqlite3
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN

LOG = logging.getLogger("kalshi-paper")
MAX_CAPITAL = Decimal("20.00")
MAX_ENTRY = Decimal("2.00")
MAX_OPEN_TRADES = 1
MIN_SECONDS_TO_CLOSE = 90


def money(value):
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def init_paper(db):
    db.execute("""CREATE TABLE IF NOT EXISTS paper_trades
        (ticker TEXT PRIMARY KEY, direction TEXT NOT NULL, price TEXT NOT NULL,
         contracts INTEGER NOT NULL, reserved TEXT NOT NULL,
         created_at REAL NOT NULL, close_at REAL NOT NULL,
         status TEXT NOT NULL DEFAULT 'OPEN')""")


def plan_paper_trade(db, ticker, direction, yes_bid, yes_ask, close_at, now=None):
    """One open simulated trade, hard spending caps; no real execution."""
    now = time.time() if now is None else now
    init_paper(db)
    if not ticker.startswith("KXBTC15M-") or direction not in ("UP", "DOWN"):
        return None
    if close_at - now < MIN_SECONDS_TO_CLOSE:
        return None
    bid, ask = money(yes_bid), money(yes_ask)
    if bid is None or ask is None or not (0 < bid <= ask < 1):
        return None
    if ask - bid > Decimal("0.04"):
        return None
    price = ask if direction == "UP" else Decimal("1") - bid
    if price <= 0 or price >= 1:
        return None
    if db.execute("SELECT 1 FROM paper_trades WHERE ticker=?", (ticker,)).fetchone():
        return None
    open_trades = db.execute(
        "SELECT reserved FROM paper_trades WHERE status='OPEN'").fetchall()
    if len(open_trades) >= MAX_OPEN_TRADES:
        return None
    committed = sum((Decimal(row[0]) for row in open_trades), Decimal(0))
    budget = min(MAX_ENTRY, MAX_CAPITAL - committed)
    if budget <= 0:
        return None
    contracts = int((budget / price).to_integral_value(rounding=ROUND_DOWN))
    if contracts < 1:
        return None
    reserved = price * contracts
    if reserved > MAX_ENTRY or committed + reserved > MAX_CAPITAL:
        return None
    db.execute("""INSERT INTO paper_trades
        (ticker,direction,price,contracts,reserved,created_at,close_at,status)
        VALUES (?,?,?,?,?,?,?,'OPEN')""",
        (ticker, direction, str(price), contracts, str(reserved), now, close_at))
    LOG.info("PAPER ONLY %s %s contracts=%d cost=$%s (no order sent)",
             ticker, direction, contracts, reserved)
    return {"ticker": ticker, "direction": direction,
            "contracts": contracts, "reserved": str(reserved)}


def settle_paper(db, ticker, result):
    if result not in ("yes", "no"):
        return
    init_paper(db)
    row = db.execute("SELECT direction, contracts, reserved FROM paper_trades WHERE ticker=? AND status='OPEN'", (ticker,)).fetchone()
    if row:
        direction, contracts, reserved = row
        won = (direction == "UP" and result == "yes") or (direction == "DOWN" and result == "no")
        gross = Decimal(contracts) if won else Decimal(0)
        pnl = gross - Decimal(reserved)
        db.execute("UPDATE paper_trades SET status=? WHERE ticker=?", ("WON" if won else "LOST", ticker))
        LOG.info("PAPER settled %s result=%s gross_pnl=%s before fees", ticker, result, pnl)
