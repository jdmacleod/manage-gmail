#!/usr/bin/env bash
#
# Gmail rescue script — scans Trash for messages worth keeping and offers to
# restore them via `gws gmail users messages untrash`. Run this periodically
# (especially after prune.sh, and before Trash auto-empties after ~30 days).
#
# Anything restored also gets the "Reference" label applied, so prune.sh
# won't trash it again later (see scripts/label_reference.sh), plus an
# "Audit/Rescued/<category>" label and an entry in ../audit_log.jsonl
# (timestamp, category, sender, subject, message id). Run
# scripts/review_audit.sh periodically — senders that show up in both
# pruned and rescued events are a signal a prune rule is too aggressive.
#
# Usage:
#   ./rescue.sh            Interactive: shows counts + samples, asks before restoring each category
#   ./rescue.sh --dry-run  Preview only: shows counts + samples, restores nothing
#
# Requires: gws (authenticated, see ../SETUP.md), jq
#
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# ---- Setup ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGFILE="$SCRIPT_DIR/../PRUNE_LOG.md"
AUDIT_LOG="$SCRIPT_DIR/../audit_log.jsonl"
LABEL_NAME="Reference"

command -v gws >/dev/null 2>&1 || { echo "gws not found on PATH. See ../SETUP.md."; exit 1; }
command -v jq  >/dev/null 2>&1 || { echo "jq not found. Install it (e.g. 'brew install jq')."; exit 1; }

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

RUN_START_TS=$(date +%s)

REFERENCE_LABEL_ID=""
if [[ "$DRY_RUN" == "false" ]]; then
  REFERENCE_LABEL_ID=$(get_or_create_label_id "$LABEL_NAME")
  if [[ -z "$REFERENCE_LABEL_ID" ]]; then
    echo "Warning: could not find or create the '$LABEL_NAME' label — restored"
    echo "messages won't be labeled. Check auth/scopes (see ../SETUP.md)."
  fi
fi

# Parallel arrays (bash 3.2 compatible — no associative arrays).
# Add new rules here as you discover senders/subjects that keep getting
# swept into Trash but should be kept.
NAMES=(
  "Receipts/statements/orders (1y+)"
  "Policy/terms docs (1y+)"
  "Travel confirmations"
  "Account security alerts (recent)"
  "Warranty/registration docs"
)

QUERIES=(
  "in:trash older_than:1y subject:(statement OR invoice OR order)"
  "in:trash older_than:1y subject:(policy OR terms)"
  "in:trash subject:(itinerary OR \"boarding pass\" OR \"flight confirmation\" OR \"hotel confirmation\" OR \"reservation confirmation\") -category:promotions"
  "in:trash subject:(\"security alert\" OR \"new sign-in\" OR \"password changed\") newer_than:1y"
  "in:trash subject:(\"product registration\" OR \"registration confirmation\" OR warranty OR \"proof of purchase\") -category:promotions"
)

# Personal sender/subject rules (e.g. specific people or services you want
# rescued regardless of age) live outside this script — see
# config/keep_rules.local.tsv (gitignored) and
# config/keep_rules.local.example.tsv for the format.
load_local_keep_rules "rescue"

# Prints this category's runtime and (unless --dry-run) appends a log entry
# for it to PRUNE_LOG.md. Relies on $name, $q, $cat_start_ts from the loop.
finish_category() {
  local outcome="$1"
  local elapsed=$(( $(date +%s) - cat_start_ts ))
  echo "Category runtime: $(fmt_duration "$elapsed")"
  if [[ "$DRY_RUN" == "false" ]]; then
    printf -- '- %s — **%s** (rescue) — query: `%s` — %s — runtime %s\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$name" "$q" "$outcome" "$(fmt_duration "$elapsed")" >> "$LOGFILE"
  fi
}

