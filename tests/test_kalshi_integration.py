"""Offline integration checks for Rafa's Kalshi order path.

These tests use a fake HTTP transport and generated keys. They do not contact
Kalshi or submit real/demo orders.
"""
import base64
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kalshi_orders import DEMO, KalshiOrders, purchase_fits
from kalshi_trading import Trading


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class FakeOrders:
    def __init__(self, *, response=None, fail_submit=False):
        self.response = response
        self.fail_submit = fail_submit
        self.submissions = []
        self.positions = {}
        self.resting = set()

    def _request(self, method, endpoint):
        if endpoint.startswith("/portfolio/positions"):
            ticker = endpoint.split("ticker=", 1)[1]
            qty = self.positions.get(ticker, Decimal("0"))
            return {"market_positions": [{"position_fp": str(qty)}], "cursor": ""}
        if endpoint.startswith("/portfolio/orders"):
            ticker = endpoint.split("ticker=", 1)[1].split("&", 1)[0]
            orders = [{"order_id": "resting-1"}] if ticker in self.resting else []
            return {"orders": orders, "cursor": ""}
        raise AssertionError(f"Unexpected request: {method} {endpoint}")

    def submit_ioc(self, **kwargs):
        self.submissions.append(kwargs)
        if self.fail_submit:
            raise TimeoutError("simulated ambiguous submission")
        payload = self.response or {}
        return {
            "order_id": payload.get("order_id", "order-1"),
            "client_order_id": kwargs["client_id"],
            "fill_count": payload.get("fill_count", "1.00"),
            "remaining_count": payload.get("remaining_count", "0.00"),
            "average_fill_price": payload.get("average_fill_price", str(kwargs["limit_price"])),
            "average_fee_paid": payload.get("average_fee_paid", "0.02"),
        }


class KalshiOrderPathIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger = str(Path(self.temp_dir.name) / "orders.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ed25519_signature_and_ioc_payload_use_demo_transport(self):
        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        response = {
            "order_id": "order-demo-1",
            "client_order_id": "intent-1",
            "fill_count": "1.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.40",
            "average_fee_paid": "0.01",
        }
        session = RecordingSession(FakeResponse(201, response))
        client = KalshiOrders(session, "demo-key", pem, production=False)

        result = client.submit_ioc(
            ticker="KXBTC15M-TEST",
            outcome="yes",
            selling=False,
            quantity=Decimal("1"),
            limit_price=Decimal("0.40"),
            client_id="intent-1",
        )

        self.assertEqual(result, response)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, DEMO + "/portfolio/events/orders")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["time_in_force"], "immediate_or_cancel")
        self.assertEqual(kwargs["json"]["client_order_id"], "intent-1")
        timestamp = kwargs["headers"]["KALSHI-ACCESS-TIMESTAMP"]
        signature = base64.b64decode(kwargs["headers"]["KALSHI-ACCESS-SIGNATURE"])
        signed = (timestamp + "POST" + "/trade-api/v2/portfolio/events/orders").encode()
        private_key.public_key().verify(signature, signed)

    def test_purchase_budget_enforces_per_order_and_cumulative_cap(self):
        self.assertTrue(purchase_fits(
            Decimal("2"), Decimal("0.98"), Decimal("0.04"),
            Decimal("0"), Decimal("0"),
        ))
        self.assertFalse(purchase_fits(
            Decimal("2"), Decimal("0.99"), Decimal("0.04"),
            Decimal("0"), Decimal("0"),
        ))
        self.assertTrue(purchase_fits(
            Decimal("1"), Decimal("0.96"), Decimal("0.04"),
            Decimal("19"), Decimal("0"),
        ))
        self.assertFalse(purchase_fits(
            Decimal("1"), Decimal("0.97"), Decimal("0.04"),
            Decimal("19"), Decimal("0"),
        ))

    def test_one_filled_entry_is_ledgered_and_blocks_a_second_position(self):
        orders = FakeOrders()
        trading = Trading(orders, self.ledger)

        trading.open({"ticker": "KXBTC15M-FIRST"}, "yes", Decimal("0.50"), True)
        trading.open({"ticker": "KXBTC15M-SECOND"}, "no", Decimal("0.40"), True)

        self.assertEqual(len(orders.submissions), 1)
        with trading.connect() as db:
            row = db.execute(
                "SELECT ticker, quantity, cost, settled_day, pending_id FROM trades"
            ).fetchone()
        self.assertEqual(row["ticker"], "KXBTC15M-FIRST")
        self.assertEqual(Decimal(row["quantity"]), Decimal("1"))
        self.assertEqual(Decimal(row["cost"]), Decimal("0.52"))
        self.assertIsNone(row["settled_day"])
        self.assertIsNone(row["pending_id"])

    def test_uncertain_submit_persists_intent_and_blocks_after_restart(self):
        orders = FakeOrders(fail_submit=True)
        trading = Trading(orders, self.ledger)

        with self.assertRaises(TimeoutError):
            trading.open({"ticker": "KXBTC15M-UNCERTAIN"}, "yes", Decimal("0.50"), True)

        restarted = Trading(orders, self.ledger)
        restarted.open({"ticker": "KXBTC15M-SECOND"}, "yes", Decimal("0.45"), True)

        self.assertEqual(len(orders.submissions), 1)
        with restarted.connect() as db:
            row = db.execute(
                "SELECT pending_id, settled_day FROM trades WHERE ticker=?",
                ("KXBTC15M-UNCERTAIN",),
            ).fetchone()
        self.assertIsNotNone(row["pending_id"])
        self.assertIsNone(row["settled_day"])

    def test_take_profit_sale_closes_the_ledgered_position(self):
        orders = FakeOrders()
        trading = Trading(orders, self.ledger)
        ticker = "KXBTC15M-EXIT"

        trading.open({"ticker": ticker}, "yes", Decimal("0.50"), True)
        orders.positions[ticker] = Decimal("1")
        trading.maintain(
            {ticker: {"ticker": ticker, "status": "open", "yes_bid_dollars": "0.90"}},
            lambda _: None,
            lambda _: True,
        )

        self.assertEqual(len(orders.submissions), 2)
        self.assertTrue(orders.submissions[1]["selling"])
        with trading.connect() as db:
            row = db.execute(
                "SELECT quantity, settled_day, pending_id FROM trades WHERE ticker=?",
                (ticker,),
            ).fetchone()
        self.assertEqual(Decimal(row["quantity"]), Decimal("0"))
        self.assertIsNotNone(row["settled_day"])
        self.assertIsNone(row["pending_id"])

    def test_position_mismatch_blocks_a_sale(self):
        orders = FakeOrders()
        trading = Trading(orders, self.ledger)
        ticker = "KXBTC15M-MISMATCH"

        trading.open({"ticker": ticker}, "yes", Decimal("0.50"), True)
        # The exchange reports no position while Rafa's ledger says one contract.
        orders.positions[ticker] = Decimal("0")
        trading.maintain(
            {ticker: {"ticker": ticker, "status": "open", "yes_bid_dollars": "0.90"}},
            lambda _: None,
            lambda _: True,
        )

        self.assertEqual(len(orders.submissions), 1)

    def test_resting_order_blocks_a_new_entry(self):
        orders = FakeOrders()
        trading = Trading(orders, self.ledger)
        ticker = "KXBTC15M-RESTING"
        orders.resting.add(ticker)

        trading.open({"ticker": ticker}, "yes", Decimal("0.50"), True)

        self.assertEqual(orders.submissions, [])
        with trading.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(count, 0)

    def test_unverified_fee_schedule_never_submits(self):
        orders = FakeOrders()
        trading = Trading(orders, self.ledger)

        trading.open({"ticker": "KXBTC15M-NO-FEE"}, "yes", Decimal("0.50"), False)

        self.assertEqual(orders.submissions, [])


if __name__ == "__main__":
    unittest.main()
