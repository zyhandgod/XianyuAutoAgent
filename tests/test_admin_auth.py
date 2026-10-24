import asyncio
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from admin import Admin, MultiAdmin, handler_for
from admin_auth import AccountStore, AuthenticationError


PASSWORD='temporary-test-password-123'


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=AccountStore(Path(self.temp.name)/'auth.db')
        self.owner=self.store.create('owner',PASSWORD)

    def test_password_hash_sessions_and_logout(self):
        self.assertFalse(self.store.needs_setup())
        token,user=self.store.login('OWNER',PASSWORD)
        self.assertEqual(user['role'],'admin')
        self.assertEqual(self.store.session(token)['id'],self.owner['id'])
        with self.store.connect() as db:
            row=db.execute('SELECT salt,password_hash FROM users').fetchone()
            self.assertNotEqual(row['password_hash'],PASSWORD)
            self.assertEqual(len(row['salt']),32)
            self.assertNotIn(token,str([tuple(r) for r in db.execute('SELECT * FROM sessions')]))
        self.store.logout(token)
        self.assertIsNone(self.store.session(token))
        self.assertIsNone(self.store.session('bad'))
        self.assertIsNone(self.store.session(None))
        self.assertEqual(self.store.path.stat().st_mode&0o777,0o600)

    def test_only_owner_creates_members_no_public_signup(self):
        with self.assertRaises(PermissionError):self.store.create('outsider',PASSWORD)
        member=self.store.create('member',PASSWORD,self.owner['id'])
        with self.assertRaises(PermissionError):self.store.create('other',PASSWORD,member['id'])
        with self.assertRaises(PermissionError):self.store.users(member['id'])
        with self.assertRaises(ValueError):self.store.create('MEMBER',PASSWORD,self.owner['id'])
        self.assertEqual(len(self.store.users(self.owner['id'])),2)
        for username,password in [('../bad',PASSWORD),('ok','short'),('valid-user','short')]:
            with self.assertRaises(ValueError):self.store.validate(username,password)

    def test_expired_disabled_and_changed_password_revoke_sessions(self):
        member=self.store.create('member',PASSWORD,self.owner['id'])
        token,_=self.store.login('member',PASSWORD)
        self.store.set_active(self.owner['id'],member['id'],False)
        self.assertIsNone(self.store.session(token))
        with self.assertRaises(AuthenticationError):self.store.login('member',PASSWORD)
        self.store.set_active(self.owner['id'],member['id'],True)
        token,_=self.store.login('member',PASSWORD)
        with self.assertRaises(ValueError):self.store.change_password(member['id'],'wrong',PASSWORD+'new')
        self.store.change_password(member['id'],PASSWORD,PASSWORD+'new')
        self.assertIsNone(self.store.session(token))
        with self.assertRaises(AuthenticationError):self.store.login('member',PASSWORD)
        token,_=self.store.login('member',PASSWORD+'new')
        with patch('admin_auth.time.time',return_value=time.time()+9*3600):self.assertIsNone(self.store.session(token))
        with self.assertRaises(ValueError):self.store.set_active(self.owner['id'],self.owner['id'],False)
        with self.assertRaises(ValueError):self.store.set_active(self.owner['id'],'missing',False)

    def test_failure_rate_limit_and_prepare_rollback(self):
        for _ in range(8):
            with self.assertRaises(AuthenticationError):self.store.login('owner','wrong-password')
        with self.assertRaisesRegex(AuthenticationError,'5'):self.store.login('owner',PASSWORD)
        with patch('admin_auth.time.time',return_value=time.time()+301):self.store.login('owner',PASSWORD)
        for username,password in [(None,None),('x'*129,'x'),('missing','bad')]:
            with self.assertRaises(AuthenticationError):self.store.login(username,password)
        with self.assertRaises(RuntimeError):
            self.store.create('failed-user',PASSWORD,self.owner['id'],lambda *_:(_ for _ in ()).throw(RuntimeError()))
        self.assertNotIn('failed-user',[u['username'] for u in self.store.users(self.owner['id'])])

    def test_seller_binding_is_unique_and_atomic(self):
        member=self.store.create('member',PASSWORD,self.owner['id'])
        self.store.bind(self.owner['id'],'1001',lambda:None)
        with self.assertRaises(ValueError):self.store.bind(member['id'],'1001',lambda:None)
        with self.assertRaises(RuntimeError):self.store.bind(member['id'],'1002',lambda:(_ for _ in ()).throw(RuntimeError()))
        self.store.bind(self.owner['id'],'1002',lambda:None)
        with self.assertRaises(ValueError):self.store.bind(member['id'],'1001',lambda:None)
        self.store.bind(member['id'],'1003',lambda:None)
        self.store.set_active(self.owner['id'],member['id'],False)
        with self.assertRaises(PermissionError):self.store.bind(member['id'],'1003',lambda:None)

    def test_bootstrap_concurrency_creates_only_one_owner(self):
        other=AccountStore(Path(self.temp.name)/'race.db')
        def attempt(i):
            try:return other.create('owner'+str(i),PASSWORD)['role']
            except PermissionError:return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
        self.assertEqual(results.count('admin'),1)

    def test_inflight_old_password_login_cannot_outlive_password_revocation(self):
        entered,release,changed=threading.Event(),threading.Event(),threading.Event()
        login_thread=[None]
        original=self.store.password
        def paused_password(password,salt):
            if threading.get_ident()==login_thread[0]:
                entered.set();release.wait(2)
            return original(password,salt)
        def login():
            login_thread[0]=threading.get_ident()
            return self.store.login('owner',PASSWORD)
        def change():
            self.store.change_password(self.owner['id'],PASSWORD,PASSWORD+'new')
            changed.set()
        with patch.object(self.store,'password',side_effect=paused_password),ThreadPoolExecutor(max_workers=2) as pool:
            pending_login=pool.submit(login)
            self.assertTrue(entered.wait(2))
            pending_change=pool.submit(change)
            try:self.assertFalse(changed.wait(.2))
            finally:release.set()
            token,_=pending_login.result(timeout=3)
            pending_change.result(timeout=3)
        self.assertIsNone(self.store.session(token))
        with self.assertRaises(AuthenticationError):self.store.login('owner',PASSWORD)
        self.store.login('owner',PASSWORD+'new')


class TenantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'.env').write_text("COOKIES_STR='unb=1001;_m_h5_tk=legacy_private_token'")
        legacy=Admin(self.root)
        legacy.store.product('1001','2001','Owner product')
        legacy.store.import_cards('1001','2001',['OWNER-PRIVATE-CODE'])
        self.app=MultiAdmin(self.root)
        self.owner=self.app.create('owner',PASSWORD)
        self.member=self.app.create('member',PASSWORD,self.owner['id'])

    def test_first_owner_only_inherits_legacy_and_other_data_is_empty(self):
        owner=self.app.manager(self.owner);member=self.app.manager(self.member)
        self.assertEqual(owner.store.report('1001')['inventory'][0]['available'],1)
        self.assertFalse((member.workspace/'.env').exists())
        self.assertFalse(member.state()['configured'])
        self.assertEqual(member.store.report('1001')['inventory'],[])
        self.assertNotEqual(owner.store.path,member.store.path)
        self.assertEqual(owner.workspace.stat().st_mode&0o777,0o700)
        with self.assertRaises(ValueError):self.app.manager({'id':'../outside'})

    def test_same_card_id_and_product_are_scoped_to_user(self):
        self.app.action(self.member,'cookie',{'cookie':'unb=1002;_m_h5_tk=member_private_token'})
        self.app.action(self.member,'product',{'item':'2001','name':'Member product'})
        self.app.action(self.member,'import',{'item':'2001','text':'MEMBER-PRIVATE-CODE'})
        own=self.app.manager(self.owner);other=self.app.manager(self.member)
        self.assertEqual(own.store.card_value('1001',1),'OWNER-PRIVATE-CODE')
        self.assertEqual(other.store.card_value('1002',1),'MEMBER-PRIVATE-CODE')
        self.app.action(self.member,'edit-card',{'id':1,'secret':'MEMBER-CHANGED'})
        self.assertEqual(own.store.card_value('1001',1),'OWNER-PRIVATE-CODE')
        with self.assertRaises(ValueError):self.app.action(self.member,'cookie',{'cookie':'unb=1001;_m_h5_tk=x'})
        self.assertEqual(other.seller(),'1002')

    def test_two_services_get_independent_config_db_logs_and_shared_seller_lock(self):
        self.app.action(self.member,'cookie',{'cookie':'unb=1002;_m_h5_tk=member_private_token'})
        child_a=Mock();child_a.poll.return_value=None
        child_b=Mock();child_b.poll.return_value=None
        with patch('admin.subprocess.Popen',side_effect=[child_a,child_b]) as popen:
            self.app.action(self.owner,'start',{'mode':'observe'})
            self.app.action(self.member,'start',{'mode':'observe'})
        first,second=[c.kwargs for c in popen.call_args_list]
        self.assertNotEqual(first['env']['CARD_WORKSPACE'],second['env']['CARD_WORKSPACE'])
        self.assertEqual(first['env']['CARD_LOCK_DIR'],second['env']['CARD_LOCK_DIR'])
        self.assertNotEqual(first['stdout'].name,second['stdout'].name)
        self.assertNotIn('COOKIES_STR',first['env'])
        self.app.action(self.member,'stop',{})
        child_a.send_signal.assert_not_called();child_b.send_signal.assert_called_once()
        child_a.poll.return_value=0;child_b.poll.return_value=0

    def test_disable_only_stops_target_and_revokes_sessions(self):
        token,_=self.app.auth.login('member',PASSWORD)
        with patch.object(self.app.manager(self.member),'stop') as stop:
            self.app.set_active(self.owner,self.member['id'],False)
            stop.assert_called_once()
        self.assertIsNone(self.app.auth.session(token))
        with self.assertRaises(PermissionError):self.app.set_active(self.member,self.owner['id'],False)
        with self.assertRaises(ValueError):self.app.set_active(self.owner,self.owner['id'],False)
        self.app.stop()


class AuthHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        (cls.root/'admin_static').mkdir();(cls.root/'admin_static/index.html').write_text('login screen')
        cls.app=MultiAdmin(cls.root)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(cls.app));cls.port=cls.server.server_address[1]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def request(self,method,path,data=None,token='',csrf=''):
        conn=http.client.HTTPConnection('127.0.0.1',self.port)
        headers={'Origin':f'http://127.0.0.1:{self.port}','Content-Type':'application/json',
                 'X-CSRF-Token':csrf,'Cookie':'local_admin_session='+token}
        try:
            conn.request(method,path,json.dumps(data) if data is not None else None,headers)
            response=conn.getresponse();body=json.loads(response.read())
            return response.status,body,dict(response.getheaders())
        finally:conn.close()
    def test_full_authentication_authorization_and_logout(self):
        status,public,_=self.request('GET','/api/auth/status')
        self.assertTrue(public['needs_setup']);self.assertFalse(public['authenticated'])
        for path in ['/api/state','/api/cards?item=2001','/api/card?id=1','/api/users']:
            self.assertEqual(self.request('GET',path)[0],401)
        self.assertEqual(self.request('POST','/api/auth/setup',{'username':'owner','password':PASSWORD},csrf='bad')[0],403)
        status,owner,headers=self.request('POST','/api/auth/setup',{'username':'owner','password':PASSWORD},csrf=public['csrf'])
        self.assertEqual(status,200)
        self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        token=headers['Set-Cookie'].split(';')[0].split('=',1)[1];csrf=owner['csrf']
        self.assertEqual(self.request('GET','/api/state',token=token)[0],200)
        self.assertEqual(self.request('POST','/api/auth/create-user',{'username':'member','password':PASSWORD},token,csrf)[0],200)
        _,member,headers=self.request('POST','/api/auth/login',{'username':'member','password':PASSWORD},csrf=public['csrf'])
        member_token=headers['Set-Cookie'].split(';')[0].split('=',1)[1]
        self.assertEqual(self.request('GET','/api/users',token=member_token)[0],403)
        self.assertEqual(self.request('POST','/api/auth/create-user',{'username':'hacker','password':PASSWORD},member_token,member['csrf'])[0],403)
        self.assertEqual(self.request('POST','/api/product',{'item':'2001','name':'bad'},member_token,csrf)[0],403)
        self.assertEqual(self.request('POST','/api/auth/setup',{'username':'other','password':PASSWORD},csrf=public['csrf'])[0],403)
        self.assertEqual(self.request('POST','/api/auth/password',{'old':PASSWORD,'password':PASSWORD+'new'},member_token,member['csrf'])[0],200)
        self.assertEqual(self.request('GET','/api/state',token=member_token)[0],401)
        self.assertEqual(self.request('POST','/api/auth/logout',{},token,csrf)[0],200)
        self.assertEqual(self.request('GET','/api/state',token=token)[0],401)


class RevocationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.app=MultiAdmin(self.root)
        self.owner=self.app.create('owner',PASSWORD)
        self.member=self.app.create('member',PASSWORD,self.owner['id'])
        self.app.action(self.owner,'cookie',{'cookie':'unb=1001;_m_h5_tk=owner_secret'})
        self.app.action(self.member,'cookie',{'cookie':'unb=1002;_m_h5_tk=member_secret'})
        for user,code in [(self.owner,'OWNER-ONLY'),(self.member,'MEMBER-ONLY')]:
            self.app.action(user,'product',{'item':'2001','name':user['username']+' product'})
            self.app.action(user,'import',{'item':'2001','text':code})
        self.token,self.session=self.app.auth.login('member',PASSWORD)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.app))
        self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def test_paused_start_request_cannot_resume_after_disable(self):
        validated=threading.Event()
        original=self.app.auth.session
        def observed(token):
            user=original(token)
            if token==self.token:validated.set()
            return user
        body=json.dumps({'mode':'observe'}).encode()
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            with patch.object(self.app.auth,'session',side_effect=observed),patch('admin.subprocess.Popen') as spawn:
                conn.putrequest('POST','/api/start')
                for name,value in {'Origin':f'http://127.0.0.1:{self.port}','Content-Type':'application/json',
                                   'Content-Length':str(len(body)),'Cookie':'local_admin_session='+self.token,
                                   'X-CSRF-Token':self.session['csrf']}.items():conn.putheader(name,value)
                conn.endheaders()
                self.assertTrue(validated.wait(2))
                self.app.set_active(self.owner,self.member['id'],False)
                conn.send(body)
                response=conn.getresponse();response.read()
                self.assertEqual(response.status,401)
                spawn.assert_not_called()
        finally:conn.close()

    def test_card_and_settings_apis_ignore_supplied_other_user_identity(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.port)
        try:
            headers={'Cookie':'local_admin_session='+self.token}
            conn.request('GET','/api/card?id=1&user_id='+self.owner['id'],headers=headers)
            response=conn.getresponse();body=json.loads(response.read())
            self.assertEqual(response.status,200);self.assertEqual(body['secret'],'MEMBER-ONLY')
            conn.request('GET','/api/state?user_id='+self.owner['id'],headers=headers)
            response=conn.getresponse();body=json.loads(response.read())
            self.assertEqual(body['seller'],'1002');self.assertEqual(body['user']['username'],'member')
            self.assertNotIn('OWNER-ONLY',json.dumps(body));self.assertNotIn('owner_secret',json.dumps(body))
        finally:conn.close()

    def test_switching_away_does_not_release_seller_history_to_another_user(self):
        self.app.action(self.owner,'cookie',{'cookie':'unb=1003;_m_h5_tk=other_secret'})
        with self.assertRaises(ValueError):self.app.action(self.member,'cookie',{'cookie':'unb=1001;_m_h5_tk=owner_secret'})
        self.app.action(self.owner,'cookie',{'cookie':'unb=1001;_m_h5_tk=owner_secret'})
        self.assertEqual(self.app.manager(self.owner).store.card_value('1001',1),'OWNER-ONLY')
