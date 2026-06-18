"""Tests for classify_corpus.py — voting, JSON parse, corrections dedup, startup check."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import classify_corpus
from classify_corpus import (
    _SCHEMA,
    compute_vote,
    load_corrections,
    run_corpus_classification,
    startup_check,
    write_correction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cls_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def make_gmail_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            message_id TEXT,
            from_email TEXT,
            from_name TEXT,
            subject TEXT,
            date_str TEXT,
            body_text TEXT,
            date_ts INTEGER
        )
    """)
    conn.execute("CREATE UNIQUE INDEX ux_mid ON messages(message_id) WHERE message_id IS NOT NULL")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "labels, expected",
    [
        (["keep", "keep", "keep"], "keep"),
        (["delete", "delete", "delete"], "delete"),
        (["keep", "delete", "keep"], "uncertain"),  # 2-1 split
        (["keep", "uncertain", "delete"], "uncertain"),  # 3-way disagree
        (["uncertain", "uncertain", "uncertain"], "uncertain"),
        (["keep", "keep", "delete"], "uncertain"),  # 2-1 split → uncertain
    ],
)
def test_compute_vote(labels: list[str], expected: str):
    assert compute_vote(labels) == expected


def test_compute_vote_single_model_keep():
    assert compute_vote(["keep"]) == "keep"


def test_compute_vote_single_model_delete():
    assert compute_vote(["delete"]) == "delete"


# ---------------------------------------------------------------------------
# JSON parse in classify_message
# ---------------------------------------------------------------------------


def make_mock_client(response_content: str) -> MagicMock:
    client = MagicMock()
    msg = MagicMock()
    msg.content = response_content
    client.chat.return_value.message = msg
    return client


def make_prompt() -> dict:
    return {"version": "v1.0.0", "system_prompt": "Classify the email."}


def make_message() -> dict:
    return {
        "message_id": "test123",
        "from_email": "alice@example.com",
        "from_name": "Alice",
        "subject": "Hello",
        "date_str": "Mon, 01 Jan 2024 00:00:00 +0000",
        "body_text": "Just checking in.",
    }


def test_classify_message_valid_json_keep():
    client = make_mock_client(
        '{"label": "keep", "confidence": 0.9, "reason": "Personal correspondence."}'
    )
    result = classify_corpus.classify_message(client, "qwen3.5:9b", make_prompt(), make_message())
    assert result["label"] == "keep"
    assert result["confidence"] == pytest.approx(0.9)
    assert result["reason"] == "Personal correspondence."
    assert result["error"] is None


def test_classify_message_valid_json_delete():
    client = make_mock_client('{"label": "delete", "confidence": 0.95, "reason": "Newsletter."}')
    result = classify_corpus.classify_message(client, "qwen3.5:9b", make_prompt(), make_message())
    assert result["label"] == "delete"
    assert result["error"] is None


def test_classify_message_invalid_json_returns_uncertain():
    client = make_mock_client("Sorry, I cannot classify this email. It appears to be...")
    result = classify_corpus.classify_message(client, "qwen3.5:9b", make_prompt(), make_message())
    assert result["label"] == "uncertain"
    assert result["confidence"] is None
    assert result["raw_response"] is not None
    assert result["error"] == "json_parse_error"


def test_classify_message_invalid_label_returns_uncertain():
    client = make_mock_client('{"label": "spam", "confidence": 0.8, "reason": "Spam."}')
    result = classify_corpus.classify_message(client, "qwen3.5:9b", make_prompt(), make_message())
    assert result["label"] == "uncertain"


def test_classify_message_ollama_error_returns_uncertain():
    client = MagicMock()
    client.chat.side_effect = ConnectionError("Ollama unreachable")
    result = classify_corpus.classify_message(client, "qwen3.5:9b", make_prompt(), make_message())
    assert result["label"] == "uncertain"
    assert result["raw_response"] is None
    assert "Ollama unreachable" in result["error"]


# ---------------------------------------------------------------------------
# load_corrections — dedup and malformed lines
# ---------------------------------------------------------------------------


def test_load_corrections_missing_file(tmp_path: Path):
    result = load_corrections(tmp_path / "does_not_exist.jsonl")
    assert result == {}


def test_load_corrections_empty_file(tmp_path: Path):
    path = tmp_path / "corrections.jsonl"
    path.write_text("")
    assert load_corrections(path) == {}


def test_load_corrections_single_entry(tmp_path: Path):
    path = tmp_path / "corrections.jsonl"
    entry = {
        "message_id": "msg1",
        "human_label": "keep",
        "prompt_version": "v1.0.0",
        "model_labels": {},
        "corrected_at": "2026-06-18T12:00:00+00:00",
    }
    path.write_text(json.dumps(entry) + "\n")
    result = load_corrections(path)
    assert ("msg1", "v1.0.0") in result
    assert result[("msg1", "v1.0.0")]["human_label"] == "keep"


