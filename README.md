# Repurposer

Self-hosted pipeline that watches one TikTok account, downloads each new video without the watermark, and publishes it to YouTube Shorts and Instagram Reels with per-platform captions, on a schedule, with every failure visible. The web UI mirrors the Repurpose.io console (Workflows, Content, Calendar, Connections) so nothing new has to be learned. Runs entirely on this Mac.

Spec: `specs/2026-09-02 TikTok Repurposer Build Spec.md`. Design notes: `.impeccable.md` and `notes/`.

## Status, 3 September 2026

| Piece | State |
|---|---|
| YouTube Shorts | Live. Connected as "Bami \| Tech Careers". API compliance audit approved, so uploads go out public. Two slots a day, 10:00 and 18:00 London. |
| Instagram Reels | Built and tested against the API contract, but not connected. The workflow is inactive. See "Instagram: what is needed now" below. |
| Claude rewriting | On for YouTube. `ANTHROPIC_API_KEY` is set. |
| Telegram alerts | Configured. |
| Web UI | http://127.0.0.1:8080, kept alive by launchd. Only reachable from this Mac. |

## How it works

Two processes share one SQLite database in `data/`.

| Process | What it does | How it runs |
|---|---|---|
| `worker.py` | Poll TikTok, schedule, download, normalise with ffmpeg, rewrite captions, publish, notify | launchd every 15 min |
| `app.py` | Web UI: Workflows, Content, Calendar, Connections, Runs, Account settings | launchd, always on |
| `worker.py --digest` | Weekly Telegram digest | launchd, Monday 08:00 |

The UI only reads and writes the database. Publish now and Get latest content raise a job and spawn the worker, so a UI bug can never double-post. The only platform calls the web app makes are the OAuth handshakes.

Video lifecycle: `new` → `downloaded` → `ready` → `done`, with `held`, `skipped` and `failed` as side exits. Each platform has its own status: `queued` → `scheduled` → `uploaded` (shown as "published"), or `failed` / `skipped` / `cancelled`.

## Install

```bash
cd "/Users/bami/repurpose content"
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
brew install ffmpeg yt-dlp
cp .env.example .env                       # fill in
cp overrides.yaml.example overrides.yaml   # optional
./launchd/install.sh                       # worker every 15 min, web app kept alive, digest on Mondays
```

`config.yaml` holds the TikTok handle, paths and alert method, read on every run. Everything else in it is a default that seeds the database on first run. After that, workflow settings and slots are edited in the UI and the database wins.

After editing `.env` or any Python file, restart the web app so it picks the change up:

```bash
launchctl kickstart -k gui/$(id -u)/com.bami.repurposer.web
```

The worker starts fresh every 15 minutes, so it needs no restart.

## The web UI

Sidebar, top to bottom: **Get latest content** (runs the worker now), Workflows, Content, Calendar, Connections, Runs, Account settings. On a phone the sidebar tucks behind the menu button in the top bar.

