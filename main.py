"""
ASR Downloader Backend — production-grade media extraction service.

A single POST /extract endpoint resolves any yt-dlp-supported link (Instagram, Facebook,
TikTok, Twitter/X, YouTube Shorts, Reddit, Pinterest, ...) into a *directly playable* media
URL plus the exact HTTP headers required to fetch it.

Design notes that matter in production:

  • Single progressive stream only. The Android client hands the URL to the platform
    DownloadManager, which cannot mux separate audio/video tracks or read an HLS/DASH
    manifest. resolve_format() therefore insists on one self-contained file with both
    codecs and de-prioritises m3u8/dash.

  • Headers are part of the contract. Instagram and Facebook CDNs answer 403 to any fetch
    without a matching Referer/User-Agent, so yt-dlp's per-format http_headers are returned
    to the caller rather than discarded.

  • Server budget < client budget. EXTRACT_TIMEOUT_S (default 6.5s) is deliberately below
    the app's hard 8s limit so the client receives a structured error instead of a severed
    connection.

  • yt-dlp is a moving target. Platforms break extractors weekly, so the package is
    unpinned and a background task upgrades it on a schedule (see updater.py).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from updater import current_ytdlp_version, start_auto_updater

# ─────────────────────────────────────────────────────────────────────────────────────────
# Configuration (all overridable via Render environment variables)
# ─────────────────────────────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

#: Wall-clock ceiling for one extraction. Kept below the client's 8s so the app always
#: receives a JSON error it can render, never a dangling socket.
EXTRACT_TIMEOUT_S = float(os.getenv("EXTRACT_TIMEOUT_S", "6.5"))

#: Netscape-format cookies.txt contents. Required for gated Instagram/Facebook media
#: (private accounts, age-restricted posts). Treat as a SECRET — it is a live session.
COOKIES_TXT = os.getenv("COOKIES_TXT", "").strip()

#: Written under the OS temp dir rather than a hardcoded /tmp so the same code runs on
#: Render (Linux), in Docker, and on a Windows dev box.
COOKIES_PATH = os.path.join(tempfile.gettempdir(), "asr_cookies.txt")

#: Set by write_cookie_jar() once a syntactically valid jar has been persisted. Guards
#: every use of `cookiefile` — a malformed jar makes yt-dlp abort instead of degrade.
COOKIES_READY = False

#: Optional shared secret. When set, requests must present X-API-Key. Left empty by default
#: so a freshly deployed instance works with the shipped APK immediately.
API_KEY = os.getenv("API_KEY", "").strip()

#: Crude per-IP rate limit to keep a public endpoint from being used as a free proxy.
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "30"))
RATE_LIMIT_WINDOW_S = int(os.getenv("RATE_LIMIT_WINDOW_S", "60"))

#: Extraction is blocking C/Python work, so it runs off the event loop.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))

CLIENT_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.6367.179 Mobile Safari/537.36"
)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("asr.extract")

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ytdlp")

#: ip -> list[monotonic timestamps]
_rate_state: Dict[str, List[float]] = {}


# ─────────────────────────────────────────────────────────────────────────────────────────
# Netscape cookie jar (COOKIES_TXT)
# ─────────────────────────────────────────────────────────────────────────────────────────


def normalize_netscape_cookies(raw: str) -> tuple[str, int]:
    """
    Validates and normalises Netscape-format cookie content into what yt-dlp expects.

    Returns (file_content, cookie_count). Raises ValueError when nothing usable is found.

    Why this is not just a file write: COOKIES_TXT arrives through a dashboard env var, and
    every realistic way of pasting it corrupts the format that yt-dlp's parser demands:

      • Render's multi-line editor and shell `export` both turn newlines into literal "\\n".
      • Copy/paste converts tabs — the mandatory field separator — into runs of spaces.
      • The "# Netscape HTTP Cookie File" magic first line is often lost, and yt-dlp
        rejects the whole jar without it.
      • Browser exporters emit "#HttpOnly_" prefixes and a trailing-newline-less last line.

    yt-dlp treats a malformed jar as a fatal error, not a warning, so a silently mangled
    paste would break every request rather than just the gated ones. Each line is therefore
    rebuilt into exactly 7 tab-separated fields.
    """
    text = raw.strip()
    if not text:
        raise ValueError("COOKIES_TXT is empty.")

    # Un-escape literal escape sequences produced by env-var editors and shells.
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    text = text.replace("\\t", "\t").replace("\r\n", "\n").replace("\r", "\n")

    lines_out: List[str] = ["# Netscape HTTP Cookie File"]
    count = 0

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        http_only = False
        if stripped.startswith("#"):
            # "#HttpOnly_domain" is a real cookie line, not a comment. Everything else is.
            if stripped.startswith("#HttpOnly_"):
                http_only = True
                stripped = stripped[len("#HttpOnly_"):]
            else:
                continue

        # Tabs are mandatory but frequently lost in transit; fall back to whitespace.
        fields = stripped.split("\t")
        if len(fields) < 7:
            fields = stripped.split()
        if len(fields) < 7:
            log.warning("Skipping malformed cookie line (%d fields).", len(fields))
            continue

        # A cookie value may legitimately contain spaces, so re-join any overflow fields
        # produced by the whitespace fallback back into the value (field 7).
        domain, include_sub, path, secure, expires, name = fields[:6]
        value = "\t".join(fields[6:]) if len(fields) == 7 else " ".join(fields[6:])

        include_sub = "TRUE" if include_sub.strip().upper() in ("TRUE", "1") else "FALSE"
        secure = "TRUE" if secure.strip().upper() in ("TRUE", "1") else "FALSE"
        if not expires.strip().lstrip("-").isdigit():
            expires = "0"  # session cookie

        if http_only:
            domain = "#HttpOnly_" + domain

        lines_out.append("\t".join([domain, include_sub, path, secure, expires, name, value]))
        count += 1

    if count == 0:
        raise ValueError("COOKIES_TXT contained no parsable cookie lines.")

    return "\n".join(lines_out) + "\n", count


def write_cookie_jar() -> None:
    """Materialises COOKIES_TXT at COOKIES_PATH. Never raises — degrades to no cookies."""
    global COOKIES_READY

    if not COOKIES_TXT:
        return
    try:
        content, count = normalize_netscape_cookies(COOKIES_TXT)
    except ValueError as exc:
        log.error("COOKIES_TXT ignored — %s", exc)
        return

    try:
        with open(COOKIES_PATH, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        try:
            os.chmod(COOKIES_PATH, 0o600)  # no-op semantics on Windows, required on Linux
        except OSError:
            pass
        COOKIES_READY = True
        log.info("Cookie jar ready at %s (%d cookies).", COOKIES_PATH, count)
    except OSError as exc:
        log.error("Could not write cookie jar: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI):
    write_cookie_jar()
    log.info("ASR Downloader Backend up — yt-dlp %s", current_ytdlp_version())
    start_auto_updater()
    yield
    _executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="ASR Downloader Backend",
    version="1.0.0",
    description="yt-dlp powered media resolver for the ASR Guardian Smart Downloader.",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────────────────


class ExtractRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    #: "1080" | "720" | "480" | "mp3". Anything unrecognised falls back to best available.
    quality: str = Field(default="1080", max_length=8)
    #: Explicit audio-only request. Also implied by quality == "mp3".
    audio: bool = False


class ExtractResponse(BaseModel):
    status: str = "ok"
    direct_url: str
    title: str
    ext: str
    is_audio: bool
    headers: Dict[str, str]
    filesize: Optional[int] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    protocol: Optional[str] = None
    extractor: Optional[str] = None
    elapsed_ms: int = 0


# ─────────────────────────────────────────────────────────────────────────────────────────
# Guards: SSRF, auth, rate limiting
# ─────────────────────────────────────────────────────────────────────────────────────────


def _is_private_target(hostname: str) -> bool:
    """
    True when hostname resolves to a loopback/private/link-local address.

    Without this the endpoint is an open SSRF relay: anyone could POST an internal address
    and read Render's metadata service or other services on the private network.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Unresolvable — let yt-dlp fail naturally with a clean network error.
        return False

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def validate_target_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, detail={"status": "error", "reason_code": "bad_scheme",
                                        "error": "Only http/https URLs are accepted."})
    if not parsed.hostname:
        raise HTTPException(400, detail={"status": "error", "reason_code": "bad_url",
                                        "error": "URL has no host."})
    if _is_private_target(parsed.hostname):
        log.warning("Blocked SSRF attempt at %s", parsed.hostname)
        raise HTTPException(403, detail={"status": "error", "reason_code": "blocked_host",
                                        "error": "Target host is not permitted."})
    return raw.strip()


