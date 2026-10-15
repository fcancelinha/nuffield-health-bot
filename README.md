# nuffield-bot

A small asyncio bot that books **any classes** at Nuffield Health gyms
the moment bookings open (8 days ahead, 07:00 UK time). It polls the member
booking API, grabs a matching lunchtime slot as soon as it goes live, optionally
joins the waitlist if the class is already full, and notifies you over WhatsApp.

> Personal-use automation for your own membership. Check Nuffield's website terms
> before running it, and keep the polling gentle (the defaults are deliberately so).

## How it works

- Every tick it queries the bookable classes for the target day and keeps the ones
  whose title matches any entry in `CLASS_NAME_FILTERS` and that start inside your
  time window.
- Around the daily release time it enters a **burst**: it polls much faster (with
  a little jitter) so it reacts within a fraction of a second when a slot appears.
- When a matching class is `AVAILABLE` it books it. If it's full and
  `JOIN_WAITLIST` is on, it joins the waitlist instead.
- `BOOK_ONLY_AFTER_RELEASE` (default **true**) holds all booking until the first
  release time *after startup*. This is what lets you start the container the
  night before without it grabbing whatever happens to be open already.
- Booked class ids are remembered so it never double-books or spams. At startup
  the dedup set is **seeded from places you already hold** (`GET /bookings/`),
  so a container restart does not re-book a class you are already on. Cancelled
  bookings stay in that list and are deliberately excluded, otherwise cancelling
  a class would permanently stop the bot rebooking it.
- The target day is resolved in `TIMEZONE`, not UTC. Deriving it in UTC shifts
  the whole query window by an hour during BST and drops classes either side of
  local midnight.
- `DRY_RUN` (default **true**) detects and notifies but never sends a real booking.
- A `heartbeat` line every 15 minutes shows it is alive and how long until it arms.

### Architecture

Hexagonal (ports and adapters). The domain and service never touch HTTP directly.

```
__main__            entrypoint, logging, signal handling, graceful shutdown
composition         wires settings + adapters into the service
config              pydantic-settings loaded from env / .env
domain/
  models            GymClass, BookingResult, ClassStatus
  ports             ClassRepository, Notifier, TokenProvider, Clock protocols
services/
  booking_service   polling loop, burst window, release gate, time filter,
                    dedup, waitlist, preflight, heartbeat
adapters/
  http_repository   Nuffield booking API (list + book)
  auth              Static / Seeded / Browser token providers (Playwright)
  notifiers         WhatsAppNotifier + LogNotifier fallback
  clock             SystemClock
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium     # needed for AUTO_LOGIN
cp .env.example .env            # then edit .env
```

Run it:

```bash
python -m nuffield_bot          # or: nuffield-bot (after pip install -e .)
```

Tests and lint:

```bash
pip install -e ".[dev]"
pytest -q
ruff check nuffield_bot tests
```

## Authentication

The booking API needs a short-lived Bearer token (scope `booking_api_access`,
issued by Nuffield's IDAAS / Azure AD B2C). **Tokens last one hour**, so a pasted
token is not viable for an unattended overnight run.

### Automatic login (the one that works unattended)

Set `AUTO_LOGIN=true` with `NUFFIELD_EMAIL` / `NUFFIELD_PASSWORD`. A headless
Chromium logs in and lifts the token off the first authenticated API request,
then re-logs in automatically before each expiry.

A background keeper (`keep_fresh`) holds the token at least
`TOKEN_MIN_VALIDITY_SECONDS` clear of expiry, checking every
`TOKEN_CHECK_INTERVAL_SECONDS`. This matters more than it sounds: tokens last
an hour, so a lazy refresh can land *inside* the 07:00 burst and block booking
for the ~6s a login takes. With the keeper, the token entering the burst always
has ≥15 minutes left, and a flaky login gets ~15 retries before it could ever
affect a booking. Reads of a valid token are lock-free, so a refresh in flight
never queues a booking behind it.

Two things about the real B2C page that the implementation depends on:

- The email field is `type="text"`, not `type="email"`. The selectors that work
  are `#email`, `#password`, `#next`.
- Submitting before the page's JS has wired up silently does nothing (it fails
  about half the time). The provider waits for `networkidle` and for the submit
  button to be visible, and re-clicks if it is still on the form.

Do **not** point `POST_LOGIN_URL` at an arbitrary account page: navigating
somewhere else right after submit restarts the OIDC round trip and bounces back
to the login form. The default (`https://my.nuffieldhealth.com/`) is where login
actually lands, and is only re-visited if no API call was observed.

### Pasted token (optional, short-lived)

Set `BEARER_TOKEN` to a token copied from DevTools. With `AUTO_LOGIN=true` it is
used until its `exp` passes and the browser login then takes over. With
`AUTO_LOGIN=false` it is the only credential and the bot stops working after
about an hour.

### The APIM subscription key

