# TikTok Repurposer: Build Spec

**Owner:** Bamiyo Oladipupo
**Date:** 2026-09-02
**Builder:** Claude Code
**Status:** Ready to build

---

## 1. Current state

1. **No tooling exists.** Cross-posting from TikTok to YouTube Shorts and Instagram Reels is manual: download, re-upload, retype caption, three times a week or more.
2. **Repurpose.io is the reference product.** It watches a TikTok profile, strips the watermark, pushes to YouTube and Instagram with a caption template, drip-feeds the existing back catalogue on a time-slot schedule, and exposes all of this through a workflows dashboard, a per-workflow content queue, and a calendar. That is the feature set in scope. Multi-account, aspect ratio conversion, snippet clipping from long-form, and the template designer are out of scope.
3. **Single creator, single account per platform.** No multi-tenant design needed.

## 2. Problem statement

Build a self-hosted pipeline that detects new videos on one TikTok account, downloads them without watermark, and publishes each one to YouTube Shorts and Instagram Reels with a per-platform caption, on a schedule, with every failure visible.

## 3. Scope

| In scope | Out of scope |
|---|---|
| One TikTok source account | Multiple sources or destinations |
| YouTube Shorts and Instagram Reels destinations | Facebook, X, LinkedIn, Threads |
| Watermark-free download | Aspect ratio conversion (all three are 9:16) |
| Caption mapping and hashtag rules per platform | Caption rewriting by LLM (phase 2, see open questions) |
| Per-video overrides (title, caption, skip, hold) | Snippet clipping from long-form video |
| Existing content backfill on a time-slot schedule | Multi-user accounts, billing, affiliate features |
| Web UI modelled on Repurpose.io's layout (workflows, connections, content queue, calendar) | Pixel-level clone of Repurpose.io branding, logo, or colour scheme |
| Idempotent runs, retry, failure alerts | Analytics |

## 4. Architecture

Two processes sharing one SQLite database: a scheduled worker and a small web app.

```
cron (every 15 min)
  └─ worker.py
       ├─ 1 Poller      : list recent TikTok videos, diff against DB
       ├─ 2 Downloader  : yt-dlp, watermark-free MP4
       ├─ 3 Transformer : ffmpeg normalise (H.264, AAC, 9:16 check, duration check)
       ├─ 4 Scheduler   : assign queued videos to the next free time slot per platform
       ├─ 5 Publishers  : youtube.py, instagram.py (publish anything whose slot has arrived)
       └─ 6 Notifier    : summary of successes and failures per run

uvicorn app.py (always on, port 8080)
  └─ Web UI: Workflows, Connections, Content, Calendar, Settings
```

**Stack:** Python 3.12, yt-dlp, ffmpeg, google-api-python-client, requests, SQLite. Web UI: FastAPI, Jinja2 templates, HTMX for partial updates, Tailwind via CDN. No JavaScript build step, no separate frontend repo. The UI only reads and writes the database; it never calls platform APIs directly, so a UI bug cannot double-post.

**Hosting:** any always-on Linux box. Options in order of preference: existing VPS, Raspberry Pi at home, GitHub Actions on a 30 minute schedule (fine for the poller, but the 6 hour job limit and cold starts make retries fiddlier).

## 5. Data model

Single table `videos`. One row per TikTok video, ever seen.

| Column | Type | Notes |
|---|---|---|
| tiktok_id | text PK | From yt-dlp metadata |
| tiktok_url | text | |
| tiktok_caption | text | Raw caption as posted |
| published_at | datetime | TikTok post time |
| local_path | text | Path after download, null until downloaded |
| duration_s | real | From ffprobe |
| origin | text | `new` (detected after workflow creation) or `existing` (backfilled from the catalogue) |
| status | text | `new`, `downloaded`, `ready`, `held`, `skipped`, `done`, `failed` |
| yt_status | text | `queued`, `scheduled`, `uploaded`, `failed`, `skipped`, `cancelled` |
| yt_scheduled_for | datetime | Slot assigned by the scheduler, null if mode is ASAP |
| yt_video_id | text | |
| yt_error | text | Last error message, cleared on success |
| ig_status | text | Same values as yt_status |
| ig_scheduled_for | datetime | |
| ig_container_id | text | |
| ig_media_id | text | |
| ig_error | text | |
| attempts | int | Increment per run touching this row |
| first_seen | datetime | |
| last_attempt | datetime | |

