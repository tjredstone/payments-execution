import ipaddress
import logging
import os
import re
import secrets
import time
from datetime import date, datetime
from functools import wraps

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

from config import load_truelayer_config
from db import (
    authenticate_auth_user,
    consume_oauth_state,
    create_auth_user,
    create_oauth_state,
    ensure_token_crypto_ready,
    get_auth_user_by_id,
    has_connection,
    init_db,
    migrate_plaintext_tokens,
    purge_expired_oauth_states,
    save_connection_tokens,
)
from engine import run_engine
from secret_store import read_secret
from truelayer_client import TrueLayerClient


class SensitiveDataFilter(logging.Filter):
    _PATTERNS = [
        # Key/value style fields
        re.compile(
            r"(?i)\b(access_token|refresh_token|client_secret|authorization_code|auth_code|id_token)\b"
            r"(\s*[=:]\s*)([^\s,;&]+)"
        ),
        # URL query parameters
        re.compile(r"(?i)([?&](?:access_token|refresh_token|client_secret|code)=)([^&\s]+)"),
        # Bearer tokens
        re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)([A-Za-z0-9\-._~+/]+=*)"),
    ]

    @classmethod
    def _redact_text(cls, text: str) -> str:
        redacted = text
        for pattern in cls._PATTERNS:
            redacted = pattern.sub(lambda m: f"{m.group(1)}{m.group(2) if m.lastindex and m.lastindex > 2 else ''}[REDACTED]", redacted)  # type: ignore[index]
        return redacted

    @classmethod
    def _redact_obj(cls, value):
        if isinstance(value, str):
            return cls._redact_text(value)
        if isinstance(value, dict):
            return {k: cls._redact_obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            items = [cls._redact_obj(v) for v in value]
            return tuple(items) if isinstance(value, tuple) else items
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact_obj(record.msg)
        if record.args:
            record.args = self._redact_obj(record.args)
        if record.exc_info and record.exc_info[1]:
            exc = record.exc_info[1]
            exc.args = tuple(self._redact_obj(arg) for arg in exc.args)
        return True


def _setup_secure_logging() -> None:
    logging.basicConfig(level=logging.INFO)
    redaction_filter = SensitiveDataFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(redaction_filter)
    for handler in root_logger.handlers:
        handler.addFilter(redaction_filter)


_setup_secure_logging()

load_dotenv()
init_db()
ensure_token_crypto_ready()
migrated = migrate_plaintext_tokens()
if migrated:
    logging.getLogger("advisory").warning("Migrated %s plaintext token row(s) to encrypted storage.", migrated)
purge_expired_oauth_states()

app = Flask(__name__)
ALLOW_REMOTE = os.getenv("ALLOW_REMOTE_ACCESS", "0") == "1"
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
ALLOW_SELF_REGISTRATION = os.getenv("ALLOW_SELF_REGISTRATION", "0") == "1"
ENFORCE_HTTPS = os.getenv("ENFORCE_HTTPS", "0") == "1"
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
logger = logging.getLogger("advisory")
app.secret_key = read_secret("APP_SESSION_SECRET")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = ENFORCE_HTTPS or (os.getenv("SESSION_COOKIE_SECURE", "0") == "1")

if TRUST_PROXY_HEADERS:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore[assignment]

tl_cfg = load_truelayer_config()
tl = TrueLayerClient(tl_cfg)

if ALLOW_REMOTE and FLASK_DEBUG:
    raise RuntimeError("Refusing to run with both ALLOW_REMOTE_ACCESS=1 and FLASK_DEBUG=1.")


_DASHBOARD_CACHE: dict[str, object] = {
    "key": None,
    "expires_at": 0.0,
    "payload": None,
}
_LOGIN_LIMIT_STATE: dict[str, dict[str, float]] = {}

LOGIN_MAX_ATTEMPTS = max(1, int(os.getenv("LOGIN_MAX_ATTEMPTS", "5")))
LOGIN_WINDOW_SECONDS = max(60, int(os.getenv("LOGIN_WINDOW_SECONDS", "600")))
LOGIN_LOCKOUT_SECONDS = max(60, int(os.getenv("LOGIN_LOCKOUT_SECONDS", "900")))


def _currency_symbol(currency: str | None) -> str:
    code = (currency or "").upper()
    return {
        "GBP": "£",
        "USD": "$",
        "EUR": "€",
        "JPY": "¥",
        "AUD": "A$",
        "CAD": "C$",
    }.get(code, f"{code} " if code else "")


@app.template_filter("money")
def money_filter(amount: float | int | None, currency: str | None = "GBP") -> str:
    symbol = _currency_symbol(currency)
    value = float(amount or 0)
    return f"{symbol}{value:,.2f}"


@app.template_filter("local_date")
def local_date_filter(value: str | None) -> str:
    if not value:
        return "-"
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.strftime("%d/%m/%Y")
        d = date.fromisoformat(value)
        return d.strftime("%d/%m/%Y")
    except ValueError:
        return value


def _is_loopback_ip(addr: str | None) -> bool:
    if not addr:
        return False
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return str(token)


@app.context_processor
def inject_csrf_token() -> dict[str, object]:
    return {"csrf_token": _csrf_token}


def _client_ip() -> str:
    return (request.remote_addr or "unknown").strip()


def _login_rate_limit_key(email: str) -> str:
    return f"{_client_ip()}::{email.strip().lower()}"


def _is_login_rate_limited(key: str) -> tuple[bool, int]:
    now = time.time()
    state = _LOGIN_LIMIT_STATE.get(key)
    if not state:
        return False, 0

    lock_until = state.get("lock_until", 0.0)
    if lock_until > now:
        return True, int(lock_until - now)

    first_attempt = state.get("first_attempt", now)
    if now - first_attempt > LOGIN_WINDOW_SECONDS:
        _LOGIN_LIMIT_STATE.pop(key, None)
        return False, 0
    return False, 0


def _record_failed_login(key: str) -> None:
    now = time.time()
    state = _LOGIN_LIMIT_STATE.get(key)
    if not state or (now - state.get("first_attempt", now) > LOGIN_WINDOW_SECONDS):
        state = {"first_attempt": now, "failed_attempts": 0.0, "lock_until": 0.0}
    state["failed_attempts"] = float(state.get("failed_attempts", 0.0)) + 1.0
    if state["failed_attempts"] >= float(LOGIN_MAX_ATTEMPTS):
        state["lock_until"] = now + float(LOGIN_LOCKOUT_SECONDS)
    _LOGIN_LIMIT_STATE[key] = state


def _clear_failed_login(key: str) -> None:
    _LOGIN_LIMIT_STATE.pop(key, None)


def _current_user() -> dict[str, str] | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = get_auth_user_by_id(str(user_id))
    if not user:
        session.clear()
        return None
    return {"user_id": str(user["user_id"]), "email": str(user["email"])}


def _login_required(handler):
    @wraps(handler)
    def wrapper(*args, **kwargs):
        if not _current_user():
            return redirect(url_for("login", next=request.path))
        return handler(*args, **kwargs)

    return wrapper


@app.before_request
def enforce_localhost_only():
    if ENFORCE_HTTPS and not request.is_secure:
        if request.method == "GET":
            secure_url = request.url.replace("http://", "https://", 1)
            return redirect(secure_url, code=301)
        return "HTTPS required.", 403

    if ALLOW_REMOTE:
        pass
    elif not _is_loopback_ip(request.remote_addr):
        logger.warning("Rejected non-local request from %s", request.remote_addr)
        return "Forbidden", 403

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf_token")
        provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not provided or provided != expected:
            return "CSRF validation failed.", 400

    return None


@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if _current_user():
        return redirect(url_for("home"))

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        rl_key = _login_rate_limit_key(email)
        blocked, retry_after = _is_login_rate_limited(rl_key)
        if blocked:
            error = f"Too many attempts. Try again in {retry_after} seconds."
            return render_template(
                "auth.html",
                mode="login",
                error=error,
                allow_self_registration=ALLOW_SELF_REGISTRATION,
            )

        user = authenticate_auth_user(email=email, password=password)
        if not user:
            _record_failed_login(rl_key)
            error = "Invalid email or password."
        else:
            _clear_failed_login(rl_key)
            session.clear()
            session["_csrf_token"] = secrets.token_urlsafe(32)
            session["user_id"] = user["user_id"]
            session["email"] = user["email"]
            next_path = request.args.get("next", "/")
            return redirect(next_path if next_path.startswith("/") else "/")
    return render_template(
        "auth.html",
        mode="login",
        error=error,
        allow_self_registration=ALLOW_SELF_REGISTRATION,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if not ALLOW_SELF_REGISTRATION:
        return "Registration is disabled.", 403
    if _current_user():
        return redirect(url_for("home"))

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        try:
            user_id = create_auth_user(email=email, password=password)
            session.clear()
            session["_csrf_token"] = secrets.token_urlsafe(32)
            session["user_id"] = user_id
            session["email"] = email.lower()
            return redirect(url_for("home"))
        except ValueError as exc:
            error = str(exc)
    return render_template("auth.html", mode="register", error=error, allow_self_registration=ALLOW_SELF_REGISTRATION)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@_login_required
def home():
    user = _current_user()
    if not user:
        return redirect(url_for("login"))
    user_id = user["user_id"]
    provider = "truelayer"
    connected = has_connection(user_id=user_id, provider=provider)

    return render_template(
        "dashboard.html",
        connected=connected,
        user_email=user["email"],
    )


def _dashboard_cached(user_id: str, provider: str) -> dict:
    lookback_days = int(os.getenv("UI_NORMALISE_LOOKBACK_DAYS", os.getenv("NORMALISE_LOOKBACK_DAYS", "90")))
    advisory_window_days = int(os.getenv("ADVISORY_WINDOW_DAYS", "14"))
    cache_ttl_seconds = int(os.getenv("DASHBOARD_CACHE_TTL_SECONDS", "45"))
    now = time.time()
    cache_key = f"{user_id}:{provider}:{lookback_days}:{advisory_window_days}"

    if (
        _DASHBOARD_CACHE["payload"] is not None
        and _DASHBOARD_CACHE["key"] == cache_key
        and float(_DASHBOARD_CACHE["expires_at"]) > now
    ):
        return _DASHBOARD_CACHE["payload"]  # type: ignore[return-value]

    payload = run_engine(
        user_id=user_id,
        provider=provider,
        normalise_lookback_days=lookback_days,
        advisory_window_days=advisory_window_days,
    )
    _DASHBOARD_CACHE["key"] = cache_key
    _DASHBOARD_CACHE["payload"] = payload
    _DASHBOARD_CACHE["expires_at"] = now + max(5, cache_ttl_seconds)
    return payload


@app.get("/api/dashboard")
def api_dashboard():
    user = _current_user()
    if not user:
        return jsonify({"connected": False, "error": "Authentication required."}), 401
    user_id = user["user_id"]
    provider = "truelayer"
    if not has_connection(user_id=user_id, provider=provider):
        return jsonify({"connected": False, "dashboard": None}), 200
    try:
        payload = _dashboard_cached(user_id=user_id, provider=provider)
        return jsonify({"connected": True, "dashboard": payload}), 200
    except ValueError as exc:
        return jsonify({"connected": True, "error": str(exc)}), 400


@app.get("/connect")
@_login_required
def connect():
    user = _current_user()
    if not user:
        return redirect(url_for("login"))
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
    state = create_oauth_state(user_id=user["user_id"])
    auth_link = tl.generate_auth_link(scopes=scopes, state=state, providers=providers)
    logger.info("Starting TrueLayer connect flow for user=%s.", user["user_id"])
    return redirect(auth_link)


@app.get("/truelayer/callback")
@_login_required
def callback():
    user = _current_user()
    if not user:
        return redirect(url_for("login"))
    error = request.args.get("error")
    if error:
        description = request.args.get("error_description", "No description provided.")
        return f"Bank auth failed: {error} ({description})", 400

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return "Missing code or state parameter in callback URL.", 400

    if not consume_oauth_state(user_id=user["user_id"], state=state):
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
            user_id=user["user_id"],
            provider="truelayer",
            tokens=tokens,
        )
    except ValueError:
        logger.exception("Rejected malformed token payload.")
        return "Could not persist provider tokens.", 500

    return redirect("/")


if __name__ == "__main__":
    app.run(
        debug=FLASK_DEBUG,
        host="127.0.0.1",
        port=5001,
    )
