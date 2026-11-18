"""Select two live Twitch sources, preferring explicit priorities over directory discovery."""
from concurrent.futures import ThreadPoolExecutor
import re
import shutil
import time

import httpx
from unified_publisher import ACCOUNT_ID, CHANNELS, PRIMARY, atomic, channel

GAME = 'Grand Theft Auto VI'
LOGIN = re.compile(r'^[a-z0-9_]{2,25}$')
FIELDS = 'login displayName stream{id title viewersCount createdAt type game{id name}}'
QUERY = ('query($p0:String!,$p1:String!,$a0:String!,$a1:String!,$game:String!){'
         + 'p0:user(login:$p0){'+FIELDS+'}p1:user(login:$p1){'+FIELDS+'}'
         + 'a0:user(login:$a0){'+FIELDS+'}a1:user(login:$a1){'+FIELDS+'}'
         + 'game(name:$game){id name streams(first:50,options:{sort:VIEWER_COUNT,languages:["EN"]})'
         + '{edges{node{id title viewersCount broadcaster{login displayName}}}}}}')


def clean_stream(login, name, stream):
    login = str(login or '').lower()
    if not LOGIN.fullmatch(login):
        return None
    return {'login':login, 'name':str(name or login)[:80], 'viewers':int(stream.get('viewersCount') or 0),
            'title':str(stream.get('title') or '')[:140]}


def parse(data, current):
    if data.get('errors') or not isinstance(data.get('data'),dict):
        raise ValueError('Twitch directory lookup did not return usable data.')
    root = data['data']
    game = root.get('game')
    if not game or game.get('name') != GAME:
        raise ValueError('The exact GTA VI category could not be verified.')
    available, priority, states = {}, [], {}
    for key in ('p0','p1','a0','a1'):
        user = root.get(key) or {}
        login = str(user.get('login') or '').lower()
        stream = user.get('stream') or {}
        live = stream.get('type') == 'live'
        if LOGIN.fullmatch(login):
            states[login] = {'live':live,'viewers':stream.get('viewersCount'), 'title':str(stream.get('title') or '')[:140],
                             'started':None, 'embed':login}
        # Every source, including a priority creator, must currently be in the exact GTA VI category.
        in_game = (stream.get('game') or {}).get('id') == game.get('id')
        if live and in_game:
            value = clean_stream(login,user.get('displayName'),stream)
            if value:
                available[login] = value
                if key.startswith('p'):
                    priority.append(login)
    directory = []
    for edge in (game.get('streams') or {}).get('edges',[]):
        stream = edge.get('node') or {}
        broadcaster = stream.get('broadcaster') or {}
        value = clean_stream(broadcaster.get('login'),broadcaster.get('displayName'),stream)
        if value:
            directory.append(value)
            available.setdefault(value['login'],value)
            states.setdefault(value['login'],{'live':True,'viewers':value['viewers'],'title':value['title'],
                                               'started':None,'embed':value['login']})
    # Keep an existing GTA source to avoid switching merely because viewer rankings move.
    order = priority + [x for x in current if x in available] + [x['login'] for x in sorted(directory,key=lambda x:-x['viewers'])]
    selected = []
    for login in order:
        if login not in [x['login'] for x in selected] and login in available:
            selected.append(available[login])
        if len(selected) == 2:
            break
    return selected, states


