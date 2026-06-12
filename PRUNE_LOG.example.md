# Gmail Prune Log

Running record of `scripts/prune.sh` runs. New entries are appended
automatically by the script; feel free to add manual notes too.

<!-- New entries appended below -->
- 2026-06-11 23:03 — **Old promotions** — query: `category:promotions older_than:6m is:unread -is:starred -is:important -in:trash` — trashed 10151, skipped 10 (contacts)
- 2026-06-11 23:10 — **Receipts/statements/orders** (reference) — query: `subject:(statement OR invoice OR order) -label:Reference -in:trash` — labeled 42 — runtime 1m 03s
- 2026-06-11 23:15 — **Caltech president emails** (rescued) — query: `in:trash from:president@example.edu` — rescued 3 — runtime 4s
