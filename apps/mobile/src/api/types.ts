/**
 * Hand-written types mirroring docs/03-engineering/api-contracts.md.
 * Every export below cites the doc section it mirrors — do NOT add a field
 * that isn't shown there; an ambiguous/unpinned shape is called out in the
 * comment above it instead of guessed at (see `CaseDetailTenant` /
 * `CaseDetailVendor` below for the two cases where the doc genuinely never
 * shows a GET response shape).
 *
 * Enum values (`Severity`, `CaseStatus`, `DraftStatus`, audit `actor`/
 * `action`) are pinned against docs/03-engineering/schema-v1.md's CHECK
 * constraints (CLAUDE.md rule 6 — schema names/values are canonical there),
 * since api-contracts.md itself only shows illustrative examples, not the
 * full vocabulary.
 */

// ---------------------------------------------------------------------------
// Conventions (api-contracts.md "Conventions")
// ---------------------------------------------------------------------------

/** schema-v1.md `cases.severity` CHECK. */
export type Severity = "emergency" | "urgent" | "routine";

/** schema-v1.md `cases.status` CHECK. */
export type CaseStatus = "open" | "awaiting_approval" | "awaiting_tenant" | "resolved" | "reopened";

/** schema-v1.md `drafts.status` CHECK. */
export type DraftStatus =
  "pending" | "stale" | "approved" | "sending" | "sent" | "rejected" | "cancelled";

/** schema-v1.md `drafts.recipient` CHECK. */
export type DraftRecipient = "tenant" | "vendor";

/** schema-v1.md `audit_log.actor` CHECK. */
export type AuditActor = "agent" | "landlord" | "system" | "prefilter";

/** schema-v1.md `audit_log.action` CHECK — the full vocabulary, so the
 *  timeline renderer never has to fall back to an unhandled-string case for
 *  a value this doc already promises won't occur. */
export type AuditAction =
  | "message_received"
  | "classified"
  | "case_opened"
  | "case_reopened"
  | "case_resolved"
  | "drafted"
  | "draft_stale"
  | "approved"
  | "edited"
  | "rejected"
  | "sent"
  | "send_cancelled"
  | "auto_sent"
  | "emergency_triggered"
  | "emergency_call_attempt"
  | "acknowledged"
  | "vendor_engaged"
  | "degraded_mode"
  | "trust_unlocked"
  | "trust_revoked"
  | "billing_changed"
  | "settings_changed";

/** messages.media jsonb shape, schema-v1.md `messages` table. */
export interface MediaItem {
  url: string;
  content_type: string;
}

/**
 * The error envelope ("Conventions" section) — every non-2xx response.
 * Endpoint-specific extra fields (e.g. `fresh_draft_id` on `draft_stale`)
 * are allowed alongside the three reserved keys; callers narrow via `code`.
 */
export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string;
  [extra: string]: unknown;
}

// ---------------------------------------------------------------------------
// Me ("Me" section)
// ---------------------------------------------------------------------------

export interface VoiceProfile {
  tone: string;
  samples: string[];
}

/** GET /v1/me response. */
export interface LandlordMe {
  id: string;
  email: string;
  full_name: string;
  timezone: string;
  voice_profile: VoiceProfile;
  price_cohort: string;
  subscription_tier: string;
  subscription_status: string;
  created_at: string;
}

/**
 * PATCH /v1/me body — exactly the four documented fields ("Me" section +
 * its v1.9 amendment: "any of `full_name`, `phone`, `timezone`,
 * `voice_profile`"). Notification prefs / quiet-hours overrides are NOT
 * here on purpose: the amendment explicitly records them as unimplemented
 * pending a schema-doc-first decision, and "emergency notifications are
 * not a settable preference" by construction. Note `phone` is settable but
 * never returned by GET (the backend's MeResponse excludes it as
 * internal-only) — a write-only field; flagged in the M2 report.
 * Response: the full updated `LandlordMe` (mirrors GET).
 */
export interface UpdateMeInput {
  full_name?: string;
  phone?: string;
  timezone?: string;
  voice_profile?: VoiceProfile;
}

// ---------------------------------------------------------------------------
// Properties ("Properties" section + the v1.12 provisioning amendment)
// ---------------------------------------------------------------------------

export interface QuietHours {
  start: string;
  end: string;
}

