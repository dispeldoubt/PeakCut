import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as N
import unittest

from native_discovery import Discovery, parse, PRIMARY, GAME
from unified_publisher import CHANNELS


def person(login,game='gta6',live=True):
    return {'login':login,'displayName':login,'stream':{'type':'live','id':login,'viewersCount':20,
            'title':'Gameplay','game':{'id':game,'name':GAME}} if live else None}


def response(priority=True):
    return {'data':{'game':{'id':'gta6','name':GAME,'streams':{'edges':[
        {'node':{'viewersCount':1000,'title':'Gameplay','broadcaster':{'login':'other_player','displayName':'Other'}}},
        {'node':{'viewersCount':500,'title':'Gameplay','broadcaster':{'login':'next_player','displayName':'Next'}}}]}},
        'p0':person(PRIMARY[0],live=priority),'p1':person(PRIMARY[1],live=priority),
        'a0':person('other_player'),'a1':person('next_player')}}


class Creators:
    def __init__(self):self.rows=[]
    def all_creators(self):return copy.deepcopy(self.rows)
    def create(self,c):
        c=dict(c,id=c['name'].lower().replace(' ','_'))
        self.rows.append(c)
        return c
    def update(self,cid,patch):
        c=next(x for x in self.rows if x['id']==cid)
        c.update(patch)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.private=Path(self.temp.name)
        self.channels=set(CHANNELS)
        self.creators=Creators()
        self.sessions=[]
        self.m=N(creators=self.creators,live=N(active_mine=lambda:self.sessions),profiles=N(_live={}))
        self.d=Discovery(self.m,self.private,clock=lambda:0)

    def tearDown(self):
        CHANNELS.clear();CHANNELS.update(self.channels)
        self.temp.cleanup()

    def test_explicit_priorities_win_over_higher_viewer_fallbacks(self):
        selected,_=parse(response(),['other_player','next_player'])
        self.assertEqual([s['login'] for s in selected],list(PRIMARY))

    def test_english_directory_candidates_fill_offline_priority_seats(self):
        selected,_=parse(response(False),[])
        self.assertEqual([s['login'] for s in selected],['other_player','next_player'])

    def test_existing_gta_capture_is_not_replaced_for_viewer_count_alone(self):
        data=response(False)
        data['data']['a0']=person('stable_player')
        selected,_=parse(data,['stable_player'])
        self.assertEqual(selected[0]['login'],'stable_player')

    def test_wrong_game_and_graphql_errors_cannot_trigger_auto_import(self):
        data=response();data['data']['game']['name']='Grand Theft Auto V'
        with self.assertRaises(ValueError):parse(data,[])
        with self.assertRaises(ValueError):parse({'errors':[{'message':'unavailable'}]},[])

    def test_priority_creators_are_excluded_unless_they_are_in_gta_vi(self):
        data=response()
        data['data']['p0']=person(PRIMARY[0],game='another-game')
        selected,_=parse(data,[])
        self.assertNotIn(PRIMARY[0],[x['login'] for x in selected])
        self.assertIn(PRIMARY[1],[x['login'] for x in selected])

    def test_registration_is_idempotent_and_keeps_expected_clip_settings(self):
        self.d.initialize();self.d.initialize()
        self.assertEqual(len(self.creators.rows),2)
        self.assertEqual([c['name'] for c in self.creators.rows],list(PRIMARY))
        for c in self.creators.rows:
            self.assertTrue(c['autopost'])
            self.assertEqual((c['min_len'],c['max_len']),(30,60))
            self.assertEqual(c['live_per_hour'],3)

    def test_new_fallback_registered_and_old_capture_switched_only_after_verified_result(self):
        stopped=[]
        self.sessions=[N(job={'source':{'url':'https://www.twitch.tv/jynxzi'}},stop=lambda note:stopped.append(note))]
        self.d.apply(parse(response(False),[]))
        self.assertEqual(len(self.creators.rows),2)
        self.assertEqual(self.d.selected,['other_player','next_player'])
        self.assertEqual(len(stopped),1)
        saved=json.loads((self.private/'managed-twitch-channels.json').read_text())
        self.assertIn('other_player',saved['channels'])
        restored=Discovery(self.m,self.private)
        self.assertIn('other_player',CHANNELS)

    def test_failed_lookup_keeps_healthy_sources(self):
        class Failed:
            def done(self):return True
            def result(self):raise ValueError('no data')
        self.d.future=Failed();self.d.pool=N();self.d.at=0
        self.d.tick()
        self.assertEqual(self.d.selected,[])
        self.assertIsNotNone(self.d.error)
        self.assertEqual(self.creators.rows,[])

    def test_empty_directory_does_not_kill_a_healthy_capture(self):
        stopped=[]
        self.sessions=[N(job={'source':{'url':'https://www.twitch.tv/jynxzi'}},stop=lambda note:stopped.append(note))]
        self.d.apply(([],{}))
        self.assertEqual(stopped,[])

    def test_invalid_twitch_login_is_rejected_before_file_changes(self):
        with self.assertRaises(ValueError):self.d.ensure('../bad')
        self.assertEqual(list(self.private.iterdir()),[])


if __name__=='__main__':unittest.main()
