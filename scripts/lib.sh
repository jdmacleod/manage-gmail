#!/usr/bin/env bash
#
# Shared helpers sourced by the other scripts in this directory. Not meant
# to be run directly.
#
# Requires: gws (authenticated, see ../SETUP.md), jq
#

# Formats a duration in seconds as "Xh Ym Zs" / "Ym Zs" / "Zs".
fmt_duration() {
  local secs=$1 h m s
  h=$(( secs / 3600 ))
  m=$(( (secs % 3600) / 60 ))
  s=$(( secs % 60 ))
  if (( h > 0 )); then
    printf '%dh %dm %ds' "$h" "$m" "$s"
  elif (( m > 0 )); then
    printf '%dm %ds' "$m" "$s"
  else
    printf '%ds' "$s"
  fi
}

# Estimates remaining time given $1 items processed so far, $2 total items,
# and $3 the start timestamp (date +%s) of this run/category. Prints
# "~<duration>" (via fmt_duration), "~0s" once processed >= total, or nothing
# if no items have been processed yet (no rate to extrapolate from).
fmt_eta() {
  local processed=$1 total=$2 start_ts=$3 elapsed remaining
  (( processed <= 0 )) && return 0
  (( total <= processed )) && { printf '~0s'; return 0; }
  elapsed=$(( $(date +%s) - start_ts ))
  remaining=$(( elapsed * (total - processed) / processed ))
  printf '~%s' "$(fmt_duration "$remaining")"
}

# Returns " — elapsed <duration>[, ETA <estimate>]" for appending to a
# per-item progress line. $1 processed, $2 total, $3 start timestamp
# (date +%s) of this run/category.
progress_suffix() {
  local processed=$1 total=$2 start_ts=$3 elapsed eta
  elapsed=$(( $(date +%s) - start_ts ))
  eta=$(fmt_eta "$processed" "$total" "$start_ts")
  if [[ -n "$eta" ]]; then
    printf ' — elapsed %s, ETA %s' "$(fmt_duration "$elapsed")" "$eta"
  else
    printf ' — elapsed %s' "$(fmt_duration "$elapsed")"
  fi
}

# Prints the Gmail label ID for $1, creating the label (visible in the
# label list and message list) if it doesn't already exist.
get_or_create_label_id() {
  local label_name="$1"
  local existing

  existing=$(gws gmail users labels list --params '{"userId":"me"}' --format json 2>/dev/null \
    | jq -r --arg n "$label_name" '.labels[]? | select(.name == $n) | .id' | head -n1)

  if [[ -n "$existing" ]]; then
    echo "$existing"
    return 0
  fi

  gws gmail users labels create \
    --params '{"userId":"me"}' \
    --json "$(jq -nc --arg n "$label_name" '{"name":$n,"labelListVisibility":"labelShow","messageListVisibility":"show"}')" \
    --format json 2>/dev/null \
    | jq -r '.id // empty'
}

# Extracts a header value (e.g. "From", "Subject") from a messages.get
# --format metadata JSON blob ($1). Prints empty string if absent.
header_value() {
  local meta="$1" header_name="$2"
  echo "$meta" | jq -r --arg h "$header_name" \
    '.payload.headers[]? | select(.name == $h) | .value' 2>/dev/null | head -n1
}

# Extracts and lowercases the email address from a "From" header value
# (e.g. 'Some Name <foo@bar.com>' -> 'foo@bar.com'). Prints empty string if
# no address found.
extract_email() {
  local from_header="$1"
  echo "$from_header" | grep -oE '[^[:space:]<]+@[^[:space:]>]+' | tr '[:upper:]' '[:lower:]'
}

# Prints the modification time of file $1 as a Unix timestamp (seconds since
# epoch), or nothing if the file doesn't exist. Works on both BSD/macOS and
# GNU stat.
file_mtime() {
  local f="$1"
  [[ -e "$f" ]] || return 0
  # GNU stat first (-f means "filesystem" on BSD/macOS stat, so trying that
  # first there would silently return the wrong thing rather than erroring).
  stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null
}

# Appends personal "keep" rules from config/keep_rules.local.tsv (if present)
# to the caller's NAMES/QUERIES arrays — for sender/subject rules that are
# specific to your mailbox and shouldn't live in the public scripts. Each
# non-comment, non-blank line is "name<TAB>core query fragment" (e.g. "Alma
# mater president emails" / "from:president@example.edu" — no in:trash or
# -label:Reference, both modes add those). $1 selects how the fragment is
# wrapped: "rescue" -> "in:trash <core>"; "reference" -> "<core>
# -label:<label> -in:trash" (label name from $2). Requires SCRIPT_DIR to be
# set by the caller. See config/keep_rules.local.example.tsv for the format.
load_local_keep_rules() {
  local mode="$1" label_name="${2:-}"
  local file="$SCRIPT_DIR/../config/keep_rules.local.tsv"
  [[ -s "$file" ]] || return 0

  local name core
  while IFS=$'\t' read -r name core || [[ -n "$name" ]]; do
    [[ -z "$name" ]] && continue
    [[ "$name" =~ ^[[:space:]]*# ]] && continue
    case "$mode" in
      rescue)    NAMES+=("$name"); QUERIES+=("in:trash $core") ;;
      reference) NAMES+=("$name"); QUERIES+=("$core -label:${label_name} -in:trash") ;;
    esac
  done < "$file"
}

# Appends one JSON-lines record to audit log $1, for ongoing review of
# prune/rescue decisions. Fields: ts (UTC), action ("pruned"/"rescued"),
# category, message_id, from, subject.
log_audit_event() {
  local logfile="$1" action="$2" category="$3" msg_id="$4" from_email="$5" subject="$6"
  jq -nc \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg action "$action" \
    --arg category "$category" \
    --arg id "$msg_id" \
    --arg from "$from_email" \
    --arg subject "$subject" \
    '{ts: $ts, action: $action, category: $category, message_id: $id, from: $from, subject: $subject}' \
    >> "$logfile"
}
