#!/usr/bin/env bash
#
# Gmail prune script — uses the `gws` CLI to move low-value mail to Trash.
# Nothing is permanently deleted; Trash auto-empties after ~30 days, and
# anything can be restored with `gws gmail users messages untrash`.
#
# A message is never trashed, regardless of which category it matches, if:
#   - the sender is in your Contacts / Other Contacts (run
#     scripts/export_contacts.sh, requires the `people` OAuth scope), or
#   - you've ever replied within its thread (checked via threads.get), or
#   - it carries the "Reference" label (see scripts/label_reference.sh).
#
# Every message that IS trashed gets an "Audit/Pruned/<category>" label, and
# an entry is appended to ../audit_log.jsonl (timestamp, category, sender,
# subject, message id). Run scripts/review_audit.sh periodically to spot
# rules worth tuning — e.g. senders that show up in both pruned and rescued
# events.
#
# Before pruning, this script also checks how long it's been since
# label_reference.sh last ran (see REFERENCE_MAX_AGE_DAYS below) and offers
# to run it first — that's the main protection for senders in
# noisy_senders.txt who also send mail worth keeping (e.g. a bank's
# statements vs. its marketing). noisy_senders.txt entries can also carry
# per-sender subject exclusions for the same purpose (see that file).
#
# Usage:
#   ./prune.sh            Interactive: shows counts + samples, asks before trashing each category
#   ./prune.sh --dry-run  Preview only: shows counts + samples for every category, trashes nothing
#
# Requires: gws (authenticated, see ../SETUP.md), jq
#
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# ---- Thresholds (edit to taste) ----
PROMO_AGE="6m"          # old promotions (combined with is:unread)
SOCIAL_AGE="3m"         # social/notification noise
AUTH_CODE_AGE="7d"      # expired verification codes / password resets
AUTOREPLY_AGE="30d"     # out-of-office / bounce / DSN noise
CALENDAR_NOTIF_AGE="30d" # past Google Calendar notification emails
SHIPPING_AGE="3m"       # post-delivery shipping notifications
OLD_UPDATES_AGE="1y"    # old read newsletters/digests (category:updates)
OLD_READ_AGE="1y"       # general old read mail sweep
OLD_READ_SIZE="1M"      # size ceiling for the general sweep
OLD_UNREAD_AGE="1y"     # old unread mail outside Primary
NOISY_AGE="30d"         # noisy/low-engagement senders (see noisy_senders.txt)
REFERENCE_MAX_AGE_DAYS=7 # warn if label_reference.sh hasn't run in this long

# ---- Setup ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGFILE="$SCRIPT_DIR/../PRUNE_LOG.md"
CONTACTS_FILE="$SCRIPT_DIR/../contacts_emails.txt"
NOISY_SENDERS_FILE="$SCRIPT_DIR/../noisy_senders.txt"
AUDIT_LOG="$SCRIPT_DIR/../audit_log.jsonl"
REFERENCE_STAMP="$SCRIPT_DIR/../.label_reference_last_run"

command -v gws >/dev/null 2>&1 || { echo "gws not found on PATH. See ../SETUP.md."; exit 1; }
command -v jq  >/dev/null 2>&1 || { echo "jq not found. Install it (e.g. 'brew install jq')."; exit 1; }

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

if [[ -s "$CONTACTS_FILE" ]]; then
  contact_count=$(grep -c . "$CONTACTS_FILE" || true)
  echo "Contact protection: ON ($contact_count address(es) from $CONTACTS_FILE)"
else
  echo "Contact protection: OFF — no $CONTACTS_FILE found."
  echo "Run ./export_contacts.sh first to skip pruning mail from people in your Contacts."
fi
echo "Thread-reply protection: ON (skips mail in any thread you've replied to)"

# Reference labeling is the main protection for mail that's worth keeping
# but comes from an otherwise "noisy" sender (e.g. a bank's statements vs.
# its marketing). Warn if label_reference.sh hasn't run recently, since
# anything it would have labeled is still exposed to every category below.
ref_mtime=$(file_mtime "$REFERENCE_STAMP")
if [[ -z "$ref_mtime" ]]; then
  ref_stale=true
  echo "Reference labeling: label_reference.sh has never completed a run."
else
  ref_age_days=$(( ( $(date +%s) - ref_mtime ) / 86400 ))
  if [[ "$ref_age_days" -gt "$REFERENCE_MAX_AGE_DAYS" ]]; then
    ref_stale=true
    echo "Reference labeling: last completed run was $ref_age_days day(s) ago (recommended: every $REFERENCE_MAX_AGE_DAYS day(s))."
  else
    ref_stale=false
    echo "Reference labeling: OK (last run $ref_age_days day(s) ago)."
  fi
fi