Second table `runs`: run_id, started, finished, videos_seen, videos_published, failures, log_path. One row per cron execution.

Third table `connections`: platform, account_name, account_id, token_expires_at, last_checked, healthy (bool), last_error. One row per connected platform; drives the Connections page.

Fourth table `slots`: platform, weekday (0 to 6), local_time. The auto publish schedule. Up to five slots per platform per day, minimum two hours apart, matching Repurpose.io's own limits so behaviour feels familiar.

## 6. Configuration

`config.yaml`, committed without secrets. Secrets in `.env`, never committed. Every value below is a placeholder or a sensible default; the builder must not hardcode any handle, account ID, or hashtag anywhere in Python. Account names and IDs for YouTube and Instagram are captured during the OAuth flows and stored in the `connections` table, never in config.

```yaml
source:
  tiktok_handle: "@YOUR_TIKTOK_HANDLE"   # replace before first run
  lookback_days: 7            # first run only pulls this window
  min_age_minutes: 60         # do not repost until TikTok post is this old

youtube:
  enabled: true
  privacy: "public"           # will be forced private until Google audit passes
  title_template: "{caption_first_line}"
  description_template: "{caption}\n\n{yt_hashtags}"
  hashtags: ["#Shorts"]           # add your own; #Shorts keeps it on the Shorts shelf
  category_id: "27"           # Education
  made_for_kids: false

instagram:
  enabled: true
  caption_template: "{caption}\n.\n.\n{ig_hashtags}"
  hashtags: []                    # add your own
  share_to_feed: true

publish:
  mode: "schedule"            # asap | schedule | manual
  timezone: "Europe/London"
  # slots live in the DB and are edited in the UI; these seed the first run
  default_slots:
    youtube:   ["10:00", "18:00"]
    instagram: ["11:00", "19:00"]

existing_content:
  enabled: true
  start_from: "2026-09-08"    # first slot date for backfill; new content always takes priority
  order: "newest_first"       # newest_first | oldest_first
  include_before: null        # ISO date; null means the whole catalogue
  exclude_if_caption_contains: ["#ad", "#sponsored", "paid partnership"]

overrides_file: "overrides.yaml"

notify:
  method: "telegram"          # or "email" or "stdout"
```

`overrides.yaml` is keyed by tiktok_id and lets Bami hold, skip, or replace captions for specific videos without touching code:

```yaml
7412345678901234567:
  action: hold                # hold | skip | publish
  yt_title: "Custom title"
  ig_caption: "Custom caption"
```

## 7. Build order

### Step 1: Skeleton and database
1. Repo layout: `run.py`, `db.py`, `poller.py`, `downloader.py`, `transform.py`, `publishers/youtube.py`, `publishers/instagram.py`, `notify.py`, `config.yaml`, `.env.example`, `README.md`.
2. `db.py` creates both tables on first run. Migrations via plain numbered SQL files.
3. `run.py` wires the stages, wraps each in try/except, and writes a `runs` row whether or not anything succeeded. Exit code non-zero if any stage failed.

### Step 2: Poller and downloader
1. Use yt-dlp's Python API against the profile URL with `--dateafter` derived from `lookback_days` on first run, then from the newest `published_at` in the DB thereafter.
2. Insert unseen IDs as `new`. Never re-insert.
3. Download with format selection that prefers the watermark-free source. Store under `media/{tiktok_id}.mp4`. Record `local_path`, set status `downloaded`.
4. Respect `min_age_minutes` so a TikTok that Bami deletes within the hour never gets cross-posted.

