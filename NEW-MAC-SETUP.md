# Moving Repurposer to a new Mac

Two GitHub repos (account `bami-oladipupo`) plus three items moved by hand.

| What | Where it lives |
|---|---|
| App code, tests, launchd jobs, site source | `repurposer` (public), branch `main` |
| Public homepage, privacy, terms, Instagram OAuth relay | `repurposer`, branch `gh-pages` |
| Database, notes, Claude memory | `repurposer-private` (private) |
| `.env`, `tokens/`, `media/` | Not on GitHub. AirDrop or password manager |

The new Mac must use the same username (`bami`) and the same folder
(`/Users/bami/repurpose content`). The launchd plists and `scripts/open-ui.sh` hardcode that path.

## 1. On the old Mac, last thing before switching

Stop the jobs first so both Macs never post at once and the database snapshot is final:

```bash
"/Users/bami/repurpose content/launchd/uninstall.sh"
```

```bash
"/Users/bami/repurpose content/scripts/backup-state.sh"
```

Then AirDrop these three from `~/repurpose content` to the new Mac:

| Item | Why |
|---|---|
| `.env` | Telegram, Instagram app secret, Anthropic key, Cloudflare R2 keys, IV Repost app token and push settings |
| `tokens/` | Google client secret plus the YouTube and Instagram logins |
| `media/` | Videos already downloaded for upcoming slots (about 200 MB) |

## 2. On the new Mac

```bash
brew install gh python@3.12 ffmpeg yt-dlp
```

```bash
gh auth login
```

```bash
gh repo clone bami-oladipupo/repurposer ~/"repurpose content"
```

```bash
gh repo clone bami-oladipupo/repurposer-private ~/repurposer-private
```

```bash
cd ~/"repurpose content" && /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Restore the state (database, notes, Claude memory):

```bash
cd ~/"repurpose content" && mkdir -p data logs .claude ~/.claude/projects/-Users-bami-repurpose-content && cp ~/repurposer-private/data/repurposer.db data/ && cp -R ~/repurposer-private/notes . && cp ~/repurposer-private/claude/launch.json .claude/ && cp -R ~/repurposer-private/claude/memory ~/.claude/projects/-Users-bami-repurpose-content/
```

Drop the AirDropped `.env`, `tokens/` and `media/` into `~/repurpose content`, then check and start:

```bash
cd ~/"repurpose content" && .venv/bin/python -m pytest -q
```

```bash
~/"repurpose content"/launchd/install.sh
```

Open http://127.0.0.1:8080 and confirm Connections shows YouTube and Instagram as connected.
If either shows expired, reconnect from that page.

## 3. Optional: the Desktop launcher

```bash
osacompile -o ~/Desktop/Repurposer.app -e 'do shell script "/bin/zsh \"/Users/bami/repurpose content/scripts/open-ui.sh\""'
```

## 4. The IV Repost iPhone app

The app (source in `~/IV iOS Apps/IVRepost`) talks to this Mac at `http://<Mac name>.local:8080` with the
`APP_TOKEN` from `.env`. On a new Mac check three things:

| Check | Why |
|---|---|
| `WEB_HOST=0.0.0.0` in `.env` | Lets the phone reach the Mac. The web pages still answer only to the Mac itself |
| `APNS_KEY_FILE` points at the push key on this Mac | Push alerts to the phone |
| The Mac's name is unchanged, or update the address in the app's Settings | The app stores the address it was given |

## Afterwards

Run `scripts/backup-state.sh` whenever you want a fresh off-machine copy of the database and notes.