# Logs a per-message API failure (e.g. "Precondition check failed", which
# Google's API returns when a message's state has changed — already
# restored/deleted/relabeled — since the candidate list was built) to the
# terminal and PRUNE_LOG.md, then lets the category continue with the next
# message. No retry: re-running rescue.sh is safe, since anything already
# restored just won't match `in:trash` again. Relies on $name, $LOGFILE,
# $DRY_RUN from the enclosing loop.
log_api_failure() {
  local action="$1" id="$2" err="$3"
  local oneline
  oneline=$(printf '%s' "$err" | grep -v '^Using keyring backend' | tr '\n' ' ' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//') || true
  echo ""
  echo "Warning: $action failed for message $id: $oneline"
  if [[ "$DRY_RUN" == "false" ]]; then
    printf -- '- %s — **%s** (rescue) — WARNING: %s failed for message `%s`: %s\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$name" "$action" "$id" "$oneline" >> "$LOGFILE"
  fi
}

RUN_FAILED_TOTAL=0

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
    echo "Nothing to restore."
    finish_category "nothing to do (0 matches)"
    continue
  fi

  echo "Sample (up to 10):"
  sample_params=$(jq -nc --arg q "$q" '{"userId":"me","q":$q,"maxResults":10}')
  gws gmail users messages list --params "$sample_params" --format table || true
  echo ""

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "(dry run — not restoring)"
    finish_category "dry run — not restored"
    continue
  fi

  read -r -p "Restore ~${count} matching message(s) from Trash? [y/N] " answer
  case "$answer" in
    [yY]*) ;;
    *)
      echo "Skipped."
      finish_category "skipped by user (0 restored)"
      continue
      ;;
  esac

  RESCUED_LABEL_ID=$(get_or_create_label_id "Audit/Rescued/${name}")
  if [[ -z "$RESCUED_LABEL_ID" ]]; then
    echo "Warning: could not find/create label 'Audit/Rescued/${name}' — restored mail won't get this label."
  fi

  # $count (resultSizeEstimate above) is an estimate, so the progress bar's
  # denominator may run a little ahead of or behind the actual total — close
  # enough for a progress indicator.
  restored=0
  failed=0
  processed=0
  max_batches=1000
  batch=0
  while [[ $batch -lt $max_batches ]]; do
    batch=$((batch + 1))
    batch_params=$(jq -nc --arg q "$q" '{"userId":"me","q":$q,"maxResults":100}')
    page=$(gws gmail users messages list --params "$batch_params" --format json)

    ids=$(echo "$page" | jq -r '.messages // [] | .[].id')
    [[ -z "$ids" ]] && break

    while IFS= read -r id; do
      [[ -z "$id" ]] && continue
      processed=$((processed + 1))
      pct=$(( count > 0 ? processed * 100 / count : 100 ))
      (( pct > 100 )) && pct=100

      meta=$(gws gmail users messages get \
        --params "$(jq -nc --arg id "$id" '{"userId":"me","id":$id,"format":"metadata","metadataHeaders":["From","Subject"]}')" \
        --format json 2>/dev/null) || meta=""
      from_email=$(extract_email "$(header_value "$meta" "From")")
      subject=$(header_value "$meta" "Subject")

      untrash_params=$(jq -nc --arg id "$id" '{"userId":"me","id":$id}')
      # If the message's state changed since it was listed (e.g. already
      # restored/deleted by another client), the API can return an error
      # such as "Precondition check failed" here. Don't let `set -e` kill
      # the whole run over one message — log it and move on.
      if ! untrash_err=$(gws gmail users messages untrash --params "$untrash_params" 2>&1 >/dev/null); then
        failed=$((failed + 1))
        log_api_failure "untrash" "$id" "$untrash_err"
        printf '\r  [%d/%d] %d%% — restored %d, failed %d%s   ' \
          "$processed" "$count" "$pct" "$restored" "$failed" \
          "$(progress_suffix "$processed" "$count" "$cat_start_ts")"
        sleep 0.05
        continue
      fi

      add_label_ids=()
      [[ -n "$REFERENCE_LABEL_ID" ]] && add_label_ids+=("$REFERENCE_LABEL_ID")
      [[ -n "$RESCUED_LABEL_ID" ]] && add_label_ids+=("$RESCUED_LABEL_ID")
      if [[ ${#add_label_ids[@]} -gt 0 ]]; then
        modify_body=$(printf '%s\n' "${add_label_ids[@]}" | jq -R . | jq -sc '{addLabelIds: .}')
        if ! modify_err=$(gws gmail users messages modify --params "$untrash_params" --json "$modify_body" 2>&1 >/dev/null); then
          # Message is already restored at this point — just missing its
          # Reference/Audit labels. Log and keep going; not counted as
          # "failed".
          log_api_failure "label" "$id" "$modify_err"
        fi
      fi

      log_audit_event "$AUDIT_LOG" "rescued" "$name" "$id" "$from_email" "$subject"

      restored=$((restored + 1))
      printf '\r  [%d/%d] %d%% — restored %d, failed %d%s   ' \
        "$processed" "$count" "$pct" "$restored" "$failed" \
        "$(progress_suffix "$processed" "$count" "$cat_start_ts")"
      sleep 0.05
    done <<< "$ids"
  done
  [[ "$processed" -gt 0 ]] && echo ""

  if [[ "$failed" -gt 0 ]]; then
    echo "Restored $restored message(s), failed $failed (see $LOGFILE), in category: $name"
  else
    echo "Restored $restored message(s) in category: $name"
  fi
  RUN_FAILED_TOTAL=$((RUN_FAILED_TOTAL + failed))
  finish_category "restored $restored message(s), failed $failed"
done

TOTAL_ELAPSED=$(( $(date +%s) - RUN_START_TS ))
echo "=========================================="
if [[ "$RUN_FAILED_TOTAL" -gt 0 ]]; then
  echo "Done. Total runtime: $(fmt_duration "$TOTAL_ELAPSED") — $RUN_FAILED_TOTAL API failure(s), see $LOGFILE"
else
  echo "Done. Total runtime: $(fmt_duration "$TOTAL_ELAPSED")"
fi
if [[ "$DRY_RUN" == "false" ]]; then
  echo "Run log: $LOGFILE"
  if [[ "$RUN_FAILED_TOTAL" -gt 0 ]]; then
    printf -- '- %s — **Run complete** — total runtime %s — %d API failure(s)\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$(fmt_duration "$TOTAL_ELAPSED")" "$RUN_FAILED_TOTAL" >> "$LOGFILE"
  else
    printf -- '- %s — **Run complete** — total runtime %s\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$(fmt_duration "$TOTAL_ELAPSED")" >> "$LOGFILE"
  fi
fi
