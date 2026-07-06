---
name: stoop-docs-and-writing
description: Load this skill whenever you touch anything under docs/, write or edit customer-facing words, or make a claim about Stoop in public. Triggers include - editing schema-v1.md, api-contracts.md, severity-rubric-v1.md, eval-scenarios-v1.md, or any doc of record; adding a table/column (doc must change FIRST); changing an endpoint (contract doc updates in the SAME PR); versioning the frozen rubric or eval scenarios; writing an amendment block; adding an engineering-decisions.md entry; writing or reading an issue spec; writing marketing copy, landing-page text, SMS templates, UI strings, or emails; "can we say X publicly", "what are the exact prices", "is 'triage' allowed", "can we mention the LTB", "can we claim we never miss an emergency"; testimonials or social proof; fixing doc drift (broken doc pointers, stale mockup references, stale CLAUDE.md layout). Owns the authority hierarchy (docs are source of truth), per-doc change discipline, the house issue-spec style, copy rules, and the public-claims ledger.
---

# Stoop — docs of record, amendment discipline, copy, and public claims

Repo root: `/Users/laith/Businesses/LandlordAI` (monorepo; run all commands from repo root unless prefixed with `cd apps/api`). "Doc of record" = a file under `docs/` that code is required to follow; code that disagrees with the doc is the bug.

## When NOT to use this skill

| You are actually doing… | Load instead |
|---|---|
| Shipping code (gates, reviewers, /ship flow, merge rules) | `stoop-change-control` |
| Investigating why something broke | `stoop-debugging-playbook`, `stoop-failure-archaeology` |
| Understanding system design / invariants | `stoop-architecture-contract` |
| Rubric semantics, Ontario tenancy law, SMS/LLM-safety theory | `stoop-domain-reference` |
| Running tests/evals, deciding what counts as evidence | `stoop-validation-and-qa` |
| Producing a number to publish (benchmarks, eval scores) | `stoop-proof-and-analysis-toolkit` |
| Deciding whether a public safety claim is earned yet | `stoop-research-and-frontier` (owns the zero-missed-emergency milestone) |
| Env setup, running the app, config axes | `stoop-build-and-env`, `stoop-run-and-operate`, `stoop-config-and-flags` |

## 1. The authority hierarchy

1. `docs/` is **the source of truth. Code follows these docs, not vice versa** (root `CLAUDE.md`). If code and doc disagree, fix the code — or amend the doc through the discipline in §2, never by quietly making the doc match the code.
2. `docs/README.md` is the index: folders numbered in reading order (01-strategy → 02-product → 03-engineering → 04-roadmap → 05-gtm → 06-legal, plus `mockups/`).
3. Ownership map (from root `CLAUDE.md`; read the owner doc before touching its area):

| Doc | Owns |
|---|---|
| `docs/03-engineering/architecture.md` | stack, system diagram, ADRs, scaling triggers |
| `docs/03-engineering/schema-v1.md` | **canonical table/column names — never invent names** |
| `docs/03-engineering/api-contracts.md` | endpoint shapes, error envelope, pagination |
| `docs/03-engineering/dev-agents.md` | the 5-subagent crew, review gates |
| `docs/03-engineering/issue-specs/` | build specs for GitHub issues #1–#15 (+ EPIC) |
| `docs/02-product/severity-rubric-v1.md` | classification rubric **v1.0, frozen** |
| `docs/02-product/eval-scenarios-v1.md` | the 10 eval cases + scoring rules |
| `docs/02-product/conversation-model.md` | channel vs case, lifecycle, stale-draft rule |
| `docs/02-product/emergency-prefilter.md` | Tier-0 filter, degraded mode, escalation chain |
| `docs/02-product/plain-language-rules.md` | every tenant/landlord-facing message's readability rules |
| `docs/04-roadmap/release-train.md` | Trains 1–3 roadmap |
| `docs/01-strategy/business-model.md`, `three-year-plan.md` | pricing, segments, horizons |
| `docs/05-gtm/*` | channels, outreach scripts, waitlist nurture, video plan |
| `docs/06-legal/*` | ToS (DRAFT), privacy policy (DRAFT), pilot kit |

