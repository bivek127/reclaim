"""Sends one correctly-signed webhook to the running API, for a live demo.

Simulates a real `subscription.charge.failed` delivery: same JSON shape, same
HMAC-SHA256 signature scheme Razorpay uses, computed with the webhook secret
already configured in `.env`. The running system cannot tell this apart from
a real delivery, which is the point — it exercises the actual ingestion,
diagnosis, policy, and dispatch code paths, live, with no other setup.

Usage:
    python3 scripts/demo_webhook.py
    python3 scripts/demo_webhook.py --api http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request


def load_webhook_secret(env_path: str = ".env") -> str:
    if os.environ.get("RAZORPAY_WEBHOOK_SECRET"):
        return os.environ["RAZORPAY_WEBHOOK_SECRET"]
    with open(env_path) as f:
        for line in f:
            if line.startswith("RAZORPAY_WEBHOOK_SECRET="):
                return line.strip().split("=", 1)[1]
    raise SystemExit("RAZORPAY_WEBHOOK_SECRET not found in the environment or .env")


def build_payload(*, subscription_id: str, amount_minor: int, customer_ref: str) -> bytes:
    now = int(time.time())
    return json.dumps({
        "event": "subscription.charge.failed",
        "payload": {
            "subscription": {"entity": {"id": subscription_id, "current_start": now}},
            "payment": {"entity": {
                "id": f"pay_demo_{now}",
                "amount": amount_minor,
                "currency": "INR",
                "customer_id": customer_ref,
            }},
        },
    }, separators=(",", ":")).encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--subscription-id", default="sub_TWR5kgSCc9ZG89")
    ap.add_argument("--amount-minor", type=int, default=50_000)
    ap.add_argument("--customer-ref", default="cust_demo_video")
    args = ap.parse_args()

    secret = load_webhook_secret()
    body = build_payload(
        subscription_id=args.subscription_id,
        amount_minor=args.amount_minor,
        customer_ref=args.customer_ref,
    )
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        f"{args.api}/api/webhooks/razorpay",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": f"evt_demo_{int(time.time())}",
        },
    )
    try:
        resp = urllib.request.urlopen(req)
        print(f"webhook accepted: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"webhook rejected: HTTP {e.code} {e.read().decode()}")
        return 1

    print("watch the console: a new case should appear within a few seconds "
          "and move NEW -> ENRICHING -> DIAGNOSING -> POLICY_EVAL -> "
          "ACTION_READY -> EXECUTING -> AWAITING_CUSTOMER on its own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
