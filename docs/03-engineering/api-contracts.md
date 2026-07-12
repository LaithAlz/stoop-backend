# API Contracts v1 — `apps/api`

> **Status:** Designed 2026-06-11. Canonical request/response shapes for
> every v1 endpoint. Implementations (issues #11, #40, #44, #45, #53–#57,
> #115) match this doc; the dashboard codes against it. Field names follow
> `schema-v1.md` exactly.

## Conventions

- Base path `/v1`, JSON only. Auth: `Authorization: Bearer <supabase JWT>`
  on everything except `/healthz`, `/readyz`, `/webhooks/*`.
- **Error envelope** (every non-2xx):
  ```json
  { "error": { "code": "draft_stale", "message": "Human-readable.", "request_id": "req_…" } }
  ```
  Codes are stable snake_case strings; messages may change freely.
  `request_id` is server-generated as `req_` + 32 hex chars unless the
  caller supplies a well-formed `X-Request-ID` request header, in which
  case that value is honored and echoed back unchanged (not re-prefixed).
- IDs are uuids as strings. Timestamps ISO-8601 UTC (`2026-06-11T14:02:00Z`).
- **Pagination**: `?limit=` (default 25, max 100) + `?cursor=`; responses
  carry `"next_cursor": string|null`. Lists are newest-first.
- Status/severity strings exactly as in `schema-v1.md` CHECKs.
- Idempotency: POST actions accept `Idempotency-Key` header (optional v1,
  honored where noted).

---

## Me

`GET /v1/me` → 200
```json
{ "id": "…", "email": "…", "full_name": "…", "timezone": "America/Toronto",
  "voice_profile": {"tone": "warm, direct", "samples": ["…"]},
  "price_cohort": "early_access", "subscription_tier": "free",
  "subscription_status": "none", "created_at": "…" }
```
- 403 `email_required` if the verified token has no `email` claim
  (phone-only signup case) — checked before any write; fail-closed.
- 403 `account_deleted` if the `landlords` row matching this auth_user_id
  has `deleted_at` set (soft-deleted via migration 0004's `auth.users`
  lifecycle trigger) — resurrection is not allowed. Same stable code and
  static message as `require_landlord`'s `account_deleted` (below). The
  upsert's `ON CONFLICT ... WHERE deleted_at IS NULL` guard makes the
  check atomic with the write itself — no separate pre-SELECT race — and
  nothing is mutated (`email`/`updated_at` untouched) before the error is
  returned. Closes #135 part 1.

`PATCH /v1/me` — body: any of `full_name`, `phone`, `timezone`,
`voice_profile`. Emergency notifications are not a settable preference.

**v1.9 amendment (2026-07-12 — #57 implementation):** `PATCH` never
lazily provisions — a caller with no live `landlords` row (never
provisioned, or soft-deleted) gets the same 403 `account_deleted` as
`GET`/`require_landlord`, and nothing is written. `timezone` is `NOT NULL`
in schema-v1.md; an explicit `null` 422s with code `invalid_field` rather
than a raw `NotNullViolation`. Issue #57's own acceptance criteria
additionally lists "notification prefs" and "quiet-hours overrides" as
settable here — neither has a column on `landlords` in schema-v1.md, and
this shape (the four fields above) already predates that AC. NOT
implemented pending a schema-doc-first decision (add the columns, or
confirm quiet-hours stays property-scoped only per the `Property.
quiet_hours` field above and drop the AC bullet).

`GET/PATCH /v1/me` use `require_user` directly (the provisioning path — a
brand-new auth user has no `landlords` row yet, and this lazily creates
one), not `require_landlord`. `GET /v1/me`'s own upsert now filters
`deleted_at` itself (see the `account_deleted` note above), atomically
with the write. Every OTHER authenticated endpoint (#53 onward) uses the
`require_landlord` dependency (#22) instead, which looks up the caller's
`landlords` row **excluding soft-deleted rows** (`deleted_at IS NULL`) and
403s with the same stable code `account_deleted` — same error envelope —
if none is found. #135 part 1 is now fully closed: both `require_landlord`
(every other endpoint) and `GET /v1/me` itself reject a soft-deleted
account instead of silently resurrecting or exposing it.

## Properties

`GET /v1/properties` → `{ "items": [Property], "next_cursor": null }`
`POST /v1/properties` — body: `label`, `address_line1`, `city`,
`province?`, `postal_code?`, `house_rules?`, `backup_contact?`.
Provisions a Twilio number (#53); 201 → full `Property`.
`GET /v1/properties/{id}` · `PATCH` (same fields + `quiet_hours`,
`heating_season`) · `DELETE` (409 with code `has_open_cases` if any).

`Property`:
```json
{ "id": "…", "label": "41 Palmerston", "address_line1": "…", "city": "Toronto",
  "province": "ON", "postal_code": "…", "twilio_number": "+1416…",
  "house_rules": "…", "quiet_hours": {"start":"21:00","end":"08:00"},
  "heating_season": {"start":"09-15","end":"06-01"},
  "backup_contact": {"name":"…","phone":"+1…"}, "open_case_count": 1,
  "created_at": "…" }
```

**v1.9 amendment (2026-07-12 — #54 implementation):** `DELETE` can also
409 with code `has_dependents` when the property survives the
`has_open_cases` check but still has FK-referencing rows — the explicit
`ON DELETE RESTRICT` columns targeting `properties(id)` (schema-v1.md):
`tenants.property_id`, `cases.property_id`, `messages.property_id`,
`trust_metrics.property_id` — surfaced cleanly instead of a raw 500 on the
underlying `IntegrityError`. `GET`/`PATCH`/`DELETE /v1/properties/{id}`
404 with code `property_not_found` for a missing or cross-tenant id (the
two are indistinguishable by design — never leak cross-tenant existence).
**"Voice-profile fields"** (#54's own acceptance-criteria wording) live on
`landlords.voice_profile` (schema-v1.md), not on `properties` — that AC is
satisfied via `PATCH /v1/me` (below), not this endpoint; noted here so the
two don't look like an unaddressed gap.

## Tenants & Vendors

`GET/POST /v1/properties/{id}/tenants` · `PATCH/DELETE /v1/tenants/{id}`
Tenant body/shape: `name?`, `phone`, `unit?`, `vulnerable_occupant?`, `notes?`.

`GET/POST /v1/vendors` · `PATCH/DELETE /v1/vendors/{id}`
Vendor shape: `name`, `trade`, `phone`, `notes?`, `working_hours?`, `active`.

**v1.9 amendment (2026-07-12 — #54 implementation):** `DELETE` on both
resources is a SOFT delete (`active = false`, returns the updated row) —
neither `tenants` nor `vendors` has a `deleted_at` column (schema-v1.md).
Of the FKs targeting them, only `cases.tenant_id` is an explicit
`ON DELETE RESTRICT`; `messages.tenant_id` and `cases.vendor_id` carry no
explicit `ON DELETE` clause (Postgres default `NO ACTION`, schema-v1.md) —
which still blocks an immediate hard delete while a referencing row
exists, just not via the `RESTRICT` keyword specifically. Either way, once
any case/message history exists a hard delete is structurally blocked, so
soft-delete is the only viable semantics regardless of which FK fires.
Idempotent: deleting an already-inactive row just re-confirms the state.
404 codes:
`tenant_not_found` / `vendor_not_found`, same cross-tenant-safe
non-disclosure as `property_not_found` above. `GET /v1/properties/{id}/
tenants` is unpaginated (per-property tenant counts are small); `GET
/v1/vendors` follows the standard cursor convention.

## Queue (the dashboard's main read)

`GET /v1/queue` → one card per case needing action, ordered
emergency-followup → urgent (oldest first) → routine (oldest first):
```json
{ "items": [{
    "case_id": "…", "draft_id": "…", "severity": "urgent",
    "title": "No heat — Unit 2", "property_label": "41 Palmerston",
    "tenant_name": "Maria", "unit": "2", "received_at": "…",
    "tenant_message": "hey sorry to text so late…",
    "draft_body": "Hi Maria — so sorry…", "draft_recipient": "tenant",
    "why": "No heat on a cold night with a baby in the unit can't wait, so I treated it as urgent.",
    "reasoning": ["no heat + overnight + infant present", "Ontario bylaw ≥21°C", "…"],
    "refusal_flags": [],
    "has_media": false, "media_note": null
  }],
  "counts": { "total": 3, "emergency": 0, "urgent": 1, "routine": 2,
              "awaiting_tenant": 1 } }
```

**v1.1 amendments (2026-07-06 — fields the Clarity dashboard rebuild
needs; PR #181 review):**

- **`why`** (new, required, nullable): ONE warm plain-English sentence
  for the card's margin note. Durable source: the `summary` key on the
  `audit_log` `'classified'` payload (schema-v1 **v1.7** amendment —
  `reasoning_log` itself is transient graph state and cannot be queried).
  Rows classified before the key ships have no summary → `why: null`,
  and the dashboard falls back to its generic margin-note copy.
  `reasoning` (the terse rule-fragment array, from the same payload's
  `rules_fired`) stays as the expandable audit trail; the two serve
  different surfaces and BOTH ship.
- **`title` is the emergency-banner headline.** Agent-written plain
  English per case, never client-side template copy — the dashboard must
  not hardcode incident wording (PR #181 shipped a hardcoded "reported a
  flood" once; never again).
- **`has_media` / `media_note`** (new; both always present —
  `has_media` defaults `false`, `media_note` nullable): `media_note` is
  an agent-written plain-English note ("Sent a photo of the ceiling"),
  `null` until MMS lands (#46). Full media objects stay on `GET /v1/cases/{id}` only —
  the queue card carries at most the note.
- **`counts.awaiting_tenant`** (new): number of cases in
  `awaiting_tenant` status. NOT included in `counts.total` — `total`
  counts action-needed cards only; `awaiting_tenant` feeds the
  "N waiting on tenants" line.
- **The "waiting on tenants" footnote** (named cases, not just a count)
  is served by the EXISTING `GET /v1/cases?status=awaiting_tenant&limit=3`
  — no new field; the dashboard makes a second read.
- **Which id drives which action:** `draft_id` → approve / undo / reject
  / edit-and-send; `case_id` → full view + conversation navigation.
  Clients must not conflate them into one key.
- **Undo countdowns derive from `undo_until`** (returned by approve),
  never a client-local constant — server time owns the window ("the undo
  window is data").
- **Ordering/pagination:** `/v1/queue` is deliberately UNPAGINATED and
  oldest-first per severity tier — an exception to the newest-first +
  cursor convention above, because the queue is bounded by open cases,
  not message volume (`conversation-model.md`, "queue ordering").
- **Auto-handled feed: deferred, slot reserved.** The dashboard's
  "I handled this myself" note requires the trust ladder (#60); nothing
  auto-sends today (rule 3). Endpoint to be specified WITH #60
  (working name `GET /v1/activity?kind=auto_sent`) — until then the
  dashboard renders no handled note from live data.

## Cases

`GET /v1/cases?status=&severity=&property_id=` → `{ items: [CaseSummary], next_cursor }`

`CaseSummary` (pinned 2026-07-06 — the "waiting on tenants" footnote and
all case lists read this shape):
```json
{ "id": "…", "title": "No heat — Unit 2", "status": "awaiting_tenant",
  "severity": "urgent", "tenant_name": "Maria", "unit": "2",
  "property_label": "41 Palmerston", "last_activity_at": "…" }
```
`GET /v1/cases/{id}` → full timeline (messages + audit entries interleaved,
oldest-first):
```json
{ "id": "…", "status": "awaiting_approval", "severity": "urgent",
  "title": "No heat — Unit 2", "property": {...}, "tenant": {...},
  "vendor": null, "opened_at": "…", "resolved_at": null,
  "timeline": [
    { "kind": "message", "direction": "inbound", "party": "tenant",
      "body": "…", "media": [], "at": "…" },
    { "kind": "audit", "actor": "agent", "action": "classified",
      "payload": { "severity": "urgent", "rules_fired": ["…"] }, "at": "…" },
    { "kind": "draft", "id": "…", "status": "pending", "body": "…", "at": "…" }
  ] }
```
404 with code `case_not_found` for a missing or cross-tenant id (same
non-disclosure convention as `property_not_found`).

**v1.9 amendments (2026-07-12 — #55 implementation):**
- **`draft` timeline entries gain `id`** (the draft's uuid) — closes one of
  PR #190's three catalogued contract gaps: the dashboard needs it to wire
  approve/undo/reject actions from inside the thread view, exactly like
  `GET /v1/queue`'s `draft_id`. Additive field, not a breaking change.
- **`payload.summary` surfacing** (PR #190's second gap) needed no contract
  change: the audit timeline entry was always specified as the full,
  opaque `audit_log.payload` object, so `summary` (schema-v1 v1.7) surfaces
  automatically once `classify_severity` writes it — implementations must
  pass the payload column through verbatim, never reconstruct a narrower
  shape.
- **Media captions** (PR #190's third gap) remain UNRESOLVED — `messages.
  media` (schema-v1.md) is `[{url, content_type}]` with no caption
  sub-key, and captioning is MMS-pipeline work (#46) that hasn't landed.
  Flagged, not invented.
- **`GET /v1/messages` is NOT specified.** Issue #55's acceptance criteria
  additionally lists a "channel view per tenant" endpoint, but no shape for
  it exists anywhere in this doc, and PR #190's frontend rebuild already
  consumes `GET /v1/cases/{id}`'s timeline as the conversation view
  instead. Not implemented pending a doc-first decision: either specify a
  real `GET /v1/messages` shape, or confirm the case timeline supersedes it
  and drop the AC bullet.
- **Pagination cursor validation:** a malformed `cursor` on any
  cursor-paginated list (`GET /v1/properties`, `GET /v1/vendors`,
  `GET /v1/cases`) 400s with code `invalid_cursor` rather than 500ing or
  silently ignoring it.

`POST /v1/cases/{id}/resolve` — body `{ "reason": "landlord" }` → 200.
`POST /v1/cases/{id}/ask-vendor` — body `{ "vendor_id": "…", "note?": "…" }`
→ 201 `{ "draft_id": "…" }` (vendor draft enters the same queue, #115).

## Drafts (the approve loop)

`POST /v1/drafts/{id}/approve` → 200
```json
{ "status": "approved", "scheduled_send_at": "…(+5s)", "undo_until": "…" }
```
- 409 `draft_stale` if a newer tenant message invalidated it — body
  includes `"fresh_draft_id"`. Idempotent on repeat.
- Undo window is +5s from the dashboard (approve-by-SMS: +5min — see
  Webhooks).
`DELETE /v1/drafts/{id}/approve` → 200 `{ "status": "pending" }`
(cancels within the undo window; 409 `already_sent` after).
`POST /v1/drafts/{id}/reject` — body `{ "note?": "…" }` → 200.
`POST /v1/drafts/{id}/edit-and-send` — body `{ "body": "…" }` → same
response as approve. Records `edited: true` for trust metrics.

## Notifications / emergencies

`POST /v1/notifications/{id}/ack` → 200 `{ "acknowledged_at": "…" }`
(also reachable via tokenized GET link from SMS: `/ack/{token}`).
`GET /v1/notifications?type=emergency_call&status=pending` for the
dashboard's emergency banner.

## Billing (Train 2)

`POST /v1/billing/checkout` — body `{ "plan": "full" }` → `{ "checkout_url": "…" }`
(price chosen server-side from `price_cohort` — never client-supplied).
`POST /v1/billing/portal` → `{ "portal_url": "…" }`.

## Webhooks (no auth header; signature-verified)

- `POST /webhooks/twilio/sms` — form-encoded from Twilio. Always 200 fast;
  persist before process; dedupe on `MessageSid`. (#40) **Approve-by-SMS
  (#122):**
  - Tier-0 (`emergency-prefilter.md`) runs on **every** inbound SMS
    before any routing split. A Tier-0 hard trigger on a
    landlord-authored message does **not** invoke the tenant emergency
    protocol (there is no tenant/case to act on); it is recorded,
    surfaced as a `needs_eyes` notification, and acknowledged in the
    reply — never silently dropped.
  - Routing predicate after Tier-0: `From` == the landlord's phone for
    the property owning the `To` number AND `From` does not match an
    active tenant of that property → approve-by-SMS handler. On
    collision (self-managing landlord living in-unit) the **tenant
    pipeline wins**, so an emergency can never be bypassed.
  - Replies correlate to the draft id carried in that landlord's most
    recent draft-ready notification (`notifications.payload`), scoped
    to the property owning the `To` number (via
    `case_id → cases.property_id`) — a multi-property landlord replying
    `1` can only ever act on a draft for the property whose thread they
    replied in. If that draft is no longer pending
    (stale/superseded/approved) nothing sends and the landlord gets a
    fresh-draft notice.
  - `1` = approve — identical trust/audit/stale-draft semantics to
    `POST /v1/drafts/{id}/approve` but `scheduled_send_at = now()+5min`;
    `2` = reject; `UNDO` within those 5 minutes cancels.
  - The `From` number is a weak authenticator (spoofable): a reply can
    only act on the single referenced draft, and the 5-minute window
    bounds a spoofed approve.
  - The landlord's reply is stored in `messages` (`party='landlord'`,
    `tenant_id` NULL) and **never** forwarded to a tenant or vendor.
  - Unrecognized replies are recorded in `messages` and surfaced —
    never routed to app logs, never silently dropped.
  - **Unknown `To` number** (#40 contract addition — the contract was
    previously silent on this): if the `To` number matches no
    `properties.twilio_number`, persistence is structurally impossible
    (there is no `landlord_id`/`property_id` to satisfy `messages`' NOT
    NULL columns) — the handler answers 200 with a metadata-only log (no
    phone number) and stores nothing. Not a 500 (Twilio would retry a
    request that can never succeed) and not a silent no-op (the log line
    exists). Covers a number that isn't provisioned yet or has been
    released.
  - **Out-of-order delivery / timestamp ordering** (#40 contract note):
    the inbound SMS webhook payload Twilio sends carries no per-message
    timestamp field to order by, and `messages` has no `sent_at` column
    (`created_at` is arrival time) — see schema-v1.md. #40 stores
    messages in arrival order; the eventual ordering guarantee referenced
    by issue #40's acceptance criteria is delivered by #110's case/
    timeline logic, not by this endpoint. Flagged here rather than
    inventing a schema column pre-#110.
- `POST /webhooks/twilio/voice` — TwiML callbacks for the emergency call
  (`Digits=1` → acknowledge). (#108)
- `POST /webhooks/twilio/status` — Twilio delivery-status callback,
  signature-verified like the others. Looks up the message by
  `twilio_sid`, appends a `message_status_events` row (#151, endpoint:
  #152). Every callback is appended as a fact — duplicates/out-of-order
  arrivals expected; delivery state derives from status precedence
  (terminal wins), never recency. Unknown `twilio_sid` or
  out-of-vocabulary status → 200 + drop with a metadata-only log (no
  body, no phone). Always 200 fast — the entire post-signature body is
  wrapped so a transient DB error never surfaces as anything but 200
  (#152 contract fidelity fix: an earlier revision could 500 on a DB
  blip during the `twilio_sid` lookup).
  - **Replay bound** (#40/#152 contract addition): at most 100
    `message_status_events` rows are appended per `message_id`; once that
    cap is reached, further callbacks for that message are dropped with a
    metadata-only log (count only) rather than accepted forever. Bounds
    storage under a replay storm while remaining generous headroom over
    any legitimate delivery flow (a message realistically sees at most a
    handful of status transitions).
- `POST /webhooks/stripe` — signature + event-id idempotent. (#59)

## Health

`GET /healthz` → 200 always · `GET /readyz` → 200 / 503 (DB unreachable).
