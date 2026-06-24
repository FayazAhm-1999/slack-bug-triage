"""Tests for Slack request signature verification."""

import hashlib
import hmac
import time

from fastapi.testclient import TestClient

from app.main import app, verify_slack_signature

_SECRET = "test_signing_secret"


def _make_signed_request(body: bytes, secret: str = _SECRET, timestamp: int | None = None):
    """Return headers with a valid Slack signature."""
    ts = str(timestamp if timestamp is not None else int(time.time()))
    sig_basestring = f"v0:{ts}:{body.decode()}"
    mac = hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256)
    signature = f"v0={mac.hexdigest()}"
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": signature}


def test_valid_signature_accepted():
    client = TestClient(app, raise_server_exceptions=False)
    body = b'{"type": "url_verification", "challenge": "abc123"}'
    headers = _make_signed_request(body)
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200


def test_missing_signature_rejected():
    client = TestClient(app, raise_server_exceptions=False)
    body = b'{"type": "url_verification", "challenge": "abc123"}'
    resp = client.post("/slack/events", content=body)
    assert resp.status_code == 403


def test_wrong_secret_rejected():
    client = TestClient(app, raise_server_exceptions=False)
    body = b'{"type": "url_verification", "challenge": "abc123"}'
    headers = _make_signed_request(body, secret="wrong_secret")
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 403


def test_stale_timestamp_rejected():
    client = TestClient(app, raise_server_exceptions=False)
    body = b'{"type": "url_verification", "challenge": "abc123"}'
    # Use a fixed stale timestamp well outside the 5-minute window
    stale_ts = 1_000_000_000  # year 2001 — always stale
    headers = _make_signed_request(body, timestamp=stale_ts)
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 403


def test_url_verification_returns_challenge():
    client = TestClient(app, raise_server_exceptions=False)
    body = b'{"type": "url_verification", "challenge": "my_challenge_token"}'
    headers = _make_signed_request(body)
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["challenge"] == "my_challenge_token"


def test_retry_header_short_circuits():
    """Slack retries should be acknowledged immediately without processing."""
    client = TestClient(app, raise_server_exceptions=False)
    body = b'{"type": "event_callback", "event": {"type": "message"}}'
    headers = {**_make_signed_request(body), "X-Slack-Retry-Num": "1"}
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
