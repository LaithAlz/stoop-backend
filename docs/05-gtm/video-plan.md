# Marketing Video Library — full production spec (Higgsfield)

> **Status:** v2, 2026-06-12 — expanded per founder: full video library,
> complete generation prompts for every shot, execution-ready for the
> Higgsfield MCP session. Brand rules apply throughout: plain English
> (no "triage"), no scale tells, prices exactly as locked ($5/month early
> access · locked for life · emergency line free), Heritage brand for all
> end cards.

## Guardrails (non-negotiable, all videos)

1. **No synthetic humans presented as customers or endorsers.** People
   appear only as dramatization — hands, silhouettes, sleeping figures,
   over-shoulder. Faces never focal, never speaking to camera, never
   captioned as real users.
2. **No AI-generated product UI.** Every screen is a real capture (app or
   `docs/mockups/04`/`05`), composited in post. Ads never show a product
   that doesn't exist.
3. **Claims match the ToS:** "only a true emergency rings your phone" ✔ ·
   "never misses" ✘ · "you approve everything" ✔ · "fully automatic" ✘.
4. Tasteful drama; captions burned in on every cut (sound-off viewing).

## Shared production language

**Grade suffix — append to every night prompt:**
`cinematic realism, 35mm lens, shallow depth of field, cool blue-dark
interior with a single warm amber practical light, quiet domestic
atmosphere, photorealistic, no people's faces in focus, subtle film grain`

**Grade suffix — append to every morning prompt:**
`cinematic realism, 35mm lens, shallow depth of field, warm golden
morning light through windows, calm domestic atmosphere, photorealistic,
no people's faces in focus, subtle film grain`

**Avoid on all generations:** readable text on generated screens (UI is
composited), logos, recognizable faces, horror tone, lens flares,
oversaturation.

**End-card spec (built in brand, never generated):** canvas `#FDFCFB`
background · wordmark "Stoop." in Fraunces semibold, forest `#2D4A3E`
with the period in amber · headline in Fraunces, ink `#1A1C19` ·
sub-line in Plus Jakarta Sans medium, ink-muted · domain bottom-center,
letterspaced uppercase 11px. 5 s hold, 0.4 s fade-in.

**Clip lengths:** generate 4–6 s per shot at the highest available
resolution, 24 fps look. Master in 16:9; 9:16 derived per the reframe
notes on each shot. Music: sparse warm piano/ambient; sound design notes
per video. VO: none unless noted — captions carry the words.

---

## Video index

| # | Title | Len | Ratio | Destination | New gen shots |
|---|---|---|---|---|---|
| V1 | 2:12 AM (hero) | 35 s | 16:9 + 9:16 | /early-access, YouTube, FB | 5 |
| V2 | When It Can't Wait | 20 s | 16:9 + 9:16 | site §emergency, ads | 4 |
| V3 | The Silent Phone (cutdown) | 15 s | 9:16 | Reels/Shorts | 0 (re-edit) |
| V4 | The Ring (cutdown) | 12 s | 9:16 | Reels/Shorts | 0 (re-edit) |
| V5 | The Parking Question | 15 s | 9:16 | Reels/Shorts (light) | 3 |
| V6 | Your Guy, Booked | 20 s | 16:9 + 9:16 | site §vendors, ads | 4 |
| V7 | The Paper Trail | 20 s | 16:9 | site, LinkedIn-later | 4 |
| V8 | Before / After | 15 s | 9:16 | Reels/Shorts | 4 |
| V9 | Is It an Emergency? (checker promo) | 10 s | 9:16 + 1:1 | social → quiz | 1 |
| V10 | Every Message (brand mood) | 12 s | 16:9 | site background loop, pre-roll | 4 |

---

## V1 · "2:12 AM" — the sleep promise (hero, 35 s)

Story: the phone that doesn't ring. Night anxiety → morning calm; the
grade arc carries the emotion.

