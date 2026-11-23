"""Connect new alerts to durable, bounded Kalshi order intents."""

import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from kalshi_orders import KalshiOrders, purchase_fits, reaches_profit_target

LOG = logging.getLogger(__name__)
CENTRAL = ZoneInfo("America/Chicago")
ZERO = Decimal(0)
ONE = Decimal(1)
# One contract maximum. Covers the standard taker fee and rounding.
FEE_RESERVE = Decimal("0.04")


def number(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Nonfinite Kalshi amount")
    return amount


class Trading:
    def __init__(self, orders, ledger):
        self.orders = orders
        self.ledger = ledger
        self.exit_quotes = {}

        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    ticker TEXT PRIMARY KEY, outcome TEXT NOT NULL,
                    quantity TEXT NOT NULL, cost TEXT NOT NULL,
                    loss TEXT NOT NULL DEFAULT '0', settled_day TEXT,
                    pending_id TEXT
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS reversal_losses (
                    order_id TEXT PRIMARY KEY, trading_day TEXT NOT NULL, loss TEXT NOT NULL
                )
            """)

    @classmethod
    def from_environment(cls, session):
        mode = os.getenv("RAFA_TRADING_MODE", "verify")
        if mode not in {"verify", "live"}:
            return None
        key_id = (os.getenv("KALSHI_API_KEY_ID") or os.getenv("KALSHI_KEY_ID")
                  or os.getenv("KALSHI_API_KEY"))
        pem = os.getenv("KALSHI_PRIVATE_KEY_PEM") or os.getenv("KALSHI_PRIVATE_KEY")
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH") or os.getenv("KALSHI_PRIVATE_KEY_FILE")
        if not pem:
            secret_file = Path(key_path or "/etc/secrets/kalshi_private.pem")
            if key_path or secret_file.is_file():
                pem = secret_file.read_text()
        if not key_id or not pem:
            LOG.error("Kalshi credential inputs: key ID configured=%s; private key configured=%s",
                      bool(key_id), bool(pem))
            return None
        orders = KalshiOrders(
            session, key_id, (pem or "").replace("\\n", "\n").encode(), production=True
        )
        orders.balance()
        LOG.info("Kalshi production authentication verified; persistent mount=%s", os.path.ismount("/var/data"))
        if mode == "verify":
            return None
        if not os.path.ismount("/var/data"):
            raise RuntimeError("Kalshi order ledger requires a persistent mount")
        trading = cls(orders, "/var/data/rafa_orders.sqlite3")
        LOG.info("Rafa live trading enabled; persistent order ledger ready")
        return trading

    def connect(self):
        conn = sqlite3.connect(self.ledger, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def position(self, ticker):
        snapshot = self.orders._request("GET", "/portfolio/positions?ticker=" + ticker)
        if snapshot.get("cursor"):
            raise RuntimeError("Incomplete position snapshot")
        return sum((number(p["position_fp"]) for p in snapshot["market_positions"]), ZERO)

    def no_resting_orders(self, ticker):
        snapshot = self.orders._request(
            "GET", "/portfolio/orders?ticker=" + ticker + "&status=resting"
        )
        return not snapshot["orders"] and not snapshot.get("cursor")

    def open(self, market, outcome, price, fee_verified):
        if not fee_verified:
            LOG.info("Kalshi entry skipped for %s: fee schedule could not be verified", market["ticker"])
            return
        ticker = market["ticker"]
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM trades WHERE pending_id IS NOT NULL OR settled_day IS NULL OR ticker=?",
                (ticker,),
            ).fetchone():
                return
            # Profits never increase the authorized initial bankroll.
            loss = sum((number(row[0]) for row in db.execute(
                "SELECT loss FROM trades WHERE settled_day IS NOT NULL "
                "UNION ALL SELECT loss FROM reversal_losses"
            )), ZERO)
            if not purchase_fits(ONE, price, FEE_RESERVE, loss, ZERO):
                LOG.info("Kalshi entry skipped for %s: cost exceeds available trading budget", ticker)
                return
            if self.position(ticker) or not self.no_resting_orders(ticker):
                return
            intent = uuid4().hex
            db.execute(
                "INSERT INTO trades (ticker, outcome, quantity, cost, pending_id) VALUES (?, ?, '0', '0', ?)",
                (ticker, outcome, intent),
            )
            db.commit()
            self.execute(db, ticker, outcome, ONE, price, intent, selling=False)

    def maintain(self, markets, get_market, fee_verified):
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM trades WHERE pending_id IS NOT NULL").fetchone():
                LOG.warning("Kalshi orders paused pending reconciliation")
                return
            row = db.execute("SELECT * FROM trades WHERE settled_day IS NULL").fetchone()
            if row is None:
                return
            ticker, outcome = row["ticker"], row["outcome"]
            quantity, cost = number(row["quantity"]), number(row["cost"])
            market = markets.get(ticker) or get_market(ticker)
            if not market:
                return
            if market.get("status") in {"settled", "finalized"} and market.get("result") in {"yes", "no"}:
                payout = quantity if market["result"] == outcome else ZERO
                loss = max(ZERO, cost - payout)
                db.execute("UPDATE trades SET loss=?, settled_day=? WHERE ticker=?", (
                    str(loss), datetime.now(CENTRAL).date().isoformat(), ticker,
                ))
                db.commit()
                return
            if ticker not in markets or not fee_verified(market):
                return
            bid = market.get(f"{outcome}_bid_dollars")
            if bid is None:
                return
            price = number(bid)
            if not (FEE_RESERVE < price < ONE):
                return
            now = datetime.now(CENTRAL).timestamp()
            history = self.exit_quotes.get(ticker, [])
            history = [(ts, bid) for ts, bid in history if now - ts <= 120]
            history = (history + [(now, price)])[-3:]
            self.exit_quotes = {ticker: history}
            take_profit = reaches_profit_target(cost, price * quantity, FEE_RESERVE * quantity)
            reversal = len(history) == 3 and history[0][1] > history[1][1] > price
            if not take_profit and not reversal:
                return
            signed_quantity = quantity if outcome == "yes" else -quantity
            if self.position(ticker) != signed_quantity or not self.no_resting_orders(ticker):
                LOG.warning("Kalshi position differs from Rafa's ledger for %s", ticker)
                return
            intent = uuid4().hex
            db.execute("UPDATE trades SET pending_id=? WHERE ticker=?", (intent, ticker))
            db.commit()
            self.execute(db, ticker, outcome, quantity, price, intent, selling=True, reversal=not take_profit)

    def execute(self, db, ticker, outcome, count, limit, intent, *, selling, reversal=False):
        # Commit the intent before sending. An ambiguous or invalid response
        # leaves the ledger blocked, including across process restarts.
        response = self.orders.submit_ioc(
            ticker=ticker, outcome=outcome, selling=selling, quantity=count,
            limit_price=limit, client_id=intent,
        )
        filled = number(response["fill_count"])
        if not response["order_id"] or response.get("client_order_id") != intent:
            raise RuntimeError("Kalshi order identity could not be verified")
        if number(response["remaining_count"]) != ZERO or not (ZERO <= filled <= count):
            raise RuntimeError("Kalshi IOC fill state could not be verified")
        today = datetime.now(CENTRAL).date().isoformat()
        if filled:
            price = number(response["average_fill_price"])
            fee = number(response["average_fee_paid"]) * filled
            if outcome == "no":
                price = ONE - price
            if not (ZERO < price < ONE) or not (ZERO <= fee <= FEE_RESERVE * filled):
                raise RuntimeError("Kalshi order fill economics could not be verified")
            if selling:
                row = db.execute("SELECT cost FROM trades WHERE ticker=?", (ticker,)).fetchone()
                basis = number(row["cost"])
                sold_basis = basis * filled / count
                proceeds = price * filled - fee
                if price < limit or (not reversal and not reaches_profit_target(sold_basis, price * filled, fee)):
                    raise RuntimeError("Kalshi sale did not satisfy its limit")
                if reversal:
                    db.execute("INSERT INTO reversal_losses VALUES (?, ?, ?)", (
                        response["order_id"], today, str(max(ZERO, sold_basis - proceeds)),
                    ))
                    LOG.info("Kalshi reversal exit: %s", ticker)
                remaining = count - filled
                cost = basis * remaining / count
                settled = today if remaining == ZERO else None
            else:
                if price > limit:
                    raise RuntimeError("Kalshi buy was above the submitted limit")
                remaining, cost, settled = filled, filled * price + fee, None
            db.execute(
                "UPDATE trades SET quantity=?, cost=?, settled_day=?, pending_id=NULL WHERE ticker=? AND pending_id=?",
                (str(remaining), str(cost), settled, ticker, intent),
            )
        elif selling:
            db.execute("UPDATE trades SET pending_id=NULL WHERE ticker=? AND pending_id=?", (ticker, intent))
        else:
            db.execute("UPDATE trades SET settled_day=?, pending_id=NULL WHERE ticker=? AND pending_id=?", (today, ticker, intent))
        db.commit()
        LOG.info("Kalshi %s %s: %s filled", "sale" if selling else "purchase", ticker, filled)
