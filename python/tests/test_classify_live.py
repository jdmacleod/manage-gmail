"""Tests for classify_live.py — label resolution, exclusions, checkpoint."""

from __future__ import annotations

import json
from pathlib import Path

import classify_live
from classify_live import (
    has_reference_label,
    load_checkpointed_ids,
    load_contacts,
    write_checkpoint,
)

# ---------------------------------------------------------------------------
# Contact loading
# ---------------------------------------------------------------------------


def test_load_contacts_missing_file(tmp_path: Path):
    result = load_contacts(tmp_path / "no_contacts.txt")
    assert result == set()


def test_load_contacts_normalises_case(tmp_path: Path):
    path = tmp_path / "contacts.txt"
    path.write_text("Alice@Example.COM\nbob@example.org\n")
    result = load_contacts(path)
    assert "alice@example.com" in result
    assert "bob@example.org" in result


def test_load_contacts_skips_blank_lines(tmp_path: Path):
    path = tmp_path / "contacts.txt"
    path.write_text("alice@example.com\n\nbob@example.org\n")
    assert len(load_contacts(path)) == 2


# ---------------------------------------------------------------------------
# Reference label check
# ---------------------------------------------------------------------------


def test_has_reference_label_true():
    assert has_reference_label(["INBOX", "Reference", "UNREAD"]) is True


def test_has_reference_label_false():
    assert has_reference_label(["INBOX", "UNREAD"]) is False


def test_has_reference_label_empty():
    assert has_reference_label([]) is False


# ---------------------------------------------------------------------------
# Audit log checkpoint
# ---------------------------------------------------------------------------


def test_load_checkpointed_ids_missing_file(tmp_path: Path):
    result = load_checkpointed_ids(tmp_path / "audit.jsonl")
    assert result == set()


def test_write_and_load_checkpoint(tmp_path: Path):
    log = tmp_path / "audit_log.jsonl"
    write_checkpoint(log, "msg123", "AI/Keep", "v1.1.0")
    ids = load_checkpointed_ids(log)
    assert "msg123" in ids


def test_checkpoint_other_categories_not_loaded(tmp_path: Path):
    """Only ai_classify category entries should be in the checkpoint set."""
    log = tmp_path / "audit_log.jsonl"
    # Write a prune.sh-style entry (different category)
    with log.open("a") as f:
        f.write(json.dumps({"ts": "...", "category": "prune", "message_id": "msg_pruned"}) + "\n")
    write_checkpoint(log, "msg_ai", "AI/Delete", "v1.1.0")
    ids = load_checkpointed_ids(log)
    assert "msg_ai" in ids
    assert "msg_pruned" not in ids


def test_checkpoint_malformed_line_skipped(tmp_path: Path):
    log = tmp_path / "audit_log.jsonl"
    with log.open("a") as f:
        f.write("NOT JSON\n")
    write_checkpoint(log, "msg_good", "AI/Uncertain", "v1.1.0")
    ids = load_checkpointed_ids(log)
    assert "msg_good" in ids


def test_checkpoint_resumability(tmp_path: Path):
    """Second run skips already-checkpointed IDs."""
    log = tmp_path / "audit_log.jsonl"
    write_checkpoint(log, "msg_done", "AI/Keep", "v1.1.0")
    write_checkpoint(log, "msg_also_done", "AI/Delete", "v1.1.0")
    ids = load_checkpointed_ids(log)
    assert len(ids) == 2
    assert "msg_done" in ids
    assert "msg_also_done" in ids


# ---------------------------------------------------------------------------
# Label resolution (get_or_create_label_id)
# ---------------------------------------------------------------------------


def test_get_or_create_label_id_existing(monkeypatch):
    """Label exists → returns existing ID without creating."""
    labels_response = {
        "labels": [
            {"id": "Label_1", "name": "AI/Keep"},
            {"id": "Label_2", "name": "AI/Delete"},
        ]
    }

    def mock_gws_json(args):
        if "list" in args:
            return labels_response
        raise AssertionError("Should not call create")

    monkeypatch.setattr(classify_live, "_gws_json", mock_gws_json)
    result = classify_live.get_or_create_label_id("AI/Keep")
    assert result == "Label_1"


def test_get_or_create_label_id_missing(monkeypatch):
    """Label not present → creates it and returns new ID."""
    created = {"id": "Label_99", "name": "AI/Uncertain"}
    call_log = []

    def mock_gws_json(args):
        call_log.append(args)
        if "list" in args:
            return {"labels": []}  # no existing labels
        if "create" in args:
            return created
        raise AssertionError(f"Unexpected gws call: {args}")

    monkeypatch.setattr(classify_live, "_gws_json", mock_gws_json)
    result = classify_live.get_or_create_label_id("AI/Uncertain")
    assert result == "Label_99"
    assert any("create" in c for c in call_log)