### Step 3: Transformer
1. ffprobe: capture duration, resolution, codec.
2. Re-encode only if not already H.264 video with AAC audio in an MP4 container. Otherwise pass through untouched.
3. Hard rules: reject (status `skipped`, reason logged) if duration over 180 seconds or aspect ratio not 9:16 within tolerance. Instagram Reels via API and YouTube Shorts both need vertical; Reels published through the API are capped at 90 seconds, so set `ig_status = skipped` with reason for anything between 90 and 180 seconds rather than failing the whole video.
4. Set status `ready`.

### Step 4: YouTube publisher
1. OAuth 2.0 desktop flow, scope `youtube.upload`. Token stored in `tokens/youtube.json`, refreshed automatically.
2. `videos.insert` with resumable upload. Snippet from templates. `#Shorts` in title or description so it lands in the Shorts shelf.
3. Quota: default allocation is 100 `videos.insert` calls per day, well above need. Log remaining quota estimate per run.
4. On success record `yt_video_id`, status `uploaded`. On failure record the full API error body in `yt_error`, do not retry within the same run.
5. Known constraint: until the Google Cloud project passes YouTube's API compliance audit, every upload is locked to private regardless of the privacy setting. Build proceeds anyway; the audit request is a setup task (section 9).

### Step 5: Instagram publisher
1. Instagram API with Instagram Login (no Facebook Page dependency). Long-lived token, 60 day expiry, refresh in the notifier if under 10 days remaining and alert if refresh fails.
2. Use the resumable upload protocol: create container with `media_type=REELS`, `upload_type=resumable`, then upload the local file to the `rupload` endpoint. This removes the need to host the video at a public URL. Fallback if resumable proves unreliable: push the file to a Supabase Storage bucket and pass `video_url`.
3. Poll `GET /{container_id}?fields=status_code` every 15 seconds, up to 10 minutes, until `FINISHED`. `ERROR` or `EXPIRED` sets `ig_status = failed` with the status detail captured.
4. `POST /{ig_user_id}/media_publish`. Record `ig_media_id`.
5. Check `GET /{ig_user_id}/content_publishing_limit` at the start of each run and skip Instagram publishing for the run if the quota is exhausted, logging that clearly.

### Step 6: Notifier and retry
1. End of every run: one message listing videos published (with platform links), videos held, and every failure with its error text. Silent runs with nothing new send nothing; runs with failures always send.
2. Retry policy: failed publishes are retried on the next three runs, then marked `failed` permanently and included in every subsequent summary under "needs manual attention" until Bami sets an override.
3. Token expiry, yt-dlp extraction failures, and disk space under 2 GB are alert conditions in their own right.

### Step 7: CLI
`python worker.py` (full cycle), `python worker.py --dry-run` (poll and download, no publish), `python worker.py --republish <tiktok_id> --platform ig`, `python worker.py --status` (table of last 20 videos and their platform statuses).

### Step 8: Existing content backfill
1. `python worker.py --import-catalogue` walks the entire TikTok profile with yt-dlp (metadata only, no download) and inserts every video as `origin = existing`, status `new`. Expect a few hundred rows; this is a one-off that can take several minutes and must show progress on stdout.
2. Apply `exclude_if_caption_contains` at import: matching videos are inserted as `held` with the matching keyword recorded as the reason, so sponsored content never leaves the building without a deliberate release.
3. Downloading is lazy. The worker only downloads a backfill video when it is within 24 hours of its assigned slot, so disk use stays flat and a deleted TikTok is caught before it is reposted.
4. Scheduler rule: on each run, for each platform, fill empty future slots from `start_from` onwards. New content (`origin = new`) claims the earliest free slot first; existing content fills what remains in the configured order. A video is never assigned two slots on the same platform.
5. Duration and format rules from Step 3 apply identically; a backfill video over 90 seconds gets YouTube only.
6. Pausing: the Auto Publish toggle on the workflow (section 7, Step 9) sets mode to `manual`. Scheduled videos keep their slots but nothing publishes until it is switched back. Cancelling a single video sets `cancelled`; the UI offers "re-add to schedule", which returns it to `queued`.
7. Instagram's publishing limit is checked before every publish. If it would be exceeded, the slot rolls to the next day and the summary says why.

