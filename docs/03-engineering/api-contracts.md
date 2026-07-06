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

## Tenants & Vendors

`GET/POST /v1/properties/{id}/tenants` · `PATCH/DELETE /v1/tenants/{id}`
Tenant body/shape: `name?`, `phone`, `unit?`, `vulnerable_occupant?`, `notes?`.

`GET/POST /v1/vendors` · `PATCH/DELETE /v1/vendors/{id}`
Vendor shape: `name`, `trade`, `phone`, `notes?`, `working_hours?`, `active`.

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

- **`why`** (new, required): ONE warm plain-English sentence for the
  card's margin note — the classify-severity summary line the agent
  already writes to `reasoning_log` (landlord-visible by rule).
  `reasoning` (the terse rule-fragment array) stays as the expandable
  audit trail; the two serve different surfaces and BOTH ship.
- **`title` is the emergency-banner headline.** Agent-written plain
  English per case, never client-side template copy — the dashboard must
  not hardcode incident wording (PR #181 shipped a hardcoded "reported a
  flood" once; never again).
- **`has_media` / `media_note`** (new): `media_note` is an agent-written
  plain-English note ("Sent a photo of the ceiling"), `null` until MMS
  lands (#46). Full media objects stay on `GET /v1/cases/{id}` only —
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
- **Auto-handled feed: deferred, slot reserved.** The dashboard's
  "I handled this myself" note requires the trust ladder (#60); nothing
  auto-sends today (rule 3). Endpoint to be specified WITH #60
  (working name `GET /v1/activity?kind=auto_sent`) — until then the
  dashboard renders no handled note from live data.

## Cases

`GET /v1/cases?status=&severity=&property_id=` → `{ items: [CaseSummary], next_cursor }`
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
    { "kind": "draft", "status": "pending", "body": "…", "at": "…" }
  ] }
```
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