| # | s | Generation prompt (+ night/morning suffix) | Camera | Overlay / caption | Audio |
|---|---|---|---|---|---|
| 1 | 0–5 | `Extreme close-up of a smartphone lying face-up on a wooden nightstand beside a glass of water, screen suddenly illuminating in a dark bedroom at night, soft amber glow on the wood, alarm clock blur in background reading 2:12` + night suffix | Slow dolly-in, locked vertical | Real SMS bubble fades in over screen: "the heat stopped working and it's getting really cold… anything you can do tonight??" · caption: **2:12 AM. A tenant texts.** | Single soft buzz; room tone; faint furnace silence |
| 2 | 5–9 | `The same smartphone screen fading back to black on the nightstand, a figure asleep under a duvet in the background completely still, moonlight through curtains, dust motes drifting` + night suffix | Static, breathing handheld 5% | Caption: **Stoop read it.** | Music enters: single piano note, very sparse |
| 3 | 9–13 | *(no generation — UI moment)* Real mockup capture on dark canvas | — | Chip animates in: **URGENT — NOT AN EMERGENCY** · sub: "your phone stays silent" · caption mirrors | Soft UI tick; piano holds |
| 4 | 13–18 | `Time-lapse of a bedroom window, night turning to warm golden sunrise, light crawling across a duvet and wooden floor, kettle steam rising in a kitchen in the final second` + morning suffix | Locked time-lapse, cut to macro steam | Caption: **It waited. Politely.** | Music warms: second voice joins; kettle hiss |
| 5 | 18–24 | `Over-the-shoulder view of hands holding a smartphone at a kitchen counter next to a fresh coffee, morning light, thumb hovering over the screen, relaxed posture, face out of frame` + morning suffix | Slow orbit 10° | Real approval-queue capture comped onto screen; thumb taps **Approve & send**; 5-second undo bar slides · caption: **One tap. Your words.** | Gentle tap; undo-bar whoosh, quiet |
| 6 | 24–29 | `A different sunlit kitchen, a smartphone buzzing on a counter beside houseplants, a hand picking it up, relieved body language, face soft-focus out of frame` + morning suffix | Handheld, slight push | Real bubble: "Hi Maria — so sorry. My HVAC guy will be there at 7:30." · caption: **Fixed before it became a fight.** | Cheerful double-buzz; music resolves |
| 7 | 29–35 | *(end card, built)* | — | **"Your tenants text. You sleep."** · $5/month early access — locked for life · Emergency line free forever · [domain] | Final piano chord; silence last 1 s |

**9:16 reframe:** shots 1–2 center-punch on the phone; shot 5 reframe to
hands+screen; captions move to top third.
**Pass/fail per shot:** no readable generated text; no focal faces;
nightstand scene must feel *calm*, not eerie — regenerate if horror-adjacent.

---

## V2 · "When It Can't Wait" — the trust counterweight (20 s)

Story: same grammar, opposite outcome. Answers "what if it's real?"

| # | s | Generation prompt | Camera | Overlay / caption | Audio |
|---|---|---|---|---|---|
| 1 | 0–4 | `Extreme close-up of a smartphone on a nightstand illuminating in a dark bedroom, 12:47 on a clock blurred behind, harsher colder light than warm, slight urgency in the glow` + night suffix | Quick dolly-in | Real bubble: "WATER IS COMING THROUGH THE CEILING LIGHT" · caption: **12:47 AM.** | One hard buzz; room tone; *one full second of silence* |
| 2 | 4–9 | `The same smartphone erupting with an incoming call, screen bright in the dark room, vibration visibly shaking the nightstand, water glass rippling` + night suffix | Crash zoom to screen | Real call-screen capture: **Stoop — Emergency · 88 Dovercourt** · caption: **This one rings.** | Full ringtone, loud after the silence — the campaign's best audio moment |
| 3 | 9–13 | `A bedside lamp snapping on, a figure sitting up and grabbing the phone from the nightstand, urgent but controlled movement, face away from camera` + night suffix | Handheld, energy | Caption: **You're awake because you should be.** | Lamp click; rustle; ring cuts on answer |
| 4 | 13–16 | *(UI moment, no gen)* Real capture | — | Tenant safety message timestamped 12:47:31: "Shut off the breaker for the living room now — help is moving." · caption: **Your tenant already knows what to do.** | Calm UI tick under tension |
| 5 | 16–20 | *(end card)* | — | **"A dripping tap waits until morning. This doesn't."** · "Stoop knows the difference." · [domain] | Low warm chord; resolve |

