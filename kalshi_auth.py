"""Read-only Kalshi authentication check. Never submits orders."""
import base64
import logging
import os
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization

LOG = logging.getLogger("kalshi-auth")
BASE = "https://external-api.kalshi.com"
KEY_PATH = "/etc/secrets/kalshi_private.pem"


def check_private_api():
    key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    if not key_id:
        LOG.warning("Kalshi private API: KALSHI_API_KEY_ID missing; trading disabled")
        return False
    path = Path(KEY_PATH)
    if not path.is_file():
        LOG.warning("Kalshi private API: secret file missing; trading disabled")
        return False
    try:
        private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if private_key.__class__.__name__ != "Ed25519PrivateKey":
            LOG.error("Kalshi private API: expected Ed25519 key; trading disabled")
            return False
        timestamp = str(int(time.time() * 1000))
        route = "/trade-api/v2/portfolio/balance"
        signature = base64.b64encode(private_key.sign((timestamp + "GET" + route).encode())).decode()
        response = requests.get(BASE + route, headers={
            "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }, timeout=12)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "balance" not in data:
            LOG.error("Kalshi private API: unexpected balance response; trading disabled")
            return False
        LOG.info("Kalshi private API: authenticated read-only balance check OK; trading disabled")
        return True
    except Exception as exc:
        LOG.error("Kalshi private API: authentication check failed (%s); trading disabled", type(exc).__name__)
        return False