4. **Git rules for docs** (root `CLAUDE.md` §Git): pushing docs-only changes directly to `main` is normal (`docs: …` commits). App code goes through the `/ship` flow (branch → PR → CI → review → squash-merge) — see `stoop-change-control`. A doc change that *drives* a code change (schema amendment, contract change) travels **in the same PR as the code**, not as a separate push.

## 2. Change discipline by doc type

| Doc | Discipline |
|---|---|
| `schema-v1.md` | Edit the doc **first**, then write the migration. Column names in the doc are canonical — never invent variants in code. History is append-only amendment blocks (below). |
| `api-contracts.md` | New/changed endpoint ⇒ update this doc **in the same PR** (`apps/api/CLAUDE.md` §Conventions). Error envelope `{"error": {"code", "message", "request_id"}}`, cursor pagination, ISO-8601 UTC are fixed conventions. |
| `severity-rubric-v1.md` | **Frozen v1.0** (approved 2026-06-11). Any behavior change = a **new version file** + new prompt file (`app/agent/prompts/v{n+1}.py` — prompt versions are frozen, never edited) + **full eval run**. Never an in-place edit. The rubric text is embedded byte-identical in `apps/api/app/agent/rubric.py`, enforced by a pinned sha256 in `apps/api/tests/test_rubric.py` (`_PINNED_SHA256`) — the checksum, rubric.py, and the doc change together or not at all. |
| `eval-scenarios-v1.md` | Approved v1.0; grows **only** via the production-misclassification rule (new eval YAML in the same week a production misclassification is found). Non-semantic fixes (a wrong illustrative enum name, invalid YAML quoting) are allowed as a dated inline "Correction" note stating no scenario content changed and no re-approval was needed — the 2026-07-05 correction block in the doc header is the template. |
| `plain-language-rules.md` | Binds `draft_response`, emergency safety templates, holding ack, vendor messages. The eval grader enforces the testable rules — a rules change implies an eval-expectation review. |
| `architecture.md` | ADRs are numbered and appended (ADR-1…5 exist); never rewrite a decided ADR — supersede with a new one. |
| `06-legal/*` | ToS and privacy policy are **DRAFT, lawyer review flagged** — cite them as constraints on copy (§5–6) but do not present them publicly as final. |

**Eval-run reality check (as of 2026-07-05):** a "full eval run" is paid (real Anthropic API, `pytest -m eval`) and requires the founder's go-ahead — agents never fire it autonomously (session-verified 2026-07-05; no repo artifact). The eval harness merged to `main` in PR #177 (2026-07-06).

### The amendment-block style (worked example: schema-v1.md v1.1–v1.6)

Approved doc sections are **never rewritten**. History accretes as dated, versioned blockquote blocks at the top of the doc. `schema-v1.md` carries six as of 2026-07-05 — read them (`grep -n 'amendments' docs/03-engineering/schema-v1.md`) before adding a seventh. The anatomy:

