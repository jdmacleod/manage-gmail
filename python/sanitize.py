"""
Sanitize untrusted email content before passing to an LLM classifier.

Two usage paths:
  corpus  sanitize(body_text, strip_html=False)
          body_text is pre-extracted plain text from gmail.db (mbox_import.py
          already discards HTML MIME parts); HTML stripping is skipped.

  live    sanitize(raw_body, strip_html=True)
          raw_body is the HTML body from a gws API response; HTML is stripped
          to plain text before injection detection.

Public API
----------
sanitize(text, *, strip_html=True, redact_pii=False) -> str
    Returns sanitized text wrapped in [EMAIL_CONTENT]...[/EMAIL_CONTENT].

build_user_turn(from_addr, subject, date_str, sanitized_body) -> str
    Formats the full user-turn message sent to the model.
    Headers are outside the truncated window; only body is bounded.

Known v1.0.0 gaps (documented, not defended against):
  - Base64-encoded instruction strings in plain text
  - Unicode homoglyph substitution (е vs e in "Ignore")
"""

from __future__ import annotations

import html as _html_stdlib
import re

# ---------------------------------------------------------------------------
# Injection detection
# ---------------------------------------------------------------------------

_INJECTION_PREFIXES = (
    "Ignore",
    "System:",
    "Assistant:",
    "You are",
    "INST",
)

_INJECTION_TOKENS = ("<<SYS>>", "[INST]", "</s>")

_REDACTED = "[REDACTED_INJECTION_ATTEMPT]"

# ---------------------------------------------------------------------------
# PII patterns
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b")

# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

# Matches <script>...</script> and <style>...</style> including multiline content.
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</(script|style)>",
    re.DOTALL | re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Body length
# ---------------------------------------------------------------------------

_BODY_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_BLOCK_END_RE = re.compile(r"</(?:p|div|li|tr|h[1-6]|blockquote)>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_INLINE_SPACE_RE = re.compile(r"[^\S\n]+")  # spaces/tabs but not newlines
_EXCESS_NEWLINE_RE = re.compile(r"\n{3,}")


def _strip_html(html: str) -> str:
    """Strip HTML to plain text, preserving paragraph boundaries as newlines.

    Block-level closing tags (</p>, </div>, etc.) and <br> become newlines so
    that injection patterns starting a new paragraph are still caught at the
    beginning of a line by _detect_injection().
    """
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = _html_stdlib.unescape(text)
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = _EXCESS_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def _detect_injection(text: str) -> str:
    """Replace lines containing injection patterns with the redaction placeholder.

    Runs AFTER HTML-to-text conversion so that HTML-encoded injection is caught
    (e.g. <p>Ignore previous instructions</p> → "Ignore previous instructions"
    after stripping → caught here).
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _INJECTION_PREFIXES) or any(
            token in line for token in _INJECTION_TOKENS
        ):
            out.append(_REDACTED)
        else:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize(
    text: str,
    *,
    strip_html: bool = True,
    redact_pii: bool = False,
) -> str:
    """Sanitize email body text for safe LLM classification.

    Args:
        text: Raw email body. May be HTML (live path) or pre-extracted plain
              text (corpus path, from gmail.db body_text).
        strip_html: Strip HTML tags before injection detection. Use True for
                    the live path; False for the corpus path (already plain text).
        redact_pii: Replace email addresses with [EMAIL_REDACTED] and phone
                    numbers with [PHONE_REDACTED] before the model sees them.

    Returns:
        Sanitized, truncated body wrapped in [EMAIL_CONTENT]...[/EMAIL_CONTENT].
    """
    if not text or not text.strip():
        return "[EMAIL_CONTENT]\n[/EMAIL_CONTENT]"

    body = text

    if strip_html:
        body = _strip_html(body)

    body = _detect_injection(body)

    if redact_pii:
        body = _EMAIL_RE.sub("[EMAIL_REDACTED]", body)
        body = _PHONE_RE.sub("[PHONE_REDACTED]", body)

    if len(body) > _BODY_MAX_CHARS:
        body = body[:_BODY_MAX_CHARS]

    return f"[EMAIL_CONTENT]\n{body}\n[/EMAIL_CONTENT]"


def build_user_turn(
    from_addr: str,
    subject: str,
    date_str: str,
    sanitized_body: str,
) -> str:
    """Build the full user-turn message sent to the model.

    Headers are placed BEFORE [EMAIL_CONTENT] so they are always visible to the
    model even when the body is long and gets truncated. The 2000-char limit
    applies only to the body text inside sanitize().

    Args:
        from_addr:      Sender's email address or display name.
        subject:        Email subject line.
        date_str:       Date string from the email header.
        sanitized_body: Output of sanitize() — already contains the delimiters.
    """
    return f"From: {from_addr}\nSubject: {subject}\nDate: {date_str}\n\n{sanitized_body}"
