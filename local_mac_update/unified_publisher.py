"""Use the existing Zernio authorization from PeakCut's own posting queue."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse

PRIMARY = ("xqc", "typicalgamer")
CHANNELS = {"jynxzi", "zackrawrr", *PRIMARY}
ACCOUNT_ID = "instagram_zernio"
LIMIT = 100
GAP = 600
CANDIDATE_TTL = 86400
PUBLISHED_TTL = 6 * 3600


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".native-publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def channel(job):
    url = urlparse(str((job.get("source") or {}).get("url") or ""))
    name = url.path.strip("/").lower()
    return name if url.scheme in {"https", "http"} and url.hostname in {"twitch.tv", "www.twitch.tv"} and name in CHANNELS else None


def slot(events, now, cap=LIMIT, gap=GAP):
    times = sorted(t for t in events if isinstance(t, (int, float)) and t > 0)
    due = max(now, times[-1] + gap if times else now)
    recent = [t for t in times if t > due - 86400]
    if len(recent) >= cap:
        due = max(due, recent[-cap] + 86400 + 1)
    return due


class Publisher:
    def __init__(self, root, private, modules):
        self.root, self.private, self.m = Path(root), Path(private), modules
        self.jobs_dir = self.private / "jobs"
        self.target = json.loads((self.private / "instagram.json").read_text())
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", self.target.get("username", "").lstrip("@")) or not isinstance(self.target.get("accountId"), str) or not self.target["accountId"]:
            raise RuntimeError("Unexpected Instagram target; publication disabled.")
        if not (self.private / "zernio-key").is_file():
            raise RuntimeError("The existing Zernio key is missing.")
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def histories(self):
        result = {}
        for path in self.jobs_dir.glob("*.json"):
            row = json.loads(path.read_text())
            if row.get("account_id") == self.target["accountId"]:
                result[path.stem] = row
        return result

    def events(self, include_scheduled=False):
        history = self.histories()
        events = [max(r.get("first_submit_at") or 0, r.get("published_at") or 0) for r in history.values()]
        if include_scheduled:
            for p in self.m.posting._mine():
                if p.get("account") != ACCOUNT_ID or p.get("status") not in self.m.posting.OPEN + ("posted",):
                    continue
                r = history.get(p.get("publication_key"), {})
                if r.get("first_submit_at") or r.get("published_at"):
                    continue
                events.append(p.get("posted_at") or p.get("due_at") or 0)
        return events

    def save_post(self, post, **values):
        with self.m.posting._lock:
            post.update(values)
            self.m.posting._save()
        self.m.posting._mirror(post)
        self.m.ctx.bump(post.get("user"))

    def caption(self, job, clip):
        tags = clip.get("hashtags") or []
        tags = tags.split() if isinstance(tags, str) else tags
        parts = [clip.get("title") or clip.get("hook"), clip.get("description"),
                 " ".join("#" + str(t).lstrip("#") for t in tags)]
        return "\n\n".join(str(p).strip() for p in parts if p)[:2050].rstrip() + "\n\nSource: twitch.tv/" + channel(job)

    def prepare(self, post, job, clip):
        filename = str(clip.get("file") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.mp4", filename):
            raise RuntimeError("Unexpected clip filename.")
        source = Path(job["dir"]) / "clips" / filename
        if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= 300_000_000:
            raise RuntimeError("The finished clip is missing or outside the size limit.")
        sha = digest(source)
        key = hashlib.sha256((self.target["accountId"] + ":" + sha).encode()).hexdigest()
        if post.get("publication_key") and post["publication_key"] != key:
            raise RuntimeError("The clip changed after publication was queued.")
        self.save_post(post, publication_key=key)
        path = self.jobs_dir / (key + ".json")
        state = json.loads(path.read_text()) if path.exists() else {
            "id": str(uuid.uuid4()), "account_id": self.target["accountId"], "video_sha256": sha,
            "file": str(path.with_suffix(".mp4")), "source": str(source), "caption": self.caption(job, clip),
            "idempotency_key": str(uuid.uuid4()), "created_at": time.time(), "status": "queued",
        }
        if state.get("account_id") != self.target["accountId"] or state.get("video_sha256") != sha:
            raise RuntimeError("Publication history does not match this video.")
        if state.get("status") == "published":
            return path, state
        if not state.get("first_submit_at") and not state.get("post_id"):
            made = clip.get("made_at") or job.get("created") or time.time()
            if time.time() - made >= CANDIDATE_TTL:
                return path, dict(state, status="expired")
        if state.get("status") == "queued":
            if shutil.disk_usage(self.private).free < source.stat().st_size + 2_000_000_000:
                raise RuntimeError("Waiting for storage space before preparing a post.")
            output = path.with_suffix(".mp4")
            if Path(state["file"]) != output:
                raise RuntimeError("Unexpected saved upload path.")
            probe = self.root / ".venv/bin/ffprobe"
            metadata = json.loads(subprocess.check_output([str(probe), "-v", "error", "-show_entries", "format=duration",
                                                           "-of", "json", str(source)], timeout=30, text=True))
            if not 3 <= float(metadata["format"]["duration"]) <= 900:
                raise RuntimeError("The clip duration is outside publishing limits.")
            partial = output.with_suffix(".part")
            try:
                shutil.copyfile(source, partial)
                if digest(partial) != sha:
                    raise RuntimeError("The video changed while preparing its upload.")
                partial.replace(output)
            finally:
                partial.unlink(missing_ok=True)
            state.update(status="prepared", native_job=job["id"], native_clip=clip["id"],
                         producer_made_at=clip.get("made_at") or time.time())
            atomic(path, state)
        return path, state

    def post(self, p):
        try:
            job = self.m.jobs.get(p["job"])
            clip = next((c for c in (job or {}).get("clips", []) if c["id"] == p["clip"]), None)
            if not job or not clip or clip.get("status") != "done" or job.get("kind") != "live" or not channel(job):
                self.save_post(p, status="cancelled", error="The selected live clip is no longer available.")
                return
            if not job.get("gta_vi_verified"):
                self.save_post(p, status="cancelled", error="Excluded: this clip was not captured under the GTA VI-only policy.")
                return
            if float(clip.get("end") or 0) > float(job.get("gta_vi_safe_until") or 0):
                self.save_post(p, status="scheduled", due_at=time.time()+30,
                               error="Waiting for GTA VI category confirmation covering the clip.")
                return
            path, state = self.prepare(p, job, clip)
            if state.get("status") == "published":
                self.save_post(p, status="posted", posted_at=state["published_at"], remote_id=state.get("post_id", ""),
                               url=state["permalink"], error=None)
                return
            if state.get("status") == "expired":
                atomic(path, state)
                self.save_post(p, status="cancelled", error="Unposted candidate expired after 24 hours.")
                return
            if state.get("status") in {"failed", "held"}:
                self.save_post(p, status="failed", error="The saved platform outcome needs checking; no new post was sent.")
                return
            now = time.time()
            if (self.private / "PAUSE").exists():
                self.save_post(p, status="scheduled", due_at=now + 60, error="Posting is paused.")
                return
            if not state.get("post_id") and not state.get("first_submit_at"):
                due = slot(self.events(), now)
                if due > now:
                    self.save_post(p, status="scheduled", due_at=due, error=None)
                    return
            if state.get("first_submit_at") and not state.get("post_id") and now - state["first_submit_at"] >= 23 * 3600:
                atomic(path, dict(state, status="held"))
                self.save_post(p, status="failed", error="An older unconfirmed request was held to prevent duplicates.")
                return
            environment = os.environ.copy()
            environment.update(TP_VIDEO=str(Path(job["dir"]) / "clips" / clip["file"]), TP_STATE=str(path),
                               TP_SNAPSHOT=state["file"], TP_EXPECTED_SHA=state["video_sha256"], TP_CAPTION=state["caption"])
            log = self.private / "integrated-publisher.log"
            with log.open("a") as output:
                subprocess.run([sys.executable, "-u", str(self.root / "publisher_client.py")], env=environment,
                               stdout=output, stderr=subprocess.STDOUT, timeout=610)
            state = json.loads(path.read_text())
            if state.get("status") == "published":
                self.save_post(p, status="posted", posted_at=state["published_at"], remote_id=state.get("post_id", ""),
                               url=state["permalink"], error=None)
            elif state.get("status") in {"failed", "held"}:
                self.save_post(p, status="failed", error="Instagram reported a terminal or unconfirmed outcome; see the private log.")
            else:
                self.save_post(p, status="scheduled", due_at=time.time() + 60, error="Saved upload is awaiting confirmation; checking again.")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
            self.save_post(p, status="scheduled", due_at=time.time() + 120,
                           tries=p.get("tries", 0) + 1, error="Publication interrupted; saved progress will be retried.")

    def attach(self):
        m = self.m
        original_post = m.posting._post
        original_slot = m.posting._next_slot
        original_views = m.accounts.instagram_views
        original_start = m.posting.start
        def post(p):
            account = m.accounts.get(p["account"])
            return self.post(p) if account and account.get("backend") == "zernio" else original_post(p)
        def next_slot(aid, gap_min, cap):
            return slot(self.events(True), time.time()) if aid == ACCOUNT_ID else original_slot(aid, gap_min, cap)
        def views(acc, rid):
            return None if acc.get("backend") == "zernio" else original_views(acc, rid)
        def start():
            owner = m.auth.local_user()
            with m.ctx.as_user(owner.id):
                m.accounts._put({"id": ACCOUNT_ID, "platform": "instagram", "name": "@" + self.target["username"].lstrip("@") + " (Zernio)",
                                 "added": time.time(), "backend": "zernio", "remote_account_id": self.target["accountId"],
                                 "detail": "Publishes through your existing Instagram authorization."})
                for creator in m.creators.all_creators():
                    ch = channel({"source": {"url": (creator.get("channels") or {}).get("twitch") or creator.get("channel")}})
                    if ch:
                        m.creators.update(creator["id"], {"accounts": [ACCOUNT_ID], "autopost": True, "auto_watch": False,
                            "post_min_score": 70, "post_gap_min": GAP // 60, "daily_cap": min(50, LIMIT), "live_per_hour": 3, "min_len": 30, "max_len": 60, "post_footer": "Source: twitch.tv/" + ch})
                for p in m.posting._mine():
                    if p.get("account") == ACCOUNT_ID and p.get("status") == "posting":
                        p.update(status="scheduled", due_at=time.time(), error="Resuming saved publication.")
                # Re-plan unsent posts under the new shared policy. Resume known requests immediately.
                pending = sorted((p for p in m.posting._mine() if p.get("account") == ACCOUNT_ID
                                  and p.get("status") == "scheduled"), key=lambda p:p.get("created",0))
                history = self.histories()
                for p in pending:
                    p["due_at"] = 0
                for p in pending:
                    state = history.get(p.get("publication_key"), {})
                    due = time.time() if state.get("first_submit_at") or state.get("post_id") else slot(self.events(True), time.time())
                    self.save_post(p, due_at=due, error=None)
                m.posting._save()
                for job in m.jobs.list_jobs():
                    if job.get("kind") == "live" and channel(job) and job.get("gta_vi_verified"):
                        for clip in job.get("clips", []):
                            if clip.get("status") == "done" and clip.get("judges") and clip.get("score", 0) >= 70 and time.time() - clip.get("made_at", 0) < CANDIDATE_TTL:
                                m.posting.enqueue(job, clip, [ACCOUNT_ID])
            return original_start()
        m.posting._post, m.posting._next_slot, m.accounts.instagram_views, m.posting.start = post, next_slot, views, start