if [[ "$ref_stale" == "true" ]]; then
  echo "Mail matching its 'keep' rules (statements, etc.) that arrived since then may not"
  echo "yet have the Reference label, especially relevant for the 'Noisy senders' category."
  if [[ "$DRY_RUN" == "false" ]]; then
    read -r -p "Run label_reference.sh now before pruning? [y/N] " ref_answer
    case "$ref_answer" in
      [yY]*) "$SCRIPT_DIR/label_reference.sh" || echo "Warning: label_reference.sh exited with an error — continuing with prune." ;;
      *) echo "Continuing without running label_reference.sh." ;;
    esac
  fi
fi
echo ""

# Caches for thread-reply protection, scoped to this run. Avoids repeat
# threads.get calls when multiple candidates share a thread.
REPLIED_THREADS_CACHE="$(mktemp)"
CHECKED_THREADS_CACHE="$(mktemp)"
trap 'rm -f "$REPLIED_THREADS_CACHE" "$CHECKED_THREADS_CACHE"' EXIT

# Returns 0 (true) if email address $1 matches someone in CONTACTS_FILE.
# Returns 1 (false) if no contacts file, empty address, or no match.
from_is_contact_email() {
  local email="$1"
  [[ -s "$CONTACTS_FILE" ]] || return 1
  [[ -z "$email" ]] && return 1
  grep -qxF "$email" "$CONTACTS_FILE"
}

# Returns 0 (true) if you've ever replied within thread $1 (i.e. any message
# in the thread carries the SENT label). Caches results for this run.
thread_has_reply() {
  local thread_id="$1"
  [[ -z "$thread_id" ]] && return 1

  if grep -qxF "$thread_id" "$REPLIED_THREADS_CACHE" 2>/dev/null; then
    return 0
  fi
  if grep -qxF "$thread_id" "$CHECKED_THREADS_CACHE" 2>/dev/null; then
    return 1
  fi

  local sent_count
  sent_count=$(gws gmail users threads get \
    --params "$(jq -nc --arg tid "$thread_id" '{"userId":"me","id":$tid,"format":"minimal"}')" \
    --format json 2>/dev/null \
    | jq -r '[.messages[]?.labelIds[]? | select(. == "SENT")] | length' 2>/dev/null)
  sent_count="${sent_count:-0}"

  if [[ "$sent_count" -gt 0 ]]; then
    echo "$thread_id" >> "$REPLIED_THREADS_CACHE"
    return 0
  else
    echo "$thread_id" >> "$CHECKED_THREADS_CACHE"
    return 1
  fi
}

# Returns 0 (protect / never trash) if message $1 is from a contact, or if
# you've ever replied within its thread. Returns 1 (safe to prune)
# otherwise, including on any lookup failure.
#
# As a side effect, sets LAST_FROM_EMAIL and LAST_SUBJECT from the message's
# metadata (used for audit logging by the caller when pruning proceeds).
should_protect() {
  local id="$1"
  local meta thread_id from_header

  LAST_FROM_EMAIL=""
  LAST_SUBJECT=""

  meta=$(gws gmail users messages get \
    --params "$(jq -nc --arg id "$id" '{"userId":"me","id":$id,"format":"metadata","metadataHeaders":["From","Subject"]}')" \
    --format json 2>/dev/null) || return 1

  from_header=$(header_value "$meta" "From")
  LAST_FROM_EMAIL=$(extract_email "$from_header")
  LAST_SUBJECT=$(header_value "$meta" "Subject")

  from_is_contact_email "$LAST_FROM_EMAIL" && return 0

  thread_id=$(echo "$meta" | jq -r '.threadId // empty')
  thread_has_reply "$thread_id"
}

# Parses NOISY_SENDERS_FILE (one entry per line, blank lines and
# #-comments ignored) into:
#   NOISY_PLAIN      - addresses with no subject exclusions
#   NOISY_EXCL_ADDR  - addresses that have subject exclusions (parallel to
#                      NOISY_EXCL_KW)
#   NOISY_EXCL_KW    - comma-separated exclude keywords for the matching
#                      NOISY_EXCL_ADDR entry
#
# Format: "address" or "address<TAB>keyword1,keyword2,...". Keywords
# containing spaces (e.g. "payment due") are matched as phrases.
NOISY_PLAIN=()
NOISY_EXCL_ADDR=()
NOISY_EXCL_KW=()
parse_noisy_senders() {
  [[ -s "$NOISY_SENDERS_FILE" ]] || return 0

  local line addr kws
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    if [[ "$line" == *$'\t'* ]]; then
      addr="${line%%$'\t'*}"
      kws="${line#*$'\t'}"
      NOISY_EXCL_ADDR+=("$addr")
      NOISY_EXCL_KW+=("$kws")
    else
      NOISY_PLAIN+=("$line")
    fi
  done < "$NOISY_SENDERS_FILE"
}

