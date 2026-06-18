"""
Import a Gmail Takeout .mbox file into a SQLite database for querying
with Datasette.

Gmail Takeout packages mail as a standard Unix mbox file.  This script
streams through it message-by-message (never loading the whole file into
memory), extracts every header plus the plaintext body, and writes rows to
a SQLite database.

Usage
-----
One-time setup (creates .venv and installs deps):
    cd python/
    uv sync

Import (resumable — safe to Ctrl+C and re-run):
    uv run python mbox_import.py ~/Takeout/Mail/All\\ mail.mbox
    uv run python mbox_import.py ~/Takeout/Mail/All\\ mail.mbox --db ~/gmail.db

Import + build FTS full-text search index:
    uv run python mbox_import.py ~/Takeout/Mail/All\\ mail.mbox --build-fts

Explore with Datasette:
    uv run datasette serve gmail.db --metadata metadata.yaml --open

Notes
-----
- Body text is capped at 500 KB per message (--max-body-kb to adjust).
  HTML parts and attachments are not stored; attachment *filenames* are.
- The UNIQUE index on message_id means re-running or resuming is safe:
  duplicate messages are silently skipped.
- The FTS5 index (--build-fts) is built after all rows are inserted and
  lets you search with:  SELECT * FROM messages_fts WHERE messages_fts
  MATCH 'invoice receipt 2024'
"""

from __future__ import annotations

import argparse
import email
import email.header
import email.message
import email.policy
import email.utils
import json
import re
import sqlite3
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

try:
    from tqdm import tqdm as _tqdm

    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA cache_size   = -131072;   -- 128 MB page cache

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,

    -- RFC 5322 identity / threading
    message_id       TEXT,       -- Message-ID header; NULL if missing/malformed
    thread_id        TEXT,       -- X-Gmail-Thread-ID (Gmail's conversation key)
    in_reply_to      TEXT,       -- In-Reply-To header (parent message-id)
    references_hdr   TEXT,       -- References header (full ancestor chain)

    -- Date / time
    date_ts          INTEGER,    -- Unix timestamp parsed from Date header
    date_str         TEXT,       -- Date header value, verbatim
    year             INTEGER,    -- Calendar year (derived from date_ts)
    year_month       TEXT,       -- 'YYYY-MM' (derived; useful for faceting)

    -- Participants
    from_raw         TEXT,       -- Full From header value (name + addr)
    from_email       TEXT,       -- Extracted sender e-mail address (lowercase)
    from_name        TEXT,       -- Extracted sender display name
    to_raw           TEXT,       -- Full To header value
    cc_raw           TEXT,       -- Full Cc header value
    bcc_raw          TEXT,       -- Full Bcc header value
    reply_to         TEXT,       -- Reply-To header
    delivered_to     TEXT,       -- Delivered-To header (envelope destination)

    -- Content
    subject          TEXT,       -- Decoded Subject
    body_text        TEXT,       -- First text/plain part, decoded (≤ max_body_kb)
    body_size_bytes  INTEGER,    -- Raw mbox message size in bytes
    content_type     TEXT,       -- Top-level Content-Type
    has_attachments  INTEGER,    -- 1 if any attachment parts found
    attachment_names TEXT,       -- JSON array of attachment filenames

    -- Gmail-specific
    labels           TEXT,       -- JSON array from X-Gmail-Labels
    is_unread        INTEGER,    -- 1 if 'Unread' in labels
    is_starred       INTEGER,    -- 1 if 'Starred' in labels
    is_important     INTEGER,    -- 1 if 'Important' in labels
    in_inbox         INTEGER,    -- 1 if 'Inbox' in labels
    in_sent          INTEGER,    -- 1 if 'Sent' in labels
    in_trash         INTEGER,    -- 1 if 'Trash' in labels
    in_spam          INTEGER,    -- 1 if 'Spam' in labels
    x_received       TEXT,       -- Last X-Received header (routing trace)

    -- All headers as JSON for completeness (nothing is discarded)
    headers_json     TEXT,       -- {header-name: value-or-[values], ...}

    -- Import bookkeeping
    mbox_offset      INTEGER,    -- Byte offset in the mbox file
    mbox_size        INTEGER     -- Byte length of this raw message
);

-- Primary deduplication key.  Messages without a Message-ID are still stored
-- (the WHERE clause excludes them from the unique constraint so multiple
-- malformed/headerless messages can coexist).
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_message_id
    ON messages (message_id)
    WHERE message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_from_email  ON messages (from_email);