- **Import catalogue** (button on Workflows, also under workflow Settings → Source) opens a dialog asking how many videos back to import. Blank means the whole profile; a number takes the newest N as TikTok lists them, pinned videos included. Safe to run again, nothing is duplicated.
- **Workflows.** One row per route, `TikTok ------> YouTube Shorts`. The purple **Auto Publish** switch is the safety catch: off keeps every slot but publishes nothing until you press Publish now. The **Active / Inactive** pill shows whether the worker looks at the route at all; change it from the ⋮ menu. Counts link to the filtered Content list. **View workflow** opens Content for that platform.
- **Content.** The workflow detail page. Header card shows the source and destination accounts, Auto Publish and Settings. Below it the content list: status chips (Scheduled, Queued, Published, Failed, Held, Skipped, Cancelled), search, an origin dropdown (new content or back catalogue), then one row per TikTok with thumbnail, duration, caption, date, status pill and one pink action. Tap the caption or thumbnail to open the editor. Hold and Cancel live in the ⋮ menu on desktop and in the editor's footer on a phone.
- **Editor.** Title and description (or Instagram caption), publish time, Save, Save and publish now, Rewrite with Claude, and the full error text if the last attempt failed.
- **Calendar.** Month, week or day. Published posts green, scheduled blue, failed red. Drag a scheduled post to another day; the time of day is kept. Click any post to edit it.
- **Connections.** A card per account with health, expiry and Reconnect. A red notice appears on every page while a connection is unhealthy. The setup cards below hold the Google client secret drop zone and the Instagram instructions.
- **Runs.** Every worker execution. Rows with problems are tinted and expanded to the full error text; video IDs link back to the Content search.
- **Workflow settings** (from the ⋮ menu or the Settings button on Content). Four tabs, mirroring Repurpose.io: **Publish** (schedule or as-soon-as-possible, extra delay, weekly slots with Add time and Copy Monday), **Content** (new only or plus back catalogue, catalogue start date and order, hold keywords), **Destination** (title and caption templates with click-to-insert placeholders, hashtags, Claude rewriting, visibility, category, made for kids, share to feed), **Source** (the TikTok handle and the Import catalogue button).
- **Account settings.** Timezone, minimum age before a repost, first-run lookback, alert method.
- **overrides.yaml.** `hold`, `skip` or `publish` per TikTok ID plus title and caption replacements. Read on every run.

## YouTube setup (done)

Kept here for a rebuild. Google Cloud project → enable YouTube Data API v3 → OAuth consent screen (External, then **Publish app** so the login does not expire after seven days) → OAuth client of type **Desktop app** → Download JSON → drop it on the Connections page → Connect → sign in with the channel's Google account. Tokens land in `tokens/client_secret.json` and `tokens/youtube.json` and refresh themselves.

New Google projects can only upload as private until YouTube's API compliance audit is approved. Ours was approved on 3 September 2026; `scripts/check_audit.py` uploads a two-second unlisted clip and reads back the status Google kept, if you ever need to re-check.

Quota: 10,000 units a day, 1,600 per upload, so about six uploads a day. The worker logs the estimate every run and moves due videos to the next free slot when the estimate is spent.

## Missed slots and burst limits

On 15 September 2026 five Shorts went out in one run. The Mac had been asleep for two days, launchd does not fire while it sleeps, and when the worker came back every slot since the 12th was still "due". The publisher had no rule against publishing a stale slot, so it cleared the backlog in one go. Three limits in `config.yaml` under `limits` now stop that, and they apply to every automatic run (Publish Now ignores them on purpose):

| Setting | Default | What it does |
|---|---|---|
| `slot_grace_minutes` | 90 | A slot that passed more than this long ago is stale. The video is never published late; it goes back to the queue and takes the next free slot. This also covers a video that was not downloaded in time. |
| `max_publish_per_run` | 1 | Uploads attempted per platform per run. Anything else that is due moves to the next free slot. |
| `max_publish_per_day` | null | Uploads per platform per local day. `null` means the number of slots configured for that weekday, so two slots means at most two uploads a day whatever happens. |

Moved videos show up in the Telegram summary under "Moved to a later slot" with their new time, and in the run's notes on the Runs page. `python worker.py --dry-run` reports how many missed slots it would move without moving them.

The limits cap the damage; they do not make the Mac run the worker while it sleeps. If you want slots hit on time, keep the machine awake (System Settings → Energy → Prevent automatic sleeping when the display is off, or leave `caffeinate -i` running) or accept that missed slots move forward.

## Instagram: what is needed now

Everything in the code is ready: the publisher (`repurposer/publishers/instagram.py`) uses the **Instagram API with Instagram Login**, which needs no Facebook Page, and does a resumable Reels upload that cannot double-post if it crashes mid-way. The web app has the Connect button, the OAuth callback and the health check. What is missing is on Meta's side and in `.env`. As of 3 September: `IG_APP_ID` and `IG_APP_SECRET` are empty, there is no `tokens/instagram.json`, and the Instagram workflow is Inactive.

