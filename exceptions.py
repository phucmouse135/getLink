"""
exceptions.py
=============
Custom exception hierarchy for the Check-Mail tool.

Error code table
----------------
E001  AuthenticationError      Bad credentials — do not retry.
E002  MailboxEmptyError         INBOX contains zero messages.
E003  NoFacebookMailError       No mail from Facebook / Meta found.
E004  SubjectNotFoundError      Facebook mails present but subject not matched.
E005  BodyFetchError            Body fetch returned empty / corrupt data.
E006  IMAPConnectionError       Network / protocol failure (retriable).
E007  MaxRetriesExceededError   All retry attempts exhausted.
E008  InputParseError           Malformed input line (cannot split credentials).
"""

from __future__ import annotations


class CheckMailBaseError(Exception):
    """Root exception for the check-mail tool."""
    code: str = "E000"

    def __init__(self, message: str = "", *, account: str = "") -> None:
        self.account = account
        super().__init__(message)

    def __str__(self) -> str:
        prefix = f"[{self.code}]"
        if self.account:
            prefix += f" ({self.account})"
        return f"{prefix} {super().__str__()}"


# ── Authentication ─────────────────────────────────────────────────────────

class AuthenticationError(CheckMailBaseError):
    """IMAP login was rejected.  Caller must NOT retry (wrong credentials)."""
    code = "E001"


# ── Mailbox state ─────────────────────────────────────────────────────────

class MailboxEmptyError(CheckMailBaseError):
    """INBOX is empty — nothing to check."""
    code = "E002"


class NoFacebookMailError(CheckMailBaseError):
    """SEARCH returned no results for any Facebook / Meta sender."""
    code = "E003"


class SubjectNotFoundError(CheckMailBaseError):
    """Facebook emails exist but none matches the target subject."""
    code = "E004"


class BodyFetchError(CheckMailBaseError):
    """Body fetch succeeded at the protocol level but returned no usable data."""
    code = "E005"


# ── Connectivity ──────────────────────────────────────────────────────────

class IMAPConnectionError(CheckMailBaseError):
    """Transient network or IMAP protocol error — safe to retry."""
    code = "E006"


class MaxRetriesExceededError(CheckMailBaseError):
    """All MAX_RETRIES attempts have been exhausted without success."""
    code = "E007"

    def __init__(self, attempts: int, last_error: Exception, **kwargs) -> None:
        self.attempts   = attempts
        self.last_error = last_error
        super().__init__(
            f"Failed after {attempts} attempt(s). Last error: {last_error}",
            **kwargs,
        )


# ── Input ─────────────────────────────────────────────────────────────────

class InputParseError(CheckMailBaseError):
    """A line in the input file could not be split into (email, password)."""
    code = "E008"
