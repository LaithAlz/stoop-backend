# Conversation Model — channels, cases, and the stale-draft rule

> **Status:** Draft for founder review, 2026-06-11.
> **Why this doc:** "what is a conversation?" leaks into the schema
> (issues #17–19), the agent's context loading (#30), the approval queue
> UI, and the audit trail. Decided here, before migrations exist.

## The core distinction: channel vs case

A tenant has exactly **one SMS thread** on their phone. But "the leak" and
"the parking question" are different units of work with different
severities and lifecycles. So the model separates them:

- **Channel** — the tenant↔property SMS relationship. One per tenant,
  permanent. All `messages` belong to the channel (this is what the tenant
  experiences). Exception: landlord command-channel replies
  (`party='landlord'` — approve-by-SMS, #122) live on the landlord's own
  notification thread, not a tenant channel, and are excluded from ALL
  tenant-facing channel reads, including the dispute export.
- **Case** — a unit of triage work: one issue, one severity, one LangGraph
  thread, one approval-queue card. Cases are *our* segmentation of the
  channel; the tenant never sees them.

Schema consequence for #17–19: the table the old spec called
`conversations` becomes `cases`. `messages` carry `tenant_id` +
`property_id` (the channel) and a nullable `case_id` — nullable because a
message can arrive before its case exists, and pure-chitchat messages
("thanks!!") may never belong to one. A message that raises two issues
links to two cases via a `message_cases` join table; `messages.case_id`
holds the *primary* case for the common single-issue path.

## Case lifecycle

```
            ┌────────────────────────────────────────────┐
            ▼                                            │
 OPEN ──► AWAITING_APPROVAL ──► AWAITING_TENANT ──► RESOLVED ──► (REOPENED→OPEN)
   │            │                     │                ▲
   │            └── approve/send ─────┘                │
   └── landlord resolves directly ─────────────────────┘
                                  auto-stale (14 d inactivity) ──► RESOLVED(auto)
```

- **Open:** when the agent's `identify_case` step (see routing below)
  decides an inbound message starts a new issue.
- **Close:** (a) landlord marks resolved, (b) tenant confirms fixed
  (agent detects "all good now, thanks" and *proposes* resolution —
  landlord-visible, auto-applies after 48 h if not contradicted), or
  (c) auto-stale after **14 days** of inactivity, recorded as
  `resolved(auto-stale)` so reporting can distinguish it.
- **Reopen:** a new message matching a case resolved within the last
  **30 days** reopens that case (same id, same audit trail) rather than
  opening a duplicate. Past 30 days → new case with a `related_case_id`
  link. Reopening preserves the LTB-friendly property that one issue =
  one continuous record.

## Message routing (`identify_case`)

Every inbound message, after the channel is identified, gets routed by the
agent: *open cases for this tenant are listed in the prompt; the model
assigns the message to existing case(s), new case(s), or chitchat.*

- Single issue, no open cases → new case.
- Clearly continues an open case ("any update on the heat?") → that case.
- Multi-issue ("also the bathroom fan…") → splits: existing case gets its
  part, new case opens for the fan. Severity is assessed **per case**; the
  emergency pre-filter (see `emergency-prefilter.md`) runs on the **raw
  message** before any of this, so splitting can never delay an emergency.
- Ambiguous → attach to the most recent open case AND note the ambiguity
  in `reasoning_log` (visible on the approval card, so the landlord can
  re-route with one tap — that correction is training signal).

## The stale-draft rule (the race-condition answer)

Invariant: **a case has at most one pending draft.**

If a new inbound message lands on a case while a draft is
`awaiting_approval`:

1. The pending draft is marked `stale` (kept in the audit trail, never
   shown as sendable again).
2. The graph re-runs from `load_context` with the full case history
   including the new message.
3. The approval card updates in place — the landlord always sees one card
   per case with the freshest draft, never two drafts racing.

Edge: landlord taps approve at the same moment a new message arrives →
the approve action carries the draft id; if that id is already stale, the
send is rejected and the card refreshes ("Maria replied — draft updated").
The 5-second undo window absorbs most of this race in practice.
(Approve-by-SMS uses a 5-minute window, which widens this race ~60x —
a message arriving after approval but before send does not supersede
the approved draft. How the sender handles that gap is an open design
point tracked on #122.)

## Approval queue ordering

One card per case needing action, sorted: emergency follow-ups (rare) →
urgent (oldest first) → routine (oldest first). Cases in
`awaiting_tenant` don't occupy queue space. The queue is therefore
bounded by open cases, not by message volume.

## Audit consequences

`audit_log` entries reference `case_id` wherever one exists. The
"export this dispute" artifact (future) is: all messages on the channel
in range + every audit entry for the cases involved — both are cheap
queries under this model.

## Open judgment calls for founder

1. **14-day auto-stale / 30-day reopen window** — both arbitrary;
   confirm or adjust.
2. **Tenant-confirmed resolution auto-applies after 48 h** — saves
   landlord taps but lets the agent close cases; comfortable?
3. **Chitchat messages with no case** — proposed: logged on the channel,
   agent may reply per trust level ("you're welcome!"), no queue card.
   Alternative: everything gets a case (noisier, simpler). I propose the
   former.
