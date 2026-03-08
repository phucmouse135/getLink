"""
core.py
=======
Per-account check logic.  Wraps check_facebook_email.check_email_by_subject()
with structured logging, error classification, and result persistence.

Public API
----------
    result = check_one_account(email_login, password)

    result is a dict:
        { "status": "found" | "not_found" | "error",
          "account": "user@gmx.com",
          "detail":  <str>            # human-readable outcome }
"""

from __future__ import annotations

import imaplib
import traceback
from typing import Any, Dict

import config
from check_facebook_email import check_email_by_subject
from exceptions import (
    AuthenticationError,
    IMAPConnectionError,
    MaxRetriesExceededError,
)
from utils import dump_error_trace, log, write_result


# ═══════════════════════════════════════════════════════════════
# INTERNAL: IMAP call wrapped with retry
# ═══════════════════════════════════════════════════════════════

def _call_checker(email_login: str, password: str) -> Dict[str, Any]:
    """
    Call check_email_by_subject() and surface IMAP-level exceptions as the
    tool's typed exception hierarchy so callers can react without parsing
    raw imaplib error strings.

    Retries are already baked into check_email_by_subject() (via _connect()).
    This function adds an extra classification layer on top.
    """
    try:
        return check_email_by_subject(
            email_login    = email_login,
            password       = password,
            target_subject = config.TARGET_SUBJECT,
        )

    except imaplib.IMAP4.error as exc:
        msg = str(exc).lower()
        if any(h in msg for h in ("authentication", "login", "credentials",
                                   "password", "no such user", "invalid")):
            raise AuthenticationError(str(exc), account=email_login) from exc
        raise IMAPConnectionError(str(exc), account=email_login) from exc

    except (OSError, TimeoutError, ConnectionError) as exc:
        raise IMAPConnectionError(str(exc), account=email_login) from exc


# ═══════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════

def check_one_account(email_login: str, password: str) -> Dict[str, str]:
    """
    Full pipeline for one account:
      1. Call the IMAP checker.
      2. Classify the result.
      3. Log every step with a consistent prefix.
      4. Write the outcome to the appropriate results file.
      5. On unexpected errors, dump the traceback to errors/<account>.txt.

    Returns
    -------
    {
        "status":  "found" | "not_found" | "error",
        "account": <email>,
        "detail":  <human-readable string>
    }
    """
    log.info("[INFO]  ┌── Checking: %s", email_login)

    # ── Step 1: IMAP call ──────────────────────────────────────
    result: Dict[str, Any] = {}
    try:
        result = _call_checker(email_login, password)

    # Auth failures — permanent, do not retry
    except AuthenticationError as exc:
        log.error("[ERROR] %s — Login failed (wrong credentials).", exc)
        return _finish(email_login, "error", f"AUTH_FAIL | {exc}")

    # Transient network errors — already retried inside _call_checker /
    # check_email_by_subject; if we land here all retries are exhausted.
    except (IMAPConnectionError, MaxRetriesExceededError) as exc:
        log.error("[ERROR] %s — Connection failed after retries.", exc)
        dump_error_trace(email_login, traceback.format_exc())
        return _finish(email_login, "error", f"CONN_FAIL | {exc}")

    # Any other unexpected exception
    except Exception as exc:  # noqa: BLE001
        log.exception("[ERROR] %s — Unexpected error: %s", email_login, exc)
        dump_error_trace(email_login, traceback.format_exc())
        return _finish(email_login, "error", f"UNKNOWN | {exc}")

    # ── Step 2: Classify checker output ───────────────────────
    if not result.get("found"):
        log.info("[INFO]  └── NOT FOUND: %s", email_login)
        return _finish(email_login, "not_found", "No password-change email found.")

    # ── Step 3: Found ─────────────────────────────────────────
    uid     = result.get("uid", "?")
    date    = result.get("date", "?")
    subject = result.get("subject", "?")
    snippet = result.get("content", "")[:200].replace("\n", " ")

    detail = (
        f"UID={uid} | DATE={date} | "
        f"SUBJECT={subject!r} | SNIPPET={snippet!r}"
    )
    log.info("[INFO]  └── FOUND: %s → %s", email_login, detail)
    return _finish(email_login, "found", detail)


# ═══════════════════════════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════════════════════════

def _finish(account: str, status: str, detail: str) -> Dict[str, str]:
    """Write result to file and return the standard dict."""
    write_result(status, f"{account} | {detail}")
    return {"status": status, "account": account, "detail": detail}
