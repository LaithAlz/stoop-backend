# apps/web — frontend addendum

Adds to the root `AGENTS.md` (all rules there apply). Facts here stamped as
of 2026-07-12.

## Commands (Bun — no Node/npm here)

```bash
cd apps/web
bun install
bun run dev        # vite dev server
bun run build      # production build (Cloudflare Workers target)
bun run lint       # eslint .
bun run format     # prettier --write .
```

There is **no `test` or `typecheck` script** — type errors surface only via
`bun run build` or the editor. Never claim "web tests pass"; there are none.

## No web CI — lint + build ARE the gate

`.github/workflows/ci.yml` runs the backend only. A green PR check says
NOTHING about the web app. Before any web PR:
`bun run lint && bun run build` — both clean, run manually, results stated
in the PR body.

## Design authority

- **Clarity is the app design** (founder-approved 2026-07-05):
  `docs/mockups/07-clarity-redesign.html`, adopted by the dashboard rebuild
  (PR #181, components under `src/components/clarity/`). Match it screen by
  screen; `docs/README.md` §mockups is the ranking of record.
- 06 "Heritage light" is the PREVIOUS authority — legacy screens not yet
  rebuilt still use it; do not build anything new on it. 01–04 (Brownstone
  et al.) are explored directions only. Note: root `CLAUDE.md`'s one-line
  mockup note predates this — `docs/README.md` wins.
- The dashboard reads mock data (`src/lib/mock-app.ts`); there is no API
  client or Supabase session in the browser yet. Don't quietly wire live
  data — that is issue-scoped backend-contract work (`/v1/queue`, #56).

## Copy rules (root rule 8 — enforced on every customer-visible string)

Plain English (never "triage"), no legal/LTB mentions on marketing pages,
never "founding/cohort/spot counts" — say "early access". Exact prices only:
free Emergency Line / $10 Full Plan / $5 early-access (grandfathered) /
PMs $1.50/door. Full ruleset: `docs/02-product/plain-language-rules.md` and
`.claude/skills/stoop-docs-and-writing/SKILL.md`. Any string a landlord,
tenant, or visitor reads gets a copy review before the PR.

## SSR/hydration lesson (learned in PR #181 — don't relearn it)

SSR runs on Cloudflare Workers in UTC; the browser re-renders in the user's
locale/timezone. **Never compute clock-, locale-, or random-dependent values
during render** — it is a guaranteed hydration mismatch. Pattern of record:
`src/components/clarity/GreetingHeader.tsx` — render a stable value on the
server, settle the real one in `useEffect` after mount. Applies to
greetings, relative dates, `toLocaleString`, `Date.now()`, `Math.random()`.

## Where the depth lives

| Topic | Read |
|---|---|
| Copy/public-claims detail, doc-of-record discipline | `../../.claude/skills/stoop-docs-and-writing/SKILL.md` |
| Web QA expectations (what counts as evidence without tests) | `../../.claude/skills/stoop-validation-and-qa/SKILL.md` |
| Bun/env facts, version-pin warning (Bun itself is unpinned) | `../../.claude/skills/stoop-build-and-env/SKILL.md` |
| wrangler.jsonc, D1 placeholder, web config axes | `../../.claude/skills/stoop-config-and-flags/SKILL.md` |
| What the dashboard is (and isn't) wired to | `../../.claude/skills/stoop-architecture-contract/SKILL.md` |