export interface HeatingSeason {
  start: string;
  end: string;
}

export interface BackupContact {
  name: string;
  phone: string;
}

/** The full `Property` shape from the "Properties" section. `postal_code`
 *  is nullable — it's optional on create (`postal_code?`) and the backend's
 *  own `PropertyResponse` types it `str | None`. */
export interface Property {
  id: string;
  label: string;
  address_line1: string;
  city: string;
  province: string;
  postal_code: string | null;
  twilio_number: string | null;
  house_rules: string | null;
  quiet_hours: QuietHours | null;
  heating_season: HeatingSeason | null;
  backup_contact: BackupContact | null;
  open_case_count: number;
  created_at: string;
}

/** GET /v1/properties response — standard cursor pagination ("Conventions"
 *  section; a malformed cursor 400s with `invalid_cursor`, v1.9). */
export interface PropertiesResponse {
  items: Property[];
  next_cursor: string | null;
}

/**
 * POST /v1/properties body — the documented create fields plus the v1.12
 * amendment's optional `area_code` (a 3-digit NANP string; a transient
 * provisioning hint, never persisted). Provisioning failure codes this call
 * can 4xx/5xx with: 409 `property_limit_reached`, 409 `duplicate_property`,
 * 503 `no_numbers_available`, 502 `provisioning_failed` — all mapped to
 * house lines in src/api/errors.ts.
 */
export interface CreatePropertyInput {
  label: string;
  address_line1: string;
  city: string;
  province?: string;
  postal_code?: string;
  house_rules?: string;
  backup_contact?: BackupContact;
  area_code?: string;
}

/** PATCH /v1/properties/{id} body — "same fields + `quiet_hours`,
 *  `heating_season`" per the Properties section. */
export interface UpdatePropertyInput {
  label?: string;
  address_line1?: string;
  city?: string;
  province?: string;
  postal_code?: string;
  house_rules?: string;
  backup_contact?: BackupContact;
  quiet_hours?: QuietHours;
  heating_season?: HeatingSeason;
}

// ---------------------------------------------------------------------------
// Tenants ("Tenants & Vendors" section) — sub-resource routes:
// GET/POST /v1/properties/{id}/tenants · PATCH/DELETE /v1/tenants/{id}.
// The doc pins the request body ("name?, phone, unit?,
// vulnerable_occupant?, notes?"); the GET/response row shape below is the
// backend's own TenantResponse (schema-v1.md's `tenants` table verbatim:
// + id, property_id, active, created_at). DELETE is a SOFT delete
// (active=false, returns the updated row) per the v1.9 amendment.
// ---------------------------------------------------------------------------

/** schema-v1.md `tenants.vulnerable_occupant` CHECK; null = no one. */
export type VulnerableOccupant = "infant" | "elderly" | "medical_device";

export interface Tenant {
  id: string;
  property_id: string;
  name: string | null;
  phone: string;
  unit: string | null;
  vulnerable_occupant: VulnerableOccupant | null;
  notes: string | null;
  active: boolean;
  created_at: string;
}

/** GET /v1/properties/{id}/tenants — unpaginated per the v1.9 amendment
 *  ("per-property tenant counts are small"); `next_cursor` is always null
 *  but the envelope keeps the standard list shape. */
export interface TenantsResponse {
  items: Tenant[];
  next_cursor: string | null;
}

/** POST /v1/properties/{id}/tenants body. 409 `duplicate_phone` on a
 *  `(property_id, phone)` collision (v1.10 amendment). */
export interface CreateTenantInput {
  phone: string;
  name?: string;
  unit?: string;
  vulnerable_occupant?: VulnerableOccupant;
  notes?: string;
}

/** PATCH /v1/tenants/{id} body — same optional fields. */
export interface UpdateTenantInput {
  phone?: string;
  name?: string;
  unit?: string;
  vulnerable_occupant?: VulnerableOccupant | null;
  notes?: string;
}

// ---------------------------------------------------------------------------
// Trust ("Drafts" section, v1.13 amendment) — WRITE-side only. There is no
// read contract for trust state anywhere in api-contracts.md (the
// `Property` shape carries no trust fields and no GET endpoint surfaces
// `trust_metrics`), so the app can only offer the revoke ACTION, never
// display unlock/streak state — flagged as a contract gap in the M2 report,
// not invented here.
// ---------------------------------------------------------------------------

