"""Kalshi event-order client and Rafa's limits, for integration testing."""

import base64
import time
from decimal import Decimal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


MAX_PURCHASE = Decimal("2.00")
TRADING_BANKROLL = Decimal("20.00")
PROFIT_MULTIPLIER = Decimal("1.50")
PRODUCTION = "https://external-api.kalshi.com/trade-api/v2"
DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"


def purchase_fits(quantity, limit_price, maximum_fee, realized_losses, open_risk):
    """Reserve the entire order's worst-case cost before submission."""
    values = (quantity, limit_price, maximum_fee, realized_losses, open_risk)
    if any(not value.is_finite() for value in values):
        return False
    if quantity <= 0 or not (0 < limit_price < 1):
        return False
    if min(maximum_fee, realized_losses, open_risk) < 0:
        return False
    order_cost = quantity * limit_price + maximum_fee
    return (
        order_cost <= MAX_PURCHASE
        and realized_losses + open_risk + order_cost <= TRADING_BANKROLL
    )


def reaches_profit_target(entry_cost, sale_proceeds, maximum_sale_fee):
    """Compare the proceeds for the quantity being closed, after fees."""
    values = (entry_cost, sale_proceeds, maximum_sale_fee)
    if any(not value.is_finite() for value in values):
        return False
    if entry_cost <= 0 or min(sale_proceeds, maximum_sale_fee) < 0:
        return False
    return sale_proceeds - maximum_sale_fee >= entry_cost * PROFIT_MULTIPLIER


class KalshiOrders:
    def __init__(self, session, key_id, private_key_pem, *, production):
        if not key_id or not private_key_pem:
            raise ValueError("Kalshi API credentials are missing")
        self.session = session
        self.key_id = key_id
        self.private_key = serialization.load_pem_private_key(
            private_key_pem, password=None
        )
        if not isinstance(self.private_key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
            raise ValueError("Unsupported Kalshi API key type")
        self.base_url = PRODUCTION if production else DEMO

    def _request(self, method, endpoint, payload=None):
        url = self.base_url + endpoint
        timestamp = str(time.time_ns() // 1_000_000)
        message = (timestamp + method + urlsplit(url).path).encode()
        if isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
            digest = hashes.SHA256()
            signature = self.private_key.sign(
                message, padding.PSS(mgf=padding.MGF1(digest), salt_length=32), digest
            )
        response = self.session.request(
            method,
            url,
            headers={
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            },
            json=payload,
            timeout=10,
            allow_redirects=False,
        )
        expected_status = 201 if method == "POST" else 200
        if response.status_code != expected_status:
            raise RuntimeError(f"Kalshi {method} {endpoint}: HTTP {response.status_code}")
        return response.json()

    def balance(self):
        return self._request("GET", "/portfolio/balance")

    def submit_ioc(self, *, ticker, outcome, selling, quantity, limit_price, client_id):
        """Submit once. The caller must persist the intent and reconcile ambiguity."""
        if outcome not in ("yes", "no") or not client_id:
            raise ValueError("Invalid outcome or absent client order ID")
        if not quantity.is_finite() or quantity <= 0 or quantity != round(quantity, 2):
            raise ValueError("Invalid order quantity")
        if not limit_price.is_finite() or not (0 < limit_price < 1):
            raise ValueError("Invalid limit price")
        if limit_price != round(limit_price, 4):
            raise ValueError("Unsupported limit price precision")

        yes_price = limit_price if outcome == "yes" else 1 - limit_price
        book_side = "ask" if selling == (outcome == "yes") else "bid"
        return self._request("POST", "/portfolio/events/orders", {
            "ticker": ticker,
            "client_order_id": client_id,
            "side": book_side,
            "count": f"{quantity:.2f}",
            "price": f"{yes_price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": selling,
        })
