#!/usr/bin/env bash
#
# Reports senders worth adding to ../noisy_senders.txt: high-volume,
# low-engagement senders in Promotions/Updates/Social, regardless of
# whether Gmail's own categories already cover them.
#
# This is read-only — it doesn't trash or label anything. Review the
# report and copy addresses you recognize into ../noisy_senders.txt; then
# prune.sh's "Noisy senders (low engagement)" category will pick them up.
#
# Usage: ./find_noisy_senders.sh [--debug]
#
# --debug echoes each gws/jq command before it runs, shows gws's raw stderr
# instead of suppressing it, and reports the message-list call's timing and
# raw id counts. Use it if the script produces no output, seems to hang, or
# finds no senders and the regular diagnostics below aren't enough to tell
# why.
#
# Requires: gws (authenticated, see ../SETUP.md), jq
#
set -euo pipefail

DEBUG=false
if [[ "${1:-}" == "--debug" ]]; then
  DEBUG=true
fi

# ---- Thresholds (edit to taste) ----
LOOKBACK="90d"      # how far back to sample
SAMPLE_LIMIT=300    # max messages to inspect (keeps runtime reasonable)
MIN_COUNT=5         # only report senders with at least this many messages
MAX_READ_PCT=10     # ...and at most this % read

# ---- Setup ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v gws >/dev/null 2>&1 || { echo "gws not found on PATH. See ../SETUP.md."; exit 1; }
command -v jq  >/dev/null 2>&1 || { echo "jq not found. Install it (e.g. 'brew install jq')."; exit 1; }

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

RUN_START_TS=$(date +%s)

DATA_FILE="$(mktemp)"
ERR_FILE="$(mktemp)"
trap 'rm -f "$DATA_FILE" "$ERR_FILE"' EXIT

QUERY="(category:promotions OR category:updates OR category:social) -in:trash -is:starred -is:important -label:Reference older_than:${LOOKBACK}"

echo "Sampling up to $SAMPLE_LIMIT message(s) matching:"
echo "  $QUERY"
echo ""

# A single page with maxResults=500 already covers SAMPLE_LIMIT (300), so
# --page-all isn't needed here. --page-all was paginating through *every*
# matching message in the whole lookback window (potentially thousands),
# which made this call slow enough to look like a hang and produced no
# output at all until it either finished or panicked on a broken pipe to
# `head`. One page is enough for a sample.
list_params=$(jq -nc --arg q "$QUERY" '{"userId":"me","q":$q,"maxResults":500}')
echo "+ gws gmail users messages list --params '$list_params' --format json | jq -r '.messages[]?.id // empty'"
if [[ "$DEBUG" == "true" ]]; then
  echo "[debug] running message-list call..."
fi

list_start_ts=$(date +%s)
if [[ "$DEBUG" == "true" ]]; then
  # In debug mode let gws's stderr go straight to the terminal instead of a
  # file, so auth prompts / scope errors / rate limiting show up immediately.
  list_result=$(gws gmail users messages list --params "$list_params" --format json)
else
  if ! list_result=$(gws gmail users messages list --params "$list_params" --format json 2>"$ERR_FILE"); then
    echo ""
    echo "Error: the message-list call failed:"
    sed 's/^/  /' "$ERR_FILE"
    echo ""
    echo "Check auth (gws auth login -s gmail) and try again, or re-run with"
    echo "--debug to see gws's raw output as it runs."
    exit 1
  fi
fi
if [[ "$DEBUG" == "true" ]]; then
  echo "[debug] message-list call took $(fmt_duration $(( $(date +%s) - list_start_ts )))"
fi

list_err=$(echo "$list_result" | jq -r '.error.message // empty')
if [[ -n "$list_err" ]]; then
  echo ""
  echo "Error from Gmail API: $list_err"
  exit 1
fi

all_ids_full=$(echo "$list_result" | jq -r '.messages[]?.id // empty')
fetched=$(printf '%s\n' "$all_ids_full" | grep -c . || true)
all_ids=$(printf '%s\n' "$all_ids_full" | head -n "$SAMPLE_LIMIT")
total=$(printf '%s\n' "$all_ids" | grep -c . || true)

echo "Fetched $fetched message ID(s) matching the query; sampling $total of them."
echo ""

if [[ "$total" -eq 0 ]]; then
  echo "No messages matched — nothing to analyze."
  echo "If this is unexpected, double-check the query above. You can run it"
  echo "directly with:"
  echo "  gws gmail users messages list --params '$list_params' --format table"
  echo "or re-run this script with --debug to see the raw API response."
  exit 0
fi