export type RevokeTrustScope = "property" | "global";

/** POST /v1/properties/{id}/trust/revoke body (both fields optional
 *  server-side; this client always sends `scope` explicitly). */
export interface RevokeTrustInput {
  scope: RevokeTrustScope;
  reason?: string;
}

/** 200 response — idempotent; `revoked_count: 0` when nothing was
 *  unlocked (still records the landlord's `trust_revoked` audit row). */
export interface RevokeTrustResponse {
  scope: RevokeTrustScope;
  revoked_count: number;
}

// ---------------------------------------------------------------------------
// Queue ("Queue (the dashboard's main read)" section, v1.1 amendments)
// ---------------------------------------------------------------------------

export interface QueueCounts {
  total: number;
  emergency: number;
  urgent: number;
  routine: number;
  awaiting_tenant: number;
}

/**
 * One card per case needing action. `case_id` drives navigation/full-view;
 * `draft_id` drives approve/undo/reject/edit-and-send — the doc is explicit
 * these must never be conflated.
 *
 * No notification id anywhere on this shape (checked against the doc's own
 * JSON example and every v1.1 amendment bullet) — see the mobile M1 report
 * for what that means for the emergency banner's acknowledge action.
 */
export interface QueueItem {
  case_id: string;
  draft_id: string;
  severity: Severity;
  /** Agent-written, per-case; null until #197's title work lands (per this
   *  work's own brief) — never client-templated. */
  title: string | null;
  property_label: string;
  tenant_name: string;
  unit: string | null;
  received_at: string;
  tenant_message: string;
  draft_body: string;
  draft_recipient: DraftRecipient;
  /** One warm plain-English sentence for the margin note; null for rows
   *  classified before the `summary` audit key shipped. */
  why: string | null;
  /** Terse rule-fragment array — the expandable "reasoning" disclosure. */
  reasoning: string[];
  refusal_flags: string[];
  has_media: boolean;
  media_note: string | null;
}

/** GET /v1/queue response. Deliberately unpaginated (see the doc's own
 *  "Ordering/pagination" bullet) — no `next_cursor` here, unlike every
 *  other list endpoint. */
export interface QueueResponse {
  items: QueueItem[];
  counts: QueueCounts;
}

// ---------------------------------------------------------------------------
// Cases ("Cases" section)
// ---------------------------------------------------------------------------

/** GET /v1/cases list item ("pinned 2026-07-06" shape). */
export interface CaseSummary {
  id: string;
  title: string | null;
  status: CaseStatus;
  severity: Severity | null;
  tenant_name: string;
  unit: string | null;
  property_label: string;
  last_activity_at: string;
}

export interface CasesResponse {
  items: CaseSummary[];
  next_cursor: string | null;
}

export interface TimelineMessageEntry {
  kind: "message";
  direction: "inbound" | "outbound";
  party: "tenant" | "vendor" | "landlord";
  body: string;
  media: MediaItem[];
  at: string;
}

/**
 * The audit timeline entry's `payload` is specified as the FULL, opaque
 * `audit_log.payload` jsonb column — implementations pass it through
 * verbatim (v1.9 amendment), so it stays `Record<string, unknown>` here
 * rather than a per-action union. `ClassifiedAuditPayload` below is a
 * narrowing HELPER for the one action (`classified`) the UI actually reads
 * from (the case-detail "why" plaque, since — unlike the queue — this
 * endpoint has no top-level `why`/`reasoning` field; see the mobile M1
 * report), not a claim that the server only ever sends these keys.
 */
export type AuditPayload = Record<string, unknown>;

/** Narrowing helper for `action === "classified"` payloads — field names
 *  from schema-v1.md's `audit_log` column comment and the queue section's
 *  v1.1 amendment (`summary`/`rules_fired` are the same durable source the
 *  queue's `why`/`reasoning` read from). Every field optional: this is a
 *  read-time cast of an opaque jsonb blob, never a guaranteed shape. */
export interface ClassifiedAuditPayload {
  severity?: Severity;
  summary?: string | null;
  rules_fired?: string[];
  modifier?: string;
  refusal_flags?: string[];
}

export interface TimelineAuditEntry {
  kind: "audit";
  actor: AuditActor;
  action: AuditAction;
  payload: AuditPayload;
  at: string;
}