### Step 9: Web UI
Model the information architecture and page flow on Repurpose.io so the tool is instantly familiar, without copying its branding, logo, or visual identity. Use a neutral, minimal look: left sidebar, white content area, one accent colour, system font.

**Pages**

1. **Workflows.** Card per workflow (this build ships two: TikTok to YouTube Shorts, TikTok to Instagram Reels). Each card shows source and destination icons, an Auto Publish toggle (Manual / Auto), next scheduled post, counts for queued, published, failed, and a gear icon opening Settings. Buttons: View Content, Run Now.
2. **Workflow Settings (modal).** Auto Publish Mode (As Soon As Possible / On a Schedule), Content Scope (New only / New and Existing), Existing content start date and order, the weekly slot grid (add up to five slots per day, enforce the two-hour gap in the UI and again in the API), title and caption templates, hashtag list, exclusion keywords.
3. **Content.** Per-workflow table of videos: thumbnail, TikTok caption, posted date, origin (New / Existing), platform status pill, scheduled time. Row actions: Publish Now, Edit (opens the publish modal), Cancel, Re-add to schedule, Hold. Filters by status and origin. The publish modal shows the video preview, editable title and caption pre-filled from the template, and a Publish Now or Save button.
4. **Calendar.** Month, week, and day views. Each entry shows a platform icon and thumbnail, published items in a solid style and scheduled items outlined. Click opens the publish modal for scheduled items and the live post URL for published items. Drag to another day reassigns the slot and is confirmed with a toast.
5. **Connections.** One row per platform: account name, connection health, token expiry countdown, Reconnect button that launches the OAuth flow. Red banner across every page if any connection is unhealthy.
6. **Runs (log).** Table of worker runs with counts and a link to the log file. Failures expand to show the full error text.

**Behaviour rules**

1. Every write from the UI goes through the same functions the worker uses (`db.py` and a thin `actions.py`), so state transitions are identical whichever side triggers them.
2. Publish Now from the UI enqueues an immediate job and returns straight away; the row updates via HTMX polling every five seconds until it reaches a terminal state. The UI never blocks on an upload.
3. No login screen. The app binds to localhost or sits behind Tailscale or a basic-auth reverse proxy. Document both options in the README.
4. Mobile layout must work; Bami will approve or hold posts from his phone.

## 8. Error handling requirements

1. **Nothing fails silently.** Every except block logs the exception with traceback and writes the message to the relevant `*_error` column. A bare `except: pass` fails code review.
2. **Platforms are independent.** A YouTube failure never blocks Instagram for the same video, and vice versa.
3. **Idempotency.** Re-running after a crash never double-posts. Publishers check the status column before acting, and the `media_publish` step is only called when a container ID exists and no `ig_media_id` exists.
4. **Logs** rotate daily under `logs/`, kept 30 days.

## 9. Setup tasks (Bami, before the publishers can be tested)