The API sits behind Azure API Management. **Every request must carry
`ocp-apim-subscription-key` or it returns `401 Access denied`, no matter how
valid the bearer token is.** `API_SUBSCRIPTION_KEY` holds the bootstrap value;
`BrowserTokenProvider` relearns the current key on each login and the repository
prefers the learned one, so a rotation heals itself.

## Configuration

All settings come from environment variables (or `.env`). See `.env.example`.

| Variable | Default | Description |
| --- | --- | --- |
| `NUFFIELD_EMAIL` | required | Account email |
| `NUFFIELD_PASSWORD` | required | Account password |
| `AUTO_LOGIN` | `false` | Fetch/refresh the token with a headless browser |
| `BEARER_TOKEN` | unset | Optional seed token, used until its `exp` passes |
| `LOGIN_URL` | IDAAS login | Login page URL |
| `POST_LOGIN_URL` | `https://my.nuffieldhealth.com/` | Where login lands |
| `LOGIN_HEADLESS` | `true` | Run the login browser headless |
| `LOGIN_EMAIL_SELECTOR` | `#email` | Email field selector |
| `LOGIN_PASSWORD_SELECTOR` | `#password` | Password field selector |
| `LOGIN_SUBMIT_SELECTOR` | `#next` | Submit button selector |
| `TOKEN_MIN_VALIDITY_SECONDS` | `900` | Keep the token this far from expiry |
| `TOKEN_CHECK_INTERVAL_SECONDS` | `60` | How often the keeper checks |
| `API_SUBSCRIPTION_KEY` | baked in | Azure APIM key (relearned on login) |
| `API_ORIGIN` | booking microsite | `Origin` header sent to the API |
| `LOCATION_ID` | `a2T4J000001JJgMUAW` | Gym facility id (Baltimore Wharf) |
| `CLASS_NAME_FILTERS` | Signature + Foundations | Comma-separated title substrings, ANY match |
| `TARGET_DAYS_AHEAD` | `8` | Day to target (when bookings open) |
| `DRY_RUN` | `true` | Detect + notify only, never book |
| `BOOK_ONLY_AFTER_RELEASE` | `true` | Hold booking until the release after startup |
| `JOIN_WAITLIST` | `true` | Join the waitlist if the class is full |
| `BOOKING_CHECK_INTERVAL_SECONDS` | `600` | How often to look for waitlist promotions |
| `CLASS_TIME_FROM` | `11:00` | Earliest class start (local) |
| `CLASS_TIME_TO` | `14:00` | Latest class start (local) |
| `POLL_INTERVAL_SECONDS` | `5.0` | Normal poll cadence |
| `BURST_INTERVAL_SECONDS` | `0.2` | Poll cadence inside the burst |
| `BURST_JITTER_SECONDS` | `0.15` | Random extra added during burst |
| `BURST_WINDOW_SECONDS` | `45` | Burst half-width around release |
| `RELEASE_HOUR` / `RELEASE_MINUTE` | `7` / `0` | When bookings open (local) |
| `TIMEZONE` | `Europe/London` | Timezone for windows |
| `NOTIFY_WHATSAPP_TOKEN` | unset | WhatsApp Cloud API token |
| `NOTIFY_WHATSAPP_PHONE_NUMBER_ID` | unset | WhatsApp sender phone number id |
| `NOTIFY_WHATSAPP_RECIPIENT` | unset | Recipient number |

If the WhatsApp values are unset, notifications fall back to structured logs.

`CLASS_NAME_FILTERS` is annotated `NoDecode` because pydantic-settings otherwise
JSON-parses list fields straight from the environment and rejects the
comma-separated form.

## Notifications

Uses the WhatsApp Cloud API (Meta Graph). Create a Meta app with WhatsApp, then set
`NOTIFY_WHATSAPP_TOKEN`, `NOTIFY_WHATSAPP_PHONE_NUMBER_ID`, and
`NOTIFY_WHATSAPP_RECIPIENT`.

## Booking API reference

- List: `GET /booking/member/1.0/bookable_items/gym/?location=<id>&from_date=..&to_date=..`
- My bookings: `GET /booking/member/1.0/bookings/?past=false&page=N`
  → `{"count": N, "next": <url|null>, "items": [{"sfid": .., "my_booking":
  {"status": "Booked"|"Waitlist"|"User Cancelled", "waitlist_position": N,
  "heroku_id": ..}}]}`. Note `sfid` here is the **class** id, the same value
  posted as `reservation`.
- Book / join waitlist: `POST /booking/member/1.0/bookings/gym/` with `{"reservation": "<sfid>"}`
  → `{"status": "BOOKED", "heroku_id": "<ref>", "waitlist_position": N, ...}`
- Cancel: `PATCH /booking/member/1.0/bookings/gym/<heroku_id>/cancel/`

Base host: `https://api.nuffieldhealth.com`. Every call sends
`Authorization: Bearer <token>` **and** `ocp-apim-subscription-key`.

Retries are deliberately limited to transient failures (5xx, 429, transport
errors). A 401 is not retried, because each retry would trigger another browser
login; instead `_request` re-authenticates exactly once, and the provider refuses
to log in more than once every 30 seconds.

