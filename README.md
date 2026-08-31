# ASR Downloader Backend

yt-dlp powered media resolver for the ASR Guardian Smart Downloader. One endpoint takes a
social/video page URL and returns a **directly playable media URL plus the exact HTTP headers
required to fetch it**, so the Android client can hand the URL straight to the platform
`DownloadManager`.

Deployed at `https://asr-downloader-backend.onrender.com` — the URL the shipped APKs call.

## Why it looks like this

- **One progressive file, never a manifest.** Android's `DownloadManager` cannot mux separate
  DASH audio/video tracks or read an HLS playlist. The format selector demands a single file
  with both codecs present and de-prioritises `m3u8`/`dash`.
- **Headers are part of the contract.** Instagram and Facebook CDNs answer `403` to any fetch
  without a matching `Referer`/`User-Agent`, so yt-dlp's per-format `http_headers` are returned
  to the caller instead of discarded.
- **The server budget sits below the client's.** `EXTRACT_TIMEOUT_S` (6.5s) is deliberately
  under the app's hard 8s ceiling, so the client always receives a structured JSON error rather
  than a severed connection.
- **yt-dlp is a moving target.** Platforms break extractors weekly, so the package is unpinned
  and `updater.py` upgrades it in place on a schedule.

## API

### `POST /extract`

```jsonc
// request
{ "url": "https://www.instagram.com/reel/ABC123/", "quality": "1080", "audio": false }
```

`quality` is `"1080" | "720" | "480" | "mp3"` (default `1080`); `"mp3"` implies `audio: true`.

```jsonc
// 200 OK
{
  "status": "ok",
  "direct_url": "https://scontent.cdninstagram.com/...mp4",
  "title": "Some Reel",
  "ext": "mp4",
  "is_audio": false,
  "headers": { "User-Agent": "...", "Referer": "https://www.instagram.com/" },
  "filesize": 4718592,
  "duration": 30.1,
  "width": 1080, "height": 1920,
  "protocol": "https",
  "extractor": "Instagram",
  "elapsed_ms": 1840
}
```

Every failure is a JSON object with the same shape, so the client can branch on `reason_code`:

```jsonc
{ "status": "error", "reason_code": "login_required", "error": "..." }
```

| HTTP | `reason_code` | Meaning |
| ---- | ------------- | ------- |
| 400 | `bad_scheme`, `bad_url` | Not an http(s) URL / no host |
| 401 | `unauthorized` | `API_KEY` is set and `X-API-Key` did not match |
| 403 | `blocked_host` | Target resolves to a private/loopback address (SSRF guard) |
| 404 | `not_found` | Media removed or unavailable |
| 422 | `login_required` | Gated content — set `COOKIES_TXT` (see below) |
| 422 | `unsupported`, `no_progressive_format`, `extraction_failed` | No usable single-file stream |
| 429 | `rate_limited`, `upstream_rate_limited` | Per-IP limit, or the platform throttled us |
| 451 | `geo_restricted` | Blocked in the server's region |
| 504 | `timeout` | Exceeded `EXTRACT_TIMEOUT_S` |

### `GET /health`

Does no yt-dlp work, so it is cheap enough to double as the client's cold-start warm-up ping
and an external keep-alive cron target. Returns the live yt-dlp version, whether a cookie jar
loaded, and the configured timeout.

## Environment variables

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `COOKIES_TXT` | *(unset)* | **Secret.** Netscape `cookies.txt` contents. Unlocks age-gated / private / restricted Facebook and Instagram links. |
| `API_KEY` | *(unset)* | **Secret.** When set, callers must send a matching `X-API-Key`. Leave unset to work with the shipped APKs. |
| `EXTRACT_TIMEOUT_S` | `6.5` | Per-request extraction ceiling. **Must stay below the client's 8s.** |
| `MAX_WORKERS` | `4` | Extraction thread-pool size. |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_S` | `30` / `60` | Per-IP rate limit. |
| `YTDLP_UPDATE_INTERVAL_H` | `12` | Background yt-dlp upgrade interval; `0` disables. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `PORT` | `8000` | Injected by Render. |

## Netscape cookie support (`COOKIES_TXT`)

Age-gated, private, and region-restricted Facebook/Instagram posts are only extractable with a
logged-in session. Export `cookies.txt` from a browser where you are logged in (any
Netscape-format exporter works) and paste the **entire file contents** into the `COOKIES_TXT`
environment variable in the Render dashboard.

On startup the content is normalised and written to a `0600` file that is passed to yt-dlp as
`cookiefile`. The normaliser exists because yt-dlp treats a malformed jar as a **fatal** error
rather than a warning, and every realistic way of pasting a cookie file into a dashboard
corrupts it:

- Render's multi-line editor and shell `export` turn real newlines into literal `\n`.
- Copy/paste converts the mandatory tab separators into runs of spaces.
- The `# Netscape HTTP Cookie File` magic first line is frequently lost.
- Browser exporters emit `#HttpOnly_` prefixes, which are cookies rather than comments.

