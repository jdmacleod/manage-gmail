"""
Classify live Gmail inbox messages using Ollama and apply AI/* labels via gws.

Requires:
  - gws authenticated (see ../SETUP.md)
  - Ollama running (set OLLAMA_HOST for remote instance)
  - classifications.db populated by classify_corpus.py (Stage 2-3)
  - corrections.jsonl with human-labeled examples (Stage 4)
  - Prompt version >= v1.1.0 with <5% error rate on corrections.jsonl

Safety model:
  - Contact exclusion: messages from contacts_emails.txt are NEVER classified
  - Reference label exclusion: messages carrying the "Reference" label are skipped
  - Thread-reply exclusion: any message in a thread you've replied to is skipped
  - Output is labels (AI/Keep, AI/Delete, AI/Uncertain), NEVER permanent deletion
  - Checkpoint: audit_log.jsonl is written BEFORE each gws label call (SIGTERM safe)
  - Resume: already-checkpointed message IDs are skipped on re-run

Deployment gate (enforced at startup):
  - Prompt version must be >= v1.1.0
  - Error rate on corrections.jsonl must be < 5%
  (Override with --skip-gate for development / dry-run testing)

Usage:
  export OLLAMA_HOST=http://192.168.1.100:11434   # if Ollama is on another host
  uv run python classify_live.py --dry-run         # preview, no gws calls
  uv run python classify_live.py                   # live classification
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import ollama

from classify_corpus import (
    DEFAULT_MODELS,
    classify_message,
    compute_vote,
    load_corrections,
    load_prompt,
    startup_check,
)

# ---------------------------------------------------------------------------
# Paths relative to repo root (../  from python/)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.parent
_CONTACTS_FILE = _REPO_ROOT / "contacts_emails.txt"
_AUDIT_LOG = _REPO_ROOT / "audit_log.jsonl"

# ---------------------------------------------------------------------------
# AI labels applied to messages
# ---------------------------------------------------------------------------

AI_LABELS = ("AI/Keep", "AI/Delete", "AI/Uncertain")

# ---------------------------------------------------------------------------
# Deployment gate
# ---------------------------------------------------------------------------

_MIN_PROMPT_VERSION = "v1.1.0"
_MAX_ERROR_RATE = 0.05


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.lstrip("v").split("."))


def check_deployment_gate(
    prompt_version: str,
    corrections_path: Path,
    classifications_db: Path,
    models: list[str],
) -> None:
    """Abort if the deployment gate is not met.

    Gate conditions:
      1. prompt_version >= v1.1.0
      2. Error rate on corrections.jsonl < 5% (compared against classifications.db)
    """
    if _version_tuple(prompt_version) < _version_tuple(_MIN_PROMPT_VERSION):
        print(
            f"ERROR: Deployment gate — prompt version {prompt_version} is below "
            f"the minimum {_MIN_PROMPT_VERSION}.\n"
            "  Complete the human feedback loop (Stage 4) and bump the prompt version.",
            file=sys.stderr,
        )
        sys.exit(1)

    corrections = load_corrections(corrections_path)
    if not corrections:
        print(
            "WARNING: No corrections.jsonl found — skipping error rate gate.\n"
            "  Run `classify_corpus.py --review-uncertain` to build a correction set.",
            file=sys.stderr,
        )
        return

    correct = 0
    total = 0
    if classifications_db.exists():
        conn = sqlite3.connect(classifications_db)
        for (msg_id, pv), corr in corrections.items():
            if pv != prompt_version:
                continue
            rows = conn.execute(
                "SELECT model, label FROM classifications WHERE message_id=? AND prompt_version=?",
                (msg_id, pv),
            ).fetchall()
            if not rows:
                continue
            model_labels = {r[0]: r[1] for r in rows}
            vote = compute_vote(list(model_labels.values()))
            total += 1
            if vote == corr["human_label"]:
                correct += 1
        conn.close()

    if total == 0:
        print(
            f"WARNING: No corrections for prompt version {prompt_version} found in "
            "classifications.db — cannot measure error rate. Proceeding.",
            file=sys.stderr,
        )
        return

    error_rate = 1.0 - (correct / total)
    print(
        f"Deployment gate: {correct}/{total} correct on corrections.jsonl "
        f"(error rate {error_rate:.1%}, threshold {_MAX_ERROR_RATE:.0%})"
    )
    if error_rate >= _MAX_ERROR_RATE:
        print(
            f"ERROR: Error rate {error_rate:.1%} exceeds {_MAX_ERROR_RATE:.0%} threshold.\n"
            "  Improve the prompt and re-run classify_corpus.py before live deployment.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Contact/label/thread exclusions (ported from scripts/prune.sh)
# ---------------------------------------------------------------------------


def load_contacts(contacts_file: Path) -> set[str]:
    """Return the set of lowercase contact email addresses."""
    if not contacts_file.exists():
        return set()
    return {
        line.strip().lower()
        for line in contacts_file.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def _gws_json(args: list[str]) -> dict:
    """Run a gws command and return parsed JSON output. Raises on error."""
    result = subprocess.run(
        ["gws"] + args + ["--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(json.loads(result.stdout))


def get_or_create_label_id(label_name: str) -> str:
    """Python equivalent of lib.sh's get_or_create_label_id.

    Returns the Gmail label ID for label_name, creating it if it doesn't exist.
    """
    labels_data = _gws_json(["gmail", "users", "labels", "list", "--params", '{"userId":"me"}'])
    for lbl in labels_data.get("labels", []):
        if lbl.get("name") == label_name:
            return str(lbl["id"])

    # Create the label
    new_label = _gws_json(
        [
            "gmail",
            "users",
            "labels",
            "create",
            "--params",
            '{"userId":"me"}',
            "--json",
            json.dumps(
                {
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                }
            ),
        ]
    )
    return str(new_label["id"])


def has_reference_label(msg_labels: list[str]) -> bool:
    """Return True if the message carries the 'Reference' label."""
    return any(lbl in ("Reference", "Label_Reference") for lbl in msg_labels)


def sender_replied_in_thread(thread_id: str, user_email: str) -> bool:
    """Return True if the user has sent a message in this thread.

    Calls gws gmail users threads get to inspect all messages in the thread.
    """
    try:
        thread_data = _gws_json(
            [
                "gmail",
                "users",
                "threads",
                "get",
                "--params",
                json.dumps({"userId": "me", "id": thread_id}),
            ]
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return False

    for msg in thread_data.get("messages", []):
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        from_addr = headers.get("from", "").lower()
        if user_email.lower() in from_addr:
            return True
    return False


# ---------------------------------------------------------------------------
# Audit log checkpoint
# ---------------------------------------------------------------------------


def load_checkpointed_ids(audit_log: Path) -> set[str]:
    """Return message IDs already checkpointed in audit_log.jsonl with category=ai_classify."""
    if not audit_log.exists():
        return set()
    ids: set[str] = set()
    with audit_log.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("category") == "ai_classify" and entry.get("message_id"):
                ids.add(entry["message_id"])
    return ids


def write_checkpoint(audit_log: Path, message_id: str, label: str, prompt_version: str) -> None:
    """Append a checkpoint entry to audit_log.jsonl BEFORE the gws call."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "category": "ai_classify",
        "message_id": message_id,
        "label": label,
        "prompt_version": prompt_version,
    }
    with audit_log.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Main live classification loop