# Builds a "from:(a OR b OR c)" fragment from NOISY_PLAIN. Prints nothing if
# empty.
build_noisy_query() {
  [[ ${#NOISY_PLAIN[@]} -eq 0 ]] && return 0
  local joined
  joined=$(printf '%s\n' "${NOISY_PLAIN[@]}" | paste -sd' ' - | sed 's/ / OR /g')
  echo "from:($joined)"
}

# Builds a `-subject:(kw1 OR kw2 OR "multi word")` fragment from a
# comma-separated keyword string $1. Prints nothing if $1 is empty/blank.
build_subject_exclusion() {
  local kws="$1" kw joined=""
  local IFS=','
  for kw in $kws; do
    kw="$(echo "$kw" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [[ -z "$kw" ]] && continue
    if [[ "$kw" == *" "* ]]; then
      kw="\"$kw\""
    fi
    if [[ -z "$joined" ]]; then
      joined="$kw"
    else
      joined="$joined OR $kw"
    fi
  done
  [[ -z "$joined" ]] && return 0
  echo "-subject:($joined)"
}

# Prints this category's runtime and (unless --dry-run) appends a log entry
# for it to PRUNE_LOG.md. Relies on $name, $q, $cat_start_ts from the loop.
finish_category() {
  local outcome="$1"
  local elapsed=$(( $(date +%s) - cat_start_ts ))
  echo "Category runtime: $(fmt_duration "$elapsed")"
  if [[ "$DRY_RUN" == "false" ]]; then
    printf -- '- %s — **%s** — query: `%s` — %s — runtime %s\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$name" "$q" "$outcome" "$(fmt_duration "$elapsed")" >> "$LOGFILE"
  fi
}

RUN_START_TS=$(date +%s)

# Parallel arrays (bash 3.2 compatible — no associative arrays)
NAMES=(
  "Old promotions"
  "Social/notification noise"
  "Expired codes/resets/verifications"
  "Auto-reply/bounce noise"
  "Past calendar notifications"
  "Post-delivery shipping notices"
  "Old newsletters/digests"
  "Old read mail"
  "Old unread mail"
)

QUERIES=(
  "category:promotions older_than:${PROMO_AGE} is:unread -is:starred -is:important -label:Reference -in:trash"
  "category:social older_than:${SOCIAL_AGE} -is:starred -is:important -label:Reference -in:trash"
  "subject:(\"verification code\" OR \"reset your password\" OR \"verify your email\") older_than:${AUTH_CODE_AGE} -is:starred -is:important -label:Reference -in:trash"
  "subject:(\"out of office\" OR \"automatic reply\" OR \"delivery status notification\") older_than:${AUTOREPLY_AGE} -is:starred -is:important -label:Reference -in:trash"
  "from:calendar-notification@google.com older_than:${CALENDAR_NOTIF_AGE} -is:starred -is:important -label:Reference -in:trash"
  "from:(amazon.com OR ups.com OR fedex.com OR usps.com) subject:(delivered OR \"out for delivery\") older_than:${SHIPPING_AGE} -is:starred -is:important -label:Reference -in:trash"
  "category:updates is:read older_than:${OLD_UPDATES_AGE} -is:starred -is:important -label:Reference -in:trash"
  "is:read -is:starred -is:important -label:Reference -in:trash -in:sent -in:chats older_than:${OLD_READ_AGE} smaller:${OLD_READ_SIZE}"
  "is:unread -is:starred -is:important -label:Reference -in:trash -category:primary older_than:${OLD_UNREAD_AGE}"
)

# Engagement-based categories: senders you've curated into noisy_senders.txt
# (via find_noisy_senders.sh) get pruned more aggressively when their mail
# lands in Promotions/Updates/Social — the same scope find_noisy_senders.sh
# samples from, so a sender's transactional mail sitting in Primary isn't
# swept up just because their marketing mail is noisy.
#
# Senders with subject exclusions (e.g. "statement,payment due") get their
# own category below, excluding those subjects for that sender specifically
# — for the common case of one address sending both keepable mail (account
# statements) and disposable mail (promos) under the same From address.
parse_noisy_senders

NOISY_QUERY_FRAGMENT=$(build_noisy_query)
if [[ -n "$NOISY_QUERY_FRAGMENT" ]]; then
  NAMES+=("Noisy senders (low engagement)")
  QUERIES+=("(category:promotions OR category:updates OR category:social) $NOISY_QUERY_FRAGMENT older_than:${NOISY_AGE} -is:starred -is:important -label:Reference -in:trash")
fi

for i in "${!NOISY_EXCL_ADDR[@]}"; do
  addr="${NOISY_EXCL_ADDR[$i]}"
  kws="${NOISY_EXCL_KW[$i]}"
  excl=$(build_subject_exclusion "$kws")
  NAMES+=("Noisy sender: ${addr} (excluding: ${kws})")
  QUERIES+=("(category:promotions OR category:updates OR category:social) from:(${addr}) ${excl} older_than:${NOISY_AGE} -is:starred -is:important -label:Reference -in:trash")
done

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"
  q="${QUERIES[$i]}"
  cat_start_ts=$(date +%s)

  echo "=========================================="
  echo "Category: $name"
  echo "Query:    $q"
  echo "------------------------------------------"

  params=$(jq -nc --arg q "$q" '{"userId":"me","q":$q,"maxResults":500}')
  result=$(gws gmail users messages list --params "$params" --format json)

  err=$(echo "$result" | jq -r '.error.message // empty')
  if [[ -n "$err" ]]; then
    echo "API error: $err"
    echo "Skipping this category."
    finish_category "API error: $err"
    continue
  fi

  count=$(echo "$result" | jq -r '.resultSizeEstimate // 0')
  echo "Estimated matches: $count"

  if [[ "$count" -eq 0 ]]; then
    echo "Nothing to do."
    finish_category "nothing to do (0 matches)"
    continue
  fi

  echo "Sample (up to 10):"
  sample_params=$(jq -nc --arg q "$q" '{"userId":"me","q":$q,"maxResults":10}')
  gws gmail users messages list --params "$sample_params" --format table || true
  echo ""

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "(dry run — not trashing)"
    finish_category "dry run — not trashed"
    continue
  fi

  read -r -p "Move ~${count} matching message(s) to Trash (contacts and replied threads will be skipped)? [y/N] " answer
  case "$answer" in
    [yY]*) ;;
    *)
      echo "Skipped."
      finish_category "skipped by user (0 trashed)"
      continue
      ;;
  esac

  PRUNED_LABEL_ID=$(get_or_create_label_id "Audit/Pruned/${name}")
  if [[ -z "$PRUNED_LABEL_ID" ]]; then
    echo "Warning: could not find/create label 'Audit/Pruned/${name}' — pruned mail won't be labeled."
  fi

  # Gather all candidate message IDs up front. Doing this before any
  # trashing avoids re-running the search mid-loop, which would otherwise
  # keep re-matching messages we deliberately skipped (contacts/replies).
  list_params=$(jq -nc --arg q "$q" '{"userId":"me","q":$q,"maxResults":500}')
  all_ids=$(gws gmail users messages list --params "$list_params" --page-all --page-limit 50 --format json \
    | jq -r '.messages[]?.id // empty')

  total=$(printf '%s\n' "$all_ids" | grep -c . || true)

  trashed=0
  skipped_protected=0
  processed=0
  while IFS= read -r id; do
    [[ -z "$id" ]] && continue
    processed=$((processed + 1))
    pct=$(( total > 0 ? processed * 100 / total : 100 ))

    if should_protect "$id"; then
      skipped_protected=$((skipped_protected + 1))
      printf '\r  [%d/%d] %d%% — moved %d, skipped %d (contact/replied)%s   ' \
        "$processed" "$total" "$pct" "$trashed" "$skipped_protected" \
        "$(progress_suffix "$processed" "$total" "$cat_start_ts")"
      continue
    fi

    trash_params=$(jq -nc --arg id "$id" '{"userId":"me","id":$id}')
    gws gmail users messages trash --params "$trash_params" >/dev/null
    if [[ -n "$PRUNED_LABEL_ID" ]]; then
      modify_body=$(jq -nc --arg lid "$PRUNED_LABEL_ID" '{"addLabelIds":[$lid]}')
      gws gmail users messages modify --params "$trash_params" --json "$modify_body" >/dev/null
    fi
    log_audit_event "$AUDIT_LOG" "pruned" "$name" "$id" "$LAST_FROM_EMAIL" "$LAST_SUBJECT"
    trashed=$((trashed + 1))
    printf '\r  [%d/%d] %d%% — moved %d, skipped %d (contact/replied)%s   ' \
      "$processed" "$total" "$pct" "$trashed" "$skipped_protected" \
      "$(progress_suffix "$processed" "$total" "$cat_start_ts")"
    sleep 0.05
  done <<< "$all_ids"
  [[ "$total" -gt 0 ]] && echo ""

  echo "Moved $trashed message(s) to Trash, skipped $skipped_protected (contact/replied thread), in category: $name"
  finish_category "moved $trashed to Trash, skipped $skipped_protected (contact/replied)"
done

TOTAL_ELAPSED=$(( $(date +%s) - RUN_START_TS ))
echo "=========================================="
echo "Done. Total runtime: $(fmt_duration "$TOTAL_ELAPSED")"
if [[ "$DRY_RUN" == "false" ]]; then
  echo "Run log: $LOGFILE"
  printf -- '- %s — **Run complete** — total runtime %s\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$(fmt_duration "$TOTAL_ELAPSED")" >> "$LOGFILE"
fi