Do these in order. Steps 1 to 5 are one-off clicks in Instagram and Meta's developer site; step 6 is the only thing on this Mac.

1. **Convert the Instagram account to a professional account.** Instagram app → Settings → Account type and tools → Switch to professional account → **Creator**. The API refuses personal accounts; the health check reports `account type is PERSONAL` if this is skipped. The account stays a normal account in every other way and can be switched back later.

2. **Create a Meta developer app.** https://developers.facebook.com/apps → Create app → use case **"Other"** then type **Business**, or pick the **Instagram** use case if it is offered. Name it Repurposer. This needs a Facebook account for the developer login only; no Facebook Page is linked to the Instagram account.

3. **Add the Instagram product and pick the Instagram Login flow.** In the app dashboard → Add product → **Instagram** → set up → choose **"Instagram API with Instagram Login"** (not "with Facebook Login"). The left menu then shows "API setup with Instagram business login".

4. **Copy the credentials and register the redirect.** On that API setup page:
   - Copy the **Instagram app ID** and **Instagram app secret** (these differ from the Meta app ID at the top of the dashboard; use the Instagram ones).
   - Under "Set up Instagram business login" → Business login settings → **OAuth redirect URIs**, add exactly `http://localhost:8080/oauth/instagram/callback`. Meta accepts a plain http localhost address while the app is in Development mode. If the form rejects it, tell Claude and we will front the app with an HTTPS address via Tailscale instead.

5. **Add the account as a tester.** App dashboard → App roles → Roles → **Add people** → Instagram Tester → enter the handle. Then accept the invite in the Instagram app: Settings → Website permissions (or Apps and websites) → Tester invites → Accept. While the app is in Development mode only testers can log in, which is exactly what we want. No App Review is needed because nobody else will ever use this app.

6. **Fill in `.env` and connect.**
   ```
   IG_APP_ID=<Instagram app ID from step 4>
   IG_APP_SECRET=<Instagram app secret from step 4>
   IG_REDIRECT_URI=http://localhost:8080/oauth/instagram/callback
   ```
   Restart the web app (`launchctl kickstart -k gui/$(id -u)/com.bami.repurposer.web`), open Connections, press **Connect** on the Instagram card, log in with the Instagram account and approve the two permissions (`instagram_business_basic`, `instagram_business_content_publish`). The callback stores a 60-day token in `tokens/instagram.json`; the worker refreshes it once it has under ten days left, so it never needs redoing unless the password changes or the tester role is removed.

7. **Activate the workflow.** Workflows → ⋮ on the Instagram row → **Activate workflow**, and check Auto Publish is set the way you want. Default slots are 11:00 and 19:00 London; change them under Settings → Publish. The caption template and hashtags are under Settings → Destination.

Things to know once it is running:

- **Reels via the API are capped at 90 seconds** (`ig_max_duration_s` in `config.yaml`). Anything between 90 and 180 seconds goes to YouTube only and shows as `skipped` for Instagram with that reason.
- **Publishing limit is 100 posts per 24 hours.** The worker reads the live figure before every run.
- **Share to feed** (Settings → Destination) controls whether the Reel also appears on the profile grid. Default on.
- **Photo carousels and non-vertical videos** are skipped on both platforms.

## Telegram alerts (done)

Bot created with @BotFather; token and chat ID are in `.env`. `python worker.py --test-notify` sends a test message. The weekly digest covers published and scheduled per platform, failures, held videos, connection health, token expiry and disk space.

## Caption rewriting with Claude

Per workflow: Settings → Destination → "Rewrite with Claude before publishing". The worker writes a YouTube title and description (or an Instagram caption) from the TikTok caption using `voice.md`, before the video reaches its slot. The text lands in the editor where you can change it, and the Rewrite with Claude button there regenerates it. Hashtags from the workflow settings are still appended.

