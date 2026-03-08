"""
config.py
=========
Central configuration store for the Check-Mail tool.

All tuneable knobs are in one place — no magic numbers scattered across files.
"""

from __future__ import annotations

from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════

BASE_DIR      = Path(__file__).parent
RESULTS_DIR   = BASE_DIR / "results"
LOG_DIR       = BASE_DIR / "logs"
ERROR_DIR     = BASE_DIR / "errors"   # error dump files (body / trace)

# Auto-create output directories on import.
for _d in (RESULTS_DIR, LOG_DIR, ERROR_DIR):
    _d.mkdir(exist_ok=True)

# ═══════════════════════════════════════════════════════════════
# IMAP CONNECTION
# ═══════════════════════════════════════════════════════════════

IMAP_HOST         = "imap.gmx.net"
IMAP_PORT         = 993              # TLS/SSL
IMAP_TIMEOUT      = 30               # per-socket timeout (seconds)

# ═══════════════════════════════════════════════════════════════
# RETRY / BACK-OFF
# ═══════════════════════════════════════════════════════════════

MAX_RETRIES       = 3                # maximum connection / network retries
RETRY_BASE_DELAY  = 2.0              # back-off: delay = BASE × attempt_number

# ═══════════════════════════════════════════════════════════════
# SEARCH PARAMETERS
# ═══════════════════════════════════════════════════════════════

# Display-name fragments used with IMAP SEARCH FROM.
# Matches the full From header (display name + address), e.g.:
#   "Facebook <security@facebookmail.com>"  → matched by "Facebook"
#   "Meta Platforms <noreply@meta.com>"     → matched by "Meta"
FB_SENDER_NAMES: list[str] = [
    "Facebook",
    "Meta",
]

# Maximum number of Facebook/Meta emails to inspect per account.
# Keeping this bounded prevents excessive data transfer on busy inboxes.
MAX_EMAILS_CHECK  = 50

# Subject to look for — describes a password-change confirmation.
# Written in English; the checker will translate email subjects to English
# before comparing, so language of the actual email does not matter.
TARGET_SUBJECT    = "Your password has been changed"

# ═══════════════════════════════════════════════════════════════
# CONCURRENCY
# ═══════════════════════════════════════════════════════════════

# Number of accounts to check in parallel (thread-pool workers).
# Keep ≤ 10 to avoid hammering the GMX IMAP server.
MAX_WORKERS       = 5

# ═══════════════════════════════════════════════════════════════
# OUTPUT FILES
# ═══════════════════════════════════════════════════════════════

RESULT_FOUND_FILE     = RESULTS_DIR / "found.txt"
RESULT_NOT_FOUND_FILE = RESULTS_DIR / "not_found.txt"
RESULT_ERROR_FILE     = RESULTS_DIR / "errors.txt"
LOG_FILE              = LOG_DIR / "checkmail.log"

# ═══════════════════════════════════════════════════════════════
# INPUT FORMAT
# ═══════════════════════════════════════════════════════════════

# Supported delimiter characters that separate email from password on one line.
# The parser tries each in this order and stops at the first that splits the
# line into exactly two non-empty parts.
INPUT_DELIMITERS: list[str] = ["|", ":", ";", "\t", " "]