class Discovery:
    def __init__(self, modules, private, clock=time.monotonic):
        self.m, self.private, self.clock = modules, private, clock
        self.selected = []
        self.future, self.at, self.last_success, self.error = None, -1000, None, None
        self.pool = None
        registry = private/'managed-twitch-channels.json'
        if registry.exists():
            import json
            saved = json.loads(registry.read_text())
            CHANNELS.update(x for x in saved.get('channels',[]) if isinstance(x,str) and LOGIN.fullmatch(x))

    def ensure(self, login, name=None):
        if not LOGIN.fullmatch(login):
            raise ValueError('Invalid discovered channel name.')
        newly_managed = login not in CHANNELS
        CHANNELS.add(login)
        registry = self.private/'managed-twitch-channels.json'
        if newly_managed or not registry.exists():
            atomic(registry,{'channels':sorted(CHANNELS)})
        matches = [c for c in self.m.creators.all_creators() if channel({'source':{'url':c.get('channel')}}) == login]
        if matches:
            return matches[0]
        template = next(iter(self.m.creators.all_creators()),{})
        url = 'https://www.twitch.tv/'+login
        creator = self.m.creators.create({**template,'name':name or login,'channel':url,'channels':{'twitch':url},
            'auto_watch':login in self.selected, 'autopost':True,'accounts':[ACCOUNT_ID], 'min_len':30,'max_len':60,
            'live_per_hour':3,'post_gap_min':10,'daily_cap':50,'post_min_score':70,'must_clip_min':0,
            'post_footer':'Source: twitch.tv/'+login})
        return creator

    def initialize(self):
        for login in PRIMARY:
            self.ensure(login)
        # Retire the two test profiles without erasing their publication ledgers or local job files.
        for creator in self.m.creators.all_creators():
            if channel({'source':{'url':creator.get('channel')}}) not in {'jynxzi','zackrawrr'}:
                continue
            self.m.creators.update(creator['id'],{'auto_watch':False,'autopost':False,'accounts':[]})
            source = self.m.creators.root(creator['id'])/'creator.json'
            archive = self.private/'retired-creators'
            archive.mkdir(parents=True,exist_ok=True,mode=0o700)
            destination = archive/(creator['id']+'.json')
            if source.exists():
                if not destination.exists():
                    shutil.copy2(source,destination)
                    destination.chmod(0o600)
                source.unlink()
            with self.m.creators._lock:
                self.m.creators._mine().pop(creator['id'],None)
            self.m.ctx.bump()

    def verified(self, login):
        return login in self.selected and self.last_success is not None and time.time()-self.last_success < 90

    def fetch(self, selected):
        active = (selected+list(PRIMARY))[:2]
        variables = {'p0':PRIMARY[0],'p1':PRIMARY[1],'a0':active[0],'a1':active[1],'game':GAME}
        with httpx.Client(timeout=10,follow_redirects=False) as client:
            response = client.post('https://gql.twitch.tv/gql',json={'query':QUERY,'variables':variables},
                                   headers={'Client-ID':self.m.profiles._TWITCH_WEB})
            response.raise_for_status()
            return parse(response.json(),selected)

    def apply(self, result):
        selected, states = result
        desired = [x['login'] for x in selected]
        for row in selected:
            self.ensure(row['login'],row['name'])
        for creator in self.m.creators.all_creators():
            login = channel({'source':{'url':creator.get('channel')}})
            if not login:
                continue
            wanted = login in desired
            if bool(creator.get('auto_watch')) != wanted:
                self.m.creators.update(creator['id'],{'auto_watch':wanted})
        self.selected = desired
        for session in self.m.live.active_mine():
            login = channel(session.job)
            if login in desired and session.job.get('gta_vi_verified'):
                with session.lock:
                    session.job['gta_vi_safe_until'] = session.samples/16000.0
                self.m.jobs.save(session.job)
            if desired and login and login not in desired:
                session.stop('Automatic GTA VI selection: switching to a priority or live gameplay creator.')
        for login,state in states.items():
            self.m.profiles._live['https://www.twitch.tv/'+login]=(time.time(),state)
        self.last_success=time.time()
        self.error=None

    def tick(self):
        if self.pool is None:
            self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='gta-directory')
        if self.future is not None and self.future.done():
            try:
                self.apply(self.future.result())
            except Exception:
                # A failed lookup never displaces working sources or imports unverified creator data.
                self.error='Directory check failed; retaining the previous selection and retrying.'
            self.future=None
        if self.future is None and self.clock()-self.at >= 20:
            self.at=self.clock()
            self.future=self.pool.submit(self.fetch,list(self.selected))

    def status(self):
        return {'category':GAME,'language':'English','priority':list(PRIMARY),'selected':list(self.selected),
                'last_success_at':self.last_success,'problem':self.error,
                'lookup_running':bool(self.future is not None and not self.future.done())}
