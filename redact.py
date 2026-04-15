"""Redact sensitive data before posting content to GitHub.

Every string that leaves this system for GitHub (PR bodies, issue comments,
error messages) must pass through ``redact()`` first.  The function applies
pattern-based scrubbing for common secret formats and replaces matches with
a safe placeholder so context is preserved without leaking values.
"""

from __future__ import annotations

import re

# Placeholder inserted in place of redacted values
_REDACTED = "[REDACTED]"

# Each entry is (compiled regex, replacement template).
# Order matters — more specific patterns should come first to avoid partial
# matches by broader patterns.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # --- Cloud provider keys ---
    # AWS access key ID (always starts with AKIA)
    (re.compile(r"AKIA[0-9A-Z]{16}"), _REDACTED),
    # AWS secret access key (40-char base64-ish after a separator)
    (re.compile(r"(?<=[\s=:\"'])[A-Za-z0-9/+=]{40}(?=[\s\"',;]|$)"), _REDACTED),
    # Google Cloud / GCP service-account key ID
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), _REDACTED),

    # --- API tokens / bearer tokens ---
    # Generic Bearer token header
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_.~+/=-]+"), rf"\1{_REDACTED}"),
    # Anthropic API key
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), _REDACTED),
    # OpenAI API key
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), _REDACTED),
    # GitHub tokens (classic PATs, fine-grained, app tokens)
    (re.compile(r"gh[ps]_[A-Za-z0-9_]{36,}"), _REDACTED),
    (re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), _REDACTED),
    # Slack tokens
    (re.compile(r"xox[bpras]-[A-Za-z0-9-]+"), _REDACTED),
    # Generic "token" / "key" / "secret" in key=value assignments
    (
        re.compile(
            r"(?i)"
            r"((?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token"
            r"|secret[_-]?key|private[_-]?key|client[_-]?secret"
            r"|password|passwd|pwd)"
            r"""\s*[:=]\s*['"]?)"""
            r"[^\s'\"]{8,}"
        ),
        rf"\1{_REDACTED}",
    ),

    # --- Connection strings ---
    # Database URLs  (postgres, mysql, mongodb, redis, amqp, etc.)
    (
        re.compile(
            r"(?i)((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)"
            r"://[^@]*@)"
            r"[^\s'\",;]+"
        ),
        rf"\1{_REDACTED}",
    ),
    # Generic user:pass@host in URIs
    (re.compile(r"(://[^/:@\s]+):([^/@\s]{3,})@"), rf"\1:{_REDACTED}@"),

    # --- Private keys ---
    (re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"), _REDACTED),
    (re.compile(r"-----BEGIN[A-Z ]*CERTIFICATE-----[\s\S]*?-----END[A-Z ]*CERTIFICATE-----"), _REDACTED),

    # --- Env-file lines ---
    # Lines that look like  SECRET_KEY=value  or  export DB_PASS="value"
    (
        re.compile(
            r"(?im)^((?:export\s+)?"
            r"(?:[A-Z_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|KEY|CREDENTIAL|AUTH)[A-Z_]*)"
            r"""\s*=\s*['"]?)"""
            r".+?(['\"]?\s*)$"
        ),
        rf"\1{_REDACTED}\2",
    ),

    # --- Absolute filesystem paths (Windows & Unix home dirs) ---
    # Prevents leaking the user's home directory or system layout
    (re.compile(r"(?i)[A-Z]:\\Users\\[^\s\\]+"), "[LOCAL_PATH]"),
    (re.compile(r"/home/[^\s/]+"), "[LOCAL_PATH]"),
    (re.compile(r"/Users/[^\s/]+"), "[LOCAL_PATH]"),

    # --- JWT tokens ---
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), _REDACTED),
]


def redact(text: str) -> str:
    """Scrub *text* of secrets, keys, tokens, and filesystem paths.

    Returns a copy with sensitive substrings replaced by safe placeholders.
    Idempotent — running it twice produces the same output.
    """
    if not text:
        return text

    result = text
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result
