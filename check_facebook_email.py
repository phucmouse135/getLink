"""
check_facebook_email.py
=======================
Production-ready IMAP checker — GMX × Facebook / Meta emails.

Finds whether a "success" email with the given subject exists in INBOX
without altering the read/unread state of any message.

Requirements
------------
    pip install deep-translator

Usage
-----
    from check_facebook_email import check_email_by_subject

    result = check_email_by_subject(
        email_login    = "user@gmx.com",
        password       = "s3cr3t",
        target_subject = "Your password has been changed",
    )
    if result["found"]:
        print("UID    :", result["uid"])
        print("Date   :", result["date"])
        print("Subject:", result["subject"])
        print("Snippet:", result["content"])
    else:
        print("Email not found.")
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import time
import unicodedata
from email.header import decode_header
from typing import Any, Dict, List, Optional, Set

try:
    from deep_translator import GoogleTranslator
    _TRANSLATION_AVAILABLE = True
except ImportError:
    _TRANSLATION_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

IMAP_HOST         = "imap.gmx.net"
IMAP_PORT         = 993
IMAP_TIMEOUT      = 30        # socket timeout in seconds

MAX_RETRIES       = 3
RETRY_BASE_DELAY  = 2         # seconds; actual delay = base × attempt index

MAX_EMAILS_CHECK  = 50        # cap on Facebook/Meta emails to inspect per call

# Display-name fragments used in IMAP SEARCH FROM.
# IMAP FROM search matches the entire From header value (display name AND
# address), so "Facebook" matches both "Facebook <no-reply@facebookmail.com>"
# and any variation that contains the word "Facebook" or "meta".
FB_SENDER_NAMES: List[str] = [
    "Facebook",
    "Meta",
]

log = logging.getLogger("checkmail")


# ═══════════════════════════════════════════════════════════════
# TRANSLATION — session-scoped in-process cache
# ═══════════════════════════════════════════════════════════════

_trans_cache: Dict[str, str] = {}


def _to_english(text: str) -> str:
    """
    Translate *text* to lower-case English.

    Falls back to the original (lowercased) when deep_translator is
    unavailable or the API call fails.  Results are cached in-process so
    repeated identical strings incur at most one API call per run.
    """
    if not text:
        return ""

    key = text.strip()
    if key in _trans_cache:
        return _trans_cache[key]

    if _TRANSLATION_AVAILABLE:
        try:
            out = GoogleTranslator(source="auto", target="en").translate(key)
            result = (out or key).lower().strip()
        except Exception as exc:
            log.debug("Translation failed (%s): %r", exc, key[:60])
            result = key.lower().strip()
    else:
        result = key.lower().strip()

    _trans_cache[key] = result
    return result


# ═══════════════════════════════════════════════════════════════
# MIME / HEADER HELPERS
# ═══════════════════════════════════════════════════════════════

def _decode_header_str(raw: str) -> str:
    """Decode an RFC 2047-encoded header value to a plain Unicode string."""
    if not raw:
        return ""
    segments: List[str] = []
    for payload, charset in decode_header(raw):
        if isinstance(payload, bytes):
            enc = charset or "utf-8"
            try:
                segments.append(payload.decode(enc))
            except (UnicodeDecodeError, LookupError):
                segments.append(payload.decode("utf-8", errors="replace"))
        else:
            segments.append(str(payload))
    return "".join(segments)


_RE_REPLY_PREFIX = re.compile(r"^\s*(re|fwd?|tr|aw)\s*:\s*", re.IGNORECASE)


def _clean_subject(subject: str) -> str:
    """Strip reply/forward prefixes and surrounding whitespace."""
    return _RE_REPLY_PREFIX.sub("", subject).strip()


# Common English words that carry no semantic weight for matching.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can",
    "of", "in", "on", "at", "to", "for", "by", "with", "from",
    "and", "or", "but", "not", "no",
    "you", "your", "we", "our", "they", "their", "it", "its",
    "this", "that", "these", "those",
})

# Synonym table: maps translation variants AND multilingual words to a shared
# canonical token.  Works as a fallback when Google Translate is unavailable
# and the original (non-English) text reaches the token-overlap pass.
_SYNONYMS: Dict[str, str] = {
    # ── "changed" family ──────────────────────────────────────────────
    "modified":   "changed",   # EN alternate
    "updated":    "changed",   # EN alternate
    "reset":      "changed",   # EN alternate
    "altered":    "changed",   # EN alternate
    "edited":     "changed",   # EN alternate
    "replaced":   "changed",   # EN alternate
    # Spanish
    "cambiado":   "changed",   # Se ha cambiado
    "cambiada":   "changed",
    "cambio":     "changed",
    # French (diacritic-stripped forms)
    "modifie":    "changed",   # modifié / modifiée
    "modifiee":   "changed",
    # German (umlaut-stripped)
    "geandert":   "changed",   # geändert
    "geaendert":  "changed",
    # Italian
    "modificata": "changed",
    "modificato": "changed",
    # Portuguese
    "alterado":   "changed",
    "alterada":   "changed",
    "mudado":     "changed",
    "mudada":     "changed",
    # ── "password" family ─────────────────────────────────────────────
    "pass":       "password",  # EN shorthand
    "pwd":        "password",
    "passcode":   "password",
    # Spanish
    "contrasena": "password",  # contraseña (stripped)
    "contrasenha":"password",
    # French — "mot de passe" tokenises to ["mot", "passe"]
    "passe":      "password",
    # German
    "passwort":   "password",
    # Portuguese / Italian share "password" / "senha"
    "senha":      "password",
    # Dutch
    "wachtwoord": "password",
}


def _canon(token: str) -> str:
    """Return the canonical form of *token* via the synonym table."""
    return _SYNONYMS.get(token, token)


def _strip_accents(text: str) -> str:
    """Remove diacritical marks: 'modifié' → 'modifie', 'contraseña' → 'contrasena'."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _subjects_match(email_subj: str, target_subj: str) -> bool:
    # Three-pass matching: translated containment, diacritic-stripped
    # containment, then synonym-normalised token overlap (>=75%).
    a_raw = _to_english(_clean_subject(email_subj))
    b_raw = _to_english(_clean_subject(target_subj))
    if not a_raw or not b_raw:
        return False
    if b_raw in a_raw or a_raw in b_raw:
        return True
    a = _strip_accents(a_raw)
    b = _strip_accents(b_raw)
    if b in a or a in b:
        return True
    key_tokens_raw = [w for w in re.findall(r"[a-z]{3,}", b) if w not in _STOPWORDS]
    if not key_tokens_raw:
        return False
    key_canon   = [_canon(t) for t in key_tokens_raw]
    email_canon = {_canon(w) for w in re.findall(r"[a-z]{3,}", a) if w not in _STOPWORDS}
    found = sum(1 for t in key_canon if t in email_canon)
    ratio = found / len(key_canon)
    log.debug("Token-overlap: %.0f%% (%d/%d) | key=%s | email=%s",
              ratio * 100, found, len(key_canon), key_canon, sorted(email_canon)[:12])
    return ratio >= 0.75

