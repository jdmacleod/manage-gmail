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
| v1.5.0 | gemma3:12b, gpt-oss:20b, qwen2.5:14b | 500 | 19 | **3.8%** | Gen 4 first run; new best overall |

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

**qwen2.5:14b, gpt-oss:20b, gemma3:12b** — default prompt v1.5.0

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

#### Cumulative outlier votes (500 emails, prompt v1.5.0)

19 unique disagreements (22 raw records; 3 Schwab message IDs appeared twice due
to a re-run overlap).

| Model | Outlier votes | False-deletes | False-keeps | Uncertain |
|-------|-------------:|-------------:|------------:|----------:|
| gemma3:12b | 5 | 0 | 5 | 0 |
| gpt-oss:20b | 7 | 1 | 4 | 2 |
| qwen2.5:14b | 7 | 4 | 3 | 0 |

#### gemma3:12b — retained

Fewest outlier votes in the Gen 4 run. All errors are false-keeps, but the
pattern has expanded beyond financial institution alerts (the known Gen 3 failure
mode) to a broader category of transactional notifications that resemble records:

- **Financial institution alerts**: account-change confirmations, account
  verification emails, and marketing emails from financial institutions → KEEP.
  Majority correct label: DELETE.
- **E-commerce delivery updates**: order delivery notifications → KEEP. Correct
  label: DELETE.
- **Platform arrival countdowns**: "X is arriving in N days" countdown emails
  from a home-exchange platform → KEEP. Correct label: DELETE.
- **Professional network community posts**: introduction posts on a VFX/3D
  community mailing list → KEEP. Correct label: DELETE.

Zero false-deletes. Carry forward to Gen 5 or next prompt iteration.

#### gpt-oss:20b — under evaluation

7 outlier votes — tied with qwen2.5:14b.

**Persistent false-keeps:**
- LinkedIn "I want to connect" connection-request emails: 3 cases, all from the
  same narrow category. gpt-oss votes KEEP while both other models vote DELETE.
  Systematic blind spot — LinkedIn connection requests are clearly transactional
  automated mail.
- ISP welcome/onboarding email: 1 false-keep. Correct label: DELETE.

**Uncertain outputs:**
- Forwarded personal emails (Fwd: in subject): 2 cases where gpt-oss returns
  `uncertain` while both other models vote KEEP. Content is personal (real-estate
  discussion, travel itinerary); the outer forward sender is automated. gpt-oss
  appears to apply its automated-sender heuristic to the wrapper rather than the
  payload. Correct label: KEEP.

**Outlier-delete (possibly correct):**
- 1 case: a financial institution account-verification email where gpt-oss alone
  votes DELETE while gemma3 and qwen2.5 vote KEEP. Likely correct — this is an
  account management notification, and gemma3's financial-record false-keep
  pattern explains the opposing votes.

#### qwen2.5:14b — under evaluation

7 outlier votes — tied with gpt-oss:20b.

**Persistent false-deletes (same failure mode as qwen2.5:7b):**
- Platform-mediated personal message reply threads (home-exchange service): 2–3
  cases. qwen2.5:14b continues to trigger an "automated platform relay" DELETE
  heuristic on reply-thread notifications from platforms, despite the content
  being a real person's reply. The 7B→14B upgrade did **not** fix this.
- Professional email with minimal subject: 1 case (employer-domain test email).
  Ambiguous — qwen may be pattern-matching on a single-word subject.
- Professional link-share email: 1 case (colleague sharing a post). Correct
  label: KEEP.

**False-keeps:**
- Fitness class reminder emails (studio booking platform): 2 cases. qwen2.5
  votes KEEP — treating them as calendar appointments — while both other models
  vote DELETE. Correct label: DELETE.
- Service disconnection confirmation: 1 case. qwen2.5 treats it as an important
  service document; others vote DELETE. Correct label likely DELETE.

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

### Known remaining classification gaps (v1.5.0 / Gen 4)

- **gemma3:12b broadening false-keep pattern**: extends beyond financial
  institution alerts (known from Gen 3) to e-commerce delivery notifications,
  platform arrival countdowns, and community mailing list posts — all treated as
  records worth keeping. 5 cases in 500 emails; wider than previously measured.
- **gpt-oss:20b LinkedIn blind spot**: systematic false-keep on LinkedIn
  connection-request emails (3 cases). Narrow category, fixable in v1.6.0 with
  an explicit DELETE rule.
- **gpt-oss:20b forwarded personal emails**: returns `uncertain` when forwarded
  content is personal but the outer sender is automated (2 cases). The v1.5.0
  `Fwd/Fw in subject → KEEP` rule is not firing for gpt-oss; needs strengthening.
- **qwen2.5:14b platform-mediated personal messages**: the 7B→14B upgrade did
  not fix the automated-relay false-delete. Reply-thread notifications from
  home-exchange and similar services still trigger the DELETE heuristic. Primary
  target for v1.6.0.
- **qwen2.5:14b fitness/booking reminders**: 2 false-keeps treating class
  reminder emails from booking platforms as calendar appointments. Fixable with
  an explicit DELETE rule for transactional reminder emails.

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
