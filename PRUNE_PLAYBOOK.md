# Gmail Prune Playbook

A repeatable routine for clearing out low-value email while protecting
anything important. Everything below moves messages to **Trash** (Gmail
auto-empties Trash after 30 days, so it's reversible) — nothing is
permanently deleted.

Run it via `scripts/prune.sh` (see that file for usage), or copy/paste the
individual `gws` commands below if you want to handle one category at a time.

## Standing exclusions

Every query below already excludes:

- `-is:starred` — you starred it on purpose
- `-is:important` — Gmail's importance markers
- `-in:trash` — don't touch what's already trashed

In addition, every query also excludes `-label:Reference`, and `prune.sh`
applies two more checks to every candidate message before trashing it:

- **Contacts**: checks the sender against `contacts_emails.txt` (built by
  `scripts/export_contacts.sh`) and **never trashes mail from someone in
  your Contacts or Other Contacts**, no matter which category it matches.
  Run `export_contacts.sh` once (see `SETUP.md`) and re-run it occasionally
  to keep the list current.
- **Thread replies**: checks (via `threads.get`) whether you've ever replied
  within the message's thread, and **never trashes mail in a thread you've
  replied to** — these are conversations, not noise.

If there are other senders/labels you always want to protect (e.g. a
specific non-contact address), add `-from:someone@example.com` to any query,
or apply the `Reference` label (see below) and it'll be excluded everywhere.

## Categories

### 1. Old promotional / marketing email

Retail deals, sale announcements, newsletters from brands — but only ones
you never even opened, and old enough that they're definitely stale.

```
category:promotions older_than:6m is:unread -is:starred -is:important -in:trash
```

Default threshold: 6 months + unread. Lower `PROMO_AGE` in `prune.sh` for a
more aggressive sweep, or drop `is:unread` to also catch promos you opened
but never acted on.

### 2. Social / notification noise

"X liked your post", app alerts, social network digests.

```
category:social older_than:3m -is:starred -is:important -in:trash
```

Default threshold: 3 months — this category is high-volume and low-value
even when fresh.

### 3. Expired codes / resets / verifications

One-time codes, password reset links, "verify your email" — useless once a
few days old, never worth keeping.

```
subject:("verification code" OR "reset your password" OR "verify your email") older_than:7d -is:starred -is:important -in:trash
```

Default threshold: 7 days.

### 4. Auto-reply / bounce noise

Out-of-office replies, automatic replies, delivery-status-notifications.

```
subject:("out of office" OR "automatic reply" OR "delivery status notification") older_than:30d -is:starred -is:important -in:trash
```

Default threshold: 30 days.

### 5. Past calendar notifications

Google Calendar's own notification emails for events that have already
happened — the event record lives in Calendar, not the inbox.

```
from:calendar-notification@google.com older_than:30d -is:starred -is:important -in:trash
```

Default threshold: 30 days.

### 6. Post-delivery shipping notices

"Your package has been delivered" / "out for delivery" emails from common
carriers, once the delivery has clearly happened.

```
from:(amazon.com OR ups.com OR fedex.com OR usps.com) subject:(delivered OR "out for delivery") older_than:3m -is:starred -is:important -in:trash
```

Default threshold: 3 months. The order confirmation itself (if it matched
`subject:(invoice OR order)`) is a separate message and isn't affected.

### 7. Old newsletters / digests

Read newsletters and digest-style mail in the Updates category that have
sat around for a long time.

```
category:updates is:read older_than:1y -is:starred -is:important -in:trash
```

Default threshold: 1 year. Note this can overlap with category 8 below —
that's fine, whichever runs first just leaves nothing for the other to find.

### 8. Old read mail (general inbox cleanup)

Anything you've already read, isn't starred/important, and has sat around
for a long time. This is the broad "general clutter" sweep.

```
is:read -is:starred -is:important -in:trash -in:sent -in:chats older_than:1y smaller:1M
```

Default threshold: read, older than 1 year, and under 1MB (so large emails
with attachments you might want aren't swept up automatically — review those
separately).

### 9. Old unread mail

Unread messages you've never opened and are unlikely to ever read — typically
automated notices, digests, or things that lost relevance.

```
is:unread -is:starred -is:important -in:trash -category:primary older_than:1y
```

Default threshold: 1 year, and excludes Primary tab (so unread mail from real
people you haven't gotten to isn't included — only Promotions/Social/Updates/
Forums tabs).

### 10. Noisy senders (low engagement)

Mail from senders you've identified as high-volume and rarely read, when it
lands in Promotions, Updates, or Social. This category (and the per-sender
ones below it) only runs if `noisy_senders.txt` has at least one entry.

```
(category:promotions OR category:updates OR category:social) from:(addr1 OR addr2 OR ...) older_than:30d -is:starred -is:important -label:Reference -in:trash
```

Default threshold: 30 days. Build the address list with
`scripts/find_noisy_senders.sh` (see below) — it samples recent
Promotions/Updates/Social mail and reports senders with 5+ messages and a
read rate of 10% or less over the last 90 days. Review the report and copy
addresses you recognize into `noisy_senders.txt` (one per line, `#`-comments
allowed).

The `category:promotions/updates/social` restriction matches the scope
`find_noisy_senders.sh` samples from, so a sender's transactional mail
sitting in Primary isn't swept up just because their marketing mail is
noisy.

#### Same sender, mixed content (e.g. a bank's statements vs. its marketing)

If a noisy sender *also* sends mail worth keeping under the same address,
add it to `noisy_senders.txt` with a tab-separated list of subject
keywords to exclude, e.g.:

```
notifications@examplebank.com	statement,payment due
```

This gives that address its own category with an added
`-subject:(statement OR "payment due")` exclusion:

```
(category:promotions OR category:updates OR category:social) from:(notifications@examplebank.com) -subject:(statement OR "payment due") older_than:30d -is:starred -is:important -label:Reference -in:trash
```

If the keep-worthy pattern is general enough to apply mailbox-wide (not just
to this sender), prefer adding a rule to `scripts/label_reference.sh`
instead (or in addition) — see the next section.

## The Reference label (protecting mail long-term)

The `Reference` label marks mail you want to keep indefinitely — receipts,
policy docs, travel confirmations, security alerts, and the other "keep"
categories used by `rescue.sh`. Every prune query excludes `-label:Reference`,
so labeled mail is never trashed no matter what else matches.

- **`scripts/label_reference.sh`** proactively applies `Reference` across
  your whole mailbox (minus Trash) using the same rules as `rescue.sh`'s
  retain list. Safe to re-run anytime — already-labeled mail is skipped.
- **`scripts/rescue.sh`** also applies `Reference` to anything it restores
  from Trash, so rescued mail doesn't get re-trashed later.
- If you find a new category of mail worth keeping long-term, add a rule to
  both `label_reference.sh` and `rescue.sh` (the comments in each file point
  at each other).
- **`prune.sh` checks how recently `label_reference.sh` last completed.** If
  it's been more than `REFERENCE_MAX_AGE_DAYS` (default 7) or it's never run,
  `prune.sh` warns and (outside `--dry-run`) offers to run it for you first.
  This matters most for category 10 (Noisy senders) — without a recent
  Reference pass, new "keep" mail from a noisy sender may not be labeled yet
  and could get caught by that category's broader query.

## Adjusting thresholds

All thresholds live as variables at the top of `scripts/prune.sh`:

| Variable | Default | Controls |
|---|---|---|
| `PROMO_AGE` | `6m` | category 1 (combined with `is:unread`) |
| `SOCIAL_AGE` | `3m` | category 2 |
| `AUTH_CODE_AGE` | `7d` | category 3 |
| `AUTOREPLY_AGE` | `30d` | category 4 |
| `CALENDAR_NOTIF_AGE` | `30d` | category 5 |
| `SHIPPING_AGE` | `3m` | category 6 |
| `OLD_UPDATES_AGE` | `1y` | category 7 |
| `OLD_READ_AGE` | `1y` | category 8 |
| `OLD_READ_SIZE` | `1M` | category 8 |
| `OLD_UNREAD_AGE` | `1y` | category 9 |
| `NOISY_AGE` | `30d` | category 10 (noisy senders, see `noisy_senders.txt`) |
| `REFERENCE_MAX_AGE_DAYS` | `7` | how long since `label_reference.sh` last ran before `prune.sh` warns/offers to run it |

Gmail search date units: `d` (days), `m` (months), `y` (years).

## Manual workflow (no script)

For any query `Q` above:

```bash
# 1. See how many match + sample of what they are
gws gmail users messages list \
  --params '{"userId": "me", "q": "Q", "maxResults": 25}' \
  --format table

# 2. If it looks right, trash one message at a time by ID
gws gmail users messages trash --params '{"userId": "me", "id": "MESSAGE_ID"}'
```

`scripts/prune.sh` automates step 2 across every matching message, with a
preview + confirmation per category.

## Recovering a mistake

Trashed mail stays in Trash for 30 days:

```bash
gws gmail users messages untrash --params '{"userId": "me", "id": "MESSAGE_ID"}'
```

Or search Trash in the Gmail web UI (`in:trash`) and click "Move to Inbox".

## Rescue scan (run before Trash empties)

After a prune run, scan Trash for anything that should actually be kept and
restore it. Run via `scripts/rescue.sh`, or manually:

```bash
# Receipts/invoices/orders/policy docs
in:trash older_than:1y subject:(statement OR invoice OR order)
in:trash older_than:1y subject:(policy OR terms)

# Specific senders worth keeping — defined per-mailbox in
# config/keep_rules.local.tsv (gitignored; see
# config/keep_rules.local.example.tsv), e.g.:
# in:trash from:president@example.edu

# Travel confirmations
in:trash subject:(itinerary OR "boarding pass" OR "flight confirmation" OR "hotel confirmation" OR "reservation confirmation") -category:promotions

# Recent account security alerts (last year)
in:trash subject:("security alert" OR "new sign-in" OR "password changed") newer_than:1y

# Warranty / registration / proof of purchase
in:trash subject:("product registration" OR "registration confirmation" OR warranty OR "proof of purchase") -category:promotions
```

For each match you want to keep:

```bash
gws gmail users messages untrash --params '{"userId": "me", "id": "MESSAGE_ID"}'
```

For personal sender/subject rules (specific people, organizations, or
services), add entries to `config/keep_rules.local.tsv` as you discover
things that keep getting swept up — that's the main way this playbook should
evolve over time. A single entry there feeds both `rescue.sh` and
`label_reference.sh` automatically. For generic, non-personal rules that
would benefit other users of this toolkit, add the rule directly to
`scripts/rescue.sh` and the equivalent rule to `label_reference.sh`, so
matching mail gets the `Reference` label proactively next time, before it
ever reaches Trash.

Anything `rescue.sh` restores also gets the `Reference` label automatically,
so it stays protected from future prune runs.

## Auditing and tuning the rules over time

Every message `prune.sh` trashes gets an `Audit/Pruned/<category>` label;
every message `rescue.sh` restores gets an `Audit/Rescued/<category>` label
(in addition to `Reference`). Both scripts also append a line to
`audit_log.jsonl` (timestamp, category, sender, subject, message ID) —
unlike the Gmail labels, this log survives Trash's 30-day auto-delete, so
it's the durable record for analysis.

Run `scripts/review_audit.sh` periodically (or `--since 30` for just the
last month) to see:

- per-category pruned/rescued totals
- senders that have been both pruned and rescued — a sign that a rule is
  too broad for that sender
- specific messages pruned under one category and later rescued under
  another — the rescued category's pattern is exactly what the pruning
  category's query should also exclude

When you spot a recurring false positive, the fix is usually one of: add a
`label_reference.sh` rule for that sender/subject pattern (so it's never
pruned in the first place), tighten the offending category's query/age
threshold, give the sender a subject exclusion in `noisy_senders.txt` (if
it's category 10 and the sender also sends mail worth keeping), or remove
the sender from `noisy_senders.txt` entirely.

You can also browse Trash in the Gmail web UI filtered by
`label:Audit/Pruned/<category>` to spot-check a category's output while it's
still recoverable.

## After each run

Append an entry to `PRUNE_LOG.md` (the script does this automatically) so you
have a history of what was cleared, rescued, and when.
