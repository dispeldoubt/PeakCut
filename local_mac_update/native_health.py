"""Runtime health and resource guards for the local PeakCut service."""
import os
from pathlib import Path
import shutil
import threading
import time

from unified_publisher import LIMIT, GAP

CHANNELS = {"jynxzi", "zackrawrr"}


def session_snapshot(session, sample_rate=16000):
    with session.lock:
        recorded = session.samples / sample_rate
        transcribed = session.t_done
        return {"creator": session.job["creator"], "job_id": session.job["id"],
                "recorded_seconds": round(recorded, 1), "transcribed_seconds": round(transcribed, 1),
                "asr_lag_seconds": round(max(0, recorded - transcribed), 1),
                "buffered_audio_seconds": round(len(session.pending) / (2 * sample_rate), 1),
                "ready_clips": sum(c.get("status") == "done" for c in session.job["clips"]),
                "rendering_clips": sum(c.get("status") in {"queued", "rendering"} for c in session.job["clips"])}


def attach_health(server, live, jobs, load, config):
    @server.app.get("/api/local-health")
    def local_health():
        return {"pid": os.getpid(), "checked_at": time.time(), "load": load.state(),
                "publisher": "zernio-native-queue", "recovery_version": 3, "gta_vi_only": True,
                "published_copy_retention_hours": 6, "new_clip_seconds": [30, 60], "daily_cap": LIMIT, "rolling_hours": 24, "gap_minutes": GAP // 60,
                "live_poll_seconds": 20, "capture_stall_seconds": 120,
                "live_monitor": live.local_watchdog.monitoring(),
                "automatic_discovery": live.local_watchdog.discovery.status(),
                "posting_paused": server.accounts.settings()["paused"] or (live.local_watchdog.discovery.private/'PAUSE').exists(),
                "asr_model": config.ASR_MODEL_NAME, "recording_limit": config.MAX_LIVE_TOTAL,
                "pending_live_tasks": jobs._live_q.qsize(),
                "recordings": [{**session_snapshot(s), **live.local_watchdog.snapshot(s)} for s in live.active_mine()]}
    return local_health


def attach_start_guard(live, load, data_dir):
    original = live.start_session
    def start(creator, url=None):
        if shutil.disk_usage(data_dir).free < 4_000_000_000:
            raise ValueError("Waiting for at least 4 GB free storage before recording.")
        if load.busy():
            raise ValueError("Waiting for processor and memory headroom before recording.")
        return original(creator, url)
    live.start_session = start
    return original