export interface TimelineDraftEntry {
  kind: "draft";
  id: string;
  status: DraftStatus;
  body: string;
  at: string;
}

export type TimelineEntry = TimelineMessageEntry | TimelineAuditEntry | TimelineDraftEntry;

/**
 * The nested `tenant`/`vendor` objects on `GET /v1/cases/{id}` are shown
 * only as `{...}` in the doc — no GET response shape is pinned anywhere for
 * either resource (the "Tenants & Vendors" section only specifies REQUEST
 * bodies). These are a best-effort read of the fields the UI actually needs
 * (name/phone/unit for the header line), flagged as a contract gap in the
 * mobile M1 report rather than presented as verbatim-confirmed.
 */
export interface CaseDetailTenant {
  id?: string;
  name?: string | null;
  phone?: string;
  unit?: string | null;
}

export interface CaseDetailVendor {
  id?: string;
  name?: string;
  trade?: string;
  phone?: string;
}

/** GET /v1/cases/{id} response. `property` reuses the full `Property` shape
 *  from the "Properties" section — the doc doesn't re-declare it here, but
 *  gives no reason to think case-detail invents a different one. */
export interface CaseDetail {
  id: string;
  status: CaseStatus;
  severity: Severity | null;
  title: string | null;
  property: Property;
  tenant: CaseDetailTenant;
  vendor: CaseDetailVendor | null;
  opened_at: string;
  resolved_at: string | null;
  timeline: TimelineEntry[];
}

/**
 * POST /v1/cases/{id}/resolve → 200 (v1.14 amendment). Idempotent: a
 * repeat call on an already-resolved case returns the SAME shape with the
 * stored `resolved_at` — never a 409. Resolving cancels every unsent
 * pending/approved draft on the case in the same transaction (one
 * `send_cancelled` audit row each); a draft already mid-send completes
 * normally.
 */
export interface ResolveCaseResponse {
  status: "resolved";
  resolved_at: string;
}

// ---------------------------------------------------------------------------
// Drafts ("Drafts (the approve loop)" section)
// ---------------------------------------------------------------------------

/** POST /v1/drafts/{id}/approve and .../edit-and-send both return this. */
export interface ApproveDraftResponse {
  status: "approved";
  scheduled_send_at: string;
  /** The undo window's own end time — countdowns derive from THIS, never a
   *  client-local constant (the doc's own "the undo window is data"). */
  undo_until: string;
}

/** DELETE /v1/drafts/{id}/approve response. */
export interface UndoDraftResponse {
  status: "pending";
}

/** POST /v1/drafts/{id}/reject response — 200, no documented body fields
 *  beyond the implicit success; typed as unknown-but-present rather than
 *  invented. */
export type RejectDraftResponse = Record<string, never>;

// ---------------------------------------------------------------------------
// Notifications ("Notifications / emergencies" section)
// ---------------------------------------------------------------------------

export interface AckNotificationResponse {
  acknowledged_at: string;
}

// ---------------------------------------------------------------------------
// Devices ("Devices (push notifications, #210 M3)" section, v1.18 amendment)
// ---------------------------------------------------------------------------

/** Narrower than `push_tokens.platform`'s stored CHECK (`'ios','android',
 *  'web'`, schema-v1.md) — Expo push tokens have no `'web'` concept, so this
 *  app only ever registers one of these two (mirrors the backend's own
 *  `app/routers/devices.py::Platform` narrowing). */
export type DevicePlatform = "ios" | "android";

/** POST /v1/devices body. Field names (`token`/`platform`) are
 *  `push_tokens`' own column names verbatim — there is no `expo_push_token`
 *  field at either the schema or API layer. */
export interface RegisterDeviceInput {
  token: string;
  platform: DevicePlatform;
}

/** POST /v1/devices → 201. Upsert-by-token: re-registering the same token
 *  under the same landlord is a no-op that still returns this shape. */
export interface DeviceResponse {
  id: string;
  platform: DevicePlatform;
  created_at: string;
}

/** DELETE /v1/devices/{id} → 200. NOT idempotent-200 on repeat — a second
 *  call 404s `device_not_found` like any other missing id (the doc's own
 *  explicit call-out: unlike `push_tokens.revoked_at`'s soft marker, this is
 *  a genuine hard delete with no row left to re-confirm against). */
export interface DeleteDeviceResponse {
  status: "deleted";
}
