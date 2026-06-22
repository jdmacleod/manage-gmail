"""
Classify Gmail corpus messages using Ollama LLM models.

Reads from gmail.db (populated by mbox_import.py), classifies messages with one
or more Ollama models, and writes results to classifications.db.  Human
corrections are captured via --review-uncertain and stored in corrections.jsonl.

Stage 2: Single-model baseline
  uv run python classify_corpus.py --db ~/gmail.db --models qwen2.5:14b

Stage 3: Three-model adversarial run (batch-by-model for efficiency)
  uv run python classify_corpus.py --db ~/gmail.db

Stage 4: Review uncertain cases and capture corrections
  uv run python classify_corpus.py --db ~/gmail.db --review-uncertain

Stratified 200-email sample (for initial accuracy measurement):
  uv run python classify_corpus.py --db ~/gmail.db --stratified-sample 200

Resume after interruption (skips already-classified messages):
  Re-run the same command — already-classified (model, message_id, prompt_version)
  triples are skipped automatically.

Remote Ollama:
  export OLLAMA_HOST=http://192.168.1.100:11434
  uv run python classify_corpus.py --db ~/gmail.db

Loop order (Stage 3): all emails through model A, then all through model B,
then model C.  Each model is loaded once per pass (keep_alive=-1 within pass).
This is 2-4× faster than the per-email A+B+C loop on a 50K corpus.

Concurrency (within each model pass):
  Requests are sent concurrently using asyncio + ollama.AsyncClient.
  Default concurrency is 4 — match OLLAMA_NUM_PARALLEL on the Ollama server
  for best throughput (e.g. OLLAMA_NUM_PARALLEL=4 ollama serve).
  Tune with --concurrency:
    uv run python classify_corpus.py --db ~/gmail.db --concurrency 8

Output token cap:
  Each request caps model output at MAX_OUTPUT_TOKENS (1024 tokens).  Models
  produce the ~60-token JSON label line well under the cap.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ollama
import yaml

from sanitize import build_user_turn, sanitize

# ---------------------------------------------------------------------------
# Default models (Stage 3)
# ---------------------------------------------------------------------------

DEFAULT_MODELS = ["qwen2.5:14b", "gpt-oss:20b", "gemma3:12b"]

# Uncertain rate threshold: if more than this fraction of the stratified sample
# comes back uncertain from a single model, the prompt needs refinement before
# adding more models.
UNCERTAIN_RATE_WARN = 0.30

# Circuit breaker: abort a model pass after this many consecutive hard Ollama
# errors (connection failures, not JSON parse errors). Prevents a host-down
# scenario from silently writing thousands of entries to errors.jsonl.
CIRCUIT_BREAKER_THRESHOLD = 5

# Maximum tokens the model may produce per response.  Thinking-mode models
# consume num_predict budget on internal chain-of-thought before producing any
# content; 1024 gives ~600 thinking tokens + the ~60-token JSON label line.
# All current models (qwen2.5, gpt-oss, gemma3) finish well under the cap.
MAX_OUTPUT_TOKENS = 1024

# Default concurrent Ollama requests per model pass (asyncio semaphore).
# Match OLLAMA_NUM_PARALLEL on the server for best throughput.
DEFAULT_CONCURRENCY = 4

# Body character limit for the --review-uncertain display (shorter than model limit)
REVIEW_BODY_CHARS = 400

# ---------------------------------------------------------------------------
# Classifications DB schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

CREATE TABLE IF NOT EXISTS classifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    prompt_version  TEXT    NOT NULL,
    label           TEXT    NOT NULL CHECK(label IN ('keep', 'delete', 'uncertain')),
    confidence      REAL,
    reason          TEXT,
    raw_response    TEXT,
    classified_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cls_message_id
    ON classifications (message_id);

CREATE INDEX IF NOT EXISTS idx_cls_msg_version
    ON classifications (message_id, prompt_version);
"""


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