# ---------------------------------------------------------------------------


def run_live_classification(
    args: argparse.Namespace,
    client: ollama.Client,
    prompt: dict,
    label_ids: dict[str, str],
    contacts: set[str],
    checkpointed: set[str],
) -> None:
    """Classify live inbox messages and apply AI/* labels via gws."""
    user_email = os.environ.get("GMAIL_USER", "me")

    # List inbox messages
    try:
        list_data = _gws_json(
            [
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps({"userId": "me", "labelIds": ["INBOX"], "maxResults": 500}),
            ]
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"ERROR: Failed to list inbox messages: {exc}", file=sys.stderr)
        sys.exit(1)

    messages = list_data.get("messages", [])
    if not messages:
        print("Inbox is empty or no messages returned.")
        return

    print(f"Inbox messages: {len(messages)}")
    classified = 0
    skipped = 0

    for msg_meta in messages:
        msg_id = msg_meta.get("id")
        if not msg_id:
            continue

        # Skip already-checkpointed
        if msg_id in checkpointed:
            skipped += 1
            continue

        # Fetch full message metadata
        try:
            msg_data = _gws_json(
                [
                    "gmail",
                    "users",
                    "messages",
                    "get",
                    "--params",
                    json.dumps({"userId": "me", "id": msg_id, "format": "full"}),
                ]
            )
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            skipped += 1
            continue

        payload = msg_data.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        from_raw = headers.get("from", "")
        subject = headers.get("subject", "(no subject)")
        date_str = headers.get("date", "")
        thread_id = msg_data.get("threadId", "")
        msg_label_ids = msg_data.get("labelIds", [])

        # Extract sender email
        import re

        m = re.search(r"[^<\s]+@[^>\s]+", from_raw)
        from_email = m.group(0).strip("<>").lower() if m else from_raw.lower()

        # --- Exclusion checks ---

        if from_email in contacts:
            skipped += 1
            continue

        if has_reference_label(msg_label_ids):
            skipped += 1
            continue

        if thread_id and sender_replied_in_thread(thread_id, user_email):
            skipped += 1
            continue

        # --- Extract body (HTML from gws) ---
        body_html = ""
        parts = payload.get("parts", [])
        if parts:
            for part in parts:
                if part.get("mimeType") == "text/html":
                    import base64

                    data = part.get("body", {}).get("data", "")
                    if data:
                        body_html = base64.urlsafe_b64decode(data + "==").decode(
                            "utf-8", errors="replace"
                        )
                    break
                if part.get("mimeType") == "text/plain" and not body_html:
                    import base64

                    data = part.get("body", {}).get("data", "")
                    if data:
                        body_html = base64.urlsafe_b64decode(data + "==").decode(
                            "utf-8", errors="replace"
                        )
        else:
            body_data = payload.get("body", {}).get("data", "")
            if body_data:
                import base64

                body_html = base64.urlsafe_b64decode(body_data + "==").decode(
                    "utf-8", errors="replace"
                )

        message_dict = {
            "message_id": msg_id,
            "from_email": from_email,
            "from_name": "",
            "subject": subject,
            "date_str": date_str,
            "body_text": body_html,
        }

        # --- Classify with all models ---
        model_labels: list[str] = []
        for i, model in enumerate(args.models):
            is_last = i == len(args.models) - 1
            ka = 0 if is_last else -1
            result = classify_message(client, model, prompt, message_dict, keep_alive=ka)
            model_labels.append(result["label"])

        vote = compute_vote(model_labels)

        # Map vote to Gmail AI label
        label_map = {"keep": "AI/Keep", "delete": "AI/Delete", "uncertain": "AI/Uncertain"}
        ai_label = label_map[vote]
        label_id = label_ids[ai_label]

        if args.dry_run:
            print(
                f"  [DRY-RUN] {msg_id} → {ai_label}  (from: {from_email}, subject: {subject[:50]})"
            )
            classified += 1
            continue

        # Checkpoint BEFORE the gws call
        write_checkpoint(_AUDIT_LOG, msg_id, ai_label, args.prompt_version)

        # Apply label via gws
        try:
            subprocess.run(
                [
                    "gws",
                    "gmail",
                    "users",
                    "messages",
                    "modify",
                    "--params",
                    json.dumps({"userId": "me", "id": msg_id}),
                    "--json",
                    json.dumps({"addLabelIds": [label_id]}),
                    "--format",
                    "json",
                ],
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(f"  ERROR applying label to {msg_id}: {exc}", file=sys.stderr)
            continue

        classified += 1
        if classified % 10 == 0:
            print(f"  {classified} classified ...", flush=True)

    print(f"\nDone. {classified} classified, {skipped} skipped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    here = Path(__file__).parent
    default_prompts_dir = here / "prompts"

    parser = argparse.ArgumentParser(
        description="Classify live Gmail inbox with Ollama and apply AI/* labels.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=here / "gmail.db",
        help="Path to gmail.db (for co-located corrections.jsonl / classifications.db)",
    )
    parser.add_argument(
        "--classifications-db",
        dest="classifications_db",
        type=Path,
        default=None,
        help="Path to classifications.db (default: co-located with --db)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS,
        help=f"Ollama model tags (default: {DEFAULT_MODELS})",
    )
    parser.add_argument(
        "--prompt-version",
        default="v1.1.0",
        help="Prompt version to use (must be >= v1.1.0 for live deployment, default: v1.1.0)",
    )
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        default=default_prompts_dir,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only — show what labels would be applied, no gws calls made",
    )
    parser.add_argument(
        "--skip-gate",
        action="store_true",
        help="Skip the deployment gate check (for development/dry-run testing)",
    )

    args = parser.parse_args()

    args.db = args.db.resolve()
    if args.classifications_db is None:
        args.classifications_db = args.db.parent / "classifications.db"

    corrections_path = args.db.parent / "corrections.jsonl"

    # Load prompt
    prompt = load_prompt(args.prompt_version, args.prompts_dir)

    # Ollama client
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    print(f"Ollama endpoint: {host}", flush=True)
    client = ollama.Client(host=host)

    # Startup checks
    startup_check(client, args.models, host)

    # Deployment gate
    if not args.skip_gate and not args.dry_run:
        check_deployment_gate(
            args.prompt_version,
            corrections_path,
            args.classifications_db,
            args.models,
        )

    # Contacts
    contacts = load_contacts(_CONTACTS_FILE)
    if contacts:
        print(f"Contact protection: ON ({len(contacts)} addresses)")
    else:
        print("Contact protection: OFF — run scripts/export_contacts.sh to enable")

    # Resolve AI label IDs at startup (fail fast if gws is not working)
    print("Resolving AI label IDs ...", flush=True)
    label_ids: dict[str, str] = {}
    if not args.dry_run:
        for lbl in AI_LABELS:
            label_ids[lbl] = get_or_create_label_id(lbl)
            print(f"  {lbl} → {label_ids[lbl]}")
    else:
        label_ids = {lbl: f"[dry-run:{lbl}]" for lbl in AI_LABELS}

    # Load checkpoint (already-processed message IDs)
    checkpointed = load_checkpointed_ids(_AUDIT_LOG)
    if checkpointed:
        print(f"Resuming: {len(checkpointed)} message(s) already checkpointed — will skip.")

    run_live_classification(args, client, prompt, label_ids, contacts, checkpointed)


if __name__ == "__main__":
    main()