CREATE INDEX IF NOT EXISTS ix_date_ts     ON messages (date_ts);
CREATE INDEX IF NOT EXISTS ix_year_month  ON messages (year_month);
CREATE INDEX IF NOT EXISTS ix_thread_id   ON messages (thread_id);
CREATE INDEX IF NOT EXISTS ix_in_inbox    ON messages (in_inbox);
CREATE INDEX IF NOT EXISTS ix_in_sent     ON messages (in_sent);
CREATE INDEX IF NOT EXISTS ix_is_unread   ON messages (is_unread);
CREATE INDEX IF NOT EXISTS ix_is_starred  ON messages (is_starred);

-- Resumable import state.
CREATE TABLE IF NOT EXISTS import_progress (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mbox_path         TEXT    NOT NULL,
    mbox_size_bytes   INTEGER NOT NULL,
    last_offset       INTEGER NOT NULL DEFAULT 0,
    messages_imported INTEGER NOT NULL DEFAULT 0,
    started_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    completed_at      TEXT                         -- NULL while in-progress
);
"""

_FTS_CREATE = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject,
    from_email,
    from_name,
    body_text,
    content     = 'messages',
    content_rowid = 'id'
);
"""

_FTS_POPULATE = """
INSERT INTO messages_fts (rowid, subject, from_email, from_name, body_text)
    SELECT id, subject, from_email, from_name, body_text
    FROM   messages;
"""


# ---------------------------------------------------------------------------
# Streaming mbox parser
# ---------------------------------------------------------------------------

# An mbox envelope line looks like:  From foo@example.com Mon Jan 1 12:00:00 2024
# We match "From " followed by at least one non-space char to avoid matching
# ">From " (the escaped form used inside message bodies).
_ENVELOPE_RE = re.compile(rb"^From \S")


def iter_mbox(path: Path, start_offset: int = 0) -> Generator[tuple[int, int, bytes], None, None]:
    """
    Stream an mbox file, yielding ``(offset, size, raw_bytes)`` for each
    message.

    *offset* is the byte position of the message's ``From `` envelope line.
    *size* is the total byte length of the raw message (including envelope).
    These are suitable for storing in ``import_progress`` and for
    deterministic resume on re-run.

    Gmail Takeout produces standard Unix mbox files.  Each message is
    separated by a line beginning with ``From `` (the envelope).  Lines
    that begin with ``>From `` inside message bodies are the properly-escaped
    form and will not be mistaken for separators.
    """
    with open(path, "rb") as fh:
        if start_offset:
            fh.seek(start_offset)

        msg_start = start_offset
        buf: bytearray = bytearray()

        while True:
            line = fh.readline()
            if not line:
                break

            if _ENVELOPE_RE.match(line) and buf:
                yield msg_start, len(buf), bytes(buf)
                msg_start += len(buf)
                buf = bytearray()

            buf.extend(line)

        if buf:
            yield msg_start, len(buf), bytes(buf)


# ---------------------------------------------------------------------------
# Header / body helpers
# ---------------------------------------------------------------------------


