"""
Local smoke test — proves /health and /extract work before deploying.

    python smoke_test.py                      # offline checks only (no network)
    python smoke_test.py --live <video-url>   # also resolves a real link

Offline checks exercise the pure logic (format selectors, header merging, error
classification, SSRF guard, schema validation) via FastAPI's TestClient, so they are
deterministic and safe to run in CI. --live additionally hits the real extractor.
"""

from __future__ import annotations

import argparse
import json
import sys

from fastapi.testclient import TestClient

import main
from main import (
    app,
    build_format_selector,
    classify_error,
    merge_headers,
    normalize_netscape_cookies,
)

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((PASS if condition else FAIL, name, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def run_offline() -> None:
    client = TestClient(app)

    # ── /health ───────────────────────────────────────────────────────────────────────
    r = client.get("/health")
    check("/health returns 200", r.status_code == 200, f"got {r.status_code}")
    body = r.json() if r.status_code == 200 else {}
    check("/health status ok", body.get("status") == "ok", json.dumps(body)[:160])
    check(
        "/health reports a real yt-dlp version",
        body.get("ytdlp_version") not in (None, "", "unknown", "unavailable"),
        f"version={body.get('ytdlp_version')}",
    )
    check(
        "server budget is below the client's 8s limit",
        float(body.get("extract_timeout_s", 99)) < 8.0,
        f"EXTRACT_TIMEOUT_S={body.get('extract_timeout_s')}",
    )

    # ── Format selection: must yield a single progressive file ─────────────────────────
    video = build_format_selector("720", audio_only=False)
    check("video selector demands audio+video", "[vcodec!=none][acodec!=none]" in video)
    check("video selector caps height", "height<=720" in video)
    check("video selector prefers plain http over manifests", "[protocol^=http]" in video)
    audio = build_format_selector("mp3", audio_only=True)
    check("audio selector requests bestaudio", audio.startswith("bestaudio"))

    # ── Header merging: format-level wins, Referer synthesised, hop-by-hop dropped ─────
    merged = merge_headers(
        {"http_headers": {"User-Agent": "info-ua", "Accept-Encoding": "gzip"}},
        {"http_headers": {"User-Agent": "format-ua"}},
        "https://www.instagram.com/reel/ABC123/",
    )
    check("format headers override info headers", merged.get("User-Agent") == "format-ua")
    check("Referer synthesised from page URL",
          merged.get("Referer") == "https://www.instagram.com/", str(merged.get("Referer")))
    check("hop-by-hop headers stripped", "Accept-Encoding" not in merged)

    # ── Error classification drives the client's message ──────────────────────────────
    check("login errors -> 422/login_required",
          classify_error("ERROR: Login required to access this post") == (422, "login_required"))
    check("removed media -> 404/not_found",
          classify_error("Video unavailable. This video has been removed") == (404, "not_found"))
    check("unsupported link -> 422/unsupported",
          classify_error("Unsupported URL: https://example.com/x") == (422, "unsupported"))
    check("timeouts -> 504/timeout", classify_error("socket timed out") == (504, "timeout"))
    check(
        "unsatisfiable selector is NOT reported as not_found",
        classify_error("ERROR: Requested format is not available")
        == (422, "no_progressive_format"),
    )

    # ── COOKIES_TXT parsing: every realistic paste corruption must still load ─────────
    canonical = (
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1799999999\tsessionid\tabc123\n"
        ".facebook.com\tTRUE\t/\tTRUE\t1799999999\tc_user\t100000\n"
    )
    content, count = normalize_netscape_cookies(canonical)
    check("canonical cookies.txt parses", count == 2, f"count={count}")
    check("magic header present", content.startswith("# Netscape HTTP Cookie File"))

    # Render's env editor / shell export collapses newlines into literal backslash-n.
    _, count = normalize_netscape_cookies(canonical.replace("\n", "\\n"))
    check("literal \\n escapes recovered", count == 2, f"count={count}")

    # Copy/paste turns the mandatory tab separators into spaces.
    _, count = normalize_netscape_cookies(canonical.replace("\t", "    "))
    check("space-separated fields recovered", count == 2, f"count={count}")

    # Browser exporters emit #HttpOnly_ prefixes; those lines are cookies, not comments.
    content, count = normalize_netscape_cookies(
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1799999999\tsessionid\tabc\n"
    )
    check("#HttpOnly_ lines kept as cookies", count == 1, f"count={count}")
    check("#HttpOnly_ prefix preserved", "#HttpOnly_.instagram.com" in content)

    # Missing magic line + no trailing newline is the most common hand-edited shape.
    _, count = normalize_netscape_cookies(
        ".x.com\tTRUE\t/\tTRUE\t0\tauth_token\tzzz"
    )
    check("missing magic line is synthesised", count == 1, f"count={count}")

    for bad, label in ((" ", "blank"), ("not a cookie file", "garbage")):
        try:
            normalize_netscape_cookies(bad)
            check(f"{label} COOKIES_TXT rejected", False, "no ValueError raised")
        except ValueError:
            check(f"{label} COOKIES_TXT rejected", True)

    check(
        "cookiefile only used once a valid jar is written",
        "cookiefile" not in main.ydl_options("720", False) or main.COOKIES_READY,
    )

    # ── Request validation ────────────────────────────────────────────────────────────
    r = client.post("/extract", json={"url": "notaurl"})
    check("non-http scheme rejected", r.status_code in (400, 422), f"got {r.status_code}")

    r = client.post("/extract", json={})
    check("missing url rejected", r.status_code == 422, f"got {r.status_code}")

    # ── SSRF guard: internal targets must be refused, not fetched ──────────────────────
    for target in ("http://127.0.0.1:8000/admin", "http://169.254.169.254/latest/meta-data/"):
        r = client.post("/extract", json={"url": target})
        check(f"SSRF blocked: {target}", r.status_code == 403,
              f"got {r.status_code} {r.text[:90]}")

    # ── Error envelope shape (the client parses reason_code) ───────────────────────────
    r = client.post("/extract", json={"url": "http://127.0.0.1/x"})
    if r.status_code == 403:
        payload = r.json()
        check("errors carry status+reason_code",
              payload.get("status") == "error" and "reason_code" in payload,
              json.dumps(payload)[:140])

    # ── Auth gate ─────────────────────────────────────────────────────────────────────
    original = main.API_KEY
    try:
        main.API_KEY = "secret-key"
        r = client.post("/extract", json={"url": "https://www.youtube.com/watch?v=x"})
        check("API_KEY set -> unauthenticated call rejected", r.status_code == 401,
              f"got {r.status_code}")
        r = client.post(
            "/extract",
            json={"url": "http://127.0.0.1/x"},
            headers={"X-API-Key": "secret-key"},
        )
        check("API_KEY set -> correct key passes auth", r.status_code != 401,
              f"got {r.status_code}")
    finally:
        main.API_KEY = original


def run_live(url: str) -> None:
    print(f"\n--- live extraction: {url} ---")
    client = TestClient(app)
    r = client.post("/extract", json={"url": url, "quality": "720"})
    check("live /extract returned 200", r.status_code == 200,
          f"got {r.status_code}: {r.text[:220]}")
    if r.status_code != 200:
        return

    data = r.json()
    check("direct_url is absolute http(s)",
          str(data.get("direct_url", "")).startswith("http"),
          str(data.get("direct_url"))[:90])
    check("title present", bool(data.get("title")), str(data.get("title"))[:70])
    check("headers include User-Agent", "User-Agent" in (data.get("headers") or {}))
    check("progressive, non-manifest protocol",
          str(data.get("protocol", "")).startswith("http")
          and "m3u8" not in str(data.get("protocol", "")),
          f"protocol={data.get('protocol')}")
    check("resolved within the client's 8s budget",
          int(data.get("elapsed_ms", 999999)) < 8000,
          f"elapsed_ms={data.get('elapsed_ms')}")
    print(f"    title={data.get('title')!r} ext={data.get('ext')} "
          f"{data.get('width')}x{data.get('height')} via {data.get('extractor')}")


def main_cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", metavar="URL", help="also resolve a real media URL")
    args = ap.parse_args()

    run_offline()
    if args.live:
        run_live(args.live)

    failures = [r for r in _results if r[0] == FAIL]
    total = len(_results)
    print(f"\n{total - len(failures)}/{total} checks passed.")
    if failures:
        print("Failed:")
        for _, name, detail in failures:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
