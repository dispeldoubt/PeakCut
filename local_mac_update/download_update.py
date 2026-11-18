"""Download a pinned, checksum-verified update from the user's public PeakCut fork."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import httpx

RELEASE_SHA = '9434a70f73bad57f80a70c22c1d101c0c41745ed4c3f87c38756df2d96948e61'
REPOSITORY = 'dispeldoubt/PeakCut'
PREFIX = 'local_mac_update'


def main():
    os.umask(0o077)
    if len(sys.argv) != 2 or not re.fullmatch(r'[0-9a-f]{40}',sys.argv[1]):
        raise SystemExit('Provide the exact published update commit.')
    revision = sys.argv[1]
    base = 'https://raw.githubusercontent.com/'+REPOSITORY+'/'+revision+'/'+PREFIX+'/'
    package = Path.home()/'Downloads'/('peakcut-update-'+revision[:12])
    package.mkdir(parents=True,exist_ok=True,mode=0o700)
    with httpx.Client(timeout=30,follow_redirects=False) as client:
        response=client.get(base+'release.json');response.raise_for_status()
        raw=response.content
        if hashlib.sha256(raw).hexdigest()!=RELEASE_SHA:
            raise SystemExit('Release verification failed. No services changed.')
        files=json.loads(raw)['files']
        expected={'manifest.json','update_live.py','native_runtime.py','native_health.py','native_watchdog.py',
                  'native_discovery.py','native_maintenance.py','unified_publisher.py'}
        if set(files)!=expected:
            raise SystemExit('Unexpected release file list. No services changed.')
        def fetch(item):
            name,digest=item
            response=client.get(base+name);response.raise_for_status()
            data=response.content
            if hashlib.sha256(data).hexdigest()!=digest:
                raise RuntimeError('Download verification failed: '+name)
            if name.endswith('.py'):
                compile(data,name,'exec')
            return name,data
        with ThreadPoolExecutor(max_workers=4) as pool:
            payload=list(pool.map(fetch,files.items()))
    # All downloads must verify before any installer is executed.
    for name,data in payload:
        fd,temporary=tempfile.mkstemp(prefix='.fetch-',dir=package)
        try:
            with os.fdopen(fd,'wb') as output:output.write(data)
            os.replace(temporary,package/name)
        finally:
            Path(temporary).unlink(missing_ok=True)
    print('VERIFIED: complete update fetched directly from your GitHub fork.',flush=True)
    result=subprocess.run([sys.executable,str(package/'update_live.py')],cwd=package)
    raise SystemExit(result.returncode)


if __name__=='__main__':
    try:
        main()
    except (OSError,ValueError,RuntimeError,httpx.HTTPError) as error:
        raise SystemExit('Download stopped ('+type(error).__name__+'). No unverified installer was executed.')
