#!/usr/bin/env bash
#
# Exports email addresses from Google Contacts ("connections") and "Other
# contacts" (auto-saved from interactions) to ../contacts_emails.txt.
#
# prune.sh uses this file to skip trashing any message whose sender matches
# one of these addresses, regardless of category.
#
# Requires the `people` OAuth scope. If you authenticated with only
# `gws auth login -s gmail`, re-run:
#   gws auth login -s gmail,people
#
# Usage: ./export_contacts.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SCRIPT_DIR/../contacts_emails.txt"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

command -v gws >/dev/null 2>&1 || { echo "gws not found on PATH. See ../SETUP.md."; exit 1; }
command -v jq  >/dev/null 2>&1 || { echo "jq not found. Install it (e.g. 'brew install jq')."; exit 1; }

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

RUN_START_TS=$(date +%s)

# These each make one or more paginated API calls (--page-all), so a large
# contact list can take a little while — report elapsed time per step plus a
# total at the end.
step_start_ts=$(date +%s)
echo "Exporting Contacts..."
gws people people connections list \
  --params '{"resourceName":"people/me","personFields":"emailAddresses","pageSize":1000}' \
  --page-all --page-limit 50 --format json \
  | jq -r '.connections[]?.emailAddresses[]?.value // empty' > "$TMP_DIR/connections.txt" \
  || echo "  (warning: connections.list failed — check 'people' scope, see ../SETUP.md)"
echo "  done (elapsed $(fmt_duration $(( $(date +%s) - step_start_ts ))))"

step_start_ts=$(date +%s)
echo "Exporting Other contacts..."
gws people otherContacts list \
  --params '{"readMask":"emailAddresses","pageSize":1000}' \
  --page-all --page-limit 50 --format json \
  | jq -r '.otherContacts[]?.emailAddresses[]?.value // empty' > "$TMP_DIR/other.txt" \
  || echo "  (warning: otherContacts.list failed — check 'people' scope, see ../SETUP.md)"
echo "  done (elapsed $(fmt_duration $(( $(date +%s) - step_start_ts ))))"

cat "$TMP_DIR/connections.txt" "$TMP_DIR/other.txt" 2>/dev/null \
  | tr '[:upper:]' '[:lower:]' \
  | sort -u \
  > "$OUT"

count=$(grep -c . "$OUT" || true)
echo "Wrote $count contact email address(es) to $OUT"

if [[ "$count" -eq 0 ]]; then
  echo "No addresses found — if this is unexpected, verify scopes with:"
  echo "  gws schema people.people.connections.list"
  echo "  gws auth login -s gmail,people"
fi

TOTAL_ELAPSED=$(( $(date +%s) - RUN_START_TS ))
echo "Total runtime: $(fmt_duration "$TOTAL_ELAPSED")"
