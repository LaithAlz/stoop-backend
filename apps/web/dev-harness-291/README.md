# Manual verification harness, issue #291 / #279 (undo pin + thread guard)

This directory is a **manual harness pending #294** (a real test runner for
`apps/web`). It is not wired into `bun run lint` (excluded in
`eslint.config.js`'s `ignores`), `bunx tsc --noEmit` (outside `tsconfig.json`'s
`include`), or `bun run build` (not referenced from any route or entry point).
It exists purely so a human (or an agent) can reproduce the safety-review
findings against issue #291/#279 without hand-rebuilding the same harness
every round, four review rounds in a row each re-derived a variant of this
from scratch, and each round broke something a previous round had already
proven fixed. Don't delete it; extend it.

It runs the **real, unmodified app code**, `useDraftActions`,
`queueEntriesReducer`, `buildQueueView`, `unverifiedSendStore`,
`useResolveUnverifiedSends`, and the actual `app.index.tsx` /
`app.conversations.$id.tsx` route components, against a hand-rolled
`fetch` mock per entry. Only five things are shimmed (`shims/`): auth,
Supabase, `@/lib/env`, the `sonner` toast library (captured into an
in-memory `NOTICES` array instead of rendering), and TanStack Router's
route helpers (`createFileRoute` etc., so a route module can be imported
and rendered outside an actual router).

## Two kinds of scenario here

**Pure (`pure.ts`, `pure_idx.ts`)**, no DOM, no React, no build step.
Exercise `queueEntriesReducer` / `buildQueueView` / `pruneQueueSnapshots`
/ `dueUnverifiedResolutions` directly. Run with `bun run pure.ts` or
`bun run pure_idx.ts` straight from this directory. They import the real
source by RELATIVE path (`../src/...`); an earlier version used the
worktree's absolute path, which would have broken the moment that
worktree was removed, on the very files the README calls the quick path.

**DOM (`entries/*.tsx`)**, a real route component, a real `QueryClient`,
mounted with `react-dom/client`, driven by a fetch mock, read back through
the DOM (button text/disabled state, textarea value, on-screen text).
These need Chromium because they depend on real focus/DOM timing
(`document.activeElement`, `MutationObserver`, `requestAnimationFrame`)
that jsdom does not model faithfully enough for what a few of these
findings actually turned on.

## Running a DOM entry

```sh
cd apps/web/dev-harness-291
bun run build.ts entry_r4a          # -> dist/entry_r4a.js, dist/index_entry_r4a.html
python3 run_entry_once.py dist index_entry_r4a.html 12   # 12s wait, prints #out + console
```

`run_entry_once.py` needs `playwright` with Chromium installed
(`python3 -m playwright install chromium`, one-time). It serves `dist/`
over a throwaway local HTTP server, loads the page in headless Chromium,
waits, then prints the entry's own `#out` log and the last 20 console
messages (page errors included).

Each entry ends its own log with `DONE`; read the log itself for the
actual assertions; it's plain `console.log`-shaped output, not a pass/fail
test runner.

## What each entry reproduces