### Rate limiting: polling competes with booking

APIM throttles with `429 {"message": "Rate limit is exceeded. Try again in 2
seconds."}`. On 2026-10-15 a 0.2s burst interval exhausted the quota **32
seconds before the release**, and the booking POST itself was then refused while
7 spots were still free. The class was gone by the time a request got through.

The lessons are baked in:

- **Polling fast is worthless if it starves the POST.** `BURST_INTERVAL_SECONDS`
  defaults to `1.0`, not `0.2`. Spots took ~30s to go from 7 free to full, so
  1s detection loses nothing.
- **The booking POST is worth fighting for.** `book()` retries for
  `BOOK_RETRY_BUDGET_SECONDS` (25s), honouring the `Retry-After` header or the
  "try again in N seconds" hint in the body. Listing polls still fail fast after
  3 attempts so they never hold up the tick loop.
- A throttled POST must not be swallowed into a `BookingResult` before the retry
  wrapper sees it — that bug turned a recoverable 429 into a lost class.

### Booking response statuses

`BOOKED` and `WAITLIST` are **both successes**. The API returns `WAITLIST` when
the class filled between the listing and the POST. Treating it as a failure
produces a misleading "❌ Failed to book" and leaves the class out of the dedup
set, so the bot keeps retrying something it already joined.

Only `status` says which one you got. **`waitlist_position` is not a signal**:
the API returns `1` for confirmed and cancelled bookings as well, so inferring
"waitlisted" from a non-zero position mislabels real bookings.

### Waitlist promotions

Nuffield appears to send no email when a waitlist place becomes a confirmed
booking (none arrived for the join either). An unnoticed promotion means
turning up to nothing, or worse, a no-show fee for a class you did not know you
had. The bot therefore re-reads `GET /bookings/` every
`BOOKING_CHECK_INTERVAL_SECONDS` and notifies on any status change:

- `Waitlist → Booked` → "🎉 Off the waitlist"
- anything held → not held (cancelled by you or the gym) → "⚠️ no longer held"

The check is skipped entirely during the release burst so it never competes
with a booking for rate-limit quota.

## Docker

```bash
docker compose up -d --build
docker compose logs -f
```

The image bakes in Chromium (`playwright install --with-deps chromium` into
`/ms-playwright`, world-readable so the unprivileged `botuser` can launch it) and
the `tzdata` pip package, which `python:3.12-slim` lacks and `ZoneInfo` needs.
Compose sets `shm_size: 512m`, without which Chromium crashes.

## Running overnight on a Mac

The bot has to be alive at 07:00, which on a laptop means two separate things:
the container must be running, **and the machine must not sleep**. Docker
Desktop suspends every container when macOS sleeps, so a sleeping Mac means no
booking, silently.

Check your current sleep setting:

```bash
pmset -g | grep '^ sleep'
```

Anything other than `0` means the Mac will sleep when idle and the bot will stop.

### Keep it awake for one night

`caffeinate` holds a power assertion for as long as it runs. Start it detached
so it survives closing the terminal, and give it a deadline so it cannot keep
the machine awake forever:

```bash
# 20 hours (72000s): covers an evening start through the 07:00 drop
nohup caffeinate -s -t 72000 >/dev/null 2>&1 &
```

- `-s` prevents *system* sleep (the display may still sleep, which is fine)
- `-t <seconds>` auto-expires, so you cannot forget about it
- `nohup ... &` detaches it from the terminal session

Confirm it is holding, then start the bot:

```bash
pgrep -lf 'caffeinate -s'          # shows the PID if it is running
pmset -g | grep '^ sleep'          # should now list "caffeinate" as preventing sleep
docker compose up -d
```

Stop it early when you no longer need it:

```bash
pkill -f 'caffeinate -s'
```

Note `-s` only guarantees no sleep **on AC power**. On battery, macOS may still
sleep; plug the machine in. Check with `pmset -g batt`.

### A permanent alternative

If this machine is always meant to run the bot, disable idle sleep on AC
instead of caffeinating nightly (needs sudo, and persists across reboots):

```bash
sudo pmset -c sleep 0              # 'c' = connected to charger
sudo pmset -c sleep 1              # undo
```

Also consider enabling "Start Docker Desktop when you sign in" in Docker
settings, otherwise a reboot leaves the bot down until you open Docker by hand.
Check whether it is set:

```bash
osascript -e 'tell application "System Events" to get the name of every login item' | tr ',' '\n' | grep -i docker
```

## Going live checklist

1. Fill `.env` (credentials, `AUTO_LOGIN=true`).
2. Run with `DRY_RUN=true` and confirm it flags the right lunchtime slots.
3. Set `DRY_RUN=false`, start the container the evening before, and check the
   startup line says `Bot up (holding until release)` with `Auth OK`.
4. After the drop: `docker compose logs | grep -E 'booking|slot.found'`.
