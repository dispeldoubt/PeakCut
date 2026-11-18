"""Verified, reversible update of the installed local PeakCut background service."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path.home() / 'Projects/peakcut-local'
PRIVATE = Path.home() / 'Library/Application Support/TwitchPilot'
DOMAIN = 'gui/' + str(os.getuid())
LABEL = 'com.twitchpilot.peakcut'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
PACKAGE = Path(__file__).resolve().parent
# Populated when this reviewed package is built.
MANIFEST_SHA = '2a1cbedaf0ce1cf8e408fa181e5b7d2651acc398f0105a86c0fe98be1078b837'
NAMES = {'native_watchdog.py', 'native_runtime.py', 'unified_publisher.py', 'native_health.py', 'native_maintenance.py', 'native_discovery.py'}


def command(*args):
    return subprocess.run(['/bin/launchctl', *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def loaded():
    return command('print', DOMAIN + '/' + LABEL) == 0


def health():
    with httpx.Client(timeout=3, trust_env=False) as client:
        response = client.get('http://127.0.0.1:8766/api/local-health')
        response.raise_for_status()
        return response.json()


def updated(row):
    return (row.get('recovery_version') == 3 and row.get('gta_vi_only') and row.get('gap_minutes') == 10
            and row.get('daily_cap') == 100 and row.get('published_copy_retention_hours') == 6)


def socket_open():
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex(('127.0.0.1',8766)) == 0


def process_running(pid):
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def write(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.live-update-', dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate():
    raw = (PACKAGE/'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA:
        raise SystemExit('Update package is incomplete or changed. No services changed.')
    manifest = json.loads(raw)
    if set(manifest['new']) != NAMES:
        raise SystemExit('Unexpected update files. No services changed.')
    payload = {}
    for name, expected in manifest['new'].items():
        source = PACKAGE/name
        if source.is_symlink():
            raise SystemExit('Unexpected package symlink. No services changed.')
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise SystemExit('Incomplete update file: ' + name + '. No services changed.')
        compile(data, name, 'exec')
        target = ROOT/name
        if target.is_symlink():
            raise SystemExit('Unexpected installed symlink. No services changed.')
        if target.exists():
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual not in {expected, manifest['old'].get(name)}:
                raise SystemExit('Installed file differs: ' + name + '. No files overwritten.')
        elif name in manifest['old']:
            raise SystemExit('Missing installed connector: ' + name + '. No services changed.')
        payload[name] = data
    return payload


def install():
    if sys.platform != 'darwin' or Path(sys.prefix) != ROOT/'.venv':
        raise SystemExit('Use the installed peakcut-local .venv Python on your Mac.')
    payload = validate()
    if not PLIST.is_file() or not loaded():
        raise SystemExit('The existing PeakCut launch agent is not loaded. No services changed.')
    before = health()
    if updated(before):
        print('Launch update already running; no changes needed.')
        return
    # This existing read-only preflight validates current credentials, model, and ledgers.
    check = subprocess.run([sys.executable,str(ROOT/'install_native.py'),'--check'],cwd=ROOT)
    if check.returncode:
        raise SystemExit('Existing authorization or source preflight needs attention. No services changed.')
    if shutil.disk_usage(ROOT).free < 4_000_000_000:
        raise SystemExit('At least 4 GB of free disk is required. No services changed.')
    backup = PRIVATE/('live-update-backup-'+time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(mode=0o700)
    old = {name:(ROOT/name).read_bytes() if (ROOT/name).exists() else None for name in NAMES}
    for name, data in old.items():
        if data is not None:
            (backup/name).write_bytes(data)
    marker = PRIVATE/'LIVE_UPDATE'
    stopped, files_changed = False, False
    with (PRIVATE/'publisher.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Instagram is processing a post. Nothing stopped; rerun after that post finishes.')
        try:
            marker.write_text(str(os.getpid()))
            if command('bootout',DOMAIN+'/'+LABEL):
                raise RuntimeError('Could not stop the existing service.')
            stopped = True
            for _ in range(30):
                if not socket_open() and not process_running(before['pid']):
                    break
                time.sleep(1)
            else:
                raise RuntimeError('The previous service has not released its port.')
            files_changed = True
            for name, data in payload.items():
                write(ROOT/name,data)
            if command('bootstrap',DOMAIN,str(PLIST)):
                raise RuntimeError('Could not start the updated service.')
            for _ in range(45):
                try:
                    row = health()
                    if updated(row) and row.get('pid') != before.get('pid'):
                        print('RUNNING: automatic Twitch detection every 20 seconds; stalled capture recovery enabled.')
                        print('xQc + TypicalGamer prioritized; automatic English GTA VI fallback; two recordings maximum.')
                        print('New highlights: 30-60 seconds, up to 3 per creator per hour; original capture quality and current AI models retained.')
                        print('Instagram: 10-minute minimum gap; 100 posts per rolling 24 hours maximum.')
                        print('Retention: published local copies 6 hours; unposted candidates 24 hours; history retained.')
                        if '--resume-gta-only' in sys.argv:
                            # The caller confirmed the Instagram cleanup. Resume only after verified strict-policy startup.
                            (PRIVATE/'PAUSE').unlink(missing_ok=True)
                            print('GTA VI-only publishing resumed after healthy startup.')
                        elif row.get('posting_paused'):
                            print('Publishing remains PAUSED while Instagram test posts are removed. Capture can continue.')
                        print('Dashboard: http://127.0.0.1:8766/app')
                        print('Backup:',backup)
                        return
                except (httpx.HTTPError,ValueError):
                    pass
                time.sleep(1)
            raise RuntimeError('The updated service did not report healthy startup.')
        except Exception as error:
            if files_changed:
                if loaded() and command('bootout',DOMAIN+'/'+LABEL):
                    print('Update stopped: unable to stop the new service. Backups retained at',backup)
                    raise SystemExit(1)
                for name, data in old.items():
                    if data is None:
                        (ROOT/name).unlink(missing_ok=True)
                    else:
                        write(ROOT/name,data)
            restored = True
            if stopped:
                restored = command('bootstrap',DOMAIN,str(PLIST)) == 0
            print('Update did not complete:',str(error))
            print('Previous service definition restored.' if restored else 'Previous service needs restart; backups retained.')
            print('Saved Instagram publication history and credentials were preserved.')
            raise SystemExit(1)
        finally:
            marker.unlink(missing_ok=True)


if __name__=='__main__':
    os.umask(0o077)
    try:
        install()
    except (OSError, ValueError, httpx.HTTPError) as error:
        raise SystemExit('Update stopped ('+type(error).__name__+'). Do not share credentials; return this message for diagnosis.')
