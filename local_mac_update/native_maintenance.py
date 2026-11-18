"""Retention and live-storage safeguards, limited to generated clips."""
from pathlib import Path
import shutil
import threading
import time

from unified_publisher import ACCOUNT_ID, CANDIDATE_TTL, PUBLISHED_TTL, atomic, channel, digest


def expired(clip, rows, now):
    if clip.get('status') in {'queued', 'rendering'} or not clip.get('judges'):
        return False
    if any(r.get('status') not in {'published', 'failed', 'expired'} and
           (r.get('first_submit_at') or r.get('post_id')) for r in rows):
        return False
    published = [r.get('published_at', 0) for r in rows if r.get('status') == 'published']
    if published:
        return now - max(published) >= PUBLISHED_TTL
    return now - clip.get('made_at', now) >= CANDIDATE_TTL


class Maintenance:
    def __init__(self, publisher):
        self.p, self.m = publisher, publisher.m

    def cleanup(self):
        m, p, now = self.m, self.p, time.time()
        history = p.histories()
        posts = m.posting._mine()
        for job in m.jobs.list_jobs():
            if job.get('kind') != 'live' or not channel(job):
                continue
            directory = Path(job['dir']).resolve() / 'clips'
            for clip in list(job.get('clips', [])):
                related = [q for q in posts if q.get('job') == job['id'] and q.get('clip') == clip['id']]
                # Queue owns a clip throughout preparation, upload, and confirmation.
                with m.posting._lock:
                    if any(q.get('status') == 'posting' for q in related):
                        continue
                    keys = {q.get('publication_key') for q in related if q.get('publication_key')}
                    rows = [r for key, r in history.items() if key in keys or
                            (r.get('native_job') == job['id'] and r.get('native_clip') == clip['id'])]
                    video = directory / Path(str(clip.get('file') or '')).name
                    if not video.is_file() or video.is_symlink():
                        continue
                    if not expired(clip, rows, now):
                        continue
                    # Hash only clips ready for removal, then re-check all matching legacy outcomes.
                    sha = digest(video)
                    rows = [r for r in history.values() if r.get('video_sha256') == sha]
                    if not expired(clip, rows, now):
                        continue
                    for q in related:
                        if q.get('status') == 'scheduled':
                            p.save_post(q, status='cancelled', error='Candidate expired after 24 hours.')
                    # The user selected 24h unposted / 6h published retention.
                    if m.account.delete_clip(job['id'], clip['id']):
                        for extra in (directory / (clip['id'] + suffix)
                                      for suffix in ('.src.ts', '.src.wav', '.ass', '.json', '.txt')):
                            if not extra.is_symlink() and extra.parent.resolve() == directory:
                                extra.unlink(missing_ok=True)
                        for key, row in history.items():
                            if row.get('video_sha256') == sha:
                                self.remove_copies(key, row)
            # Empty ended sessions contain no useful video; retain post ledgers separately.
            current = m.jobs.get(job['id'])
            if (current and not current.get('clips') and not m.account._busy(current)
                    and now - current.get('created', now) >= CANDIDATE_TTL):
                m.jobs.delete(job['id'])
        # Retire generated copies left by the old downloader, retaining every JSON ledger.
        ready = Path.home() / 'Projects/twitch-pilot/ready'
        for key, row in history.items():
            source = Path(row.get('source') or '/')
            if source.parent != ready or not source.name.startswith('peakcut-'):
                continue
            if row.get('status') == 'published':
                due = now - row.get('published_at', now) >= PUBLISHED_TTL
            else:
                due = (not row.get('first_submit_at') and not row.get('post_id')
                       and now - row.get('created_at', now) >= CANDIDATE_TTL)
            if due:
                self.remove_copies(key, row)

    def remove_copies(self, key, row):
        # Unconfirmed network requests are never discarded by expiry.
        if row.get('status') not in {'published', 'failed', 'expired'}:
            if row.get('first_submit_at') or row.get('post_id'):
                return
            row = dict(row, status='expired')
            atomic(self.p.jobs_dir / (key + '.json'), row)
        snapshot = self.p.jobs_dir / (key + '.mp4')
        if snapshot.is_file() and not snapshot.is_symlink() and digest(snapshot) == row['video_sha256']:
            snapshot.unlink()
        ready = Path.home() / 'Projects/twitch-pilot/ready'
        source = Path(row.get('source') or '/')
        if (source.parent == ready and source.name.startswith('peakcut-') and source.suffix == '.mp4'
                and source.is_file() and not source.is_symlink() and digest(source) == row['video_sha256']):
            source.unlink()
            caption = source.with_suffix('.txt')
            if not caption.is_symlink():
                caption.unlink(missing_ok=True)

    def tick(self):
        m, p = self.m, self.p
        with m.ctx.as_user(m.auth.local_user().id):
            if shutil.disk_usage(m.config.DATA_DIR).free < 3_000_000_000:
                for session in m.live.active_mine():
                    if channel(session.job):
                        session.stop('Storage guard: fewer than 3 GB free; auto-record will retry after cleanup.')
            update = p.private / 'LIVE_UPDATE'
            if update.exists() and time.time() - update.stat().st_mtime < 600:
                return
            self.cleanup()
        for name in ('integrated-publisher.log', 'native-peakcut.log'):
            log = p.private / name
            if log.is_file() and log.stat().st_size > 4_000_000:
                with log.open('rb') as source:
                    source.seek(-1_000_000, 2)
                    tail = source.read()
                with log.open('wb') as output:
                    output.write(tail)

    def start(self):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception:
                    print('Retention check interrupted; retaining files and retrying in 30 seconds.', flush=True)
                time.sleep(30)
        threading.Thread(target=loop, daemon=True, name='retention').start()