def test_load_corrections_dedup_latest_wins(tmp_path: Path):
    path = tmp_path / "corrections.jsonl"
    entries = [
        {
            "message_id": "msg1",
            "human_label": "delete",
            "prompt_version": "v1.0.0",
            "model_labels": {},
            "corrected_at": "2026-06-18T10:00:00+00:00",
        },
        {
            "message_id": "msg1",
            "human_label": "keep",
            "prompt_version": "v1.0.0",
            "model_labels": {},
            "corrected_at": "2026-06-18T12:00:00+00:00",
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    result = load_corrections(path)
    assert result[("msg1", "v1.0.0")]["human_label"] == "keep"


def test_load_corrections_different_versions_separate(tmp_path: Path):
    path = tmp_path / "corrections.jsonl"
    entries = [
        {
            "message_id": "msg1",
            "human_label": "delete",
            "prompt_version": "v1.0.0",
            "model_labels": {},
            "corrected_at": "2026-06-18T10:00:00+00:00",
        },
        {
            "message_id": "msg1",
            "human_label": "keep",
            "prompt_version": "v1.1.0",
            "model_labels": {},
            "corrected_at": "2026-06-18T11:00:00+00:00",
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    result = load_corrections(path)
    assert result[("msg1", "v1.0.0")]["human_label"] == "delete"
    assert result[("msg1", "v1.1.0")]["human_label"] == "keep"


def test_load_corrections_malformed_line_skipped(tmp_path: Path, capsys):
    path = tmp_path / "corrections.jsonl"
    good = {
        "message_id": "msg2",
        "human_label": "keep",
        "prompt_version": "v1.0.0",
        "model_labels": {},
        "corrected_at": "2026-06-18T12:00:00+00:00",
    }
    path.write_text("NOT VALID JSON\n" + json.dumps(good) + "\n")
    result = load_corrections(path)
    assert ("msg2", "v1.0.0") in result
    captured = capsys.readouterr()
    assert "malformed" in captured.err


def test_write_correction_appends(tmp_path: Path):
    path = tmp_path / "corrections.jsonl"
    entry1 = {
        "message_id": "a",
        "human_label": "keep",
        "prompt_version": "v1.0.0",
        "model_labels": {},
        "corrected_at": "2026-06-18T10:00:00+00:00",
    }
    entry2 = {
        "message_id": "b",
        "human_label": "delete",
        "prompt_version": "v1.0.0",
        "model_labels": {},
        "corrected_at": "2026-06-18T11:00:00+00:00",
    }
    write_correction(path, entry1)
    write_correction(path, entry2)
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message_id"] == "a"
    assert json.loads(lines[1])["message_id"] == "b"


# ---------------------------------------------------------------------------
# NULL message_id filtering
# ---------------------------------------------------------------------------


def test_null_message_id_excluded_from_corpus(tmp_path: Path):
    gmail_path = tmp_path / "gmail.db"
    conn = make_gmail_db(gmail_path)
    conn.execute(
        "INSERT INTO messages (message_id, from_email, subject, body_text, date_ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (None, "anon@example.com", "No ID", "Body.", 1000000),
    )
    conn.execute(
        "INSERT INTO messages (message_id, from_email, subject, body_text, date_ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("real_id@example.com", "bob@example.com", "Real", "Body.", 1000001),
    )
    conn.commit()

    rows = conn.execute("SELECT message_id FROM messages WHERE message_id IS NOT NULL").fetchall()
    ids = [r[0] for r in rows]
    assert "real_id@example.com" in ids
    assert None not in ids
    conn.close()


# ---------------------------------------------------------------------------
# Startup check
# ---------------------------------------------------------------------------


def test_startup_check_daemon_unreachable(capsys):
    client = MagicMock()
    client.list.side_effect = ConnectionError("Connection refused")
    with pytest.raises(SystemExit) as exc_info:
        startup_check(client, ["qwen3.5:9b"], "http://192.168.1.100:11434")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Cannot reach Ollama" in captured.err
    assert "not running" in captured.err  # hint for connection refused


def test_startup_check_dns_failure_hint(capsys):
    client = MagicMock()
    client.list.side_effect = ConnectionError("Name or service not known")
    with pytest.raises(SystemExit) as exc_info:
        startup_check(client, ["qwen3.5:9b"], "http://badhost:11434")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "hostname not found" in captured.err


def test_startup_check_model_missing(capsys):
    client = MagicMock()
    model_obj = MagicMock()
    model_obj.model = "gemma4:e4b"
    client.list.return_value.models = [model_obj]
    with pytest.raises(SystemExit) as exc_info:
        startup_check(client, ["gemma4:e4b", "qwen3.5:9b"], "http://localhost:11434")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "qwen3.5:9b" in captured.err
    assert "ollama pull" in captured.err


def test_startup_check_all_models_present():
    client = MagicMock()
    models_list = []
    for tag in ["qwen3.5:9b", "gemma4:e4b", "mistral-small3.2:latest"]:
        m = MagicMock()
        m.model = tag
        models_list.append(m)
    client.list.return_value.models = models_list
    # Should not raise
    startup_check(
        client, ["qwen3.5:9b", "gemma4:e4b", "mistral-small3.2:latest"], "http://localhost:11434"
    )


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def make_args(tmp_path: Path, models: list[str] | None = None) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.db = tmp_path / "gmail.db"
    ns.classifications_db = tmp_path / "classifications.db"
    ns.models = models or ["qwen3.5:9b"]
    ns.stratified_sample = 0
    ns.prompt_version = "v1.0.0"
    ns.prompts_dir = Path(__file__).parent.parent / "prompts"
    return ns


def make_failing_client(error_msg: str = "Connection refused") -> MagicMock:
    client = MagicMock()
    client.chat.side_effect = ConnectionError(error_msg)
    return client


def test_circuit_breaker_aborts_after_threshold(tmp_path: Path, capsys):
    """After CIRCUIT_BREAKER_THRESHOLD consecutive hard errors, the run aborts."""
    gmail_path = tmp_path / "gmail.db"
    conn = make_gmail_db(gmail_path)
    for i in range(10):
        conn.execute(
            "INSERT INTO messages (message_id, from_email, subject, body_text, date_ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"msg{i}", f"sender{i}@example.com", f"Subject {i}", "Body.", 1000000 + i),
        )
    conn.commit()

    cls_path = tmp_path / "classifications.db"
    cls_conn = make_cls_db(cls_path)

    args = make_args(tmp_path)
    prompt = {"version": "v1.0.0", "system_prompt": "Classify."}
    client = make_failing_client("Connection refused")

    with pytest.raises(SystemExit) as exc_info:
        run_corpus_classification(args, client, conn, cls_conn, prompt)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "consecutive Ollama errors" in captured.err
    assert "aborting" in captured.err


def test_circuit_breaker_resets_on_success(tmp_path: Path, capsys):
    """Successful responses reset the consecutive error counter."""
    gmail_path = tmp_path / "gmail.db"
    conn = make_gmail_db(gmail_path)
    for i in range(3):
        conn.execute(
            "INSERT INTO messages (message_id, from_email, subject, body_text, date_ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"msg{i}", f"sender{i}@example.com", f"Subject {i}", "Body.", 1000000 + i),
        )
    conn.commit()

    cls_path = tmp_path / "classifications.db"
    cls_conn = make_cls_db(cls_path)

    args = make_args(tmp_path)
    prompt = {"version": "v1.0.0", "system_prompt": "Classify."}

    # Client: fail twice, then succeed — should NOT trigger circuit breaker
    client = MagicMock()
    ok_msg = MagicMock()
    ok_msg.content = '{"label": "keep", "confidence": 0.9, "reason": "ok"}'
    client.chat.side_effect = [
        ConnectionError("refused"),
        ConnectionError("refused"),
        MagicMock(message=ok_msg),
    ]

    # Should complete without SystemExit
    run_corpus_classification(args, client, conn, cls_conn, prompt)
    captured = capsys.readouterr()
    assert "consecutive Ollama errors" not in captured.err


def test_circuit_breaker_json_errors_do_not_count(tmp_path: Path, capsys):
    """JSON parse errors (model quality issues) do not trigger the circuit breaker."""
    gmail_path = tmp_path / "gmail.db"
    conn = make_gmail_db(gmail_path)
    for i in range(10):
        conn.execute(
            "INSERT INTO messages (message_id, from_email, subject, body_text, date_ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"msg{i}", f"sender{i}@example.com", f"Subject {i}", "Body.", 1000000 + i),
        )
    conn.commit()

    cls_path = tmp_path / "classifications.db"
    cls_conn = make_cls_db(cls_path)

    args = make_args(tmp_path)
    prompt = {"version": "v1.0.0", "system_prompt": "Classify."}

    # Model returns non-JSON (parse error) — should be classified as uncertain but NOT abort
    bad_msg = MagicMock()
    bad_msg.content = "Sorry, I cannot classify this."
    client = MagicMock()
    client.chat.return_value = MagicMock(message=bad_msg)

    run_corpus_classification(args, client, conn, cls_conn, prompt)
    captured = capsys.readouterr()
    assert "consecutive Ollama errors" not in captured.err


# ---------------------------------------------------------------------------
# classifications.db schema
# ---------------------------------------------------------------------------


def test_schema_creates_classifications_table(tmp_path: Path):
    conn = make_cls_db(tmp_path / "c.db")
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "classifications" in tables
    conn.close()


def test_schema_check_constraint_rejects_invalid_label(tmp_path: Path):
    conn = make_cls_db(tmp_path / "c.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO classifications (message_id, model, prompt_version, label, classified_at) "
            "VALUES ('x', 'qwen3:7b', 'v1.0.0', 'spam', '2026-01-01')"
        )
    conn.close()
