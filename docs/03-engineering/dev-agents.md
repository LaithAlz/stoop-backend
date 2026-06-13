# Dev-Agent System — how this repo builds itself

> Created 2026-06-12. Five specialized Claude Code subagents live in
> `.claude/agents/`. They are the *construction crew*; the product's
> runtime agent (LangGraph) is a different thing entirely
> (`architecture.md` §5).

## The team

| Agent | Model | Role | Writes code? |
|---|---|---|---|
| `implementer` | Sonnet | One backend issue, end-to-end, tests green | ✔ |
| `frontend-builder` | Sonnet | Web UI from mockup 06 + Heritage tokens | ✔ |
| `spec-guardian` | Sonnet | Diff vs schema/contracts/ACs/never-break rules | ✘ read-only |
| `safety-reviewer` | Opus | Adversarial review of load-bearing paths | ✘ read-only |
| `copy-guardian` | Haiku | Customer-facing strings vs brand voice rules | ✘ read-only |

Design principles:
- **Builders and reviewers are different agents.** The one who wrote the
  code never approves it; reviewers are read-only so they can't "fix"
  their way past a finding.
- **Specs are the contract, agents are the enforcement.** Every guardrail
  an agent enforces points at a doc a human signed off
  (schema-v1, api-contracts, plain-language-rules, CLAUDE.md rules 1–8).
- **Model-to-risk matching.** Opus only where wrong = catastrophic
  (auth, RLS, emergency path). Haiku where the job is pattern-matching
  strings. Sonnet for the volume work.
- **Agents stop instead of inventing.** Missing column name, missing
  credential, missing contract → report and halt. The schema doc changes
  first, then code.

## The standard build loop (per issue)

```
you: /ship issue #N
 ├─ implementer (or frontend-builder)   — builds, tests green
 ├─ spec-guardian                       — APPROVE or FIX-FIRST findings
 │    └─ findings → back to builder (max 2 loops, then human)
 ├─ safety-reviewer                     — ONLY for issues touching:
 │    #10 #22 #23 #40 #44 #45 #107 #108 #109 #115 #122 or any file in
 │    agent/, webhooks/, deps.py, integrations/supabase_auth.py
 ├─ copy-guardian                       — ONLY if customer-visible
 │    strings changed
 └─ PR with the reviewers' verdicts pasted into the description
```

Human gates that never delegate: merging, `pytest -m eval` runs (cost),
anything on the humans-only list (accounts, secrets, A2P), prompt/rubric
version bumps, and pricing changes.

## Session economics

Subagents start cold — don't spawn one for a question a doc answers.
The crew earns its overhead on build-review cycles, not on lookups.
One issue = one implementer session; learnings that should persist go
into the docs (that's what "the specs record it" means in their prompts).