- Model and effort are set in `config.yaml` under `rewrite`. Default is Claude Opus 5 at medium effort, roughly a penny per video.
- A failed rewrite never blocks a publish: the template is used, the failure shows in the editor and in the run summary, and the video is not retried automatically.
- Edit `voice.md` to change how the text sounds. It is read on every run.

## Command line

```bash
.venv/bin/python worker.py --status            # what has been seen and where it is
.venv/bin/python worker.py --dry-run           # poll, schedule, download, transform; publish nothing
.venv/bin/python worker.py                     # full cycle, same as the launchd job
.venv/bin/python worker.py --import-catalogue  # bring in the whole back catalogue as existing content
.venv/bin/python worker.py --import-catalogue --limit 50   # only the newest 50 videos on the profile
.venv/bin/python worker.py --republish 7412345678901234567 --platform youtube
.venv/bin/python worker.py --check-connections
.venv/bin/python worker.py --test-notify
.venv/bin/python worker.py --digest
.venv/bin/python app.py                        # web UI in the foreground
.venv/bin/python scripts/check_audit.py        # confirm YouTube honours public uploads
.venv/bin/python -m pytest -q                  # 128 tests
```

`./launchd/uninstall.sh` removes the three launchd jobs. Logs rotate daily under `logs/` and are kept 30 days.

## What gets skipped automatically

| Case | Result |
|---|---|
| Photo carousel post (no video stream) | `skipped` on both platforms at poll time |
| Longer than 180 s or not vertical | `skipped` on both platforms after download |
| Between 90 s and 180 s | YouTube goes ahead, Instagram `skipped` |
| TikTok deleted before its slot | `skipped`, reason "removed from TikTok" |
| Video finished on every active platform | local file deleted at the end of the run, thumbnail kept |
| Caption matches a hold keyword | `held` until released |

Pinned TikToks sit at the top of the profile listing, so the poller checks the newest `poll_max_items` entries (default 30) each run and filters by date itself rather than trusting list order.

## Sponsored content

Anything whose caption matches a hold keyword (`#ad`, `#sponsored`, `#gifted`, `#partner`, `#collab`, `paid partnership`, `paid promotion`, `Kaplan` by default) is inserted as `held` and never leaves without a deliberate Release. Brand deals usually price usage rights per 30-day window, so check the contract before releasing.

## Access from your phone

The app binds to 127.0.0.1, so right now it is only reachable on this Mac. To use it from a phone:

1. **Tailscale** (recommended): install on the Mac and phone, set `WEB_HOST=0.0.0.0` in `.env`, restart the web app, open `http://<mac-tailscale-name>:8080`. Only devices in your tailnet can reach it.
2. **Basic-auth reverse proxy**: keep `WEB_HOST=127.0.0.1` and put Caddy in front with `basicauth`, listening on the LAN.

Never expose port 8080 directly to the internet; there is no login screen.

## Layout

```
worker.py                 CLI and stage orchestration
app.py                    FastAPI web UI
repurposer/
  config.py               config.yaml + .env
  db.py                   SQLite, migrations, seeding
  migrations/*.sql
  actions.py              state transitions shared by worker and UI
  scheduler.py            slot assignment, due lists
  poller.py               yt-dlp listing, catalogue import
  downloader.py           watermark-free download, lazy backfill
  transform.py            ffprobe/ffmpeg rules, thumbnails
  captions.py             templates, hold keywords
  rewrite.py              Claude caption rewriting (voice.md)
  digest.py               weekly digest text
  overrides.py            overrides.yaml
  notify.py               Telegram / email / stdout
  publishers/youtube.py   Data API v3 resumable upload
  publishers/instagram.py Instagram API with Instagram Login, resumable Reels
templates/, static/       Jinja2 + HTMX UI, styled after Repurpose.io
scripts/                  check_audit.py, demo_db.py, open-ui.sh
launchd/                  plists and install script
tests/                    pytest suite
specs/, notes/            build spec, decisions, UI critique
```
