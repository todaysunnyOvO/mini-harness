from __future__ import annotations

import re


REDACTED = "[REDACTED]"

# Summary input is an external trust boundary.  Detection therefore combines
# context-based credentials (``password=...``) with self-identifying credential
# formats (``sk-...``), instead of assuming that every secret has a label.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----"
    r"[\s\S]*?"
    r"-----END (?P=label)-----"
)

_KNOWN_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"sk-[A-Za-z0-9_-]{10,}"             # OpenAI/OpenRouter/Anthropic
    r"|github_pat_[A-Za-z0-9_]{10,}"     # GitHub fine-grained PAT
    r"|gh[opusr]_[A-Za-z0-9]{10,}"       # GitHub classic/OAuth tokens
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"     # Slack OAuth tokens
    r"|xapp-[0-9]+-[A-Za-z0-9-]{10,}"    # Slack app token
    r"|AIza[A-Za-z0-9_-]{30,}"           # Google API key
    r"|AKIA[A-Z0-9]{16}"                 # AWS access-key ID
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"  # Stripe
    r"|SG\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,})?"  # SendGrid
    r"|(?:hf_|gsk_|npm_|pypi-|tvly-|fal_)[A-Za-z0-9_-]{10,}"
    r")"
)

_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"eyJ[A-Za-z0-9_-]{10,}"
    r"\.[A-Za-z0-9_-]{8,}"
    r"\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
)

_SECRET_NAME = (
    r"(?:api[_. -]?key|access[_. -]?token|refresh[_. -]?token|"
    r"id[_. -]?token|auth[_. -]?token|client[_. -]?secret|"
    r"private[_. -]?key|password|passwd|token|secret|credential|"
    r"api[_. -]?secret)"
)
_LABELED_VALUE_RE = re.compile(
    rf"(?P<prefix>\b[A-Za-z0-9_.-]*{_SECRET_NAME}\b"
    rf"[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\s,;&\"']+)",
    re.IGNORECASE,
)

_AUTH_VALUE_RE = re.compile(
    r"(?P<prefix>\b(?:Bearer|Basic|Token)\s+)"
    r"(?P<value>[^\s\"']+)",
    re.IGNORECASE,
)

_AUTH_HEADER_RE = re.compile(
    r"(?P<prefix>\b(?:Proxy-)?Authorization\s*:\s*"
    r"(?:[A-Za-z][A-Za-z0-9.+-]*\s+)?)"
    r"(?P<value>[^\s\"']+)",
    re.IGNORECASE,
)

_URL_USER_PASSWORD_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^/:@\s]+:)"
    r"(?P<value>[^/@\s]+)"
    r"(?P<suffix>@)",
    re.IGNORECASE,
)

_URL_BARE_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://)"
    r"(?P<value>[^/:@\s]{8,})"
    r"(?P<suffix>@[^\s/]+)",
    re.IGNORECASE,
)


def redact_sensitive_text(value: str) -> str:
    """Remove recognized credentials before text crosses the summary boundary.

    The marker intentionally contains no prefix/suffix from the original value:
    a summary does not need credential fragments for debugging, and retaining
    them would needlessly disclose reusable information.
    """

    redacted = _PRIVATE_KEY_RE.sub(REDACTED, value)
    redacted = _URL_USER_PASSWORD_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{REDACTED}{match.group('suffix')}"
        ),
        redacted,
    )
    redacted = _URL_BARE_CREDENTIAL_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{REDACTED}{match.group('suffix')}"
        ),
        redacted,
    )
    redacted = _AUTH_HEADER_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )
    redacted = _AUTH_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )
    redacted = _LABELED_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )
    redacted = _JWT_RE.sub(REDACTED, redacted)
    return _KNOWN_CREDENTIAL_RE.sub(REDACTED, redacted)