def enforce_api_key(provided: Optional[str]) -> None:
    if API_KEY and provided != API_KEY:
        raise HTTPException(401, detail={"status": "error", "reason_code": "unauthorized",
                                        "error": "Invalid or missing X-API-Key."})


def enforce_rate_limit(client_ip: str) -> None:
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_S
    hits = [t for t in _rate_state.get(client_ip, []) if t >= window_start]
    if len(hits) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(429, detail={"status": "error", "reason_code": "rate_limited",
                                        "error": "Too many requests. Slow down."})
    hits.append(now)
    _rate_state[client_ip] = hits

    if len(_rate_state) > 4096:  # bound memory on a long-lived process
        for ip in [k for k, v in _rate_state.items() if not v or max(v) < window_start]:
            _rate_state.pop(ip, None)


# ─────────────────────────────────────────────────────────────────────────────────────────
# yt-dlp integration
# ─────────────────────────────────────────────────────────────────────────────────────────


def build_format_selector(quality: str, audio_only: bool) -> str:
    """
    Builds a yt-dlp format string that yields ONE self-contained progressive file.

    The Android platform DownloadManager cannot mux DASH tracks or parse an HLS manifest,
    so video selectors require both acodec and vcodec present, and non-manifest protocols
    are preferred. Each '/' step is a strictly looser fallback.
    """
    if audio_only:
        return "/".join([
            "bestaudio[protocol^=http][ext=m4a]",
            "bestaudio[protocol^=http]",
            "bestaudio",
            "best[acodec!=none]",
        ])

    try:
        height = int(quality)
    except (TypeError, ValueError):
        height = 1080

    progressive = "[vcodec!=none][acodec!=none]"
    http_only = "[protocol^=http]"
    return "/".join([
        f"best{progressive}{http_only}[ext=mp4][height<={height}]",
        f"best{progressive}{http_only}[height<={height}]",
        f"best{progressive}[ext=mp4][height<={height}]",
        f"best{progressive}[height<={height}]",
        f"best{progressive}{http_only}",
        f"best{progressive}",
        "best",
    ])


