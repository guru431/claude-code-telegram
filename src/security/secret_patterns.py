"""Shared secret/credential redaction.

A single source of truth for redacting high-confidence secrets before any
text leaves the bot (interactive Telegram replies, webhook/scheduled
notifications, verbose tool previews). This module deliberately imports only
``re`` so it can be used from any layer — including the notification service —
without pulling in the Telegram/bot stack.
"""

import re
from typing import List

# Patterns that look like secrets/credentials. Capture groups preserve the
# non-secret prefix (e.g. ``"Bearer "``, ``"sk-abc"``) and, where relevant, a
# trailing suffix (``"@host"`` for connection strings).
SECRET_PATTERNS: List["re.Pattern[str]"] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...,
    # gh[ps]_..., glpat-... GitLab, npm_..., AIzaSy... Google, hf_... HuggingFace,
    # SG.... SendGrid)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(ghs_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(ghr_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(ghu_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(glpat-[A-Za-z0-9_-]{5})[A-Za-z0-9_-]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
        r"|(xoxp-[A-Za-z0-9-]{5})[A-Za-z0-9-]*"
        r"|(xoxa-[A-Za-z0-9-]{5})[A-Za-z0-9-]*"
        r"|(npm_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(AIzaSy[A-Za-z0-9_-]{5})[A-Za-z0-9_-]*"
        r"|(hf_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(SG\.[A-Za-z0-9_-]{5})[A-Za-z0-9_.-]*"
    ),
    # Telegram bot token (digits:alphanum_-, ~46 chars)
    re.compile(r"(\d{6,12}:AA[A-Za-z0-9_-]{5})[A-Za-z0-9_-]{27,}"),
    # Anthropic / OpenAI / generic project key prefixes that vary in length
    re.compile(r"(sk-proj-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]*"),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    re.compile(r"(ASIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth"
        r"|--access-key|--bearer|--client-secret|--private-key)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|PASSWD|PASS|API_KEY|APIKEY"
        r"|AUTH_TOKEN|AUTH|PRIVATE_KEY|ACCESS_KEY|ACCESS_TOKEN"
        r"|CLIENT_SECRET|WEBHOOK_SECRET|REFRESH_TOKEN|SESSION_TOKEN"
        r"|BOT_TOKEN|GH_TOKEN|GITHUB_TOKEN|SLACK_TOKEN|DATABASE_URL"
        r"|REDIS_URL)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  scheme://user:pass@host
    # Two outer capture groups preserve the `scheme://user:` prefix and
    # the `@host` suffix; the password between them is redacted.
    re.compile(r"(://[^:/\s]+:)[^@\s]{4,}(@[^\s]+)"),
    # Private key blocks
    re.compile(
        r"(-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----)"
        r"[\s\S]+?(-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----)"
    ),
    # JSON-like "password": "...", "token": "..."
    re.compile(
        r'("(?:password|passwd|pass|token|secret|api_?key|auth(?:_token)?)"'
        r"\s*:\s*\")[^\"]{4,}(\")",
        re.IGNORECASE,
    ),
]


def _redact_match(match: "re.Match[str]") -> str:
    """Replace a matched secret while preserving structural groups.

    The patterns use capture groups for the non-secret prefix (e.g.
    ``"Bearer "``, ``"sk-abc"``) and, where relevant, a trailing suffix
    (``"@host"`` for connection strings). We concatenate all non-None groups
    around a ``***`` placeholder so the redacted output stays legible.
    """
    groups = [g for g in match.groups() if g is not None]
    if not groups:
        return "***"
    if len(groups) == 1:
        return f"{groups[0]}***"
    # Two-group case: prefix + suffix wrap the redacted secret.
    return f"{groups[0]}***{groups[1]}"


def redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(_redact_match, result)
    return result
