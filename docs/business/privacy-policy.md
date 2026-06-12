# Stoop — Privacy Policy (DRAFT)

> ⚠️ **Draft for lawyer review — do not publish as-is.** Written 2026-06-12,
> PIPEDA-oriented. Replaces the boilerplate at
> `apps/web/src/routes/privacy.tsx` once reviewed.

**Effective date:** [DATE] · **Operated by:** [LEGAL ENTITY], Ontario, Canada.

## The short version

Stoop processes tenant maintenance messages **on behalf of landlords**.
We collect only what the service needs, we don't sell personal
information, we don't use your messages to train AI models, message data
stays in Canada, and the records we keep exist so that both landlords and
tenants have an accurate history of what was said and done.

## 1. Whose information we handle

- **Landlords (our customers):** name, email, phone, properties, vendor
  contacts, billing details (held by Stripe), settings, and usage of the
  dashboard.
- **Tenants (on the landlord's behalf):** phone number, unit, the
  messages and photos they send to the property's Stoop number, and
  information the landlord records (e.g., that a household includes an
  infant — used solely to prioritize urgent issues like heat loss).
- **Vendors (on the landlord's behalf):** name, trade, phone, and
  coordination messages.

Landlords are required to inform their tenants that a software assistant
helps manage maintenance messages (we provide the notice template).

## 2. How information is used

- Receiving, sorting, and prioritizing maintenance messages — including
  AI analysis of message content to assess urgency and draft replies.
- Notifying the landlord, including phone calls for suspected emergencies.
- Coordinating with the landlord's own vendors (vendors are not shown
  tenant phone numbers; Stoop relays).
- Keeping an accurate, tamper-evident record of messages and actions.
- Billing, support, service improvement, and legal compliance.

**We do not sell personal information. We do not use message content to
train AI models, and our AI providers are contractually restricted from
doing so. We do not use tenant information for marketing.**

## 3. AI processing

Message content is processed by Anthropic (Claude models) to assess
urgency and draft replies, under terms that prohibit training on our
data. Quality records (traces) are retained in LangSmith with the same
restriction. A landlord approves AI-drafted replies before they send,
except routine replies the landlord has explicitly automated and safety
instructions during suspected emergencies.

## 4. Service providers (subprocessors)

Supabase (database & authentication; hosted in Canada — `ca-central-1`),
Twilio (SMS/voice), Anthropic (AI), LangSmith (AI quality), Stripe
(payments), Sentry (errors), PostHog (product analytics — identified
only by an internal ID; no message content), Plausible (anonymous
website analytics), Cloudflare (website hosting), Fly.io (application
hosting, Toronto region). [LAWYER: confirm cross-border disclosure
language for US-based providers — Twilio/Anthropic/Stripe process data
in the US.]

## 5. Residency & security

Message and account data is stored in Canada. Data in transit is
encrypted (TLS); data at rest is encrypted by our providers. Access is
limited to what operating the service requires. Message history and the
activity log are append-only by design.

## 6. Retention

Account data: life of the account + [90 days]. Messages and activity
records: retained while the landlord's account is active, because they
exist to provide both parties an accurate history; exported to the
landlord on cancellation and deleted [60 days] after account closure,
except where law requires longer. Analytics: [12 months].

## 7. Your rights

Landlords may access, correct, export, or delete their information via
the dashboard or by contacting us. **Tenants** may contact us at
[privacy@DOMAIN] to access information we hold about them or raise a
concern; where the landlord controls the information, we will route the
request to them and assist. You may complain to the Office of the
Privacy Commissioner of Canada.

## 8. Cookies & analytics

The marketing site uses cookieless, anonymous analytics (Plausible). The
dashboard uses PostHog to understand product usage, tied to an internal
account ID — never to message content. No advertising trackers anywhere.

## 9. Changes & contact

Material changes announced 30 days in advance to account holders.
**Privacy contact:** [privacy@DOMAIN] · [LEGAL ENTITY + ADDRESS]
