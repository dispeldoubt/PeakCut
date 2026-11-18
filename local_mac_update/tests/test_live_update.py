import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

import update_live as u


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.root=self.base/'root';self.root.mkdir()
        self.private=self.base/'private';self.private.mkdir()
        self.package=self.base/'package';self.package.mkdir()
        self.plist=self.base/'service.plist';self.plist.touch()
        (self.root/'install_native.py').touch()
        self.old={n:b'old_version=1\n' for n in u.NAMES if n not in {'native_watchdog.py','native_discovery.py'}}
        self.new={n:b'new_version=2\n' for n in u.NAMES}
        for n,b in self.old.items():(self.root/n).write_bytes(b)
        for n,b in self.new.items():(self.package/n).write_bytes(b)
        manifest=json.dumps({'old':{n:hashlib.sha256(b).hexdigest() for n,b in self.old.items()},
                             'new':{n:hashlib.sha256(b).hexdigest() for n,b in self.new.items()}}).encode()
        (self.package/'manifest.json').write_bytes(manifest)
        self.is_loaded=True;self.fail_new=False;self.bootstraps=0;self.commands=[]
        def command(*args):
            self.commands.append(args)
            if args[0]=='bootout':self.is_loaded=False
            if args[0]=='bootstrap':
                self.bootstraps+=1
                if self.fail_new and self.bootstraps==1:return 1
                self.is_loaded=True
            return 0
        def health():
            if not self.bootstraps:return {'pid':12,'daily_cap':10,'gap_minutes':60}
            return {'pid':13,'recovery_version':2,'gap_minutes':10,'daily_cap':100,'published_copy_retention_hours':6}
        self.patches=[patch.object(u,'ROOT',self.root),patch.object(u,'PRIVATE',self.private),patch.object(u,'PACKAGE',self.package),
                      patch.object(u,'PLIST',self.plist),patch.object(u,'MANIFEST_SHA',hashlib.sha256(manifest).hexdigest()),
                      patch.object(u.sys,'platform','darwin'),patch.object(u.sys,'prefix',str(self.root/'.venv')),
                      patch.object(u,'command',side_effect=command),patch.object(u,'loaded',side_effect=lambda:self.is_loaded),
                      patch.object(u,'health',side_effect=health),patch.object(u,'socket_open',return_value=False),
                      patch.object(u,'process_running',return_value=False),
                      patch.object(u.subprocess,'run',return_value=N(returncode=0)),
                      patch.object(u.shutil,'disk_usage',return_value=N(free=20_000_000_000))]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def run_update(self):
        with contextlib.redirect_stdout(io.StringIO()):u.install()

    def test_successful_update_preserves_credentials_and_publication_history(self):
        ledger=self.private/'saved-history.json';ledger.write_text('original-history')
        key=self.private/'zernio-key';key.write_text('FAKE-KEY')
        self.run_update()
        self.assertEqual(self.commands[0][0],'bootout')
        self.assertEqual(self.commands[1][0],'bootstrap')
        for n,b in self.new.items():self.assertEqual((self.root/n).read_bytes(),b)
        self.assertEqual(ledger.read_text(),'original-history')
        self.assertEqual(key.read_text(),'FAKE-KEY')
        self.assertFalse((self.private/'LIVE_UPDATE').exists())

    def test_failed_start_restores_previous_code_and_loaded_service(self):
        self.fail_new=True
        with self.assertRaises(SystemExit):self.run_update()
        for n,b in self.old.items():self.assertEqual((self.root/n).read_bytes(),b)
        self.assertFalse((self.root/'native_watchdog.py').exists())
        self.assertTrue(self.is_loaded)
        self.assertFalse((self.private/'LIVE_UPDATE').exists())

    def test_inflight_post_blocks_update_before_stopping_service(self):
        with patch.object(u.fcntl,'flock',side_effect=BlockingIOError),self.assertRaises(SystemExit):self.run_update()
        self.assertEqual(self.commands,[])
        for n,b in self.old.items():self.assertEqual((self.root/n).read_bytes(),b)

    def test_corrupted_package_is_rejected_before_stopping(self):
        (self.package/'native_watchdog.py').write_text('truncated')
        with self.assertRaises(SystemExit):self.run_update()
        self.assertEqual(self.commands,[])

    def test_unexpected_existing_change_is_never_overwritten(self):
        (self.root/'native_runtime.py').write_text('user_change=1')
        with self.assertRaises(SystemExit):self.run_update()
        self.assertEqual((self.root/'native_runtime.py').read_text(),'user_change=1')
        self.assertEqual(self.commands,[])


if __name__=='__main__':unittest.main()