def _extract_body(msg: email.message.Message) -> str:
    """
    Extract readable body text from an email.Message.

    · Skips attachments entirely (Content-Disposition: attachment).
    · Prefers text/html parts (HTML tags stripped) over text/plain.
    · Collapses excess whitespace so keyword scanning is reliable.
    """
    html:  List[str] = []
    plain: List[str] = []

    parts = list(msg.walk()) if msg.is_multipart() else [msg]

    for part in parts:
        if "attachment" in str(part.get("Content-Disposition", "")):
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except Exception:
            decoded = payload.decode("utf-8", errors="replace")

        ctype = part.get_content_type()
        if ctype == "text/html":
            html.append(decoded)
        elif ctype == "text/plain":
            plain.append(decoded)

    if html:
        raw = "\n".join(html)
        stripped = re.sub(r"<[^>]+>", " ", raw)
        return re.sub(r" {2,}", " ", stripped).strip()

    return "\n".join(plain).strip()


# ═══════════════════════════════════════════════════════════════
# IMAP CONNECTION
# ═══════════════════════════════════════════════════════════════

def _open_imap(email_login: str, password: str) -> imaplib.IMAP4_SSL:
    """
    Open and authenticate a fresh IMAP4 over TLS connection.

    Raises
    ------
    imaplib.IMAP4.error   – bad credentials or IMAP protocol rejection.
    OSError / TimeoutError – network-level failure.
    """
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.socket().settimeout(IMAP_TIMEOUT)
    conn.login(email_login, password)
    return conn


def _connect(email_login: str, password: str) -> imaplib.IMAP4_SSL:
    """
    Wrapper around _open_imap() with exponential back-off retry.

    · Authentication errors are re-raised immediately — no retry.
    · Network / protocol transient errors are retried MAX_RETRIES times.
    · Raises the last caught exception when all attempts are exhausted.
    """
    _AUTH_HINTS = ("authentication", "login", "credentials", "password", "no such user")
    last: Exception = RuntimeError("_connect(): no attempt was made")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info("IMAP connect attempt %d/%d …", attempt, MAX_RETRIES)
            conn = _open_imap(email_login, password)
            log.info("IMAP authenticated successfully.")
            return conn

        except imaplib.IMAP4.error as exc:
            if any(hint in str(exc).lower() for hint in _AUTH_HINTS):
                log.error("Authentication rejected — not retrying.")
                raise
            log.warning("IMAP protocol error (%d/%d): %s", attempt, MAX_RETRIES, exc)
            last = exc

        except (OSError, TimeoutError, ConnectionError) as exc:
            log.warning("Network error (%d/%d): %s", attempt, MAX_RETRIES, exc)
            last = exc

        if attempt < MAX_RETRIES:
            wait = RETRY_BASE_DELAY * attempt
            log.info("Waiting %ds before next attempt …", wait)
            time.sleep(wait)

    raise last