def load_prompt(version: str, prompts_dir: Path) -> dict[str, Any]:
    """Load a versioned prompt YAML file from prompts_dir."""
    path = prompts_dir / f"{version}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    with path.open() as f:
        data = yaml.safe_load(f)
    if data.get("version") != version:
        raise ValueError(
            f"Prompt file version mismatch: expected {version!r}, got {data.get('version')!r}"
        )
    return dict(data)


# ---------------------------------------------------------------------------
# Corrections — shared dedup logic
# ---------------------------------------------------------------------------


def load_corrections(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Load corrections.jsonl, returning the latest entry per (message_id, prompt_version).

    Deduplication rule: if multiple entries share the same (message_id,
    prompt_version) key, the one with the latest corrected_at timestamp wins.
    Malformed JSON lines are skipped with a warning (not a crash).

    Returns an empty dict if the file does not exist.
    """
    if not path.exists():
        return {}

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open() as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(
                    f"  WARNING: corrections.jsonl line {lineno} is malformed ({exc}) — skipping.",
                    file=sys.stderr,
                )
                continue
            key = (entry.get("message_id", ""), entry.get("prompt_version", ""))
            prev = latest.get(key)
            if prev is None or entry.get("corrected_at", "") >= prev.get("corrected_at", ""):
                latest[key] = entry
    return latest


def write_correction(path: Path, entry: dict[str, Any]) -> None:
    """Append a single correction to corrections.jsonl.

    Writes atomically to the line: json.dumps produces a complete JSON object
    before the newline is written, so a SIGTERM between the two writes is safe
    (the line is either fully present or absent).
    """
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Ollama startup checks
# ---------------------------------------------------------------------------


_CONNECT_HINTS: list[tuple[str, str]] = [
    ("Name or service not known", "hostname not found — check for a typo in OLLAMA_HOST"),
    ("nodename nor servname provided", "hostname not found — check for a typo in OLLAMA_HOST"),
    ("Connection refused", "Ollama process is not running on that host/port"),
    ("timed out", "host is reachable but Ollama is not responding — port mismatch?"),
    ("ConnectError", "cannot connect — verify host and port in OLLAMA_HOST"),
]


def _connect_hint(exc: BaseException) -> str:
    msg = str(exc)
    for fragment, hint in _CONNECT_HINTS:
        if fragment.lower() in msg.lower():
            return hint
    return "check that Ollama is running and OLLAMA_HOST is correct"


def startup_check(client: ollama.Client, required_models: list[str], host: str) -> None:
    """Verify the Ollama daemon is reachable and all required models are available.

    Aborts with a clear error message naming the missing model and how to pull it.
    """
    try:
        response = client.list()
    except Exception as exc:
        print(
            f"ERROR: Cannot reach Ollama at {host}\n"
            f"  {_connect_hint(exc)}\n"
            f"  ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        sys.exit(1)

    available = {m.model for m in response.models}
    for model in required_models:
        if model not in available:
            print(
                f"ERROR: Model '{model}' not found on {host}\n  Pull it with: ollama pull {model}",
                file=sys.stderr,
            )
            sys.exit(1)


# ---------------------------------------------------------------------------
# JSON response parsing
# ---------------------------------------------------------------------------


def _parse_label_response(raw: str) -> dict[str, Any]:
    """Parse a model's JSON label response.

    Handles two common model output quirks:
    - Markdown code fences (```json...```) wrapping the JSON
    - Truncated output where the reason string is missing its closing quote,
      e.g. '{"label":"delete","reason":"text (no-reply@)}' — recovered by
      truncating at the last complete closing brace.
    """
    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1 :] if nl >= 0 else text[3:]  # drop opening fence line
        if text.endswith("```"):
            text = text[:-3].rstrip()  # drop closing fence (own line or inline)
        text = text.strip()

    # Fast path: try parsing as-is
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Recovery: two common truncation patterns from small models.
    last_brace = text.rfind("}")
    if last_brace > 0:
        # Pattern A: trailing chars after the closing brace (e.g. stray quote).
        try:
            return json.loads(text[: last_brace + 1])
        except json.JSONDecodeError:
            pass

        # Pattern B: closing quote missing before the final brace, e.g.
        #   "reason": "text (no-reply@)}   →   "reason": "text (no-reply@)"}
        if text[last_brace - 1] != '"':
            try:
                return json.loads(text[:last_brace] + '"' + text[last_brace:])
            except json.JSONDecodeError:
                pass

    raise json.JSONDecodeError("no closing brace found", text, 0)


# ---------------------------------------------------------------------------
# Single-message classification
# ---------------------------------------------------------------------------


def classify_message(
    client: ollama.Client,
    model: str,
    prompt: dict[str, Any],
    message: dict[str, Any],
    *,
    keep_alive: int = -1,
) -> dict[str, Any]:
    """Classify one message with one model.

    Args:
        client:      Ollama client (configured with OLLAMA_HOST).
        model:       Ollama model tag (e.g. "qwen2.5:7b").
        prompt:      Loaded prompt YAML dict (system_prompt, etc.).
        message:     Dict with keys: message_id, from_email, from_name, subject,
                     date_str, body_text.
        keep_alive:  Seconds to keep the model loaded. -1 = keep loaded;
                     0 = unload after this request (use for the last message
                     in each model's batch pass).

    Returns a dict with label, confidence, reason, raw_response, error (if any).
    """
    from_addr = message.get("from_email") or message.get("from_name") or "(unknown)"
    subject = message.get("subject") or "(no subject)"
    date_str = message.get("date_str") or ""
    body_text = message.get("body_text") or ""

    sanitized = sanitize(body_text, strip_html=False)
    user_turn = build_user_turn(from_addr, subject, date_str, sanitized)

    try:
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": prompt["system_prompt"]},
                {"role": "user", "content": user_turn},
            ],
            options={"keep_alive": keep_alive, "num_predict": MAX_OUTPUT_TOKENS},
        )
        raw = response.message.content or ""
    except Exception as exc:
        return {
            "label": "uncertain",
            "confidence": None,
            "reason": None,
            "raw_response": None,
            "error": str(exc),
        }

    try:
        parsed = _parse_label_response(raw)
        label = parsed.get("label", "uncertain").lower()
        if label not in ("keep", "delete", "uncertain"):
            label = "uncertain"
        return {
            "label": label,
            "confidence": parsed.get("confidence"),
            "reason": parsed.get("reason"),
            "raw_response": raw,
            "error": None,
        }
    except json.JSONDecodeError:
        return {
            "label": "uncertain",
            "confidence": None,
            "reason": None,
            "raw_response": raw,
            "error": "json_parse_error",
        }


# ---------------------------------------------------------------------------
# Async classification helpers (corpus batch pass)
# ---------------------------------------------------------------------------


async def _classify_one_async(
    client: ollama.AsyncClient,
    sem: asyncio.Semaphore,
    model: str,
    prompt: dict[str, Any],
    message: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Classify one message asynchronously; returns (message, result)."""
    from_addr = message.get("from_email") or message.get("from_name") or "(unknown)"
    subject = message.get("subject") or "(no subject)"
    date_str = message.get("date_str") or ""
    body_text = message.get("body_text") or ""
    sanitized = sanitize(body_text, strip_html=False)
    user_turn = build_user_turn(from_addr, subject, date_str, sanitized)

    async with sem:
        try:
            response = await client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": prompt["system_prompt"]},
                    {"role": "user", "content": user_turn},
                ],
                options={"keep_alive": -1, "num_predict": MAX_OUTPUT_TOKENS},
            )
            raw = response.message.content or ""
        except Exception as exc:
            return message, {
                "label": "uncertain",
                "confidence": None,
                "reason": None,
                "raw_response": None,
                "error": str(exc),
            }

    try:
        parsed = _parse_label_response(raw)
        label = parsed.get("label", "uncertain").lower()
        if label not in ("keep", "delete", "uncertain"):
            label = "uncertain"
        return message, {
            "label": label,
            "confidence": parsed.get("confidence"),
            "reason": parsed.get("reason"),
            "raw_response": raw,
            "error": None,
        }
    except json.JSONDecodeError:
        return message, {
            "label": "uncertain",
            "confidence": None,
            "reason": None,
            "raw_response": raw,
            "error": "json_parse_error",
        }


