#!/usr/bin/env bash
#
# Proactively applies the "Reference" Gmail label to mail worth keeping
# long-term — receipts, policy docs, travel confirmations, specific
# senders, etc. — wherever it currently lives (Inbox, Archive, etc., but
# not Trash).
#
# prune.sh excludes -label:Reference from every category, so labeled mail
# is never trashed regardless of which prune category it would otherwise
# match. rescue.sh also applies this label to anything it restores from
# Trash, so rescued mail stays protected going forward.
#
# This script only adds labels — it never trashes, deletes, or restores
# anything. Safe to re-run anytime (already-labeled mail is skipped via
# -label:Reference in each query).
#
# Each successful (non-dry-run) run touches ../.label_reference_last_run.
# prune.sh checks that timestamp and warns (offering to run this script) if
# it's gotten stale — see REFERENCE_MAX_AGE_DAYS in prune.sh.
#
# Add new rules here as you discover senders/subjects worth keeping
# long-term — consider adding the analogous rule to rescue.sh too, so
# anything matching also gets rescued if it's ever trashed before this
# script runs.
#
# Usage:
#   ./label_reference.sh            Interactive: shows counts + samples, asks before labeling each category
#   ./label_reference.sh --dry-run  Preview only: shows counts + samples, labels nothing
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
LABEL_NAME="Reference"

command -v gws >/dev/null 2>&1 || { echo "gws not found on PATH. See ../SETUP.md."; exit 1; }
command -v jq  >/dev/null 2>&1 || { echo "jq not found. Install it (e.g. 'brew install jq')."; exit 1; }

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

RUN_START_TS=$(date +%s)

LABEL_ID=$(get_or_create_label_id "$LABEL_NAME")
if [[ -z "$LABEL_ID" ]]; then
  echo "Could not find or create the '$LABEL_NAME' label. Check auth/scopes (see ../SETUP.md)."
  exit 1
fi
echo "Using label: $LABEL_NAME ($LABEL_ID)"
echo ""

# Parallel arrays (bash 3.2 compatible — no associative arrays).
# Mirrors rescue.sh's "keep" rules, but applied across the whole mailbox
# (minus Trash and anything already labeled) and without the age bounds
# that make sense for "has this been sitting in Trash a while".
NAMES=(
  "Receipts/statements/orders"
  "Policy/terms docs"
  "Travel confirmations"
  "Account security alerts (recent)"
  "Warranty/registration docs"
)

QUERIES=(
  "subject:(statement OR invoice OR order) -label:${LABEL_NAME} -in:trash"
  "subject:(policy OR terms) -label:${LABEL_NAME} -in:trash"
  "subject:(itinerary OR \"boarding pass\" OR \"flight confirmation\" OR \"hotel confirmation\" OR \"reservation confirmation\") -category:promotions -label:${LABEL_NAME} -in:trash"
  "subject:(\"security alert\" OR \"new sign-in\" OR \"password changed\") newer_than:1y -label:${LABEL_NAME} -in:trash"
  "subject:(\"product registration\" OR \"registration confirmation\" OR warranty OR \"proof of purchase\") -category:promotions -label:${LABEL_NAME} -in:trash"
)

# Personal sender/subject rules live outside this script — see
# config/keep_rules.local.tsv (gitignored) and
# config/keep_rules.local.example.tsv for the format.
load_local_keep_rules "reference" "$LABEL_NAME"

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
    continue
  fi

  count=$(echo "$result" | jq -r '.resultSizeEstimate // 0')
  echo "Estimated matches: $count"

  if [[ "$count" -eq 0 ]]; then
    echo "Nothing to label."
    continue
  fi

  echo "Sample (up to 10):"
  sample_params=$(jq -nc --arg q "$q" '{"userId":"me","q":$q,"maxResults":10}')
  gws gmail users messages list --params "$sample_params" --format table || true
  echo ""

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "(dry run — not labeling)"
    continue
  fi

  read -r -p "Apply '$LABEL_NAME' label to ~${count} matching message(s)? [y/N] " answer
  case "$answer" in
    [yY]*) ;;
    *)
      echo "Skipped."
      continue
      ;;
  esac

  list_params=$(jq -nc --arg q "$q" '{"userId":"me","q":$q,"maxResults":500}')
  all_ids=$(gws gmail users messages list --params "$list_params" --page-all --page-limit 50 --format json \
    | jq -r '.messages[]?.id // empty')

  total=$(printf '%s\n' "$all_ids" | grep -c . || true)

  labeled=0
  processed=0
  while IFS= read -r id; do
    [[ -z "$id" ]] && continue
    processed=$((processed + 1))
    pct=$(( total > 0 ? processed * 100 / total : 100 ))

    modify_params=$(jq -nc --arg id "$id" '{"userId":"me","id":$id}')
    modify_body=$(jq -nc --arg lid "$LABEL_ID" '{"addLabelIds":[$lid]}')
    gws gmail users messages modify --params "$modify_params" --json "$modify_body" >/dev/null
    labeled=$((labeled + 1))
    printf '\r  [%d/%d] %d%% — labeled %d%s   ' "$processed" "$total" "$pct" "$labeled" \
      "$(progress_suffix "$processed" "$total" "$cat_start_ts")"
    sleep 0.05
  done <<< "$all_ids"
  [[ "$total" -gt 0 ]] && echo ""

  cat_elapsed=$(( $(date +%s) - cat_start_ts ))
  echo "Labeled $labeled message(s) in category: $name (runtime $(fmt_duration "$cat_elapsed"))"
  printf -- '- %s — **%s** (reference) — query: `%s` — labeled %s — runtime %s\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$name" "$q" "$labeled" "$(fmt_duration "$cat_elapsed")" >> "$LOGFILE"
done

TOTAL_ELAPSED=$(( $(date +%s) - RUN_START_TS ))
echo "=========================================="
echo "Done. Total runtime: $(fmt_duration "$TOTAL_ELAPSED")"
if [[ "$DRY_RUN" == "false" ]]; then
  echo "Run log: $LOGFILE"
  # Lets prune.sh warn if this script hasn't run recently (see
  # REFERENCE_MAX_AGE_DAYS in prune.sh) — Reference labeling is the main
  # protection for "noisy" senders that also send mail worth keeping.
  touch "$SCRIPT_DIR/../.label_reference_last_run"
fi
