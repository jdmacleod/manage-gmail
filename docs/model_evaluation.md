# LLM Model Evaluation — Gmail Classifier

Ongoing notes on model testing and prompt tuning for the adversarial three-model
email classifier (`python/classify_corpus.py`).

## Architecture

Three models classify each email independently. Unanimous vote → keep or delete.
Any disagreement → uncertain. Models are run in batch-by-model order (all emails
through model A, then B, then C) so each model is loaded into VRAM once per pass.

Disagreements are appended to `python/disagreements.jsonl` and stored in full in
`python/classifications.db` keyed by `(message_id, model, prompt_version)`.

---

## Disagreement Rate History

| Prompt | Models | Sample | Disagreements | Rate | Notes |
|--------|--------|-------:|-------------:|-----:|-------|
| v1.0.0 | gemma4:e2b-mlx, qwen3.5:4b-mlx, llama3.2:3b | 98 | 98 | 100% | Thinking-mode models starved by 120-token output cap |
| v1.1.0 | gemma3:4b, qwen2.5:7b, llama3.2:3b | 230 | 122 | 53% | gemma3:4b wrapping JSON in markdown code fences |
| v1.2.0 | gemma3:4b, qwen2.5:7b, llama3.2:3b | 386 | 84 | 22% | gemma3:4b capacity failures; llama3.2:3b malformed JSON |
| v1.3.0 | gemma3:12b, qwen2.5:7b, llama3.1:8b | 230 | 12 | **5.2%** | Model upgrade to 12b/8b; best rate achieved |
| v1.4.0 | gemma3:12b, qwen2.5:7b, llama3.1:8b | 230 | 18 | 7.8% | Prompt NOTEs ignored by llama; regression |
| v1.5.0 | gemma3:12b, qwen2.5:7b, llama3.1:8b | 230 | 14 | 6.1% | Partial recovery; diminishing prompt returns |
| v1.5.0 | gemma3:12b, gpt-oss:20b, qwen2.5:14b | 500 | 19 | 3.8% | Gen 4 first run; prior best overall |
| v1.6.0 | gemma3:12b, gpt-oss:20b, qwen2.5:14b | 500 | 11 | 2.2% | 8 gaps resolved: LinkedIn false-keeps, fitness reminders, platform countdowns, HomeExchange reply-threads |
| v1.7.0 | gemma3:12b, gpt-oss:20b, qwen2.5:14b | 500 | 5 | **1.0%** | New best; community list DELETE rule, eStatement KEEP carve-out, forwarded rule elevated |

---

## Model Generations

### Generation 1 (retired)

**gemma4:e2b-mlx, qwen3.5:4b-mlx, llama3.2:3b** — prompt v1.0.0

Retired after the first 98-email run. gemma4 and qwen3.5 are MLX thinking-mode
models that consumed the entire 120-token `num_predict` budget on internal
chain-of-thought before producing any output, returning empty responses. This was
misread as unanimous uncertain votes. Fix: raised `MAX_OUTPUT_TOKENS` to 1024.
llama3.2:3b (3B) was too small — all 98 disagreements were llama as the outlier.

---

### Generation 2 (retired)

**gemma3:4b, qwen2.5:7b, llama3.2:3b** — prompts v1.1.0, v1.2.0

#### gemma3:4b

Retired after v1.2.0 (45 false-keeps out of 84 total disagreements). Consistent
failure: applied the "platform-mediated personal message" KEEP rule to LinkedIn
marketing emails across three prompt versions. Determined to be a model capacity
issue at 4B — unable to reliably weigh multi-criteria rules against each other.
Also wrapped JSON output in markdown code fences (` ```json…``` `) requiring a
dedicated parser fix (`_parse_label_response()`).

#### llama3.2:3b

Retired alongside gemma3:4b. Issues:
- Highly sensitive to prompt wording changes — behaviour swung significantly
  between v1.1.0 and v1.2.0
- Produced malformed JSON: dropped the closing quote on the `reason` string
  (e.g. `"reason": "text (no-reply@)}`) requiring a truncation recovery parser
- 3B parameter count too small for reliable multi-criteria classification

#### qwen2.5:7b

Carried forward to Generation 3 (lowest outlier count in Gen 2 runs).

---

### Generation 3 (superseded)

**gemma3:12b, qwen2.5:7b, llama3.1:8b** — prompts v1.3.0 through v1.5.0

#### Cumulative outlier votes (690 emails, 3 prompt versions)

| Model | Outlier votes | False-deletes | False-keeps | Uncertain |
|-------|-------------:|-------------:|------------:|----------:|
| gemma3:12b | 10 | 0 | 10 | 0 |
| qwen2.5:7b | 16 | 12 | 4 | 0 |
| llama3.1:8b | 18 | 12 | 5 | 1 |

