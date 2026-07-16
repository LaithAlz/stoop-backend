# Stoop mobile — landlord app (Expo / React Native)

The native landlord app (issue #210): full parity with the Clarity web
dashboard, built in phases. Design authority is
`docs/mockups/07-clarity-redesign.html` ("Clarity"); design tokens live in
`src/theme/tokens.ts` with per-token source mapping back to that mockup and
`apps/web/src/styles.css`.

## Stack

- Expo SDK 57 (managed workflow) · React Native 0.86 · React 19.2
- expo-router 57 (file-based routing, typed routes) — auth gate via
  `Stack.Protected`
- TypeScript strict · ESLint (eslint-config-expo + prettier) · Prettier
- @supabase/supabase-js (email+password auth), sessions in
  expo-secure-store
- jest-expo + @testing-library/react-native (v13 line — the sync-render API
  expo-router 57's `renderRouter` is built against; don't bump to v14 until
  expo-router's testing library supports async render)

**Package manager: npm.** Expo's tooling (`npx expo install`, version
validation, `npx expo-doctor`) is happiest with npm, and `npx
create-expo-app` scaffolds an npm lockfile. The app is self-contained like
`apps/web` — its own `package.json` + `package-lock.json`, no monorepo JS
workspaces (do not add any; `apps/web` must stay undisturbed).

## Run it

```bash
cd apps/mobile
npm install

# env (required before the app will boot):
cp .env.example .env
# fill in EXPO_PUBLIC_SUPABASE_URL / EXPO_PUBLIC_SUPABASE_ANON_KEY
# (Supabase dashboard -> Project Settings -> API). Never commit .env.
# Never copy from apps/api/.env — the mobile app only uses these two
# public values; the service-role key must never appear here.

npm run ios       # iOS simulator (via Expo Go or a dev build)
npm run android   # Android emulator
npm start         # QR code for Expo Go on a physical device
```

## Checks

```bash
npm run typecheck     # tsc --noEmit (strict)
npm run lint          # expo lint (ESLint)
npm run format:check  # prettier
npm test              # jest (auth-gate routing tests; no network, ever)
npx expo export       # build smoke test
```

## Phase map (issue #210 — one PR each)

- **M0 (this)** — scaffold: Clarity tokens, tab shell (Home /
  Conversations / Properties / Me), Supabase email+password sign-in,
  secure session persistence, auth gate, test harness.
- **M1** — typed API client against
  `docs/03-engineering/api-contracts.md` + the core loop: queue, approval
  card (approve / undo / edit-and-send / reject), conversation thread,
  emergency banner + acknowledge.
- **M2** — parity: properties + provisioning, trust-ladder controls,
  onboarding wizard, Me/settings.
- **M3** — push notifications (new backend surface; push never carries the
  emergency path — voice/SMS remain the only emergency channels).

## Notes

- **Store submission is founder-gated** (Apple Developer program +
  Google Play registration are paid, founder-owned steps). Everything here
  is built and tested credential-free: simulator/emulator + Expo Go only.
  Bundle identifiers in `app.config.ts` are placeholders until then.
- Mobile is not in CI yet — a typecheck/lint job should be added when this
  scaffold lands (tracked in issue #210).
- Copy rules (root `CLAUDE.md` rule 8) apply to every string a landlord
  sees. Never log JWTs, tenant phone numbers, or message bodies (rule 5).
