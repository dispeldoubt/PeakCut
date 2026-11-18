import contextlib
import io
import threading
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

from native_watchdog import LiveWatchdog


class Process:
    def __init__(self):self.terms=0;self.kills=0;self.dead=False
    def poll(self):return 0 if self.dead else None
    def terminate(self):self.terms+=1
    def kill(self):self.kills+=1;self.dead=True


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.time=0
        self.process=Process()
        self.s=N(job={'id':'job','creator':'jynxzi','status':'live','source':{'url':'https://www.twitch.tv/jynxzi'}},
                 lock=threading.RLock(),samples=16000,ended=False,proc=self.process,stop_flag=threading.Event())
        def stop(note):self.s.stop_flag.set();self.s.job['status']='done'
        self.s.stop=stop
        self.sessions=[self.s]
        class Session:
            def _ingest(self):pass
            def _add_clip(self,c):return c
        self.m=N(live=N(active_mine=lambda:self.sessions,Session=Session,resolve=lambda u:{'urls':['url']},
                       NotLive=type('NotLive',(Exception,),{}),watch_loop=lambda:None,start_session=lambda c,u=None:None),
                 profiles=N(LIVE_SHARED_FOR=75),posting=N(start=lambda:None))
        self.w=LiveWatchdog(self.m,clock=lambda:self.time)

    def tick(self):
        with contextlib.redirect_stdout(io.StringIO()):self.w.tick()

    def test_active_audio_is_never_restarted(self):
        self.tick()
        for t in (50,100,200,400):
            self.time=t;self.s.samples+=16000;self.tick()
        self.assertEqual(self.process.terms,0)

    def test_stalled_ffmpeg_terminated_then_killed_without_stopping_autowatch(self):
        self.tick();self.time=119;self.tick()
        self.assertEqual(self.process.terms,0)
        self.time=120;self.tick()
        self.assertEqual(self.process.terms,1)
        self.assertFalse(self.s.stop_flag.is_set())
        self.time=126;self.tick()
        self.assertEqual(self.process.kills,1)
        self.assertEqual(self.w.recoveries,1)

    def test_healthy_render_or_silence_does_not_trigger_stall_if_pcm_advances(self):
        self.tick();self.time=125;self.s.samples+=32000;self.tick()
        self.assertEqual(self.process.terms,0)

    def test_dead_or_missing_capture_releases_recording_slot(self):
        self.s.proc=None
        self.tick();self.time=121;self.tick()
        self.assertTrue(self.s.stop_flag.is_set())

    def test_ended_session_kept_until_analysis_finishes_then_released(self):
        self.s.ended=True
        self.tick();self.time=200;self.tick()
        self.assertFalse(self.s.stop_flag.is_set())
        self.s.job['status']='done';self.tick()
        self.assertTrue(self.s.stop_flag.is_set())

    def test_unselected_creator_never_terminated(self):
        self.s.job['source']['url']='https://www.twitch.tv/someoneelse'
        self.tick();self.time=200;self.tick()
        self.assertEqual(self.process.terms,0)

    def test_failed_capture_worker_always_marks_source_ended_and_releases_live_slot(self):
        def broken(s):raise RuntimeError('private error not for logs')
        self.m.live.Session._ingest=broken
        self.w.attach()
        self.m.live.Session._ingest(self.s)
        self.assertTrue(self.s.ended)
        self.assertFalse(self.s._capture_worker_running)
        self.assertNotIn('private',self.s.job['ai']['last_error'])
        self.assertEqual(self.process.terms,1)
        self.assertEqual(self.m.profiles.LIVE_SHARED_FOR,10)

    def test_stopped_lookup_cannot_spawn_a_late_capture(self):
        def ingest(s):
            self.w.local.session=s
            s.stop_flag.set()
            self.m.live.resolve('https://example.test/live')
        self.m.live.Session._ingest=ingest
        self.w.attach()
        self.m.live.Session._ingest(self.s)
        self.assertTrue(self.s.ended)
        self.assertFalse(self.s._capture_worker_running)

    def test_no_polling_work_piles_up_for_slow_lookups(self):
        submissions=[]
        future=N(done=lambda:False)
        self.w.pool=N(submit=lambda fn:(submissions.append(fn) or future))
        self.m.ctx=N(as_user=lambda uid:contextlib.nullcontext(),bind=lambda f:f)
        self.m.auth=N(local_user=lambda:N(id='owner'))
        self.m.creators=N(all_creators=lambda:[{'id':'jynxzi','auto_watch':True,'channel':'https://www.twitch.tv/jynxzi'}])
        self.m.live._watch_one=lambda c:None
        self.w.round();self.time=20;self.w.round()
        self.assertEqual(len(submissions),1)

    def test_model_and_source_quality_are_preserved_by_bounded_lookup(self):
        import sys
        options=[]
        info={'is_live':True,'url':'https://media.example.test/stream.m3u8','title':'Gaming','uploader':'creator'}
        class YDL:
            def __init__(self, settings):options.append(settings)
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def extract_info(self,*a,**k):return info
        self.m.live._Quiet=lambda:None
        self.w.attach()
        with patch.dict(sys.modules,{'yt_dlp':N(YoutubeDL=YDL)}):
            answer=self.m.live.resolve('https://www.twitch.tv/jynxzi')
        self.assertEqual(answer['urls'],[info['url']])
        self.assertEqual(options[0]['socket_timeout'],10)
        self.assertNotIn('height<=',options[0]['format'])
        self.assertIn('vcodec!*=av01',options[0]['format'])

    def test_new_ai_clips_respect_30_to_60_second_duration(self):
        self.w.attach()
        for start,end in ((100,161),(100,129),(100,145)):
            clip={'judges':{'words':80},'start':start,'end':end}
            result=self.m.live.Session._add_clip(self.s,clip)
            self.assertGreaterEqual(result['end']-result['start'],30)
            self.assertLessEqual(result['end']-result['start'],60)

    def test_live_checks_wait_for_updated_creator_settings(self):
        self.w.attach()
        self.assertFalse(self.w.ready.is_set())
        self.m.posting.start()
        self.assertTrue(self.w.ready.is_set())


if __name__=='__main__':unittest.main()
