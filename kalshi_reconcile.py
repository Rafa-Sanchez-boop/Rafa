"""Read-only Kalshi portfolio reconciliation. Never submits orders.

API request signatures are bound to the HTTP method and URL path.
Fail closed on missing credentials, unexpected responses, or pagination errors.
"""
import base64
import logging
import os
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization

LOG = logging.getLogger("kalshi-reconcile")
BASE = "https://external-api.kalshi.com"
KEY_FILE = "/etc/secrets/kalshi_private.pem"


class ReconciliationError(RuntimeError):
    pass


class ReadOnlyKalshi:
    def __init__(self, key_id=None, key_file=KEY_FILE, session=None):
        self.key_id = key_id if key_id is not None else os.getenv("KALSHI_API_KEY_ID", "").strip()
        if not self.key_id:
            raise ReconciliationError("Missing Kalshi key identifier")
        try:
            self.private_key = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise ReconciliationError("Invalid Kalshi secret file") from exc
        if self.private_key.__class__.__name__ != "Ed25519PrivateKey":
            raise ReconciliationError("Expected Ed25519 private key")
        self.session = session or requests.Session()

    def get(self, path, params=None):
        if not path.startswith("/trade-api/v2/portfolio/"):
            raise ReconciliationError("Read-only portfolio paths only")
        stamp = str(int(time.time() * 1000))
        signature = base64.b64encode(
            self.private_key.sign((stamp + "GET" + path).encode("utf-8"))
        ).decode("ascii")
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }
        try:
            response = self.session.get(BASE + path, params=params, headers=headers, timeout=12)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ReconciliationError("Portfolio read failed") from exc
        if not isinstance(payload, dict):
            raise ReconciliationError("Unexpected portfolio response")
        return payload

    def snapshot(self):
        balance = self.get("/trade-api/v2/portfolio/balance")
        if not isinstance(balance.get("balance"), int):
            raise ReconciliationError("Invalid balance response")
        orders = self._collect("/trade-api/v2/portfolio/orders", "orders")
        positions = self._collect("/trade-api/v2/portfolio/positions", "market_positions")
        # Block any future automated entry when there is unrecognized exposure.
        # Fail closed on unknown statuses or missing position quantities.
        closed_statuses = {"canceled", "cancelled", "executed"}
        active_orders = []
        for order in orders:
            if not isinstance(order, dict) or not isinstance(order.get("status"), str):
                raise ReconciliationError("Unrecognized order record")
            if order["status"].lower() not in closed_statuses:
                active_orders.append(order)
        from decimal import Decimal, InvalidOperation
        active_positions = []
        for position in positions:
            if not isinstance(position, dict):
                raise ReconciliationError("Unrecognized position record")
            value = position.get("position_fp", position.get("position"))
            if value is None:
                raise ReconciliationError("Position quantity missing")
            try:
                quantity = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ReconciliationError("Invalid position quantity") from exc
            if not quantity.is_finite():
                raise ReconciliationError("Nonfinite position quantity")
            if quantity != 0:
                active_positions.append(position)
        return {"balance_cents": balance["balance"],
                "orders_total": len(orders),
                "active_orders": len(active_orders),
                "active_positions": len(active_positions),
                "safe_for_new_entry": not active_orders and not active_positions}

    def _collect(self, path, key):
        cursor = None
        seen = set()
        rows = []
        for _ in range(30):
            payload = self.get(path, {"limit": 200, **({"cursor": cursor} if cursor else {})})
            page = payload.get(key)
            if not isinstance(page, list):
                raise ReconciliationError("Unexpected portfolio page")
            rows.extend(page)
            cursor = payload.get("cursor") or None
            if not cursor:
                return rows
            if cursor in seen:
                raise ReconciliationError("Repeated portfolio cursor")
            seen.add(cursor)
        raise ReconciliationError("Portfolio pagination limit exceeded")


def startup_reconciliation():
    try:
        snap = ReadOnlyKalshi().snapshot()
        LOG.info("READ ONLY portfolio: balance query OK, orders=%d active_orders=%d positions=%d; trading disabled",
                 snap["orders_total"], snap["active_orders"], snap["active_positions"])
        if not snap["safe_for_new_entry"]:
            LOG.warning("Unrecognized orders/positions exist; live entry must remain blocked")
        return snap
    except (ReconciliationError, ValueError, TypeError) as exc:
        LOG.error("Portfolio reconciliation blocked: %s; trading disabled", type(exc).__name__)
        return None
