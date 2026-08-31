"""
Keeps yt-dlp current at runtime.

Platforms break extractors constantly — an instance that boots once and runs for weeks will
silently rot. Two mechanisms cover that:

  1. requirements.txt leaves yt-dlp unpinned, so every Render build/redeploy pulls latest.
  2. This module upgrades the running process's package on an interval, then reloads the
     already-imported module in place so the change takes effect without a restart.

Upgrading is best-effort by design: a failed upgrade must never take the service down, it
just means the currently installed version keeps serving.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import threading
import time

log = logging.getLogger("asr.updater")

#: 0 disables periodic upgrades (rely on redeploys only).
UPDATE_INTERVAL_H = float(os.getenv("YTDLP_UPDATE_INTERVAL_H", "12"))

#: Upgrade once shortly after boot. Render redeploys already install latest, so this mainly
#: helps long-lived instances; the delay keeps it clear of the cold-start critical path.
STARTUP_DELAY_S = float(os.getenv("YTDLP_UPDATE_STARTUP_DELAY_S", "45"))

UPGRADE_TIMEOUT_S = 180

_started = False
_lock = threading.Lock()


def current_ytdlp_version() -> str:
    try:
        import yt_dlp

        return getattr(yt_dlp.version, "__version__", "unknown")
    except Exception:  # pragma: no cover - import failure is fatal elsewhere
        return "unavailable"


def upgrade_ytdlp() -> bool:
    """Runs pip install --upgrade yt-dlp and hot-reloads it. Returns True if the version changed."""
    before = current_ytdlp_version()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             "--disable-pip-version-check", "--no-input", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=UPGRADE_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            log.warning("yt-dlp upgrade failed (rc=%s): %s", proc.returncode,
                        (proc.stderr or "").strip()[:300])
            return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("yt-dlp upgrade could not run: %s", exc)
        return False

    try:
        import yt_dlp
        import yt_dlp.version

        importlib.reload(yt_dlp.version)
        importlib.reload(yt_dlp)
    except Exception as exc:  # pragma: no cover
        log.warning("yt-dlp reload failed, keeping loaded build: %s", exc)
        return False

    after = current_ytdlp_version()
    if after != before:
        log.info("yt-dlp upgraded %s -> %s", before, after)
        return True

    log.info("yt-dlp already current at %s", before)
    return False


def _loop() -> None:
    time.sleep(STARTUP_DELAY_S)
    while True:
        try:
            upgrade_ytdlp()
        except Exception as exc:  # pragma: no cover - must never kill the thread
            log.warning("Updater iteration errored: %s", exc)
        if UPDATE_INTERVAL_H <= 0:
            return
        time.sleep(UPDATE_INTERVAL_H * 3600)


def start_auto_updater() -> None:
    """Starts the background updater exactly once. No-op when UPDATE_INTERVAL_H <= 0."""
    global _started
    with _lock:
        if _started:
            return
        if UPDATE_INTERVAL_H <= 0:
            log.info("Periodic yt-dlp updates disabled (YTDLP_UPDATE_INTERVAL_H=0).")
            _started = True
            return
        threading.Thread(target=_loop, name="ytdlp-updater", daemon=True).start()
        _started = True
        log.info("yt-dlp auto-updater armed (every %.1fh).", UPDATE_INTERVAL_H)
