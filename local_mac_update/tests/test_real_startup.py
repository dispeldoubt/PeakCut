"""Exercise the actual FastAPI startup path without model loading, network calls, or real data."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CODE = r'''
import json
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch
from studio import accounts,account,auth,config,creators,ctx,db,jobs,live,posting,profiles,server,updates
from unified_publisher import Publisher
from native_watchdog import LiveWatchdog
from native_discovery import Discovery,parse,GAME
from native_health import attach_health
from fastapi.testclient import TestClient

db.init()
owner=auth.local_user()
with ctx.as_user(owner.id):
    for login in ('jynxzi','zackrawrr'):
        creators.create({'name':login,'channel':'https://www.twitch.tv/'+login})
private=config.DATA_DIR/'private';private.mkdir()
(private/'instagram.json').write_text(json.dumps({'username':'test_account','accountId':'offline-account'}))
(private/'zernio-key').write_text('FAKE-OFFLINE-KEY')
(private/'PAUSE').touch()
# Keep all real profile/account/context logic; suppress only background workers and external effects.
posting.start=lambda:None
m=N(accounts=accounts,account=account,auth=auth,config=config,creators=creators,ctx=ctx,jobs=jobs,live=live,posting=posting,profiles=profiles)
p=Publisher(config.DATA_DIR,private,m);p.attach()
d=Discovery(m,private);w=LiveWatchdog(m,discovery=d);w.attach()
assert ctx.uid_or_none() is None
with patch.object(jobs,'start'),patch.object(live,'start'),patch.object(server.render,'install_fonts'),patch.object(updates,'start'):
    server._startup()
assert ctx.uid_or_none() is None
assert w.ready.is_set()
with ctx.as_user(owner.id):
    cs=creators.all_creators()
    assert {c['id'] for c in cs}=={'xqc','typicalgamer'},cs
    assert all(not c['auto_watch'] for c in cs)
    assert all(c['autopost'] and c['min_len']==30 and c['max_len']==60 for c in cs)
    assert {x.stem for x in (private/'retired-creators').glob('*.json')}=={'jynxzi','zackrawrr'}
    assert d.selected==[]
attach_health(server,live,jobs,server.load,config)
response=TestClient(server.app,base_url='http://127.0.0.1:8766').get('/api/local-health')
assert response.status_code==200,response.text
h=response.json()
assert h['recovery_version']==3 and h['gta_vi_only'] and h['posting_paused'],h
assert h['automatic_discovery']['priority']==['xqc','typicalgamer']
print('REAL STARTUP PASSED: owner context, old profiles retired, GTA-only gating, pause status and health endpoint.')
'''


class RealStartup(unittest.TestCase):
    def test_real_server_startup_registers_profiles_in_owner_context(self):
        here=Path(__file__).resolve()
        framework=Path(os.environ.get('PEAKCUT_TEST_REPO',str(here.parents[2])))
        helpers=Path(os.environ.get('PEAKCUT_TEST_HELPERS',str(here.parents[1])))
        with tempfile.TemporaryDirectory() as temporary:
            env={**os.environ,'PEAKCUT_DATA':temporary,'PEAKCUT_ACCOUNTS':'0','PEAKCUT_FETCH_MODEL':'0',
                 'PEAKCUT_PUBLIC_URL':'http://127.0.0.1:8766','PEAKCUT_PORT':'8766',
                 'PYTHONPATH':str(helpers)+os.pathsep+str(framework)}
            result=subprocess.run([sys.executable,'-c',CODE],env=env,cwd=framework,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertIn('REAL STARTUP PASSED',result.stdout)


if __name__=='__main__':unittest.main()
