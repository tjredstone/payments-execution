import ipaddress
import logging
import os

import requests
from flask import Flask, redirect, request
from dotenv import load_dotenv

from config import load_truelayer_config
from db import (
    consume_oauth_state,
    create_oauth_state,
    ensure_token_crypto_ready,
    init_db,
    migrate_plaintext_tokens,
    purge_expired_oauth_states,
    save_connection_tokens,
)
from truelayer_client import TrueLayerClient

load_dotenv()
init_db()
ensure_token_crypto_ready()
migrated = migrate_plaintext_tokens()
if migrated:
    logging.getLogger("advisory").warning("Migrated %s plaintext token row(s) to encrypted storage.", migrated)
purge_expired_oauth_states()

app = Flask(__name__)
LOCAL_USER_ID = os.getenv("LOCAL_USER_ID", "local-dev-user")
ALLOW_REMOTE = os.getenv("ALLOW_REMOTE_ACCESS", "0") == "1"
logger = logging.getLogger("advisory")
logging.basicConfig(level=logging.INFO)

tl_cfg = load_truelayer_config()
tl = TrueLayerClient(tl_cfg)


def _is_loopback_ip(addr: str | None) -> bool:
    if not addr:
        return False
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


@app.before_request
def enforce_localhost_only():
    if ALLOW_REMOTE:
        return None
    if not _is_loopback_ip(request.remote_addr):
        logger.warning("Rejected non-local request from %s", request.remote_addr)
        return "Forbidden", 403
    return None


@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def home():
    return """
    <h1>Rent Advisory (Stage 1)</h1>
    <p>This module is read-only (Open Banking AIS).</p>
    <p><a href="/connect">Connect bank via TrueLayer</a></p>
    """


@app.get("/connect")
def connect():
    scopes = [
        "info",
        "accounts",
        "balance",
        "transactions",
        "direct_debits",
        "standing_orders",
        "offline_access",
    ]
    providers_raw = os.getenv("TRUELAYER_PROVIDERS", "uk-cs-mock")
    providers = [item.strip() for item in providers_raw.split() if item.strip()]
    state = create_oauth_state(user_id=LOCAL_USER_ID)
    auth_link = tl.generate_auth_link(scopes=scopes, state=state, providers=providers)
    logger.info("Starting TrueLayer connect flow for local user.")
    return redirect(auth_link)


@app.get("/truelayer/callback")
def callback():
    error = request.args.get("error")
    if error:
        description = request.args.get("error_description", "No description provided.")
        return f"Bank auth failed: {error} ({description})", 400

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return "Missing code or state parameter in callback URL.", 400

    if not consume_oauth_state(user_id=LOCAL_USER_ID, state=state):
        return "Invalid or expired OAuth state.", 400

    try:
        tokens = tl.exchange_code_for_tokens(code)
    except requests.HTTPError as exc:
        logger.exception("Token exchange failed with provider response.")
        return "Token exchange failed with provider.", 502

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token or not refresh_token:
        logger.error("Token response missing required fields. Keys: %s", sorted(tokens.keys()))
        return "Unexpected token response from provider.", 500

    try:
        save_connection_tokens(
            user_id=LOCAL_USER_ID,
            provider="truelayer",
            tokens=tokens,
        )
    except ValueError:
        logger.exception("Rejected malformed token payload.")
        return "Could not persist provider tokens.", 500

    return """
    <h2>Bank connected ✅</h2>
    <p>Tokens saved locally.</p>
    <p>Next: run <code>python run_daily.py</code></p>
    """


if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        host="127.0.0.1",
        port=5001,
    )