def _decode_hdr(raw: str | None) -> str:
    """Decode an RFC 2047 encoded (or plain ASCII) header to a Unicode string."""
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
        chunks: list[str] = []
        for part, charset in parts:
            if isinstance(part, bytes):
                cs = charset or "utf-8"
                try:
                    chunks.append(part.decode(cs, errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    chunks.append(part.decode("latin-1", errors="replace"))
            else:
                chunks.append(part)
        return "".join(chunks).strip()
    except Exception:
        return (raw or "").strip()


def _parse_date(raw: str) -> tuple[int | None, str]:
    """Return ``(unix_timestamp_or_None, raw_string)``."""
    if not raw:
        return None, ""
    try:
        dt = email.utils.parsedate_to_datetime(raw.strip())
        return int(dt.timestamp()), raw
    except Exception:
        return None, raw


def _body_text(msg: email.message.Message, max_bytes: int) -> str:
    """
    Return the first text/plain body part, decoded and truncated to
    *max_bytes*.  Returns ``""`` if there is no plaintext part.
    """
    candidates: list[email.message.Message] = []

    if msg.is_multipart():
        for part in msg.walk():
            if (
                part.get_content_type() == "text/plain"
                and (part.get_content_disposition() or "").lower() != "attachment"
            ):
                candidates.append(part)
    elif msg.get_content_type() == "text/plain":
        candidates.append(msg)

    for part in candidates:
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        cs = part.get_content_charset() or "utf-8"
        try:
            return payload[:max_bytes].decode(cs, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return payload[:max_bytes].decode("latin-1", errors="replace")

    return ""


def _attachments(msg: email.message.Message) -> list[str]:
    """Return decoded filenames of all attachment parts."""
    names: list[str] = []
    for part in msg.walk():
        fname = part.get_filename()
        if fname:
            names.append(_decode_hdr(fname))
        elif (part.get_content_disposition() or "").lower() == "attachment":
            names.append(f"[{part.get_content_type()}]")
    return names


def _all_headers(msg: email.message.Message) -> dict:
    """
    Collect every header into ``{name: value}`` (or ``{name: [v1, v2]}`` for
    repeated headers).  Values are RFC 2047-decoded.
    """
    result: dict[str, object] = {}
    for key, val in msg.items():
        k = key.lower()
        v = _decode_hdr(val)
        if k in result:
            existing = result[k]
            if isinstance(existing, list):
                existing.append(v)
            else:
                result[k] = [existing, v]
        else:
            result[k] = v
    return result


def _parse_labels(raw: str) -> list[str]:
    """Parse ``X-Gmail-Labels`` into a list, e.g. ``'Inbox,Sent'`` → ``['Inbox', 'Sent']``."""
    return [lbl.strip() for lbl in raw.split(",") if lbl.strip()] if raw else []


# ---------------------------------------------------------------------------
# Parse a single raw message into a row dict
# ---------------------------------------------------------------------------


def parse_message(raw: bytes, offset: int, size: int, max_body_bytes: int) -> dict:
    """
    Parse one raw mbox message (starting with the ``From `` envelope line)
    into a dict suitable for insertion into the ``messages`` table.
    """
    # Strip the mbox envelope line before RFC 5322 parsing.
    nl = raw.find(b"\n")
    msg_bytes = raw[nl + 1 :] if nl != -1 and raw[:nl].rstrip().startswith(b"From ") else raw

    msg = email.message_from_bytes(msg_bytes)

    # Identity
    message_id = _decode_hdr(msg.get("Message-ID", "")).strip("<> ") or None
    thread_id = (msg.get("X-Gmail-Thread-ID") or msg.get("X-GM-THRID") or "").strip() or None
    in_reply_to = _decode_hdr(msg.get("In-Reply-To", "")).strip("<> ") or None
    references = _decode_hdr(msg.get("References", "")) or None

    # Date
    date_ts, date_str = _parse_date(msg.get("Date", ""))

    year = year_month = None
    if date_ts is not None:
        try:
            dt = datetime.fromtimestamp(date_ts, tz=UTC)
            year = dt.year
            year_month = dt.strftime("%Y-%m")
        except (OSError, OverflowError, ValueError):
            pass

    # Participants
    from_raw = _decode_hdr(msg.get("From", ""))
    _from_name, _from_addr = email.utils.parseaddr(from_raw)
    from_email: str | None = _from_addr.strip().lower() or None
    from_name: str | None = _from_name.strip() or None

    to_raw = _decode_hdr(msg.get("To", "")) or None
    cc_raw = _decode_hdr(msg.get("Cc", "")) or None
    bcc_raw = _decode_hdr(msg.get("Bcc", "")) or None
    reply_to = _decode_hdr(msg.get("Reply-To", "")) or None
    delivered_to = _decode_hdr(msg.get("Delivered-To", "")) or None
    x_received = _decode_hdr(msg.get("X-Received", "")) or None

    # Content
    subject = _decode_hdr(msg.get("Subject")) or None
    content_type = msg.get_content_type() or None
    body = _body_text(msg, max_body_bytes) or None
    attach_names = _attachments(msg)

    # Gmail labels
    labels_list = _parse_labels(msg.get("X-Gmail-Labels", ""))
    labels_set = {lbl.lower() for lbl in labels_list}

    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "references_hdr": references,
        "date_ts": date_ts,
        "date_str": date_str or None,
        "year": year,
        "year_month": year_month,
        "from_raw": from_raw or None,
        "from_email": from_email,
        "from_name": from_name,
        "to_raw": to_raw,
        "cc_raw": cc_raw,
        "bcc_raw": bcc_raw,
        "reply_to": reply_to,
        "delivered_to": delivered_to,
        "subject": subject,
        "body_text": body,
        "body_size_bytes": size,
        "content_type": content_type,
        "has_attachments": int(bool(attach_names)),
        "attachment_names": json.dumps(attach_names) if attach_names else None,
        "labels": json.dumps(labels_list) if labels_list else None,
        "is_unread": int("unread" in labels_set),
        "is_starred": int("starred" in labels_set),
        "is_important": int("important" in labels_set),
        "in_inbox": int("inbox" in labels_set),
        "in_sent": int("sent" in labels_set),
        "in_trash": int("trash" in labels_set),
        "in_spam": int("spam" in labels_set),
        "x_received": x_received,
        "headers_json": json.dumps(_all_headers(msg)),
        "mbox_offset": offset,
        "mbox_size": size,
    }


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_INSERT = """
INSERT OR IGNORE INTO messages (
    message_id, thread_id, in_reply_to, references_hdr,
    date_ts, date_str, year, year_month,
    from_raw, from_email, from_name,
    to_raw, cc_raw, bcc_raw, reply_to, delivered_to,
    subject, body_text, body_size_bytes, content_type,
    has_attachments, attachment_names,
    labels, is_unread, is_starred, is_important,
    in_inbox, in_sent, in_trash, in_spam,
    x_received, headers_json, mbox_offset, mbox_size
) VALUES (
    :message_id, :thread_id, :in_reply_to, :references_hdr,
    :date_ts, :date_str, :year, :year_month,
    :from_raw, :from_email, :from_name,
    :to_raw, :cc_raw, :bcc_raw, :reply_to, :delivered_to,
    :subject, :body_text, :body_size_bytes, :content_type,
    :has_attachments, :attachment_names,
    :labels, :is_unread, :is_starred, :is_important,
    :in_inbox, :in_sent, :in_trash, :in_spam,
    :x_received, :headers_json, :mbox_offset, :mbox_size
)
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _get_resume(conn: sqlite3.Connection, mbox_path: str, mbox_size: int) -> tuple[int, int]:
    """Return ``(last_offset, messages_imported)`` for an incomplete prior run."""
    row = conn.execute(
        """SELECT last_offset, messages_imported
           FROM   import_progress
           WHERE  mbox_path = ? AND mbox_size_bytes = ? AND completed_at IS NULL
           ORDER  BY id DESC LIMIT 1""",
        (mbox_path, mbox_size),
    ).fetchone()
    return (row[0], row[1]) if row else (0, 0)


def _save_progress(
    conn: sqlite3.Connection,
    mbox_path: str,
    mbox_size: int,
    last_offset: int,
    imported: int,
    completed: bool = False,
) -> None:
    now = _now()
    row = conn.execute(
        """SELECT id FROM import_progress
           WHERE  mbox_path = ? AND mbox_size_bytes = ? AND completed_at IS NULL
           ORDER  BY id DESC LIMIT 1""",
        (mbox_path, mbox_size),
    ).fetchone()

    if row:
        conn.execute(
            """UPDATE import_progress
               SET last_offset = ?, messages_imported = ?, updated_at = ?,
                   completed_at = CASE WHEN ? THEN ? ELSE completed_at END
               WHERE id = ?""",
            (last_offset, imported, now, completed, now if completed else None, row[0]),
        )
    else:
        conn.execute(
            """INSERT INTO import_progress
               (mbox_path, mbox_size_bytes, last_offset, messages_imported,
                started_at, updated_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mbox_path, mbox_size, last_offset, imported, now, now, now if completed else None),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Import a Gmail Takeout .mbox file into a SQLite database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
Basic import (creates ./gmail.db):
  uv run python mbox_import.py ~/Takeout/Mail/All\\ mail.mbox

Custom database path:
  uv run python mbox_import.py ~/Takeout/Mail/All\\ mail.mbox --db ~/gmail.db

Import + build FTS search index:
  uv run python mbox_import.py ~/Takeout/Mail/All\\ mail.mbox --build-fts

Drop and reimport from scratch:
  uv run python mbox_import.py ~/Takeout/Mail/All\\ mail.mbox --reset

Explore with Datasette (from the python/ directory):
  uv run datasette serve gmail.db --metadata metadata.yaml --open
        """,
    )
    ap.add_argument("mbox", type=Path, help="Path to the Gmail Takeout .mbox file")
    ap.add_argument(
        "--db",
        type=Path,
        default=Path("gmail.db"),
        help="Output SQLite database path (default: ./gmail.db)",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=500,
        metavar="N",
        help="Messages per SQLite commit (default: 500)",
    )
    ap.add_argument(
        "--max-body-kb",
        type=int,
        default=500,
        metavar="KB",
        help="Maximum plaintext body to store per message in KB (default: 500)",
    )
    ap.add_argument(
        "--build-fts", action="store_true", help="Build FTS5 full-text search index after importing"
    )
    ap.add_argument(
        "--reset",
        action="store_true",
        help="Delete the database and start a fresh import (ignores saved progress)",
    )
    args = ap.parse_args()

    mbox_path = args.mbox.expanduser().resolve()
    db_path = args.db.expanduser().resolve()
    max_body_bytes = args.max_body_kb * 1024

    if not mbox_path.exists():
        print(f"error: mbox file not found: {mbox_path}", file=sys.stderr)
        sys.exit(1)

    mbox_size = mbox_path.stat().st_size
    print(f"mbox:     {mbox_path}")
    print(f"          {mbox_size / 1_073_741_824:.2f} GB")
    print(f"database: {db_path}")

    if args.reset and db_path.exists():
        db_path.unlink()
        print("Database reset — starting fresh.")

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()

    start_offset, already = _get_resume(conn, str(mbox_path), mbox_size)
    if start_offset:
        pct = start_offset / mbox_size * 100
        print(
            f"Resuming from byte {start_offset:,} ({pct:.1f}% of file, "
            f"{already:,} messages already imported)."
        )
    else:
        print("Starting fresh import.")

    _save_progress(conn, str(mbox_path), mbox_size, start_offset, already)

    total = already
    batch: list[dict] = []
    last_end = start_offset  # byte position just past the last processed msg

    # Byte-based progress bar gives a meaningful percentage for a 9 GB file.
    if HAS_TQDM:
        pbar = _tqdm(
            total=mbox_size,
            initial=start_offset,
            unit="B",
            unit_scale=True,
            desc="Importing",
            dynamic_ncols=True,
        )
    else:
        pbar = None
        _milestone = max(1, mbox_size // 20)  # print every ~5 %

    try:
        for offset, size, raw in iter_mbox(mbox_path, start_offset):
            try:
                row = parse_message(raw, offset, size, max_body_bytes)
            except Exception as exc:
                print(
                    f"\nWarning: skipping malformed message at offset {offset}: {exc}",
                    file=sys.stderr,
                )
                last_end = offset + size
                if pbar:
                    pbar.update(size)
                continue

            batch.append(row)
            last_end = offset + size

            if pbar:
                pbar.update(size)
            elif (last_end - start_offset) % _milestone < size:
                pct = last_end / mbox_size * 100
                print(f"  {pct:.0f}% — {total + len(batch):,} messages...", flush=True)

            if len(batch) >= args.batch_size:
                conn.executemany(_INSERT, batch)
                conn.commit()
                total += len(batch)
                _save_progress(conn, str(mbox_path), mbox_size, last_end, total)
                batch.clear()

        # Flush the final partial batch.
        if batch:
            conn.executemany(_INSERT, batch)
            conn.commit()
            total += len(batch)
            batch.clear()

        _save_progress(conn, str(mbox_path), mbox_size, last_end, total, completed=True)

    except KeyboardInterrupt:
        if batch:
            conn.executemany(_INSERT, batch)
            conn.commit()
            total += len(batch)
        _save_progress(conn, str(mbox_path), mbox_size, last_end, total)
        if pbar:
            pbar.close()
        print(
            f"\nInterrupted at {total:,} messages.  Re-run the same command to resume.",
            file=sys.stderr,
        )
        sys.exit(130)

    if pbar:
        pbar.close()

    db_size = db_path.stat().st_size
    print(f"\nDone.  {total:,} messages → {db_path} ({db_size / 1_073_741_824:.2f} GB)")

    if args.build_fts:
        print("Building FTS5 index...", flush=True)
        conn.execute("DROP TABLE IF EXISTS messages_fts")
        conn.executescript(_FTS_CREATE)
        conn.execute(_FTS_POPULATE)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        print(f"FTS index built over {n:,} messages.")

    conn.close()

    print("\nTo explore, run from the python/ directory:")
    print(f"  uv run datasette serve {db_path} --metadata metadata.yaml --open")


if __name__ == "__main__":
    main()
