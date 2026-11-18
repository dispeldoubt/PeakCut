"""Start the local PeakCut trial with Mac resource monitoring."""
import os
import fcntl
from pathlib import Path
from types import SimpleNamespace
import shutil
import sys

import psutil


def cpu_times():
    values = psutil.cpu_times()
    total = sum(values)
    return total - values.idle, total


def fix_capture_quality(root):
    source = root / "studio/live.py"
    text = source.read_text()
    old = "b[height<=720]/bv*[height<=720]+ba/b"
    new = "bv*[vcodec!*=av01][vcodec!*=av1]+ba/b[vcodec!*=av01][vcodec!*=av1]"
    if text.count(old) == 1:
        updated = text.replace(old, new)
        compile(updated, str(source), "exec")
        backup = source.with_name("live.py.before-quality")
        if not backup.exists():
            shutil.copy2(source, backup)
        source.write_text(updated)
    elif text.count(new) != 1:
        raise SystemExit("Unexpected capture settings; startup stopped.")


def main():
    os.umask(0o077)
    if sys.platform != "darwin":
        raise SystemExit("Run this on your Mac.")
    root = Path(__file__).resolve().parent
    private = Path.home() / "Library/Application Support/TwitchPilot"
    service_lock = (private / "native-service.lock").open("a")
    try:
        fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("The unified PeakCut service is already running.")
    os.chdir(root)
    os.environ["PATH"] = str(root / ".venv/bin") + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")
    os.environ.update(
        PEAKCUT_HOST="127.0.0.1", PEAKCUT_PORT="8766",
        PEAKCUT_PUBLIC_URL="http://127.0.0.1:8766", PEAKCUT_ACCOUNTS="0",
        PEAKCUT_JOB_WORKERS="1", PEAKCUT_ASR_THREADS="2", PEAKCUT_ASR_COPIES="1",
        PEAKCUT_MAX_LIVE="2", PEAKCUT_MAX_LIVE_PER_USER="2",
        PEAKCUT_MAX_LIVE_HOURS="0", PEAKCUT_AUTO_RELOAD="0", PEAKCUT_FETCH_MODEL="0",
        PEAKCUT_MIN_FREE_RAM_GB="1.0", PEAKCUT_MIN_FREE_DISK_GB="3",
    )
    fix_capture_quality(root)
    from studio import config, fetch_model, load
    if not fetch_model.ready():
        raise SystemExit("The speech model is missing.")
    load._cpu_times = cpu_times
    load._ram_free_gb = lambda: psutil.virtual_memory().available / 1e9
    load._state.update(
        ram_free=load._ram_free_gb(),
        disk_free=shutil.disk_usage(config.DATA_DIR).free / 1e9,
    )
    if load._state["ram_free"] < load.MIN_FREE_RAM_GB or load._state["disk_free"] < load.MIN_FREE_DISK_GB:
        raise SystemExit("Startup needs at least 1 GB available RAM and 3 GB free disk.")
    from studio import server, accounts, account, auth, creators, ctx, jobs, live, posting, profiles
    from unified_publisher import Publisher, LIMIT, GAP
    from native_maintenance import Maintenance
    from native_watchdog import LiveWatchdog
    from native_discovery import Discovery
    from native_health import attach_health, attach_start_guard
    modules = SimpleNamespace(server=server, accounts=accounts, account=account, auth=auth,
                              creators=creators, ctx=ctx, jobs=jobs, live=live, posting=posting, config=config, profiles=profiles)
    publisher = Publisher(root, private, modules)
    publisher.attach()
    LiveWatchdog(modules, discovery=Discovery(modules, private)).attach()
    attach_health(server, live, jobs, load, config)
    attach_start_guard(live, load, config.DATA_DIR)
    @server.app.on_event("startup")
    def start_maintenance():
        Maintenance(publisher).start()
    print("Local PeakCut: xQc + TypicalGamer priority, automatic GTA VI fallback; saved Instagram connection.", flush=True)
    print(f"Limit: {LIMIT} posts / rolling 24 hours, {GAP // 60}-minute gap, shared across both creators.", flush=True)
    server.main()


if __name__ == "__main__":
    main()
