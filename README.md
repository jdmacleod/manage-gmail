# Manage Gmail with `gws`

Tools and a repeatable playbook for pruning low-value email from your Gmail
account using the [`gws`](https://github.com/googleworkspace/cli) CLI, while
keeping anything starred, important, or otherwise worth protecting.

## Status / setup already done

- Google Cloud project **"manage-gmail"** created at console.cloud.google.com
- `gws` installed: `npm install -g @googleworkspace/cli`
- `gcloud` CLI installed: `brew update && brew install --cask gcloud-cli`
- Google account added as a **Test user** under OAuth consent screen Audience

See `SETUP.md` for the full setup reference (auth login, scopes, troubleshooting)
if you need to redo this on another machine or re-authenticate.

## Files

Files containing personal data (your contacts, curated sender lists, run
logs, audit trail, and personal "keep" rules) are listed in `.gitignore` and
not committed. Each has a `.example` counterpart with dummy data showing the
expected format — copy it and remove the `.example` suffix to get started.

- **`SETUP.md`** — full one-time install + Google OAuth setup reference.
- **`PRUNE_PLAYBOOK.md`** — prune categories, the Gmail search queries behind
  them (including the ones already worked out below), and a "rescue scan" to
  catch anything important before Trash empties.
- **`scripts/prune.sh`** — repeatable script: shows counts + samples per
  category, asks for confirmation, then moves matches to Trash.
- **`scripts/rescue.sh`** — scans Trash for messages worth keeping (using the
  "Find emails to keep" queries below), offers to restore them, and applies
  the `Reference` label to anything restored so it stays protected.
- **`scripts/export_contacts.sh`** — builds `contacts_emails.txt` (gitignored;
  see `contacts_emails.example.txt`) from your Google Contacts + Other
  Contacts. `prune.sh` never trashes mail from these addresses, regardless of
  category.
- **`scripts/label_reference.sh`** — proactively applies the `Reference`
  label mailbox-wide to receipts, policy docs, travel confirmations, and
  other "keep" categories. `prune.sh` never trashes labeled mail.
- **`scripts/find_noisy_senders.sh`** — read-only report of high-volume,
  low-engagement senders (Promotions/Updates/Social, last 90 days) to help
  populate `noisy_senders.txt`.
- **`noisy_senders.txt`** — addresses you've curated as safe to prune
  aggressively (gitignored; see `noisy_senders.example.txt`); feeds
  `prune.sh`'s "Noisy senders (low engagement)" category.
- **`config/keep_rules.local.tsv`** — personal sender/subject "keep" rules
  (specific people, organizations, or services) for `rescue.sh` and
  `label_reference.sh` (gitignored; see
  `config/keep_rules.local.example.tsv`).
- **`scripts/review_audit.sh`** — read-only summary of `audit_log.jsonl`;
  surfaces senders/messages that were pruned and later rescued, as a signal
  for which rules need tuning.
- **`scripts/lib.sh`** — shared helpers (duration formatting, label lookup,
  audit logging) sourced by the other scripts.
- **`PRUNE_LOG.md`** — history of what was pruned/rescued/labeled and when
  (auto-updated by the scripts; gitignored, see `PRUNE_LOG.example.md`).
- **`audit_log.jsonl`** — one JSON line per pruned/rescued message
  (timestamp, category, sender, subject, message ID), for `review_audit.sh`
  (gitignored, see `audit_log.example.jsonl`).

## Quick start

```bash
cd ~/Projects/manage-gmail

# One-time (and occasionally, to keep it current)
./scripts/export_contacts.sh

# One-time (and occasionally), label mail worth keeping as Reference
./scripts/label_reference.sh

# Occasionally, find low-engagement senders worth pruning aggressively
./scripts/find_noisy_senders.sh
# then review + add addresses to noisy_senders.txt

# Preview only — trashes nothing
./scripts/prune.sh --dry-run

# Interactive — asks before trashing each category (skips contacts automatically)
./scripts/prune.sh

# Before Trash auto-empties (~30 days), rescue anything important
./scripts/rescue.sh

# Periodically, review which rules are catching false positives
./scripts/review_audit.sh
```

## Categories

`prune.sh` covers 9 standing categories (old promotions, social noise,
expired codes/resets, auto-reply/bounce noise, past calendar notifications,
post-delivery shipping notices, old newsletters, general old read mail, old
unread mail), plus a 10th "Noisy senders (low engagement)" category (scoped
to Promotions/Updates/Social) when `noisy_senders.txt` is populated. A noisy
sender that also sends mail worth keeping (e.g. a bank's statements vs. its
marketing) can get its own extra category with a subject exclusion — see
`noisy_senders.txt`. `rescue.sh` covers 9 retain rules (receipts/invoices,
policy/terms docs, specific senders, travel confirmations, recent security
alerts, warranty/registration docs). `label_reference.sh` applies the
`Reference` label mailbox-wide using the same "keep" rules. Full details,
queries, and how to tune thresholds: see `PRUNE_PLAYBOOK.md`.

## Safety model

- Everything goes to **Trash**, not permanent delete — recoverable for ~30 days.
- Starred (`is:starred`) and important (`is:important`) mail is always excluded
  from pruning.
- Mail labeled `Reference` is never pruned, regardless of category.
- Mail in a thread you've ever replied to is never pruned.
- Mail from someone in your Contacts is never pruned.
- Each category is previewed (count + sample) before you confirm it.
- All runs are logged to `PRUNE_LOG.md`.
- Every pruned message gets an `Audit/Pruned/<category>` label and every
  rescued message gets an `Audit/Rescued/<category>` label, plus an entry in
  `audit_log.jsonl` — run `scripts/review_audit.sh` to find rules that are
  catching too much.
- `prune.sh` checks how recently `label_reference.sh` last ran and, if it's
  stale (default 7+ days) or has never run, warns and offers to run it first
  — so newly-arrived "keep" mail gets a chance to be labeled `Reference`
  before any category (especially Noisy senders) can prune it.

To permanently empty Trash, do that yourself in the Gmail web UI — this
toolkit deliberately doesn't automate permanent deletion.

## Offline analysis (mbox → SQLite + Datasette)

The `python/` directory contains a separate toolchain for analysing a Gmail
Takeout export — useful for bulk queries, trend analysis, and finding things
the live-mailbox scripts can't reach (e.g. old Trash, full-body search).

**Quick start** (requires [uv](https://docs.astral.sh/uv/getting-started/installation/)):

```bash
cd python/
uv sync                          # one-time: create .venv and install deps

# Import your Takeout mbox (resumable — safe to Ctrl+C and re-run)
uv run python mbox_import.py ~/Takeout/Mail/All\ mail\ Including\ Spam\ and\ Trash.mbox

# Browse and query in your browser
uv run datasette serve gmail.db --metadata metadata.yaml --open
```

The importer stores all headers as structured columns plus a `headers_json`
fallback so nothing is discarded.  `gmail.db` is gitignored (it contains your
mail).  See `python/metadata.yaml` for the pre-built canned queries and
`python/mbox_import.py --help` for all import options.

## Versioning

This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(`MAJOR.MINOR.PATCH`), tagged in git as `vX.Y.Z`. MAJOR bumps are changes
that require user action (e.g. a new required config file or a restructured
`config/` format); MINOR bumps add new capability (a new script, category, or
flag); PATCH bumps are bug fixes and query tuning. See `CHANGELOG.md` for a
running history of changes, following the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
