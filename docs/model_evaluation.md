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

### Generation 4 (current — not yet evaluated)

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

#### Status

Models pulled, awaiting first classification run. Target: ≤5% disagreement rate
on a 230-email sample to match Gen 3's best (v1.3.0: 5.2%).

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

### Known remaining classification gaps (v1.5.0)

- **Financial institution security alerts vs. financial records**: gemma3:12b
  classifies account security/management notifications (contact-info updates,
  account-change alerts) as financial records (KEEP). Likely correct label is
  DELETE. Low priority — only 2–3 cases per 230-email sample.
- **Platform-mediated personal messages via bulk relay**: llama3.1:8b
  false-deleted these in Gen 3; unknown whether gpt-oss:20b handles them
  correctly.
- **qwen "automated content" heuristic**: unknown whether qwen2.5:14b carries
  forward qwen2.5:7b's false-delete pattern on meeting invitations and employment
  documents.

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

- `gpt-oss:20b` — flagged for future consideration