1. **Header line**: `> **v1.N amendments (YYYY-MM-DD)** — migration NNNN implements these (#issue)` — or explicitly `no migration required` (v1.6 is the doc-only precedent: it deprecates `messages.classification`/`tokens_in`/`tokens_out`/`model`/`llm_cost_cents` because writing them post-INSERT would violate the append-only rule).
2. **Numbered items**, each stating the change *and* the forcing rationale inline (v1.2's "Why no FORCE" paragraph is the standard of rigor).
3. **Cross-references to code artifacts**: the migration number, the module docstring, the test. v1.4 even cites which earlier amendment's forward note it closes.
4. **The DDL body is annotated, not silently edited**: deprecated columns stay listed with a `-- DEPRECATED v1.N: never written; see …` comment until a future DROP migration; new semantics get `-- v1.N (migration NNNN, #issue): …` comments (see `pending_resolved_at`).

To add an amendment: take the next version number, date it, name the migration (or "no migration required"), write numbered items with rationale, annotate the affected DDL lines, and land it in the same PR as the migration.

## 3. engineering-decisions.md — the durable "why" record

`docs/03-engineering/engineering-decisions.md` merged to `main` in PR #179 (2026-07-06) — read it directly; it covers PRs #123–#177. (Historical note: it lived on branch `docs/engineering-decisions` until the eval PR merged, because its §8 had to cite the real PR number.) Legacy read-without-switching command:

```bash
git show origin/docs/engineering-decisions:docs/03-engineering/engineering-decisions.md | less
```

- **Merge ordering**: it merges **after** the eval-harness PR, because its §8 ("The eval harness") must cite the real PR number once one exists (session-verified 2026-07-05; no repo artifact). Do not merge it first and backfill.
- **Charter** (from its own header): `architecture.md` says what the system is; `schema-v1.md` says what the tables are; this doc says *why a given line is the way it is* when that reason was hard-won — a live Supabase finding, a safety-review reversal, a spec-guardian ruling.
- **Entry format** — one entry per decision, reference not narrative:
  - **What**: the decision, with the exact code (a snippet is fine when the exact text is load-bearing).
  - **Why**: the forcing constraint or finding, with evidence (e.g. "18/100 requests still failed with the two-knob fix").
  - **Where**: file paths of the surviving artifacts.
- **What earns an entry**: every senior-review ruling and every settled incident. The incident *narrative* (symptom → root cause → recovery) belongs to `stoop-failure-archaeology`; the decision record holds the surviving rule and its why.
- Sections as of `ef9e2c6`: 1 Postgres platform/Supavisor · 2 Role model & connection topology · 3 Append-only mechanics · 4 Webhook front door · 5 JWKS verification · 6 LangGraph agent subsystem · 7 LLM layer · 8 Eval harness · 9 Testing conventions. New entries go under the matching section; add a section only for a genuinely new subsystem.

## 4. Issue-spec house style

Specs for GitHub issues #1–#15 live in `docs/03-engineering/issue-specs/` (`001-…` through `015-…`, plus `EPIC.md` and `README.md`). **Issues #16+ live on GitHub only** (`LaithAlz/stoop-backend`), acceptance criteria in the issue body — do not create new spec files for them (as of 2026-07-05 no spec beyond 015 exists).

Every spec uses exactly these H2 sections, in order:

1. `## Goal` — one short paragraph, the deliverable.
2. `## Why this matters` — the downstream dependency or risk.
3. `## Acceptance criteria` — checkable bullets; this is the definition of done the spec-guardian reviews against.
4. `## Out of scope` — explicit non-goals, so implementers don't gold-plate.
5. `## Effort & dependencies` — size + which issues must land first.

**Path-translation trap (repo-verified)**: specs #1–#15 predate the monorepo and write paths as `backend/…`; the real location is `apps/api/` with Python package `app`. Translate every path when reading a spec; never create a `backend/` directory to match one.

## 5. Customer-facing copy rules (the one home; copy-guardian enforces)

"Customer-facing" = any string a landlord, tenant, or visitor reads: marketing pages, dashboard UI, SMS templates, emails. The enforcement agent is `.claude/agents/copy-guardian.md` (read-only, runs whenever a change adds/edits such text — see `docs/03-engineering/dev-agents.md` for when it is mandatory).

**Root `CLAUDE.md` rule 8, verbatim:**

> Customer-facing copy: plain English (never "triage"), no legal/LTB
> mentions on marketing pages, never "founding/cohort/spot counts" — say
> "early access". Prices: free Emergency Line / $10 Full Plan /
> $5 early-access (grandfathered) / PMs $1.50/door.

Copy-guardian's full rule set (from `.claude/agents/copy-guardian.md`), in priority order:

1. **Banned words**: "triage", "founding", "cohort", spot counts, "AI agent" in tenant-facing text, legal/LTB/RTA mentions on marketing surfaces.
2. **Prices exactly**: free Emergency Line · $10/month Full Plan · $5/month early-access · $1.50/door property managers. Price-lock wording: the ToS grants the lock only "for as long as your subscription remains active" (`docs/06-legal/terms-of-service.md`), so copy says **"locked in for as long as you stay"** — never "locked for life" / "for life" (see the drift note in §7 about copy-guardian's own stale phrasing).
3. **Tenant-facing SMS**: grade-5 reading level, sentences ≤ 15 words, emergencies = max 3 numbered steps, no idioms, concrete times. Full rules and rationale: `docs/02-product/plain-language-rules.md`; the underlying panicked/ESL/half-asleep-reader doctrine lives in `stoop-domain-reference`.
4. **Claims must match the ToS**: never "never misses", never position Stoop as an emergency service; 911 language present where required (§6).
5. **Voice**: formal labels, plain first-person sentences when Stoop speaks.

### The cautionary tale: fabricated testimonials (PR #142)

Commit `8054652` ("remove fabricated testimonials and soften price-lock claim (#142)", 2026-07-03) removed three fully invented customer quotes from the landing page — named fictional people with unit counts ("Devraj, Mississauga · 2 units", "Margaret, Burlington", "Priya, Toronto") — and softened "locked in for life" to "locked in for as long as you stay" across `index.tsx`, `plans.tsx`, `early-access.tsx`. They had shipped as persuasive filler and read as real social proof.

Standing rules distilled from it:
- **Never invent social proof.** No testimonials, named customers, review counts, user counts, or "landlords love us" until real, consenting customers exist. Placeholder quotes are not placeholders — they are fabrications the moment the page is public.
- **Soften unverifiable claims to what the ToS supports.** If legal wording says "as long as your subscription remains active", marketing may not say "for life".
- When auditing, `git show 8054652` is the worked example of the fix pattern: delete the fabrication outright (don't fictionalize harder), align superlatives with the legal doc.

## 6. External positioning — the claims ledger

What may be said publicly, and what is gated on proof.

**Claimable today (as of 2026-07-05):**
- Early access is open (always "early access" — never founding/cohort/spot counts).
- The Emergency Line is free for every landlord, and is never paywalled, throttled, or gated (root `CLAUDE.md` rule 1).
- Prices: $10/month Full Plan; $5/month early-access rate "locked in for as long as you stay"; property managers $1.50/door.
- Product behavior as documented: Stoop reads every tenant text, drafts replies for landlord approval, and only rings the landlord's phone for a true emergency. Nothing sends without landlord approval except emergency safety instructions (rule 3) — approval-first is a safe, doc-backed claim.

**Never claimable (ToS-bound, copy-guardian rule 4):**
- "Never misses an emergency" or any zero-miss/100%-catch phrasing.
- Positioning Stoop as an emergency service; the ToS has an explicit not-an-emergency-service clause, and rubric judgment call #1 puts 911 first. Emergency-adjacent copy must carry 911 language where required.

**Gated on proof — the zero-missed-emergency claim.** The founder-set bar for ever making a public "record" claim: (1) real production message volume, (2) a missed-emergency ledger (an auditable record of every emergency-class message and whether the system caught it), and (3) evals running in CI — all three, none exist yet (session-verified 2026-07-05; no repo artifact). `stoop-research-and-frontier` owns the falsifiable milestone definition; do not soft-launch the claim ("industry-leading emergency detection" etc.) before it is met.

**Reproducibility standard for published numbers.** Any number that leaves the building (eval pass rates, latency, catch rates, cost) must be accompanied by a command someone else can run to regenerate it from repo artifacts — recipe patterns in `stoop-proof-and-analysis-toolkit`. A number without a rerun command is a claim, not a measurement; it doesn't ship.

## 7. Known doc drift — fix opportunistically (as of 2026-07-05)

When you're already touching the neighboring file, fix these; each is verified against the repo.

| # | Drift | Fix |
|---|---|---|
| 1 | `apps/api/.env.example` line 3: "See docs/setup/supabase.md" — `docs/setup/` does not exist. | Repoint to a real doc (or write the setup doc) next time `.env.example` changes. |
| 2 | `docs/README.md` mockups section crowns "**06 = THE app design**", but the founder green-lit the 07 "Clarity" direction (no paper texture) on 2026-07-05 (session-verified; no repo artifact). `docs/mockups/07-clarity-redesign.html` exists in the working tree but is **untracked**. | When the web rebuild PR lands: commit 07 and rewrite the README mockups paragraph to crown 07, demoting 06 to "explored directions". |
| 3 | `apps/api/CLAUDE.md` "Layout (target)" lists `app/models/`, `app/settings.py`, and `app/agent/graph.py` — none exist; reality is no models dir (yet), `app/config.py`, and `app/agent/graph_entry.py`. The heading honestly says "target", but readers keep assuming the paths are real. | Never cite a path from that layout block without `ls`-ing it first; update the block when the layout stabilizes. |
| 4 | ~~bare pytest collects the paid gate~~ RESOLVED by PR #177: `pyproject.toml` now has `addopts = "-m 'not eval'"`. Residual doc nit: `apps/api/CLAUDE.md`'s command comment ("unit + integration") is now accurate again. | No action; re-verify `grep addopts apps/api/pyproject.toml`. |
| 5 | `.claude/agents/copy-guardian.md` rule 2 still quotes the early-access price as `"locked for life"` — the exact phrase PR #142 (commit `8054652`) removed; ToS and all live copy now say "for as long as you stay". The enforcer would re-approve the banned phrase. | Update copy-guardian's rule 2 wording to match the ToS (a `.claude/agents/` edit — small PR, not a docs push). |

## Provenance and maintenance

Volatile claims in this skill, each with a one-line re-verification command (run from repo root):

| Claim | Re-verify with |
|---|---|
| Doc map matches root CLAUDE.md | `sed -n '14,30p' CLAUDE.md` |
| Rubric still frozen v1.0, checksum-pinned | `grep -n '_PINNED_SHA256' apps/api/tests/test_rubric.py && head -4 docs/02-product/severity-rubric-v1.md` |
| Amendment blocks still v1.1–v1.6 (append yours as next) | `grep -n 'amendments (' docs/03-engineering/schema-v1.md` |
| engineering-decisions.md on main (PR #179) | `git log --oneline -1 main -- docs/03-engineering/engineering-decisions.md` |
| Eval harness still off `main` | `git ls-tree main --name-only apps/api/evals/` (output ⇒ merged; update §2 note) |
| Issue specs still end at 015 | `ls docs/03-engineering/issue-specs/` |
| Specs still use `backend/` paths | `grep -rl 'backend/' docs/03-engineering/issue-specs/ \| head -3` |
| .env.example pointer still broken | `grep -n 'docs/setup' apps/api/.env.example; ls docs/setup 2>&1` |
| README still crowns mockup 06 / 07 still untracked | `grep -n '06 = THE app design' docs/README.md; git status --porcelain docs/mockups/` |
| Bare pytest still collects eval marker | `grep -n 'addopts' apps/api/pyproject.toml` (no hit ⇒ still unguarded) |
| copy-guardian still says "locked for life" | `grep -n 'locked for life' .claude/agents/copy-guardian.md` |
| Live copy price-lock wording | `grep -rn 'locked in for' apps/web/src/routes/ \| head -5` |
| ToS price-lock clause wording | `grep -n -i 'price-lock' docs/06-legal/terms-of-service.md` |
| PR #142 testimonial removal | `git show --stat 8054652` |
