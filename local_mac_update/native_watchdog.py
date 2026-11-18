"""Bounded live polling and recovery when FFmpeg stops delivering audio."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from unified_publisher import channel

POLL_SECONDS = 20
STALL_SECONDS = 120


class LiveWatchdog:
    def __init__(self, modules, clock=time.monotonic, discovery=None):
        self.m, self.clock = modules, clock
        self.discovery = discovery
        self.seen, self.pending_kills, self.tasks = {}, [], {}
        self.lock = threading.RLock()
        self.local = threading.local()
        self.ready = threading.Event()
        self.pool = None
        self.recoveries = 0
        self.last_round_at = None
        self.last_tasks = {}

    def terminate(self, proc, now):
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        if not any(p is proc for p, _ in self.pending_kills):
            self.pending_kills.append((proc, now + 5))

    def tick(self):
        with self.lock:
            self._tick()

    def _tick(self):
        now, m = self.clock(), self.m
        waiting = []
        for proc, due in self.pending_kills:
            if proc.poll() is not None:
                continue
            if now >= due:
                proc.kill()
            else:
                waiting.append((proc, due))
        self.pending_kills = waiting
        current = m.live.active_mine()
        ids = {s.job['id'] for s in current}
        with self.lock:
            self.seen = {k:v for k,v in self.seen.items() if k in ids}
            for session in current:
                if not channel(session.job):
                    continue
                with session.lock:
                    samples = session.samples
                key = session.job['id']
                row = self.seen.setdefault(key, {'samples':samples, 'at':now, 'recoveries':0})
                if row['samples'] != samples:
                    row.update(samples=samples, at=now)
                    continue
                if session.ended:
                    # Let a new stream start while already-queued renders finish.
                    if session.job.get('status') == 'done':
                        session.stop('Stream ended; waiting for the next broadcast.')
                    continue
                if now - row['at'] < STALL_SECONDS:
                    continue
                proc = session.proc
                row.update(at=now, recoveries=row['recoveries'] + 1)
                self.recoveries += 1
                if proc is not None and proc.poll() is None:
                    self.terminate(proc, now)
                    print('Capture stalled for ' + session.job['creator'] + '; reconnecting to Twitch.', flush=True)
                else:
                    # The resolve wrapper prevents an already-stopped lookup from spawning FFmpeg later.
                    session.stop('Capture lookup stalled; auto-record will check the channel again.')
                    print('Capture lookup stalled; releasing its recording slot.', flush=True)

    def snapshot(self, session):
        with self.lock:
            row = self.seen.get(session.job['id'])
            return {'audio_idle_seconds':round(max(0, self.clock()-row['at']),1) if row else None,
                    'source_ended':bool(session.ended),
                    'capture_worker_running':bool(getattr(session, '_capture_worker_running', False)),
                    'reconnect_attempts':row['recoveries'] if row else 0}

    def round(self):
        m = self.m
        with m.ctx.as_user(m.auth.local_user().id):
            self.tick()
            if self.discovery is not None:
                self.discovery.tick()
            for creator in m.creators.all_creators():
                if not creator.get('auto_watch') or not channel({'source':{'url':creator.get('channel')}}):
                    continue
                previous = self.tasks.get(creator['id'])
                if previous is not None and not previous.done():
                    continue
                if self.clock() - self.last_tasks.get(creator['id'], -1000) < POLL_SECONDS:
                    continue
                # Each creator has at most one check in flight. No polling work piles up.
                self.tasks[creator['id']] = self.pool.submit(m.ctx.bind(lambda c=creator: m.live._watch_one(c)))
                self.last_tasks[creator['id']] = self.clock()
            self.last_round_at = time.time()

    def monitoring(self):
        rows = []
        for c in self.m.creators.all_creators():
            url = c.get('channel') or ''
            if not channel({'source':{'url':url}}):
                continue
            cached = self.m.profiles._live.get(url.rstrip('/').lower())
            rows.append({'creator':c['id'], 'auto_watch':bool(c.get('auto_watch')),
                         'last_live_check_at':cached[0] if cached else None,
                         'last_reported_live':cached[1].get('live') if cached else None})
        return {'last_round_at':self.last_round_at, 'creators':rows}

    def loop(self):
        self.ready.wait()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='local-live-check')
        while True:
            started = self.clock()
            try:
                self.round()
            except Exception:
                print('Live recovery check interrupted; retrying on the next round.', flush=True)
            time.sleep(max(1, 5 - (self.clock()-started)))

    def attach(self):
        m, watchdog = self.m, self
        original_posting_start = m.posting.start
        def posting_start():
            if watchdog.discovery is not None:
                # Startup callbacks have no request context. Bind the local owner before touching profiles.
                with m.ctx.as_user(m.auth.local_user().id):
                    watchdog.discovery.initialize()
                    result = original_posting_start()
            else:
                result = original_posting_start()
            watchdog.ready.set()
            return result
        m.posting.start = posting_start
        original_ingest, original_resolve = m.live.Session._ingest, m.live.resolve
        original_session_start = m.live.start_session
        def start_session(creator, url=None):
            login = channel({'source':{'url':url or creator.get('channel')}})
            if watchdog.discovery is not None and login and login not in watchdog.discovery.selected:
                raise ValueError('This creator is outside the two currently selected recording slots.')
            if watchdog.discovery is not None and not watchdog.discovery.verified(login):
                raise ValueError('Waiting for a fresh GTA VI category check before recording.')
            job = original_session_start(creator, url)
            if watchdog.discovery is not None:
                job['gta_vi_verified'] = True
                job['gta_vi_safe_until'] = 0.0
                m.jobs.save(job)
            return job
        m.live.start_session = start_session
        original_add_clip = m.live.Session._add_clip
        def add_clip(session, clip):
            if clip.get('judges') and channel(session.job):
                start, end = max(0.0, float(clip['start'])), float(clip['end'])
                end = min(end, start + 60)
                start = max(0.0, min(start, end - 30))
                if end - start < 30:
                    raise ValueError('Not enough recorded material for a 30-second highlight yet.')
                clip.update(start=round(start, 2), end=round(end, 2))
            return original_add_clip(session, clip)
        def resolve(url):
            if watchdog.discovery is not None and channel({'source':{'url':url}}) not in watchdog.discovery.selected:
                raise m.live.NotLive('This creator is not currently selected for recording.')
            if channel({'source':{'url':url}}):
                import yt_dlp
                options = {'quiet':True, 'no_warnings':True, 'noplaylist':True,
                           'logger':m.live._Quiet(), 'socket_timeout':10, 'retries':1, 'extractor_retries':1,
                           'format':'bv*[vcodec!*=av01][vcodec!*=av1]+ba/b[vcodec!*=av01][vcodec!*=av1]'}
                try:
                    with yt_dlp.YoutubeDL(options) as ydl:
                        info = ydl.extract_info(url, download=False)
                except Exception:
                    raise m.live.NotLive('The Twitch feed could not be resolved; automatic detection will retry.') from None
                if not info or not info.get('is_live'):
                    raise m.live.NotLive('This channel is not live right now.')
                formats = info.get('requested_formats') or [info]
                title = info.get('title') or ''
                if '(live)' in title and info.get('description'):
                    title = str(info['description']).splitlines()[0][:120]
                answer = {'urls':[f['url'] for f in formats], 'title':title,
                          'uploader':info.get('uploader') or info.get('channel') or ''}
            else:
                answer = original_resolve(url)
            session = getattr(watchdog.local, 'session', None)
            if session is not None and session.stop_flag.is_set():
                raise m.live.NotLive('This recording was stopped during its channel lookup.')
            return answer
        def ingest(session):
            watchdog.local.session = session
            session._capture_worker_running = True
            try:
                return original_ingest(session)
            except Exception:
                session.job.setdefault('ai', {})['last_error'] = 'Capture worker interrupted; automatic live detection will retry.'
                if not session.samples:
                    session.job['error'] = 'Capture could not start; automatic live detection will retry.'
            finally:
                session.ended = True
                session._capture_worker_running = False
                watchdog.local.session = None
                if session.proc is not None and session.proc.poll() is None:
                    with watchdog.lock:
                        watchdog.terminate(session.proc, watchdog.clock())
        m.profiles.LIVE_SHARED_FOR = 10
        m.live.resolve, m.live.Session._ingest, m.live.Session._add_clip, m.live.watch_loop = resolve, ingest, add_clip, self.loop
        m.live.local_watchdog = self
        return self
