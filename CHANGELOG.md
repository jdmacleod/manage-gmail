# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For this project: **MAJOR** versions are changes that require user action
(e.g. a new required config file, a restructured `config/` format, or a new
required OAuth scope); **MINOR** versions add new capability (a new script,
a new prune/reference category, a new flag); **PATCH** versions are bug
fixes and query tuning.

## [Unreleased]

## [0.1.0] - 2026-06-12

### Added

- `scripts/prune.sh` — interactive/dry-run pruning across 9 standing
  categories (old promotions, social/notification noise, expired
  codes/resets, auto-reply/bounce noise, past calendar notifications,
  post-delivery shipping notices, old newsletters, old read mail, old
  unread mail), plus a 10th "Noisy senders (low engagement)" category fed by
  `noisy_senders.txt`. Everything moves to Trash, never permanently deleted.
- `scripts/rescue.sh` — scans Trash for messages worth keeping (receipts,
  policy docs, travel confirmations, security alerts, warranty/registration
  docs, plus personal sender/subject rules), restores them, and applies the
  `Reference` label.
- `scripts/label_reference.sh` — proactively applies the `Reference` label
  mailbox-wide using the same "keep" rules, so matching mail is protected
  from pruning before it's ever trashed. Stamps `.label_reference_last_run`
  for freshness checks.
- `scripts/export_contacts.sh` — builds `contacts_emails.txt` from Google
  Contacts + Other Contacts; `prune.sh` never trashes mail from these
  addresses.
- `scripts/find_noisy_senders.sh` — read-only report of high-volume,
  low-engagement senders in Promotions/Updates/Social (last 90 days), with a
  `--debug` flag for troubleshooting.
- `scripts/review_audit.sh` — read-only summary of `audit_log.jsonl`,
  surfacing senders/messages that were pruned and later rescued.
- `scripts/lib.sh` — shared helpers: label lookup/creation, audit logging
  (`Audit/Pruned/<category>` / `Audit/Rescued/<category>` labels +
  `audit_log.jsonl`), duration/ETA formatting and progress indicators, and
  `load_local_keep_rules()` for personal "keep" rules.
- Protections applied across all categories: starred/important mail,
  `Reference`-labeled mail, mail from Contacts, and mail in threads you've
  replied to are never pruned.
- `config/keep_rules.local.tsv` (gitignored) — per-mailbox personal
  sender/subject "keep" rules shared between `rescue.sh` and
  `label_reference.sh`, with `config/keep_rules.local.example.tsv` as a
  template.
- `.gitignore` plus `.example` counterparts (`noisy_senders.example.txt`,
  `contacts_emails.example.txt`, `PRUNE_LOG.example.md`,
  `audit_log.example.jsonl`, `config/keep_rules.local.example.tsv`) so
  personal data stays out of the public repo while still documenting the
  expected formats.
- `PRUNE_LOG.md` — auto-appended run history (queries, counts, timestamps).
- Documentation: `README.md` (overview, quick start, safety model),
  `SETUP.md` (install + Google OAuth setup), `PRUNE_PLAYBOOK.md` (category
  queries, rescue scan, audit/tuning guidance).