#### gemma3:12b — retained

Best performer. All errors are false-keeps in one narrow category: financial
institution account security/management alerts (contact-info updates,
account-change confirmations) classified as "financial records." Both other models
correctly vote DELETE on these. Zero uncertain outputs. Keep rate stable at 6–10%.
Carried forward to Generation 4.

#### qwen2.5:7b — replaced at Generation 4

Failure mode: hardwired "automated content / no personal interaction required"
heuristic that fires on anything non-conversational, overriding explicit KEEP rules:

- Voted DELETE on Zoom meeting invitations across all three prompt versions
  despite the rule being rewritten twice
- Voted DELETE on an employment separation/legal document across all three versions
- Voted DELETE on personal emails from a named financial adviser
- Voted DELETE on a GitHub PR human comment (v1.3.0)

The heuristic is more deeply anchored than prompt instructions can reach. No amount
of rewriting moved qwen on these cases.

#### llama3.1:8b — replaced at Generation 4

Most erratic model. Both false-delete and false-keep failure modes:

**Persistent false-deletes:**
- Platform-mediated personal messages (e.g. a home-exchange service) routed via
  a bulk mail relay — voted DELETE all three versions ("notification from platform
  / marketing content") despite the human content and named author. Three targeted
  prompt revisions had no effect.
- OOO auto-reply: ignored the "Automatic reply in subject → DELETE" rule added in
  v1.4.0 and continued voting KEEP at 1.0 confidence.

**Prompt-sensitivity failures:**
- Retailer/service receipts: voted DELETE in v1.4.0 (fixed by v1.5.0 restructuring)
- Forwarded personal messages: voted DELETE in v1.4.0 (fixed by v1.5.0)
- GitHub PR comment: KEEP (v1.3.0) → DELETE (v1.4.0, after qualifier added) →
  KEEP (v1.5.0, after simplification) — flip-flopped on wording changes
- One null output (news digest email, v1.4.0): returned `uncertain` with null
  reason and 0.0 confidence

The flip-flopping indicated the model was latching onto surface signals from
whatever the prompt most recently emphasized, not applying stable classification
logic. Unreliable as an adversarial check.

---

### Generation 4 (current)

**qwen2.5:14b, gpt-oss:20b, gemma3:12b** — default prompt v1.7.0

#### Rationale

- **gemma3:12b**: retained, best performer in Gen 3
- **qwen2.5:7b → qwen2.5:14b**: same family, doubled parameter count. Qwen 2.5
  shows strong instruction-following improvement between 7B and 14B. The 7B's
  false-delete pattern on non-conversational-but-valid KEEP categories is the
  target failure to fix.
- **llama3.1:8b → gpt-oss:20b**: Open-source GPT-family model at 20B. Different
  architecture and training lineage from both gemma (Google) and qwen (Alibaba),
  preserving ensemble diversity. Evaluated after phi4-mini-reasoning:3.8b was
  retired for excessive inference latency (≤20 tok/s). gpt-oss:20b targets the
  same llama3.1:8b weaknesses — stable multi-criteria instruction following — at
  a competitive throughput.

#### Cumulative outlier votes — prompt v1.5.0 (500 emails)

19 unique disagreements (22 raw records; 3 Schwab message IDs appeared twice due
to a re-run overlap).

| Model | Outlier votes | False-deletes | False-keeps | Uncertain |
|-------|-------------:|-------------:|------------:|----------:|
| gemma3:12b | 5 | 0 | 5 | 0 |
| gpt-oss:20b | 7 | 1 | 4 | 2 |
| qwen2.5:14b | 7 | 4 | 3 | 0 |

#### Cumulative outlier votes — prompt v1.7.0 (500 emails)

5 unique disagreements. gpt-oss:20b returned zero uncertain outputs in this run —
the forwarded-email UNCERTAIN issue from v1.5.0 is resolved.

| Model | Outlier votes | False-deletes | False-keeps | Notes |
|-------|-------------:|-------------:|------------:|-------|
| gemma3:12b | 2 | 1 | 0 | eStatement DELETE despite explicit KEEP rule; 1 correct outlier (Spectrum) |
| gpt-oss:20b | 1 | 0 | 1 | Paramount "Test" — ambiguous edge case |
| qwen2.5:14b | 2 | 0 | 2 | Persistent "action required" KEEP override: Google security alert, Schwab account verification |

#### gemma3:12b — retained

Fewest outlier votes in the Gen 4 v1.5.0 run. All v1.5.0 errors were false-keeps;
the pattern expanded beyond financial institution alerts (known Gen 3 failure mode)
to a broader category of transactional notifications that resemble records. Prompt
v1.6.0 and v1.7.0 resolved all of those categories with targeted rules.

**v1.7.0 status:** 2 outlier votes (one correct, one false-delete):
- **eStatement availability**: votes DELETE despite explicit KEEP rule added in
  v1.7.0. Root cause: the DELETE financial institution rule is weighted more
  heavily than the KEEP exception. The exception appears parenthetical within
  the financial-records KEEP bullet; a standalone bullet may be required.
- **Spectrum disconnection confirmation** (1 outlier-delete, correctly alone):
  gemma3 correctly votes DELETE while gpt-oss and qwen vote KEEP — the only
  case where gemma3's lone vote is the right label.

Zero false-deletes in v1.5.0; 1 false-delete in v1.7.0 (eStatement). Carry forward.

#### gpt-oss:20b — under evaluation

7 outlier votes in v1.5.0 — tied with qwen2.5:14b.

**v1.5.0 failure modes (now resolved):**
- LinkedIn "I want to connect" connection-request emails: 3 false-keeps. Fixed
  in v1.6.0 with an explicit LinkedIn DELETE rule.
- ISP welcome/onboarding email: 1 false-keep. Fixed by v1.6.0 rule tightening.
- Forwarded personal emails (Fwd: in subject): 2 uncertain outputs while both
  other models voted KEEP. gpt-oss applied its automated-sender heuristic to the
  forwarding wrapper rather than the payload. Fixed in v1.7.0 by elevating the
  forwarded-email rule to position 1 with an in-rule "not an UNCERTAIN case"
  statement. Zero uncertain outputs in the v1.7.0 run.

**v1.7.0 status:** 1 outlier vote:
- Paramount "Test" email from a real executive (tim.farrell@paramount.com):
  gpt-oss votes KEEP (correct per rules — personal email with scheduling link),
  gemma3 and qwen vote DELETE. Ensemble outputs uncertain. Not a prompt-fixable
  case without body content visibility.

#### qwen2.5:14b — under evaluation

7 outlier votes in v1.5.0 — tied with gpt-oss:20b.

**v1.5.0 failure modes (now resolved):**
- Platform-mediated personal message reply threads (HomeExchange): 2–3 cases.
  qwen2.5:14b triggered an "automated platform relay" DELETE heuristic on
  reply-thread notifications. Fixed in v1.6.0 with explicit carve-out for
  "Ann has replied to your message" notifications.
- Fitness class reminder emails: 2 false-keeps treating yoga/pilates reminders
  as calendar appointments. Fixed in v1.6.0 with an explicit fitness/booking
  reminder DELETE rule.
- Community mailing list posts (3DPRO VFX): 4–5 false-keeps in v1.6.0.
  Fixed in v1.7.0 with the [ListName] subject prefix DELETE rule.

**Persistent failure mode:** qwen2.5:14b applies a "requires action → KEEP"
heuristic that overrides explicit DELETE rules when the email body contains
action-oriented language. This is the same anchoring pattern seen in qwen2.5:7b
but at a narrower scope (security alerts, account verification) rather than
broad-category failures.

**v1.7.0 status:** 2 outlier votes:
- **Google security alert** (new sign-in notification): qwen votes KEEP
  reasoning "requires action to secure the account" — overriding the explicit
  "login alert → DELETE" rule. The security alert framing triggers the
  action-required override.
- **Schwab account verification**: qwen votes KEEP for "notification requiring
  action on financial matter" — borderline per v1.7.0 rules, but the "CONFIRMATION"
  subject line indicates a completed action, not a pending one. gemma3 and gpt-oss
  both vote DELETE.

---

## Prompt Version History

All prompts in `python/prompts/`. Default is always the latest version.

| Version | Key changes |
|---------|-------------|
| v1.0.0 | Baseline. Added `/no_think` to suppress chain-of-thought on supported models. |
| v1.1.0 | Tightened UNCERTAIN: requires characteristics of BOTH keep AND delete simultaneously. Added no-reply address and job-board alerts to DELETE. |
| v1.2.0 | "From a real person" explicitly means the FROM address belongs to a human. Platform-mediated personal messages added to KEEP. GitHub PR/issue comments added to KEEP. Political campaign fundraising and cold B2B outreach added to DELETE. |
| v1.3.0 | Platform-mediated KEEP rule tightened with guard clause: all of named author, addressed to me by name, no unsubscribe link. GitHub KEEP rule clarified: only when email contains actual human comment text. Zoom meeting invitations and DocuSign requests added explicitly to KEEP. No-reply DELETE rule softened to signal-not-rule. |
| v1.4.0 | OOO auto-replies (Automatic reply, Out of Office, OOO, Autosvar) added to DELETE. Boarding passes and travel documents added explicitly to KEEP. Category precedence NOTEs added (receipts/tickets/meetings override "automated transactional" DELETE). DocuSign @docusign.net domain named explicitly. *Regression: NOTEs ignored by llama3.1:8b.* |
| v1.5.0 | Restructured: no-reply exceptions moved from DELETE NOTEs into KEEP list with explicit category examples (retailer receipts, event tickets, financial institution records). Forwarded personal messages (Fwd/Fw in subject) added to KEEP. GitHub PR rule simplified (removed qualifier that caused llama regression). Meeting invitation rule made sender-address-agnostic. |
| v1.6.0 | Platform reply notifications added to KEEP (HomeExchange "Ann replied" carve-out — fixes qwen2.5:14b false-delete). Forwarded personal content rule strengthened to block UNCERTAIN outputs (fixes gpt-oss:20b). Financial records example narrowed; financial institution marketing/alerts added to DELETE (fixes gemma3:12b false-keep broadening). LinkedIn connection requests named explicitly in DELETE (fixes gpt-oss:20b false-keep). Fitness class and booking-platform reminders added to DELETE (fixes qwen2.5:14b false-keep). Platform countdown notifications ("arriving in N days") added to DELETE (fixes gemma3:12b). |
| v1.7.0 | Forwarded rule moved to position 1 with in-rule UNCERTAIN prohibition (escalation for gpt-oss:20b persistent uncertain on Fwd: emails). Financial records KEEP expanded: eStatement/document-ready notifications and financial action items (beneficiary designation) added. Financial institution DELETE rule narrowed to administrative account-change notifications only, with explicit KEEP carve-out for document-availability notifications. Community mailing list rule added to DELETE ([ListName] subject prefix — fixes 3DPRO and similar list traffic). |

### Known remaining classification gaps (v1.7.0 / Gen 4)

All five v1.5.0 gaps were resolved by v1.6.0 and v1.7.0. Three new gaps remain:

- **gemma3:12b eStatement rule override**: votes DELETE on Schwab eStatement
  availability notifications despite an explicit KEEP carve-out added in v1.7.0.
  The DELETE financial institution rule appears to outweigh a parenthetical KEEP
  exception. Proposed v1.8.0 fix: elevate eStatement notifications to a
  standalone KEEP bullet rather than an example in the financial-records item.
- **qwen2.5:14b "action required" KEEP override**: treats any email with
  action-oriented language ("secure your account", "review these updates") as
  KEEP regardless of explicit DELETE rules. Manifests on Google security alerts
  and Schwab account-change confirmations. Proposed v1.8.0 fix: extend the
  security alert DELETE rule to name "new sign-in notification" and "account
  security alert" explicitly.
- **Service management notifications (gpt-oss/qwen false-keeps)**: ISP
  disconnection confirmations treated as receipts/records by gpt-oss and
  qwen2.5 (1 case, Spectrum). The rules draw a KEEP/DELETE line for purchase
  receipts but do not address service-lifecycle notifications (activations,
  plan changes, terminations). Proposed v1.8.0 fix: explicit service management
  notification DELETE rule to distinguish from purchase receipts.
- **Paramount "Test" edge case**: persistent across v1.5.0–v1.7.0. A real
  executive's email from paramount.com with a scheduling link and one-word
  subject. gpt-oss votes KEEP (probably correct), gemma3 and qwen vote DELETE.
  Not fixable at the prompt level without email body access.

### Resolved gaps (v1.6.0 and v1.7.0)

- gemma3:12b false-keeps on community lists, e-commerce deliveries, platform countdowns
- gpt-oss:20b LinkedIn connection-request false-keeps (3 cases)
- gpt-oss:20b uncertain outputs on forwarded personal emails (2 cases) — zero uncertain in v1.7.0
- qwen2.5:14b false-deletes on platform-mediated reply threads
- qwen2.5:14b false-keeps on fitness/booking-platform reminders

---

## Parser Notes

`_parse_label_response()` in `classify_corpus.py` handles two common model output quirks:

1. **Markdown code fences**: gemma3:4b (and some other models) wrap JSON output
   in ` ```json…``` ` despite being instructed not to. Stripped before parsing.

2. **Truncated JSON**: small models (llama3.2:3b) drop the closing quote on the
   `reason` string: `"reason": "text (no-reply@)}`. Two recovery patterns:
   - Pattern A: trailing chars after `}` → truncate at last `}`
   - Pattern B: missing `"` before final `}` → insert and retry

---

## Model Candidates for Future Evaluation

See `TODO.md` for the full list.

- `phi4-reasoning:14b` — full 14B may be viable if throughput improves; mini
  variant retired at 3.8B due to ≤20 tok/s inference speed