processed=0
extracted=0
failed_gets=0
while IFS= read -r id; do
  [[ -z "$id" ]] && continue
  processed=$((processed + 1))
  pct=$(( total > 0 ? processed * 100 / total : 100 ))

  get_params=$(jq -nc --arg id "$id" '{"userId":"me","id":$id,"format":"metadata","metadataHeaders":["From"]}')
  if [[ "$DEBUG" == "true" ]]; then
    echo ""
    echo "+ gws gmail users messages get --params '$get_params' --format json"
  fi
  if ! meta=$(gws gmail users messages get --params "$get_params" --format json 2>"$ERR_FILE"); then
    failed_gets=$((failed_gets + 1))
    if [[ "$failed_gets" -eq 1 ]]; then
      echo ""
      echo "Warning: messages.get failed for id $id (showing first failure only):"
      sed 's/^/  /' "$ERR_FILE"
    fi
    printf '\r  [%d/%d] %d%% scanned%s   ' "$processed" "$total" "$pct" \
      "$(progress_suffix "$processed" "$total" "$RUN_START_TS")"
    sleep 0.05
    continue
  fi

  # head -n1 guards against duplicate "From" headers producing multiple
  # lines, which would otherwise corrupt the tab-separated DATA_FILE.
  #
  # Both lines below end in `|| true`: under `set -eo pipefail`, an
  # unconditional `var=$(...)` assignment whose pipeline has ANY non-zero
  # exit status (e.g. jq failing on unexpected/empty JSON, or grep finding
  # no match when a message has no usable From header) kills the whole
  # script right here with no error message — which is what was causing
  # this script to silently stop partway through the scan.
  from_header=$(echo "$meta" | jq -r '.payload.headers[]? | select(.name=="From") | .value' 2>/dev/null | head -n1) || true
  from_email=$(echo "$from_header" | grep -oE '[^[:space:]<]+@[^[:space:]>]+' | tr '[:upper:]' '[:lower:]' | head -n1) || true

  if [[ -n "$from_email" ]]; then
    is_read=1
    echo "$meta" | jq -e '.labelIds // [] | index("UNREAD")' >/dev/null 2>&1 && is_read=0
    is_starred=0
    echo "$meta" | jq -e '.labelIds // [] | index("STARRED")' >/dev/null 2>&1 && is_starred=1
    printf '%s\t%s\t%s\n' "$from_email" "$is_read" "$is_starred" >> "$DATA_FILE"
    extracted=$((extracted + 1))
  fi

  printf '\r  [%d/%d] %d%% scanned%s   ' "$processed" "$total" "$pct" \
    "$(progress_suffix "$processed" "$total" "$RUN_START_TS")"
  sleep 0.05
done <<< "$all_ids"
echo ""
echo ""

if [[ "$failed_gets" -gt 0 ]]; then
  echo "Note: messages.get failed for $failed_gets/$total sampled message(s) — see warning above."
fi
echo "Extracted a sender address from $extracted/$total sampled message(s)."
echo ""

echo "Senders with >= $MIN_COUNT message(s) and <= $MAX_READ_PCT% read (last $LOOKBACK, sample of $total):"
echo ""
printf '%-40s %8s %8s %10s\n' "Sender" "Count" "Read %" "Starred"
printf '%-40s %8s %8s %10s\n' "----------------------------------------" "-----" "------" "----------"

RESULTS=$(awk -F'\t' -v mincount="$MIN_COUNT" -v maxreadpct="$MAX_READ_PCT" '
{
  count[$1]++
  if ($2 == 1) read[$1]++
  if ($3 == 1) starred[$1]++
}
END {
  for (s in count) {
    pct = (count[s] > 0) ? int(read[s] * 100 / count[s]) : 0
    if (count[s] >= mincount && pct <= maxreadpct) {
      printf "%-40s %8d %7d%% %10d\n", s, count[s], pct, starred[s]+0
    }
  }
}' "$DATA_FILE" | sort -k2 -nr)

if [[ -z "$RESULTS" ]]; then
  echo "(none matched MIN_COUNT=$MIN_COUNT / MAX_READ_PCT=$MAX_READ_PCT in this sample)"
  echo "If 'Extracted' above is well below the sample size, see the warning for why"
  echo "messages.get is failing. Otherwise, try lowering MIN_COUNT, raising"
  echo "MAX_READ_PCT, or increasing SAMPLE_LIMIT/LOOKBACK at the top of this script."
else
  echo "$RESULTS"
fi

echo ""
echo "Add any addresses above to ../noisy_senders.txt (one per line) to have"
echo "prune.sh trash their mail more aggressively (NOISY_AGE, default 30d),"
echo "scoped to category:promotions/updates/social. Double-check before"
echo "adding — starred messages from a sender are a signal you sometimes"
echo "care about them."
echo ""
echo "If a sender above also sends mail worth keeping under the same address"
echo "(e.g. account statements mixed with marketing), add it with a subject"
echo "exclusion instead — see the format notes at the top of"
echo "../noisy_senders.txt."

TOTAL_ELAPSED=$(( $(date +%s) - RUN_START_TS ))
echo ""
echo "Total runtime: $(fmt_duration "$TOTAL_ELAPSED")"
