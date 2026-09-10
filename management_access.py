"""Single-maintainer authentication; independent of detail-link authorization.

Compatibility mode requires separate deployment protection. Removing both
settings can restore that mode. Logout cannot revoke copied signed cookies;
credential rotation invalidates them after the new process configuration loads.
"""

import hashlib
import hmac
import os
import time

from flask import abort, current_app, request, session

from web_security import issue_csrf_token


MANAGEMENT_TTL_SECONDS = 1800
TOKEN_MAX_LENGTH = 512
SESSION_KEY = "_management_auth"

# Methods are explicit: safe HTTP methods do not imply public authorization.
PUBLIC_METHODS = {
    "index": frozenset({"GET", "HEAD", "OPTIONS"}),
    "favicon": frozenset({"GET", "HEAD", "OPTIONS"}),
    "price_hint": frozenset({"GET", "HEAD", "OPTIONS"}),
    "defaults_preview": frozenset({"POST", "OPTIONS"}),
    "static": frozenset({"GET", "HEAD", "OPTIONS"}),
    "detail": frozenset({"GET", "HEAD", "OPTIONS"}),
    "unlock": frozenset({"GET", "HEAD", "POST", "OPTIONS"}),
    "lock": frozenset({"POST", "OPTIONS"}),
}


def management_token() -> str:
    return str(os.environ.get("MANAGEMENT_TOKEN") or "").strip()


def management_auth_required() -> bool:
    value = str(os.environ.get("MANAGEMENT_AUTH_REQUIRED") or "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise ValueError("MANAGEMENT_AUTH_REQUIRED must be an explicit boolean")


def _valid_token(value) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= TOKEN_MAX_LENGTH
        and value.isascii()
        and all(33 <= ord(char) <= 126 for char in value)
    )


def management_token_authorized(candidate) -> bool:
    expected = management_token()
    if not _valid_token(expected) or not _valid_token(candidate):
        return False
    return hmac.compare_digest(candidate, expected)


def token_generation() -> str:
    token = management_token()
    if not _valid_token(token):
        return ""
    return hashlib.sha256(("management-session-v1:" + token).encode("ascii")).hexdigest()


def _clock_epoch() -> int:
    clock = current_app.config.get("MANAGEMENT_CLOCK")
    return int(clock() if callable(clock) else time.time())


def management_session_authorized() -> bool:
    generation = token_generation()
    record = session.get(SESSION_KEY)
    if not generation or not isinstance(record, dict):
        return False
    if set(record) != {"issued_at", "generation"}:
        return False
    issued = record["issued_at"]
    stored_generation = record["generation"]
    if type(issued) is not int or not isinstance(stored_generation, str):
        return False
    if len(stored_generation) != 64 or any(char not in "0123456789abcdef" for char in stored_generation):
        return False
    return (
        0 <= _clock_epoch() - issued < MANAGEMENT_TTL_SECONDS
        and hmac.compare_digest(stored_generation, generation)
    )


def establish_management_session() -> None:
    generation = token_generation()
    if not generation:
        abort(404)
    session.clear()
    session[SESSION_KEY] = {"issued_at": _clock_epoch(), "generation": generation}
    issue_csrf_token()


def clear_management_session() -> None:
    session.pop(SESSION_KEY, None)
    session.pop("_csrf_nonce", None)


def install_management_access(app) -> None:
    # Configuration errors are explicit at startup, never interpreted as off.
    management_auth_required()

    @app.before_request
    def _enforce_management_access():
        required = management_auth_required()
        protected = required or bool(management_token())
        if not protected or request.endpoint is None:
            return None
        if request.method in PUBLIC_METHODS.get(request.endpoint, frozenset()):
            return None
        # The legacy delete view treats HEAD as its write branch, not as GET.
        if request.endpoint == "delete_subscription" and request.method == "HEAD":
            abort(404)
        if not management_session_authorized():
            abort(404)
        return None

    @app.after_request
    def _authentication_no_store(response):
        if request.endpoint in {"unlock", "lock"}:
            response.headers["Cache-Control"] = "no-store"
        return response
