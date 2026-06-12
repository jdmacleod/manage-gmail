# One-time setup: `gws` CLI for Gmail

This is a **one-time setup** you run in your own terminal (not in any sandbox).
The OAuth login needs a real browser + a localhost callback on your machine, so
it can't be done remotely.

## 1. Install `gws`

Pick one (see [repo README](https://github.com/googleworkspace/cli) for details):

```bash
# Easiest: npm wrapper that downloads the right binary
npm install -g @googleworkspace/cli

# or Homebrew (macOS/Linux)
brew install googleworkspace-cli

# or build from source
cargo install --git https://github.com/googleworkspace/cli --locked
```

Verify:

```bash
gws --version
```

## 2. Set up a Google Cloud project + OAuth credentials

You need a Google Cloud project with the Gmail API enabled and an OAuth
"Desktop app" client. Two ways to do this:

### Option A — `gws auth setup` (if you have `gcloud` installed)

```bash
gws auth setup
```

This walks you through creating a project, enabling the Gmail API, and
creating OAuth credentials.

### Option B — Manual (Cloud Console), no `gcloud` needed

1. Go to https://console.cloud.google.com/ and create (or pick) a project.
2. OAuth consent screen (`APIs & Services > OAuth consent screen`):
   - App type: **External**
   - Testing mode is fine for personal use
   - Under **Test users**, add the Google account email you'll use with
     `gws` (e.g. `you@gmail.com`) — **required**, otherwise login fails with
     "Access blocked"
3. Enable the Gmail API: `APIs & Services > Library > Gmail API > Enable`
4. Credentials (`APIs & Services > Credentials > Create Credentials > OAuth client ID`):
   - Application type: **Desktop app**
   - Download the JSON
5. Save the downloaded file as:
   ```
   ~/.config/gws/client_secret.json
   ```

## 3. Log in

Because this is an unverified personal app, the "recommended" scope bundle
(85+ scopes) will fail. Limit to Gmail **and People** (the People scope is
needed for contact-protection — see step 5):

```bash
gws auth login -s gmail,people
```

- A browser window opens — sign in with the Google account you added as a
  test user above.
- You'll see "Google hasn't verified this app" — click **Advanced** →
  **Go to "gws" (unsafe)**. This is expected and safe for your own personal app.
- When prompted for scopes, select all Gmail scopes offered (read, modify,
  labels, settings) — the prune scripts need at least `gmail.modify` to move
  messages to Trash — and the Contacts / Other Contacts (read-only) scopes
  under People.

If you already logged in with `-s gmail` only, just re-run
`gws auth login -s gmail,people` to add the People scopes without losing
Gmail access.

Credentials are encrypted at rest (AES-256-GCM) using your OS keyring.

## 4. Smoke test

```bash
# Confirm auth works
gws gmail users getProfile --params '{"userId": "me"}'

# Try a search (read-only, safe)
gws gmail users messages list --params '{"userId": "me", "q": "category:promotions older_than:6m", "maxResults": 5}' --format table
```

If both work, you're ready to use `PRUNE_PLAYBOOK.md` and `scripts/prune.sh`.

## 5. Export contacts (for contact protection)

`prune.sh` skips trashing any message from someone in your Contacts or
"Other contacts", regardless of category. Build that list once (and re-run
occasionally to keep it fresh):

```bash
./scripts/export_contacts.sh
```

This writes `contacts_emails.txt` to the project root. If it fails with an
auth/scope error, re-run `gws auth login -s gmail,people` (step 3) and make
sure to grant the Contacts scopes when prompted.

## 6. Label mail worth keeping (Reference)

`scripts/label_reference.sh` creates a `Reference` Gmail label (if it
doesn't exist) and applies it to receipts, policy docs, travel
confirmations, and similar "keep" categories. This requires the Gmail
**labels** scope, which is included if you granted "all Gmail scopes" in
step 3.

```bash
./scripts/label_reference.sh
```

Mail labeled `Reference` is permanently excluded from `prune.sh`. See
`PRUNE_PLAYBOOK.md` for details.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Access blocked" / 403 on login | Add yourself as a Test User on the OAuth consent screen, retry `gws auth login -s gmail` |
| "Google hasn't verified this app" | Expected — click Advanced → Go to app (unsafe) |
| `redirect_uri_mismatch` | OAuth client must be type **Desktop app**, not Web. Recreate it. |
| `accessNotConfigured` / Gmail API not enabled | Visit the `enable_url` link in the error, click Enable, wait ~10s, retry |
| Too many scopes error | Use `gws auth login -s gmail` (not the full "recommended" preset) |
