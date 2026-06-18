"""Smoke tests for mbox_import helpers."""

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import mbox_import


def test_decode_hdr_plain():
    assert mbox_import._decode_hdr("Hello World") == "Hello World"


def test_decode_hdr_encoded():
    assert mbox_import._decode_hdr("=?utf-8?b?SGVsbG8gV29ybGQ=?=") == "Hello World"


def test_decode_hdr_none():
    assert mbox_import._decode_hdr(None) == ""


def test_parse_labels_basic():
    assert mbox_import._parse_labels("Inbox,Sent") == ["Inbox", "Sent"]


def test_parse_labels_whitespace():
    assert mbox_import._parse_labels("Inbox, Sent, Unread") == ["Inbox", "Sent", "Unread"]


def test_parse_labels_empty():
    assert mbox_import._parse_labels("") == []


def test_parse_message_minimal():
    raw = (
        b"From test@example.com Mon Jan 01 00:00:00 2024\n"
        b"From: Alice <alice@example.com>\n"
        b"Subject: Hello\n"
        b"Message-ID: <abc123@example.com>\n"
        b"\n"
        b"Body text here.\n"
    )
    row = mbox_import.parse_message(raw, offset=0, size=len(raw), max_body_bytes=512 * 1024)
    assert row["from_email"] == "alice@example.com"
    assert row["subject"] == "Hello"
    assert row["message_id"] == "abc123@example.com"
    assert row["body_text"] is not None
    assert "Body text" in row["body_text"]


def test_parse_message_gmail_labels():
    raw = (
        b"From test@example.com Mon Jan 01 00:00:00 2024\n"
        b"From: bob@example.com\n"
        b"Subject: Test\n"
        b"X-Gmail-Labels: Inbox,Unread,Important\n"
        b"\n"
    )
    row = mbox_import.parse_message(raw, offset=0, size=len(raw), max_body_bytes=1024)
    assert row["in_inbox"] == 1
    assert row["is_unread"] == 1
    assert row["is_important"] == 1
    assert row["is_starred"] == 0


def test_iter_mbox_two_messages():
    content = (
        b"From a@example.com Mon Jan 01 00:00:00 2024\n"
        b"Subject: First\n"
        b"\n"
        b"First body.\n"
        b"From b@example.com Mon Jan 02 00:00:00 2024\n"
        b"Subject: Second\n"
        b"\n"
        b"Second body.\n"
    )
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mbox") as f:
        f.write(content)
        path = Path(f.name)
    try:
        messages = list(mbox_import.iter_mbox(path))
        assert len(messages) == 2
        assert messages[0][0] == 0  # first message starts at offset 0
    finally:
        path.unlink()


def test_schema_creates_tables():
    conn = sqlite3.connect(":memory:")
    conn.executescript(mbox_import._SCHEMA)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "messages" in tables
    assert "import_progress" in tables
    conn.close()


def test_cli_help():
    script = Path(__file__).parent.parent / "mbox_import.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "mbox" in result.stdout
