"""
utils.py
========
Shared helpers: structured logger, retry decorator, input parser, result writer.
"""

from __future__ import annotations

import csv
import functools
import imaplib
import logging
import logging.handlers
import threading
import time
from pathlib import Path
from typing import Callable, Generator, Tuple, TypeVar

import config
from exceptions import (
    InputParseError,
    MaxRetriesExceededError,
)

# ═══════════════════════════════════════════════════════════════
# LOGGER SETUP
# ═══════════════════════════════════════════════════════════════

def setup_logger(name: str = "checkmail") -> logging.Logger:
    """
    Return a logger that writes to both:
      - stdout (INFO and above, colour-free)
      - logs/checkmail.log (DEBUG and above, rotating, max 5 MB × 3 files)

    Safe to call multiple times — handlers are only attached once.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = setup_logger()

# ═══════════════════════════════════════════════════════════════
# RETRY DECORATOR
# ═══════════════════════════════════════════════════════════════

_F = TypeVar("_F", bound=Callable)

# Exceptions that are considered transient and worth retrying.
_RETRIABLE = (
    imaplib.IMAP4.abort,   # connection dropped mid-session
    imaplib.IMAP4.error,   # generic IMAP protocol error (non-auth)
    OSError,
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    BrokenPipeError,
)

# Hint strings in exception messages that mean "bad credentials" — never retry.
_AUTH_HINTS = (
    "authentication",
    "login",
    "credentials",
    "password",
    "no such user",
    "invalid",
)


def _is_auth_error(exc: Exception) -> bool:
    return any(h in str(exc).lower() for h in _AUTH_HINTS)


def imap_retry(
    max_retries: int = config.MAX_RETRIES,
    base_delay:  float = config.RETRY_BASE_DELAY,
) -> Callable[[_F], _F]:
    """
    Decorator: retry *max_retries* times on transient IMAP / network errors.

    · Authentication errors are re-raised immediately (no retry).
    · Delay between attempts follows: base_delay × attempt_index  (1×, 2×, 3×).
    · Raises MaxRetriesExceededError after all attempts fail.

    Usage::

        @imap_retry(max_retries=3, base_delay=2)
        def my_imap_call(...):
            ...
    """
    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Exception = RuntimeError("imap_retry: no attempt made")
            for attempt in range(1, max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except _RETRIABLE as exc:
                    if _is_auth_error(exc):
                        log.error("[ERROR] Auth rejected in %s — not retrying.", fn.__name__)
                        raise
                    last_exc = exc
                    log.warning(
                        "[WARN]  %s attempt %d/%d failed: %s",
                        fn.__name__, attempt, max_retries, exc,
                    )
                    if attempt < max_retries:
                        wait = base_delay * attempt
                        log.info("[INFO]  Retrying in %.1fs …", wait)
                        time.sleep(wait)
            raise MaxRetriesExceededError(max_retries, last_exc)
        return wrapper  # type: ignore[return-value]
    return decorator


# ═══════════════════════════════════════════════════════════════
# INPUT PARSER
# ═══════════════════════════════════════════════════════════════

def parse_input_file(path: str | Path) -> Generator[Tuple[str, str], None, None]:
    """
    Yield (email, password) pairs from *path*.

    Supported formats
    -----------------
    · Plain text, one credential per line:
          user@gmx.com|password
          user@gmx.com:password
          user@gmx.com password
          user@gmx.com\tpassword
    · CSV (comma-separated), first row may be a header:
          email,password
          user@gmx.com,secret

    Empty lines and lines starting with # are silently skipped.
    Lines that cannot be split raise InputParseError (logged; skipped).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    suffix = path.suffix.lower()

    # ── CSV ────────────────────────────────────────────────────
    if suffix == ".csv":
        yield from _parse_csv(path)
        return

    # ── Plain text ─────────────────────────────────────────────
    with path.open(encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pair = _split_credential_line(line, lineno)
            if pair:
                yield pair


def _parse_csv(path: Path) -> Generator[Tuple[str, str], None, None]:
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        header_skipped = False
        for lineno, row in enumerate(reader, start=1):
            if len(row) < 2:
                continue
            email_val, pw_val = row[0].strip(), row[1].strip()
            # Skip a header row (first row whose "email" cell looks like a label).
            if not header_skipped and email_val.lower() in ("email", "user", "login", "account"):
                header_skipped = True
                continue
            if email_val and pw_val:
                yield email_val, pw_val
            else:
                log.warning("[WARN]  Line %d: empty field(s), skipping.", lineno)


def _split_credential_line(
    line: str, lineno: int
) -> Tuple[str, str] | None:
    for delim in config.INPUT_DELIMITERS:
        parts = line.split(delim, maxsplit=1)
        if len(parts) == 2 and all(p.strip() for p in parts):
            return parts[0].strip(), parts[1].strip()

    err = InputParseError(
        f"Cannot split line {lineno} into email|password: {line!r}"
    )
    log.error("[ERROR] %s", err)
    return None


# ═══════════════════════════════════════════════════════════════
# RESULT WRITER
# ═══════════════════════════════════════════════════════════════

def write_result(status: str, line: str) -> None:
    """
    Append *line* to the appropriate results file.

    Parameters
    ----------
    status : "found" | "not_found" | "error"
    line   : full text line to append (newline added automatically)
    """
    mapping = {
        "found":     config.RESULT_FOUND_FILE,
        "not_found": config.RESULT_NOT_FOUND_FILE,
        "error":     config.RESULT_ERROR_FILE,
    }
    target = mapping.get(status, config.RESULT_ERROR_FILE)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def dump_error_trace(account: str, trace: str) -> None:
    """
    Write a full traceback to errors/<account>.txt for post-mortem debugging.
    The account email is sanitised so it is safe as a filename.
    """
    safe_name = account.replace("@", "_at_").replace("/", "_").replace("\\", "_")
    dest = config.ERROR_DIR / f"{safe_name}.txt"
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(f"Account: {account}\n{'─' * 60}\n{trace}\n")
    log.debug("[DEBUG] Error trace saved → %s", dest)


# ═══════════════════════════════════════════════════════════════
# PROGRESS COUNTER (thread-safe)
# ═══════════════════════════════════════════════════════════════

class Counter:
    """Simple thread-safe integer counter."""
    def __init__(self) -> None:
        self._v   = 0
        self._lock = threading.Lock()

    def increment(self) -> int:
        with self._lock:
            self._v += 1
            return self._v

    @property
    def value(self) -> int:
        return self._v