# ═══════════════════════════════════════════════════════════════
# IMAP RESPONSE PARSER
# ═══════════════════════════════════════════════════════════════

def _parse_header_fetch(raw: List[Any]) -> List[Dict[str, str]]:
    """
    Parse the flat list returned by:

        conn.uid('fetch', uid_csv,
                 '(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])')

    imaplib represents each message as a 2-tuple (meta_bytes, header_bytes)
    inside the response list, optionally followed by a b')' separator byte.

    Returns a list of dicts with keys: uid, subject, from, date.
    """
    _UID_RE = re.compile(r"\bUID\s+(\d+)\b", re.IGNORECASE)
    results: List[Dict[str, str]] = []

    for item in raw:
        if not isinstance(item, tuple) or len(item) < 2:
            continue

        meta = item[0].decode(errors="replace") if isinstance(item[0], bytes) else str(item[0])
        m = _UID_RE.search(meta)
        if not m:
            continue

        uid     = m.group(1)
        hdr_raw = item[1] if isinstance(item[1], bytes) else b""
        parsed  = email.message_from_bytes(hdr_raw)

        results.append({
            "uid":     uid,
            "subject": _decode_header_str(parsed.get("Subject", "")),
            "from":    _decode_header_str(parsed.get("From", "")),
            "date":    parsed.get("Date", ""),
        })

    return results


# ═══════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════