async def _classify_pass_async(
    host: str,
    model: str,
    prompt: dict[str, Any],
    pending: list[dict[str, Any]],
    concurrency: int,
    *,
    errors_path: Path,
    cls_conn: sqlite3.Connection,
    version: str,
    pass_start: float,
    _client: ollama.AsyncClient | None = None,
) -> None:
    """Run one model's full pass concurrently.

    Sends up to `concurrency` requests at once (asyncio.Semaphore).  Results
    are written to cls_conn as they arrive.  After all tasks complete the model
    is unloaded by sending a final keep_alive=0 request.
    """
    client = _client if _client is not None else ollama.AsyncClient(host=host)
    sem = asyncio.Semaphore(concurrency)
    total = len(pending)
    done_count = 0
    consecutive_errors = 0
    last_error: str | None = None

    tasks = [
        asyncio.create_task(_classify_one_async(client, sem, model, prompt, msg)) for msg in pending
    ]

    try:
        for coro in asyncio.as_completed(tasks):
            msg, result = await coro
            now = datetime.now(UTC).isoformat()
            done_count += 1

            if result["error"] and result["raw_response"] is None:
                consecutive_errors += 1
                last_error = result["error"]
                with errors_path.open("a") as ef:
                    ef.write(
                        json.dumps(
                            {
                                "message_id": msg["message_id"],
                                "model": model,
                                "error": result["error"],
                                "ts": now,
                            }
                        )
                        + "\n"
                    )
                if consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
                    for task in tasks:
                        task.cancel()
                    cls_conn.commit()
                    print(
                        f"\nERROR: {consecutive_errors} consecutive Ollama errors on"
                        f" model {model!r} — aborting.\n"
                        f"  Last error: {last_error}\n"
                        f"  Host: {host}\n"
                        "  Check that Ollama is still running and OLLAMA_HOST is correct.\n"
                        "  Re-run the same command to resume from where it stopped.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                continue

            consecutive_errors = 0

            cls_conn.execute(
                """
                INSERT OR REPLACE INTO classifications
                    (message_id, model, prompt_version, label, confidence,
                     reason, raw_response, classified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg["message_id"],
                    model,
                    version,
                    result["label"],
                    result["confidence"],
                    result["reason"],
                    result["raw_response"],
                    now,
                ),
            )

            if done_count % 50 == 0:
                cls_conn.commit()
                elapsed = time.monotonic() - pass_start
                progress = _fmt_progress(done_count, total, elapsed)
                print(f"  {model}: {progress}", flush=True)

    finally:
        # Unload the model after this pass (or on circuit-breaker abort).
        with contextlib.suppress(Exception):
            await client.chat(
                model=model,
                messages=[{"role": "user", "content": ""}],
                options={"keep_alive": 0, "num_predict": 1},
            )

    cls_conn.commit()


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------


def compute_vote(labels: list[str]) -> str:
    """Compute the consensus label for a set of model votes.

    Voting rule (research goal: surface ambiguity):
      - Unanimous keep → 'keep'
      - Unanimous delete → 'delete'
      - 2-1 split OR 3-way disagree → 'uncertain'
    """
    unique = set(labels)
    if len(unique) == 1:
        return next(iter(unique))
    return "uncertain"


# ---------------------------------------------------------------------------
# Progress formatting
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as a compact human string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _fmt_progress(done: int, total: int, elapsed: float) -> str:
    """Return a single-line progress string with rate and ETA."""
    pct = done / total * 100
    rate = done / elapsed if elapsed > 0 else 0.0
    rate_str = f"{rate:.1f} msg/s" if rate >= 1 else f"{rate * 60:.1f} msg/min"
    if rate > 0:
        eta = _fmt_duration((total - done) / rate)
        eta_str = f"ETA {eta}"
    else:
        eta_str = "ETA --"
    return f"{done}/{total} ({pct:.1f}%)  {rate_str}  {eta_str}"


# ---------------------------------------------------------------------------
# Main corpus classification
# ---------------------------------------------------------------------------


def run_corpus_classification(
    args: argparse.Namespace,
    gmail_conn: sqlite3.Connection,
    cls_conn: sqlite3.Connection,
    prompt: dict[str, Any],
    *,
    _async_client: ollama.AsyncClient | None = None,
) -> None:
    """Run the batch-by-model corpus classification loop.

    Loop order: all messages through model A, then all through model B, then C.
    Each model is loaded once per pass.  Already-classified (model, message_id,
    prompt_version) triples are skipped for resumability.

    Within each pass, up to args.concurrency requests are sent concurrently via
    asyncio + ollama.AsyncClient.  Set OLLAMA_NUM_PARALLEL on the Ollama server
    to the same value for maximum GPU utilisation.
    """
    version = prompt["version"]
    models = args.models
    errors_path = args.db.parent / "errors.jsonl"
    disagreements_path = args.db.parent / "disagreements.jsonl"

    # --- Build message list ---
    if args.stratified_sample:
        n = args.stratified_sample // 5
        rows = []
        # Gmail label categories that map roughly to email types
        for condition in [
            "in_inbox = 1 AND is_unread = 0",  # read inbox — general correspondence
            "in_inbox = 1 AND is_unread = 1",  # unread inbox
            "labels LIKE '%Promotions%'",  # promotions
            "labels LIKE '%Social%'",  # social
            "labels LIKE '%Updates%'",  # newsletters / updates
        ]:
            batch = gmail_conn.execute(
                f"""
                SELECT message_id, from_email, from_name, subject, date_str, body_text
                FROM messages
                WHERE message_id IS NOT NULL AND ({condition})
                ORDER BY date_ts DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
            rows.extend(batch)
    else:
        rows = gmail_conn.execute(
            """
            SELECT message_id, from_email, from_name, subject, date_str, body_text
            FROM messages
            WHERE message_id IS NOT NULL
            ORDER BY date_ts DESC
            """
        ).fetchall()

    if not rows:
        print("No messages found in gmail.db. Import your mbox first.")
        return

    cols = ["message_id", "from_email", "from_name", "subject", "date_str", "body_text"]
    messages = [dict(zip(cols, row, strict=False)) for row in rows]

    print(f"Messages to classify: {len(messages)}")
    print(f"Models: {models}")
    print(f"Prompt version: {version}")

    # --- Batch-by-model loop ---
    for model in models:
        # Find which messages need classification for this (model, version)
        done_ids: set[str] = {
            row[0]
            for row in cls_conn.execute(
                "SELECT message_id FROM classifications WHERE model=? AND prompt_version=?",
                (model, version),
            ).fetchall()
        }
        pending = [m for m in messages if m["message_id"] not in done_ids]

        if not pending:
            print(f"{model}: all {len(messages)} messages already classified — skipping.")
            continue

        already = len(messages) - len(pending)
        resume_note = f" (resuming from {already})" if already else ""
        print(f"{model}: classifying {len(pending)} messages{resume_note} ...", flush=True)

        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        pass_start = time.monotonic()
        asyncio.run(
            _classify_pass_async(
                host,
                model,
                prompt,
                pending,
                args.concurrency,
                errors_path=errors_path,
                cls_conn=cls_conn,
                version=version,
                pass_start=pass_start,
                _client=_async_client,
            )
        )
        elapsed = time.monotonic() - pass_start
        print(f"{model}: done — {len(pending)} messages in {_fmt_duration(elapsed)}.")

    # --- Voting & uncertain rate ---
    if len(models) == 1:
        model = models[0]
        total = cls_conn.execute(
            "SELECT COUNT(*) FROM classifications WHERE model=? AND prompt_version=?",
            (model, version),
        ).fetchone()[0]
        uncertain = cls_conn.execute(
            "SELECT COUNT(*) FROM classifications"
            " WHERE model=? AND prompt_version=? AND label='uncertain'",
            (model, version),
        ).fetchone()[0]
        if total:
            rate = uncertain / total
            print(f"\nUncertain rate: {uncertain}/{total} = {rate:.1%}")
            if rate > UNCERTAIN_RATE_WARN:
                print(
                    f"\nWARNING: Uncertain rate {rate:.1%} exceeds"
                    f" {UNCERTAIN_RATE_WARN:.0%} threshold.\n"
                    "  Refine the prompt (bump to v1.8.0) before the three-model Stage 3 pass.\n"
                    "  High uncertain rates from one model mean the prompt is too coarse."
                )
    else:
        # Multi-model: compute votes and write disagreement JSONL
        msg_ids = [m["message_id"] for m in messages]
        disagree_count = 0
        with disagreements_path.open("a") as df:
            for mid in msg_ids:
                rows_for_msg = cls_conn.execute(
                    """
                    SELECT model, label FROM classifications
                    WHERE message_id=? AND prompt_version=?
                    AND model IN ({})
                    """.format(",".join("?" * len(models))),
                    (mid, version, *models),
                ).fetchall()
                if not rows_for_msg:
                    continue
                model_labels = {r[0]: r[1] for r in rows_for_msg}
                labels = list(model_labels.values())
                if len(set(labels)) > 1:
                    disagree_count += 1
                    df.write(
                        json.dumps(
                            {
                                "message_id": mid,
                                "prompt_version": version,
                                **{f"{m}_label": model_labels.get(m) for m in models},
                                "date": datetime.now(UTC).date().isoformat(),
                            }
                        )
                        + "\n"
                    )
        print(f"\nDisagreements logged: {disagree_count} → {disagreements_path}")


# ---------------------------------------------------------------------------
# Review uncertain cases (--review-uncertain)
# ---------------------------------------------------------------------------


def run_review_uncertain(
    args: argparse.Namespace,
    gmail_conn: sqlite3.Connection,
    cls_conn: sqlite3.Connection,
    prompt: dict[str, Any],
) -> None:
    """Interactive loop: review AI/Uncertain messages and capture human labels.

    Writes to corrections.jsonl immediately on confirmation — each k/d/u
    keypress followed by Enter writes one line.  Ctrl-C exits cleanly without
    writing a partial entry.
    """
    version = prompt["version"]
    corrections_path = args.db.parent / "corrections.jsonl"

    # Load existing corrections to skip already-reviewed messages
    existing = load_corrections(corrections_path)

    # Find uncertain messages for this prompt version (grouped by message_id)
    uncertain_rows = cls_conn.execute(
        """
        SELECT message_id,
               GROUP_CONCAT(model || ':' || label || ':' || COALESCE(reason,''), '|') AS model_info
        FROM classifications
        WHERE prompt_version=? AND label='uncertain'
        GROUP BY message_id
        """,
        (version,),
    ).fetchall()

    # Skip already-corrected
    to_review = [r for r in uncertain_rows if (r[0], version) not in existing]

    if not to_review:
        print("No uncertain messages to review for prompt version", version)
        return

    print(f"\nReviewing {len(to_review)} uncertain messages (Ctrl-C to stop)\n")
    reviewed = 0

    for msg_id, model_info_raw in to_review:
        # Fetch message from gmail.db
        gmail_row = gmail_conn.execute(
            "SELECT from_email, from_name, subject, date_str, body_text"
            " FROM messages WHERE message_id=?",
            (msg_id,),
        ).fetchone()

        print("─" * 72)
        if gmail_row is None:
            print(f"Message-ID: {msg_id}")
            print("[body unavailable — message not found in gmail.db]")
        else:
            from_email, from_name, subject, date_str, body_text = gmail_row
            sender = f"{from_name} <{from_email}>" if from_name else (from_email or "(unknown)")
            print(f"From:    {sender}")
            print(f"Subject: {subject or '(no subject)'}")
            print(f"Date:    {date_str or ''}")
            print()
            # Show sanitized body preview
            sanitized = sanitize(body_text or "", strip_html=False)
            # Strip delimiters for display
            preview_body = (
                sanitized.replace("[EMAIL_CONTENT]", "").replace("[/EMAIL_CONTENT]", "").strip()
            )
            print(preview_body[:REVIEW_BODY_CHARS])
            if len(preview_body) > REVIEW_BODY_CHARS:
                print("  [... truncated]")

        # Show each model's label + reason
        model_labels: dict[str, str] = {}
        print()
        for entry in model_info_raw.split("|"):
            if not entry:
                continue
            parts = entry.split(":", 2)
            if len(parts) < 2:
                continue
            m, lbl = parts[0], parts[1]
            reason = parts[2] if len(parts) > 2 else ""
            model_labels[m] = lbl
            print(f"  [{m}] {lbl}" + (f": {reason}" if reason else ""))

        print()
        try:
            choice = input("Label (k=keep / d=delete / u=uncertain / s=skip): ").strip().lower()
        except KeyboardInterrupt:
            print("\n\nStopped. Progress is saved.")
            break

        if choice == "s":
            continue

        label_map = {"k": "keep", "d": "delete", "u": "uncertain"}
        human_label = label_map.get(choice)
        if human_label is None:
            print(f"  Unknown input {choice!r} — skipping.")
            continue

        # Confirm before writing
        try:
            confirm = input(f"  Confirm '{human_label}' for this message? [y/N] ").strip().lower()
        except KeyboardInterrupt:
            print("\n\nStopped. Progress is saved.")
            break

        if confirm != "y":
            print("  Skipped.")
            continue

        entry = {
            "message_id": msg_id,
            "human_label": human_label,
            "prompt_version": version,
            "model_labels": model_labels,
            "corrected_at": datetime.now(UTC).isoformat(),
        }
        write_correction(corrections_path, entry)
        reviewed += 1
        print(f"  Saved. ({reviewed} corrections this session)")

    print(f"\nSession complete. {reviewed} corrections written to {corrections_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    here = Path(__file__).parent
    default_prompts_dir = here / "prompts"

    parser = argparse.ArgumentParser(
        description="Classify Gmail corpus messages with Ollama.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=here / "gmail.db",
        help="Path to gmail.db (default: ./gmail.db)",
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
        help=f"Ollama model tags to run (default: {DEFAULT_MODELS})",
    )
    parser.add_argument(
        "--prompt-version",
        default="v1.7.0",
        help="Prompt version to use (default: v1.7.0)",
    )
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        default=default_prompts_dir,
        help=f"Directory containing prompt YAML files (default: {default_prompts_dir})",
    )
    parser.add_argument(
        "--stratified-sample",
        type=int,
        default=None,
        metavar="N",
        help="Classify a stratified sample of N messages (split across email categories)",
    )
    parser.add_argument(
        "--review-uncertain",
        action="store_true",
        help="Interactive loop: review AI/Uncertain messages and capture corrections",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        metavar="N",
        help=(
            f"Concurrent Ollama requests per model pass (default: {DEFAULT_CONCURRENCY})."
            " Match OLLAMA_NUM_PARALLEL on the server for best throughput."
        ),
    )

    args = parser.parse_args()

    # Resolve classifications.db path
    if args.classifications_db is None:
        args.classifications_db = args.db.parent / "classifications.db"
    # Expose resolved db parent for JSONL co-location in sub-functions
    args.db = args.db.resolve()

    # Load prompt
    prompt = load_prompt(args.prompt_version, args.prompts_dir)

    # Ollama client (reads OLLAMA_HOST from env)
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    print(f"Ollama endpoint: {host}", flush=True)
    client = ollama.Client(host=host)

    # Startup check (skip in review-only mode — no Ollama calls needed)
    if not args.review_uncertain:
        startup_check(client, args.models, host)

    # Open databases
    gmail_conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    gmail_conn.row_factory = sqlite3.Row

    cls_path = args.classifications_db.resolve()
    cls_conn = sqlite3.connect(cls_path)
    cls_conn.executescript(_SCHEMA)
    cls_conn.commit()

    try:
        if args.review_uncertain:
            run_review_uncertain(args, gmail_conn, cls_conn, prompt)
        else:
            run_corpus_classification(args, gmail_conn, cls_conn, prompt)
    finally:
        gmail_conn.close()
        cls_conn.close()


if __name__ == "__main__":
    main()