def ydl_options(quality: str, audio_only: bool) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "format": build_format_selector(quality, audio_only),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "nocheckcertificate": True,
        # Playlists/stories can enumerate hundreds of entries; cap the work.
        "playlist_items": "1",
        "extract_flat": False,
        "socket_timeout": max(2.0, EXTRACT_TIMEOUT_S - 1.5),
        # One retry only: the client budget does not allow more.
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "http_headers": {
            "User-Agent": CLIENT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
        # YouTube's `web` player has served DASH-only (separate audio/video) formats for years,
        # so a progressive-file requirement can never be satisfied from it. The android_vr /
        # android clients still expose the muxed itag-18 stream, which is the only YouTube
        # format the platform DownloadManager can actually save. Ordered fastest-first.
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "android", "web"],
                "player_skip": ["configs"],
            }
        },
    }
    if COOKIES_READY and os.path.exists(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH
    return opts


def pick_progressive(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Returns the best single-file format from an already-extracted info dict.

    yt-dlp normally resolves this for us and exposes it at the top level, but some
    extractors (notably Facebook and Instagram carousels) return a bare formats list, so
    we re-select defensively: progressive + non-manifest first, then progressive, then any.
    """
    if info.get("url") and info.get("protocol", "").startswith("http"):
        return info

    formats = info.get("formats") or []
    if not formats:
        return info if info.get("url") else None

    def is_progressive(f: Dict[str, Any]) -> bool:
        return f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")

    def is_plain_http(f: Dict[str, Any]) -> bool:
        return str(f.get("protocol") or "").startswith("http") and "m3u8" not in str(
            f.get("protocol") or ""
        )

    def score(f: Dict[str, Any]) -> tuple:
        return (f.get("height") or 0, f.get("tbr") or 0, f.get("filesize") or 0)

    for predicate in (
        lambda f: is_progressive(f) and is_plain_http(f),
        is_progressive,
        lambda f: bool(f.get("url")),
    ):
        candidates = [f for f in formats if f.get("url") and predicate(f)]
        if candidates:
            return max(candidates, key=score)
    return None


def merge_headers(info: Dict[str, Any], chosen: Dict[str, Any], page_url: str) -> Dict[str, str]:
    """
    Assembles the headers the client MUST replay on the media fetch.

    Format-level http_headers win over info-level ones, and a Referer is synthesised from
    the original page when the extractor did not supply one — the single most common cause
    of a 403 on an otherwise valid Instagram/Facebook CDN URL.
    """
    headers: Dict[str, str] = {}
    headers.update(info.get("http_headers") or {})
    headers.update(chosen.get("http_headers") or {})

    headers.setdefault("User-Agent", CLIENT_UA)
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")

    if "Referer" not in headers:
        parsed = urlparse(page_url)
        if parsed.scheme and parsed.netloc:
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

    # Drop hop-by-hop / connection-management headers: harmful to replay downstream.
    for banned in ("Accept-Encoding", "Connection", "Host", "Content-Length"):
        headers.pop(banned, None)

    return {str(k): str(v) for k, v in headers.items() if v}


def classify_error(message: str) -> tuple[int, str]:
    """Maps a yt-dlp error string onto (http_status, machine-readable reason_code)."""
    low = message.lower()
    if any(s in low for s in ("login required", "log in", "sign in", "private", "cookies")):
        return 422, "login_required"
    # Must precede the not_found check: "Requested format is not available" contains
    # "not available" but means "no progressive stream", not "media is gone".
    if "requested format is not available" in low or "no progressive" in low:
        return 422, "no_progressive_format"
    if any(s in low for s in ("not available", "unavailable", "removed", "deleted", "404")):
        return 404, "not_found"
    if "unsupported url" in low or "no video formats" in low:
        return 422, "unsupported"
    if any(s in low for s in ("timed out", "timeout")):
        return 504, "timeout"
    if any(s in low for s in ("rate", "429", "too many")):
        return 429, "upstream_rate_limited"
    if "geo" in low and "restrict" in low:
        return 451, "geo_restricted"
    return 422, "extraction_failed"


def _extract_info(url: str, quality: str, audio_only: bool, relax: bool) -> Dict[str, Any]:
    opts = ydl_options(quality, audio_only)
    if relax:
        # Second pass: drop the strict selector entirely and let pick_progressive() choose
        # from the full formats list. Some extractors refuse a selector they cannot satisfy
        # even though a usable single-file format is present.
        opts["format"] = "best/bestvideo+bestaudio"
        opts["ignore_no_formats_error"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def run_extraction(url: str, quality: str, audio_only: bool) -> Dict[str, Any]:
    """Blocking yt-dlp call. Executed on the thread pool, never on the event loop."""
    try:
        info = _extract_info(url, quality, audio_only, relax=False)
    except Exception as exc:
        # "Requested format is not available" means the selector was too strict for this
        # site, not that the media is gone — retry once with no selector before failing.
        if "requested format is not available" not in str(exc).lower():
            raise
        log.info("Strict selector unsatisfiable for %s — retrying unrestricted.", url[:120])
        info = _extract_info(url, quality, audio_only, relax=True)

    if not info:
        raise yt_dlp.utils.ExtractorError("Extractor returned no metadata.")

    # Playlist/story URLs collapse to their first entry.
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise yt_dlp.utils.ExtractorError("Playlist contained no playable entries.")
        info = entries[0]

    chosen = pick_progressive(info)
    if not chosen or not chosen.get("url"):
        raise yt_dlp.utils.ExtractorError("No progressive (single-file) format available.")

    ext = chosen.get("ext") or ("m4a" if audio_only else "mp4")
    return {
        "direct_url": chosen["url"],
        "title": (info.get("title") or "ASR_Download").strip()[:120],
        "ext": "mp3" if audio_only and ext in ("m4a", "webm", "opus") else ext,
        "is_audio": audio_only,
        "headers": merge_headers(info, chosen, url),
        "filesize": chosen.get("filesize") or chosen.get("filesize_approx"),
        "duration": info.get("duration"),
        "width": chosen.get("width"),
        "height": chosen.get("height"),
        "protocol": chosen.get("protocol"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
    }


# ─────────────────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> Dict[str, Any]:
    """
    Cheap liveness probe. Does NO yt-dlp work so it stays fast enough to double as the
    client's cold-start warm-up ping and the external keep-alive cron target.
    """
    return {
        "status": "ok",
        "service": "asr-downloader-backend",
        "ytdlp_version": current_ytdlp_version(),
        "cookies_loaded": COOKIES_READY,
        "auth_required": bool(API_KEY),
        "extract_timeout_s": EXTRACT_TIMEOUT_S,
    }


@app.get("/")
async def root() -> Dict[str, str]:
    return {
        "service": "asr-downloader-backend",
        "extract": "POST /extract  {\"url\": \"...\", \"quality\": \"1080\", \"audio\": false}",
        "health": "GET /health",
    }


@app.post("/extract", response_model=ExtractResponse)
async def extract(
    payload: ExtractRequest,
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> ExtractResponse:
    enforce_api_key(x_api_key)
    enforce_rate_limit(request.client.host if request.client else "unknown")

    url = validate_target_url(payload.url)
    audio_only = payload.audio or payload.quality.lower() == "mp3"

    started = time.monotonic()
    loop = asyncio.get_running_loop()

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, run_extraction, url, payload.quality, audio_only),
            timeout=EXTRACT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning("Extraction exceeded %.1fs for %s", EXTRACT_TIMEOUT_S, url[:120])
        raise HTTPException(
            504,
            detail={
                "status": "error",
                "reason_code": "timeout",
                "error": f"Extraction exceeded {EXTRACT_TIMEOUT_S:.1f}s.",
            },
        )
    except Exception as exc:  # yt-dlp raises a wide variety of error types
        message = str(exc)
        status, reason = classify_error(message)
        log.info("Extraction failed (%s) for %s: %s", reason, url[:120], message[:300])
        raise HTTPException(
            status,
            detail={"status": "error", "reason_code": reason, "error": message[:300]},
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "Resolved %s via %s in %dms (%s, %sp)",
        url[:100], result.get("extractor"), elapsed_ms, result.get("protocol"),
        result.get("height"),
    )
    return ExtractResponse(**result, elapsed_ms=elapsed_ms)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """Guarantees every failure is a JSON object the Android client can parse."""
    detail = exc.detail
    if isinstance(detail, dict):
        body = detail
    else:
        body = {"status": "error", "reason_code": "http_error", "error": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=body)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",  # noqa: S104 - container must bind all interfaces
        port=int(os.getenv("PORT", "8000")),
        log_level=LOG_LEVEL.lower(),
    )