Each line is rebuilt into exactly 7 tab-separated fields. If nothing parsable is found the jar
is refused, a clear error is logged, and the service continues **without** cookies instead of
failing every request. `GET /health` reports `cookies_loaded` so you can confirm it took.

> A `cookies.txt` is a live logged-in session — treat it exactly like a password. Never commit
> one; `.gitignore` already blocks the usual filenames. Use a throwaway account, not a personal
> one, and rotate by logging that account out (which invalidates the exported session).

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

python smoke_test.py                                     # offline checks, no network
python smoke_test.py --live "https://youtu.be/aqz-KE-bpKQ"   # real extraction

uvicorn main:app --reload --port 8000
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/extract -H "Content-Type: application/json" `
     -d '{"url":"https://youtu.be/aqz-KE-bpKQ","quality":"720"}'
```

`smoke_test.py` covers format selection, header merging, error classification, the SSRF guard,
request validation, the auth gate, and `COOKIES_TXT` parsing — all deterministic and safe for CI.

Docker parity (optional; Render uses the native Python runtime):

```bash
docker build -t asr-downloader-backend .
docker run --rm -p 8000:8000 -e COOKIES_TXT="$(cat cookies.txt)" asr-downloader-backend
```

## Deploy: GitHub → Render

Run these from **this** directory (`ASR Downloader Backend`). The repository is already
initialised with an initial commit.

```powershell
# 1. Create the GitHub repo and push (gh CLI does both in one step)
gh repo create asr-downloader-backend --public --source=. --remote=origin --push

# --- or, without the gh CLI: create the empty repo on github.com first, then ---
git remote add origin https://github.com/<your-username>/asr-downloader-backend.git
git branch -M main
git push -u origin main
```

Then in Render:

1. Dashboard → **New +** → **Blueprint**, and select the repo you just pushed.
   Render reads `render.yaml`, so the service name, build/start commands, health-check path,
   and non-secret env vars are all configured automatically.
2. Service name **must stay** `asr-downloader-backend` — it produces the
   `https://asr-downloader-backend.onrender.com` URL the shipped APKs are hardcoded to call.
   Renaming it breaks them.
3. Set the secrets Render deliberately leaves blank (`sync: false` in `render.yaml`):
   Dashboard → your service → **Environment** → add `COOKIES_TXT` (and `API_KEY` if you want
   the endpoint authenticated). Saving triggers a redeploy.
4. Verify:

```powershell
curl https://asr-downloader-backend.onrender.com/health
curl -X POST https://asr-downloader-backend.onrender.com/extract `
     -H "Content-Type: application/json" `
     -d '{"url":"https://youtu.be/aqz-KE-bpKQ","quality":"720"}'
```

`cookies_loaded: true` in the `/health` response confirms `COOKIES_TXT` parsed. `autoDeploy` is
on, so every subsequent `git push` to `main` redeploys.

### Free-tier cold starts

Render's free plan spins the instance down after ~15 minutes idle, and the next request pays a
30–60s cold start — far beyond the client's 8s budget. The app mitigates this by firing a
fire-and-forget `GET /health` at app start and again on every download gesture, so the instance
is usually already awake by the time `/extract` is called. For a hard guarantee, either upgrade
off the free plan or point an external cron (e.g. cron-job.org) at `/health` every 10 minutes.

## Files

| File | Purpose |
| ---- | ------- |
| `main.py` | FastAPI app: `/extract`, `/health`, cookie normalisation, guards |
| `updater.py` | Background yt-dlp self-upgrade |
| `smoke_test.py` | Offline + live verification |
| `requirements.txt` | Deps (yt-dlp intentionally unpinned) |
| `render.yaml` | Render blueprint |
| `Dockerfile` | Optional container path |