**Pass/fail:** shot 2's ring must *startle* after V-style silence — if the
edit doesn't make the viewer flinch slightly, recut. No water/destruction
shown; the message says it, the room stays dry (tasteful drama rule).

---

## V3 · "The Silent Phone" — 15 s vertical cutdown (re-edit of V1)

Edit map: V1-1 (3 s) → V1-3 chip (3 s) → V1-2 black screen (2 s, caption
**"That's it. That's the product."**) → V1-5 approve (4 s) → end card
(3 s, **"Your tenants text. You sleep."** + domain). Hook in first 1.5 s:
open mid-buzz. Audio: buzz → silence → tap → chord.

## V4 · "The Ring" — 12 s vertical cutdown (re-edit of V2)

Edit map: V2-1 (3 s) → V2-2 ring (4 s) → V2-4 safety message (3 s) → end
card (2 s, **"Stoop knows the difference."**). This is the paid-test
candidate: strongest hook of the library.

---

## V5 · "The Parking Question" — 15 s, light tone

Story: the 80% — most messages aren't drama, and Stoop quietly eats them.

| # | s | Generation prompt | Overlay / caption | Audio |
|---|---|---|---|---|
| 1 | 0–4 | `A smartphone on a desk beside a laptop in a bright home office buzzing once, casual daytime scene, mug and houseplant, relaxed` + morning suffix | Real bubble: "hey! is there visitor parking? my sister's visiting" · caption: **Not everything is a crisis.** | Light buzz; playful pizzicato |
| 2 | 4–8 | *(UI moment)* Real capture: chip **ROUTINE** → draft appears with parking answer | Caption: **Stoop answers from your house rules.** | Quick UI ticks, almost rhythmic |
| 3 | 8–12 | `The same desk, the phone face-down now, hands typing on the laptop unbothered, coffee steam, productive calm` + morning suffix | Caption: **You find out in tonight's recap.** | Music resolves, smug-calm |
| 4 | 12–15 | *(end card)* **"The small stuff handles itself."** · $5/month early access · [domain] | Single pluck |

---

## V6 · "Your Guy, Booked" — vendor coordination (20 s)

Story: the differentiator nobody else shows — Stoop lines up *your own*
plumber, you approve every text.

| # | s | Generation prompt | Overlay / caption | Audio |
|---|---|---|---|---|
| 1 | 0–4 | `Macro shot of a slow drip falling from a kitchen faucet into a metal sink, single droplet in sharp focus, morning window light behind` + morning suffix | Real bubble over black bar: "the kitchen faucet won't stop dripping" · caption: **Tuesday, 9:02 AM.** | Single water *plink* — the rhythm element |
| 2 | 4–9 | *(UI moment)* Real capture: draft to **Tony (plumbing)** appears in the same approval queue: "Hi Tony — slow drip at 12 Ossington unit 1B, could you fit it in Thursday AM?" | Caption: **Stoop drafts the text to YOUR plumber. You approve it.** | UI tick; plink continues |
| 3 | 9–14 | `A tradesperson's van door sliding open on a residential street, toolbox lifted out, mid-morning light, friendly working energy, face not focal` + morning suffix | Caption: **Thursday, 8:40 AM. Tony's on it.** | Van door; ambient street |
| 4 | 14–17 | *(UI moment)* Case timeline capture: drip reported → Tony booked → fixed → tenant: "all good now, thanks!!" | Caption: **Reported → booked → fixed. All on the record.** | Three soft ticks ascending; final plink *stops* |
| 5 | 17–20 | *(end card)* **"It knows your plumber."** · "You approve every text." · [domain] | Warm chord |

**Note:** the plink stopping at shot 4 is the joke/payoff — protect it in
the mix.

---

## V7 · "The Paper Trail" — the record (20 s)