| Platform | Task | Effort |
|---|---|---|
| YouTube | Create Google Cloud project, enable YouTube Data API v3, create OAuth desktop client, download `client_secret.json` | 20 min |
| YouTube | Submit the API compliance audit form so uploads are not locked to private. Expect two to four weeks. Uploads work as private in the meantime | 30 min plus wait |
| Instagram | Convert account to Professional (Creator) if not already | 5 min |
| Instagram | Create Meta developer app, add Instagram product, add Bami's account as an Instagram Tester, generate a long-lived token with content publish permission | 60 to 90 min |
| Instagram | For public use beyond test mode, submit App Review for `instagram_business_content_publish`. Not required while the only user is the app owner in test mode | 30 min if needed |
| Hosting | Provision the box, install Python 3.12, ffmpeg, yt-dlp; add cron entry | 30 min |
| Notify | Create a Telegram bot and get chat ID, or supply an SMTP login | 10 min |

## 10. Acceptance criteria

1. A new TikTok posted at time T is live on YouTube (private or public) and Instagram within T plus `min_age_minutes` plus one polling interval, with no manual step.
2. Running `run.py` five times in a row against an unchanged TikTok profile produces zero new posts and zero errors.
3. Killing the process mid-upload and re-running produces exactly one post per platform for that video.
4. A video over 90 seconds publishes to YouTube and is recorded as `ig_status = skipped` with a reason, and the summary says so.
5. An entry in `overrides.yaml` with `action: hold` is never published until the entry is changed; `action: skip` is never published at all.
6. Revoking the Instagram token causes the next run to send an alert naming the token as the cause, with no other side effects.
7. `--status` shows every video seen in the last 30 days with both platform statuses and any error text.
8. The repo contains no secrets; `.env.example` lists every required variable with a comment.
9. README covers install, the setup tasks in section 9, and how to add a new hashtag set without editing Python.
10. `--import-catalogue` on a profile with 300 videos completes, shows progress, and produces 300 rows with sponsored ones marked `held`. Running it again adds zero rows.
11. With two YouTube slots and two Instagram slots per day and Auto on, exactly four posts go out on a day with no new TikToks, filled from existing content in the configured order, and a new TikTok posted that morning takes the next free slot ahead of the backlog.
12. Switching the toggle to Manual stops all publishing within one worker cycle; switching back resumes without any slot being lost or duplicated.
13. Cancelling a video in the Content page and re-adding it puts it back in the queue at the next free slot, not its original one.
14. The Calendar shows the same scheduled items as the Content page; dragging one to a new day updates `*_scheduled_for` and the next worker run honours it.
15. The Connections page turns red within one worker cycle of a token being revoked, and the Reconnect flow restores it without a restart.

## 11. Open questions for the builder

1. **Hosting choice.** Confirm whether an existing VPS is available or whether to design for GitHub Actions. Affects how state (SQLite, tokens) is persisted between runs.
2. **Caption rewriting.** Phase 2 could call the Claude API to rewrite the TikTok caption into a YouTube title and description in Bami's voice. Build the template hook now so this slots in; do not implement it yet.
3. **Instagram login route.** Instagram Login is the preferred route (no Facebook Page). If the account is already linked to a Page, Facebook Login works too. Builder to check which permissions the existing account setup grants before choosing.
4. **Scheduling delay.** Should cross-posts go out immediately after `min_age_minutes`, or should YouTube and Instagram be staggered (for example Instagram same day, YouTube next morning) for reach? Default to immediate unless told otherwise.
5. **Brand deal content.** Sponsored TikToks may have usage restrictions that forbid reposting. The spec defaults to keyword-based holds at import (`#ad`, `#sponsored`, `paid partnership`). Confirm the keyword list and whether any past deals need additional terms.
6. **Backfill pacing.** Two slots per platform per day clears a 300-video catalogue in about five months. Confirm whether that pace is right or whether to open with more slots and taper.
7. **Old captions.** Backfilled captions may reference things that are stale (dates, offers, events). Decide whether to bulk-review them in the Content page before enabling Auto, or trust the exclusion keywords and fix as you go.
8. **UI reference.** If Bami wants the layout closer to Repurpose.io than the page descriptions above, he should share screenshots of the Workflows, Content, and Calendar pages so the builder can match spacing and hierarchy. Branding, logo, and colour palette stay original regardless.