| Entry | Route | What it checks |
|---|---|---|
| `entry7` | Home | Undo APPLIES server-side but the response is lost (ambiguous `undoMutation` failure), where does the card land, and does it ever show a live "sent" state that contradicts the tenant genuinely not having been answered. |
| `entry8` | Home | BLOCKER 2 (#252/#279) ambiguous edit-and-send guard with `GET /v1/queue` ALSO down, the give-up ceiling's timers/notices must not leak after unmount, and must not fire while nothing is mounted. |
| `entry9` | Home | Double-tapping Undo during a slow in-flight DELETE, exactly one DELETE fires, not two. |
| `entry10` | Thread | The `entry7` scenario (ambiguous undo, response lost) replayed on `app.conversations.$id.tsx` instead of Home. |
| `entry11` | Home | Skip stays reachable while the #252 unverified-send flag is up on the same draft (the escape hatch must survive a locked Approve/Edit). |
| `entry12` | Home | Once the 120s give-up ceiling releases the `#252` flag, what's actually left on the card, this is BLOCKER 2's own predecessor check; see `entry_editor_open`/`entry_r4b`/`entry_r4d` for the round-3/4 editor-open variants. |
| `entry13` | Home | A NORMAL approve (no failures at all), does the newly-pinned "sent" status linger on screen longer than it honestly should. |
| `entry14` | Home | How long "Sent." is actually on screen after the row has already dropped from a fresh `GET /v1/queue` read. |
| `entry15` | Home | Same question as `entry14`, instrumented with a `MutationObserver` over the "Sent." text node instead of polling. |
| `entry16` | Home | "Sent." visibility specifically in the #291 PIN case, the row has already dropped out of `items` but stays pinned client-side. |
| `entry17` | Home | Item 5's OTHER claimed benefit (queueEntries.ts docstring): does keeping the pin one extra commit actually give the row-level focus-return effect a chance to fire on the `sending -> sent` transition, measured directly against `document.activeElement`. |
| `entry_editor_open` | Home | BLOCKER 2, round 3: the 120s give-up ceiling fires while the editor is STILL OPEN with typed text, does the sticky notice appear where the landlord can actually see it. |
| `entry_finding3` | Home | Round 3's Finding 3: does "Sent." ever survive an actual PAINTED animation frame (`requestAnimationFrame`) post-BLOCKER-1-fix, or does the retirement effect commit before the browser ever paints it. |
| `entry_finding5` | Home | Round 3's Finding 5: edit-and-send submitted on a card that has fallen out of `decisionItems` while the editor was open, does the pinned index stay honest. |
| `entry_r4a` | Home | **Round 4 BLOCKER 1.** A `GET /v1/queue` read issued BEFORE an Approve, resolving AFTER it (an ordinary refetch, window focus, another card's `onSettled`, `useAcknowledge`'s invalidation), with no undo tap involved at all. Proves round 3's `dataUpdatedAt > approvedAtClient` gate is trippable by a plain approve, wrongly reviving Approve/Edit on an already-sent reply, and that a landlord's "correction" edit-and-send on the reopened editor then silently no-ops server-side (idempotent 200, first body kept). |
| `entry_r4b` | Home | **Round 4 BLOCKER 2.** The give-up ceiling fires while the editor is left open with typed text (mirrors `entry_editor_open` but specifically checks the sticky `UNVERIFIED_GIVE_UP_CARD_NOTICE`, not just the ceiling-clears-Send behavior), checks the notice while editing, immediately after Cancel, and after reopening Edit. |
| `entry_r4c` | Thread | Round 4 BLOCKER 1 attempt on the thread route, same shape as `entry_r4a` but for `app.conversations.$id.tsx`. Kept as a DOCUMENTED NEGATIVE RESULT: this specific scenario is masked by a pre-existing, unrelated effect (the "invalidate case query the moment the local entry hits `sent`" effect, `app.conversations.$id.tsx` lines ~359-364) that happens to win the race and self-correct before the 1s sampling in this entry can observe the false state. See `entry_r4c2` for the scenario that actually proves the bug on this route. |
| `entry_r4c2` | Thread | **Round 4 BLOCKER 1, thread route, reliable repro.** Same stale in-flight read across an Approve as `entry_r4c`, but the case endpoint is ALSO taken down (500) right after the approve, so the masking effect's own corrective read fails and the false-cleared state (`Approve` live again on an already-sent reply) is durably observable. This is the one that actually failed before the fix. |
| `entry_r4d` | Thread | **Round 4 BLOCKER 2**, thread route. Mirrors `entry_r4b` on `app.conversations.$id.tsx`'s own editing branch, the give-up ceiling's sticky notice while the editor is left open. This route never threaded `giveUpNotices` into its `EditDraftPanel` at all before the round-4 fix. |

## Before/after (round 4)

Measured against this worktree's HEAD at the start of round 4
(`ffba481`, round 3's fix) vs. after the round-4 fix below. See the PR
report for the exact before/after log lines; `entry_r4a`, `entry_r4b`,
`entry_r4c2`, and `entry_r4d` are the four that flip from failing to
passing.

## Adding a new entry

Copy the shape of any `entries/*.tsx` file: a hand-rolled `globalThis.fetch`
mock keyed on URL/method, a `render()` that mounts the real route component
inside `QueryClientProvider`, a `log()`/`flush()` pair that writes into
`#out`, and a `void main()` at the bottom. Add a row to the table above.