Story: disputes are decided on records; Stoop builds yours automatically.
(Tone: composed, not fearful. "LTB" allowed here? **No** — marketing
surface; say "if it ever gets formal.")

| # | s | Generation prompt | Overlay / caption | Audio |
|---|---|---|---|---|
| 1 | 0–5 | `A shoebox of crumpled paper receipts and sticky notes tipping over on a kitchen table next to a phone with cracked screen, chaotic warm light, papers sliding` + morning suffix | Caption: **Your current record-keeping system.** | Paper slide; wry single note |
| 2 | 5–10 | `The same table swept clean, a single smartphone placed down gently in the center, minimal and calm, morning light` + morning suffix | Real case-timeline capture comped above the phone: timestamped entries scrolling slowly | Caption: **Every message. Every reply. Every timestamp. Automatic.** | Clean whoosh; calm pad |
| 3 | 10–15 | *(UI moment)* Real capture: "Export history" → a tidy document preview | Caption: **If it ever gets formal, you're the one with receipts.** | Soft print/export sound |
| 4 | 15–20 | *(end card)* **"The folder you hope you never need — built while you sleep."** · [domain] | Resolve |

---

## V8 · "Before / After" — split-screen (15 s, vertical)

| # | s | Generation prompt | Overlay / caption |
|---|---|---|---|
| 1 | 0–7 | Top half: `A person in bed squinting at a bright phone screen at night typing with one thumb, stressed posture, harsh blue light on the duvet, face shadowed` + night suffix. Bottom half: `A person asleep in the same style of bedroom, phone face-down on the nightstand, total calm, warm dark tones` + night suffix | Top caption: **Without Stoop: you ARE the maintenance line.** Bottom: **With Stoop: the line works nights.** |
| 2 | 7–11 | Top: `the same person still typing, clock now showing later` · Bottom: `the sleeping figure unchanged, gentle moonlight` | Captions: **2:14 AM… 2:31 AM…** / **(still asleep)** |
| 3 | 11–15 | *(end card)* **"Same tenant. Same text. Different night."** · $5/month early access · [domain] |

Audio: top-half typing clicks panned slightly; bottom-half silence; the
contrast is the sound design.

---

## V9 · "Is It an Emergency?" — checker promo (10 s)

| # | s | Source | Overlay / caption |
|---|---|---|---|
| 1 | 0–3 | `Rapid montage style: a dripping tap, a flickering hallway lightbulb, frost on a window — three quick macro shots, domestic, neutral grade` (single gen, 3 cuts) | Caption per cut: **Emergency? · Emergency?? · Emergency???** |
| 2 | 3–7 | Real checker UI capture: three taps → verdict card **First thing tomorrow** | Caption: **30 seconds. Straight answer.** |
| 3 | 7–10 | *(end card)* **"Is it an emergency? Find out before 2 AM does."** · [domain]/is-it-an-emergency |

---

## V10 · "Every Message" — brand mood loop (12 s, no captions version too)

Four 3 s shots, crossfaded, for the site background and pre-roll:

1. `Macro of rain droplets on a dark windowpane, warm interior bokeh behind` + night suffix
2. `A smartphone glowing softly on a nightstand, untouched` + night suffix
3. `Steam rising from a coffee cup in golden morning light, slow` + morning suffix
4. `Hands holding a phone over a kitchen counter, one relaxed thumb tap, morning calm` + morning suffix

Single caption track (optional): **Every message read. · Every reply
yours. · Every night quieter.** → wordmark. Audio: ambient pad only,
loopable.

---

## Production order & measurement

1. **Session 1 (MCP):** V1 + V2 shots (9 generations + retakes) → master
   edits → V3/V4 derived free.
2. **Session 2:** V5, V6, V10 (the site needs these three most).
3. **Session 3:** V7, V8, V9.
- Expect 2–4 takes per shot; judge against each shot's pass/fail note
  before compositing.
- Every placement: UTM → Plausible → waitlist `source`. Test order for
  paid (post-pilot only): V4 → V3 → V8 hooks.
- The two non-AI assets (real screen-recording demo + real founder piece)
  remain on the to-shoot list and are NOT replaced by this library.