def check_email_by_subject(
    email_login:    str,
    password:       str,
    target_subject: str,
) -> Dict[str, Any]:
    """
    Determine whether a "success" email with the specified subject exists in
    the GMX INBOX for *email_login*, without modifying any email's state.

    Connection
    ~~~~~~~~~~
    · GMX IMAP4 over TLS, port 993.
    · Auth failures surface immediately (no retry).
    · Transient network / protocol errors are retried (MAX_RETRIES) with
      exponential back-off (RETRY_BASE_DELAY seconds × attempt index).

    Search strategy
    ~~~~~~~~~~~~~~~
    1. UID SEARCH FROM <name>
          Server-side filter for each display-name fragment ("Facebook",
          "meta").  Collects unique UIDs across both queries.
    2. Batch BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)]
          Fetch only header fields — no message body downloaded yet.
          PEEK ensures no \\Seen flag is set.
    3. Subject comparison (language-agnostic)
          Both the stored email subject and *target_subject* are translated
          to English before a containment check is performed.
    4. BODY.PEEK[] for the newest subject-matched email
          Full body fetched only once the subject matches.  PEEK again
          avoids marking messages as read.  The first matching email's body
          is returned verbatim as evidence — no keyword gating.

    Parameters
    ----------
    email_login     : GMX address  (e.g. "alice@gmx.com")
    password        : IMAP / app password for the account
    target_subject  : Subject text in any language; auto-translated to English

    Returns
    -------
    Success →
        {
            "found":   True,
            "subject": "<original subject from email>",
            "uid":     "<IMAP UID string>",
            "date":    "<RFC 2822 date header>",
            "content": "<first 500 chars of the body>"
        }
    Failure →
        {"found": False}
    """
    conn: Optional[imaplib.IMAP4_SSL] = None

    try:
        # ── 1. Connect (with retry) ────────────────────────────────────
        conn = _connect(email_login, password)

        # ── 2. Select INBOX ────────────────────────────────────────────
        status, sel_data = conn.select("INBOX")
        if status != "OK":
            log.error("SELECT INBOX failed: %s", sel_data)
            return {"found": False}

        total = int(sel_data[0]) if sel_data and sel_data[0] else 0
        if total == 0:
            log.info("INBOX is empty.")
            return {"found": False}

        log.info("INBOX: %d total message(s).", total)

        # ── 3. UID SEARCH — collect all Facebook / Meta UIDs ──────────
        fb_uids: Set[bytes] = set()

        for name in FB_SENDER_NAMES:
            try:
                st, data = conn.uid("search", None, f'FROM "{name}"')
                if st == "OK" and data and data[0]:
                    fb_uids.update(data[0].split())
            except imaplib.IMAP4.error as exc:
                log.debug("UID SEARCH FROM %r: %s", name, exc)

        if not fb_uids:
            log.info("No Facebook / Meta emails found in INBOX.")
            return {"found": False}

        # Sort by UID integer descending (largest UID = newest message).
        # Cap inspection window at MAX_EMAILS_CHECK to bound latency.
        sorted_uids: List[bytes] = sorted(
            fb_uids, key=lambda u: int(u), reverse=True
        )[:MAX_EMAILS_CHECK]

        log.info(
            "%d Facebook/Meta email(s) found; scanning %d newest.",
            len(fb_uids), len(sorted_uids),
        )

        # ── 4. Batch-fetch headers only ────────────────────────────────
        uid_csv = b",".join(sorted_uids)

        st, hdr_data = conn.uid(
            "fetch",
            uid_csv,
            "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])",
        )
        if st != "OK" or not hdr_data:
            log.error("Batch header FETCH failed (status=%s).", st)
            return {"found": False}

        headers = _parse_header_fetch(hdr_data)
        log.info("Headers parsed: %d record(s).", len(headers))

        # ── Candidate list (tracking) + pre-translate for debugging ───
        target_en = _to_english(_clean_subject(target_subject))
        log.info("Target subject  : %r", target_subject)
        log.info("Target (EN)     : %r", target_en)
        log.info("┌─ Candidate emails in scope (%d) ─────────────────────", len(headers))
        for i, h in enumerate(headers, start=1):
            subj_en = _to_english(_clean_subject(h["subject"]))
            log.info("│ %2d. UID=%-6s  FROM=%s", i, h["uid"], h["from"][:60])
            log.info("│     orig : %s", h["subject"])
            if subj_en != h["subject"].lower().strip():
                log.info("│     EN   : %s", subj_en)
        log.info("└──────────────────────────────────────────────────────")

        # ── 5. Subject matching (translation-aware) ────────────────────
        matched = [h for h in headers if _subjects_match(h["subject"], target_subject)]

        if not matched:
            log.info("No email subjects match target '%s'.", target_subject)
            return {"found": False}

        # Ensure newest UID first (parse_header_fetch order is not guaranteed)
        matched.sort(key=lambda h: int(h["uid"]), reverse=True)
        log.info("%d subject-matched email(s). Fetching bodies …", len(matched))
        for h in matched:
            log.info("  ↳ UID=%-6s  SUBJECT=%s", h["uid"], h["subject"])

        # ── 6. Fetch body (PEEK) — return the newest subject-matched email ──
        # No keyword gating: subject match is sufficient proof of identity.
        # The body snippet is returned as human-readable evidence.
        for info in matched:
            uid_b = info["uid"].encode()
            try:
                st, body_parts = conn.uid("fetch", uid_b, "(BODY.PEEK[])")
                if st != "OK" or not body_parts:
                    log.warning("Body FETCH failed for UID %s.", info["uid"])
                    continue

                # imaplib returns a flat list: [(meta_bytes, msg_bytes), b')']
                raw_msg: Optional[bytes] = None
                for part in body_parts:
                    if (
                        isinstance(part, tuple)
                        and len(part) >= 2
                        and isinstance(part[1], bytes)
                    ):
                        raw_msg = part[1]
                        break

                if not raw_msg:
                    log.warning("Empty body payload for UID %s.", info["uid"])
                    continue

                body = _extract_body(email.message_from_bytes(raw_msg))

                if not body.strip():
                    log.debug("UID %s: empty body, skipping.", info["uid"])
                    continue

                log.info("Email found — UID %s.", info["uid"])
                return {
                    "found":   True,
                    "subject": info["subject"],
                    "uid":     info["uid"],
                    "date":    info["date"],
                    "content": body[:500].strip(),
                }

            except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
                log.warning("Error processing UID %s: %s", info["uid"], exc)
                continue

        log.info("Subject matched but all body fetches were empty.")
        return {"found": False}

    # ── Top-level guard — always return a well-typed dict ─────────────
    except imaplib.IMAP4.error as exc:
        log.error("IMAP error: %s", exc)
        return {"found": False}

    except (OSError, TimeoutError, ConnectionError) as exc:
        log.error("Network/timeout error: %s", exc)
        return {"found": False}

    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        return {"found": False}

    finally:
        # Always attempt clean close + logout, silently ignore any errors.
        if conn is not None:
            for fn in (conn.close, conn.logout):
                try:
                    fn()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════
# QUICK CLI SMOKE-TEST
#   python check_facebook_email.py alice@gmx.com s3cr3t "Your password has been changed"
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    if len(sys.argv) != 4:
        print("Usage: python check_facebook_email.py <email> <password> <subject>")
        sys.exit(1)

    result = check_email_by_subject(
        email_login    = sys.argv[1],
        password       = sys.argv[2],
        target_subject = sys.argv[3],
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
