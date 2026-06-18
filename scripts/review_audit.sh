#!/usr/bin/env bash
#
# Read-only summary of ../audit_log.jsonl (written by prune.sh and
# rescue.sh — one JSON line per pruned or rescued message, with timestamp,
# category, sender, subject, and message ID).
#
# Use this periodically to spot prune rules worth tuning. The clearest
# signal is a message (or sender) that was pruned by one category and later
# rescued — that's a near-miss, and the rescued category/rule tells you what
# the prune query should also exclude (e.g. via -label:Reference, an
# additional label_reference.sh rule, or a tighter query).
#
# Usage:
#   ./review_audit.sh             All-time summary
#   ./review_audit.sh --since 30  Only entries from the last 30 days
#
# Requires: jq
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIT_LOG="$SCRIPT_DIR/../audit_log.jsonl"

command -v jq >/dev/null 2>&1 || { echo "jq not found. Install it (e.g. 'brew install jq')."; exit 1; }

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

# shellcheck disable=SC2034  # used inside single-quoted trap string (expanded at exit time)
RUN_START_TS=$(date +%s)
trap 'echo "Total runtime: $(fmt_duration $(( $(date +%s) - RUN_START_TS )))"' EXIT

if [[ ! -s "$AUDIT_LOG" ]]; then
  echo "No audit log found at $AUDIT_LOG yet."
  echo "Run prune.sh and/or rescue.sh first — they append to it automatically."
  exit 0
fi

JQ_ARGS=()
JQ_FILTER="."
if [[ "${1:-}" == "--since" && -n "${2:-}" ]]; then
  SINCE_DAYS="$2"
  CUTOFF=$(date -u -v-"${SINCE_DAYS}"d '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null) \
    || CUTOFF=$(date -u -d "-${SINCE_DAYS} days" '+%Y-%m-%dT%H:%M:%SZ')
  # shellcheck disable=SC2016  # $cutoff is a jq variable, not a shell variable
  JQ_FILTER='select(.ts >= $cutoff)'
  JQ_ARGS=(--arg cutoff "$CUTOFF")
  echo "Audit log summary — last $SINCE_DAYS day(s) (since $CUTOFF UTC)"
else
  echo "Audit log summary — all time"
fi
echo "Source: $AUDIT_LOG"
echo "=========================================="
echo ""

DATA=$(jq -c "${JQ_ARGS[@]}" "$JQ_FILTER" "$AUDIT_LOG")

if [[ -z "$DATA" ]]; then
  echo "No entries match."
  exit 0
fi

total_pruned=$(echo "$DATA" | jq -s '[.[] | select(.action=="pruned")] | length')
total_rescued=$(echo "$DATA" | jq -s '[.[] | select(.action=="rescued")] | length')
echo "Totals: $total_pruned pruned, $total_rescued rescued"
echo ""

echo "Per-category totals:"
printf '%-45s %8s %8s\n' "Category" "Pruned" "Rescued"
printf '%-45s %8s %8s\n' "---------------------------------------------" "------" "-------"
echo "$DATA" | jq -s -r '
  group_by(.category) | map({
    category: .[0].category,
    pruned: (map(select(.action=="pruned")) | length),
    rescued: (map(select(.action=="rescued")) | length)
  }) | sort_by(-.pruned) | .[] |
  [.category, (.pruned|tostring), (.rescued|tostring)] | @tsv
' | while IFS=$'\t' read -r cat pruned rescued; do
  printf '%-45s %8s %8s\n' "$cat" "$pruned" "$rescued"
done

echo ""
echo "Senders pruned AND rescued at least once (possible over-aggressive rules):"
printf '%-40s %8s %8s\n' "Sender" "Pruned" "Rescued"
printf '%-40s %8s %8s\n' "----------------------------------------" "------" "-------"
SENDER_ROWS=$(echo "$DATA" | jq -s -r '
  group_by(.from) | map({
    from: .[0].from,
    pruned: (map(select(.action=="pruned")) | length),
    rescued: (map(select(.action=="rescued")) | length)
  }) | map(select(.from != "" and .pruned > 0 and .rescued > 0))
    | sort_by(-.rescued) | .[] |
  [.from, (.pruned|tostring), (.rescued|tostring)] | @tsv
')
if [[ -z "$SENDER_ROWS" ]]; then
  echo "(none)"
else
  echo "$SENDER_ROWS" | while IFS=$'\t' read -r from pruned rescued; do
    printf '%-40s %8s %8s\n' "$from" "$pruned" "$rescued"
  done
fi

echo ""
echo "Messages pruned then later rescued (exact near-misses):"
printf '%-30s %-30s %-30s %s\n' "Pruned as" "Rescued as" "Sender" "Subject"
printf '%-30s %-30s %-30s %s\n' "------------------------------" "------------------------------" "------------------------------" "-------"
MSG_ROWS=$(echo "$DATA" | jq -s -r '
  group_by(.message_id)
  | map(select(
      (map(select(.action=="pruned")) | length) > 0
      and (map(select(.action=="rescued")) | length) > 0
    ))
  | .[]
  | (map(select(.action=="pruned")) | .[0]) as $p
  | (map(select(.action=="rescued")) | .[0]) as $r
  | [$p.category, $r.category, $p.from, $p.subject] | @tsv
')
if [[ -z "$MSG_ROWS" ]]; then
  echo "(none)"
else
  echo "$MSG_ROWS" | while IFS=$'\t' read -r pruned_cat rescued_cat from subject; do
    printf '%-30s %-30s %-30s %s\n' "$pruned_cat" "$rescued_cat" "$from" "$subject"
  done
fi

echo ""
echo "Tips:"
echo "- A sender/message above suggests the 'Pruned as' query is too broad for"
echo "  that sender/subject pattern. Consider adding a label_reference.sh rule"
echo "  (and matching rescue.sh rule) so it's protected before it's ever pruned."
echo "- High-volume entries in noisy_senders.txt that also show up as rescued"
echo "  should probably be removed from that file."
