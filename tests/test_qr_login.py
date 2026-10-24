import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from admin import MultiAdmin
from qr_login import QRLogin, QRLoginError


def response(body=None,text=''):
    value=Mock();value.json.return_value=body;value.text=text;value.is_redirect=False
    return value


class QRTests(unittest.TestCase):
    def flow(self, states, verification=False):
        login=QRLogin()
        session=requests.Session()
        session.cookies.set('unb','1001',domain='.goofish.com')
        session.cookies.set('_m_h5_tk','test_private_token',domain='.goofish.com')
        session.cookies.set('other','excluded',domain='.example.com')
        fake=Mock();fake.__enter__=Mock(return_value=session);fake.__exit__=Mock(return_value=False)
        steps=[response(),response(text='window.viewData = {"loginFormData":{"appName":"xianyu"}};'),
               response({'content':{'success':True,'data':{'t':1,'ck':'private','codeContent':'https://example.com/test-qr'}}})]
        steps += [response({'content':{'data':{'qrCodeStatus':state,'iframeRedirect':verification}}}) for state in states]
        if 'CONFIRMED' in states and not verification:steps.append(response())
        with patch('qr_login.requests.Session',return_value=fake),patch.object(login,'_request',side_effect=steps),patch.object(login.cancelled,'wait',return_value=False):
            login._flow()
        return login

    def test_generate_scan_confirm_keeps_cookie_backend_only(self):
        login=self.flow(['NEW','SCANED','CONFIRMED'])
        self.assertEqual(login.status,'confirmed')
        self.assertIn('unb=1001',login.cookie)
        self.assertNotIn('excluded',login.cookie)
        self.assertNotIn('private_token',str(login.snapshot()))
        self.assertEqual(login.snapshot()['image'],'')
        login.cancel();self.assertFalse(login.cookie)

    def test_confirmed_login_does_not_need_another_bootstrap_request(self):
        login=QRLogin()
        session=requests.Session()
        session.cookies.set('unb','1001',domain='.goofish.com')
        session.cookies.set('_m_h5_tk','private',domain='.goofish.com')
        fake=Mock();fake.__enter__=Mock(return_value=session);fake.__exit__=Mock(return_value=False)
        steps=[response(),response(text='window.viewData = {"loginFormData":{}};'),
               response({'content':{'success':True,'data':{'t':1,'ck':'private','codeContent':'https://example.com/test'}}}),
               response({'content':{'data':{'qrCodeStatus':'CONFIRMED'}}})]
        with patch('qr_login.requests.Session',return_value=fake),patch.object(login,'_request',side_effect=steps) as request:
            login._flow()
        self.assertEqual(login.status,'confirmed');self.assertEqual(request.call_count,4)

    def test_diagnostics_are_static_and_do_not_disclose_response_secrets(self):
        for error,code in [(QRLoginError('missing_account'),'missing_account'),
                           (requests.Timeout('private-token-in-url'),'network_timeout'),
                           (requests.HTTPError('private-response'),'http_error'),
                           (requests.ConnectionError('private-cookie'),'network_error'),
                           (ValueError('private-response'),'invalid_response')]:
            login=QRLogin();login.stage='collect_credentials'
            with patch.object(login,'_flow',side_effect=error),patch('qr_login.SLOTS.release'),self.assertLogs(level='WARNING') as logs:
                login._run()
            self.assertEqual(login.snapshot()['error_code'],code)
            self.assertNotIn('private',str(login.snapshot())+str(logs.output))

    def test_platform_verification_expiry_and_cancel(self):
        for state,status,verify in [('CONFIRMED','verification_required',True),('EXPIRED','expired',False),('CANCELLED','cancelled',False)]:
            login=self.flow([state],verify)
            self.assertEqual(login.status,status);self.assertFalse(login.cookie)
        login=QRLogin();login.cookie='private';login.created-=301
        self.assertEqual(login.snapshot()['status'],'expired');self.assertFalse(login.cookie)
        login=QRLogin();login.cancel();login._update('confirmed',cookie='private')
        self.assertEqual(login.status,'cancelled');self.assertFalse(login.cookie)

    def test_invalid_remote_payloads_fail_without_leaking_secrets(self):
        for body in [response(text='verification'),response(text='window.viewData = {};')]:
            login=QRLogin()
            fake=Mock();fake.__enter__=Mock(return_value=Mock());fake.__exit__=Mock(return_value=False)
            with patch('qr_login.requests.Session',return_value=fake),patch.object(login,'_request',side_effect=[response(),body]),patch('qr_login.SLOTS.release') as release:
                login._run()
            self.assertEqual(login.snapshot()['status'],'error');release.assert_called_once()
        for cookies in [requests.cookies.RequestsCookieJar()]:
            with self.assertRaises(ValueError):QRLogin._cookie_string(Mock(cookies=cookies))

    def test_request_no_redirect_and_limited_slots(self):
        session=Mock();session.request.return_value=response()
        QRLogin._request(session,'GET','https://passport.goofish.com/mini_login.htm')
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        session.request.return_value.is_redirect=True
        with self.assertRaises(ValueError):QRLogin._request(session,'GET','https://passport.goofish.com/')
        with patch('qr_login.SLOTS.acquire',return_value=False):
            with self.assertRaises(ValueError):QRLogin().start()
        with patch('qr_login.SLOTS.acquire',return_value=True),patch('qr_login.SLOTS.release') as release,patch('qr_login.threading.Thread.start',side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):QRLogin().start()
            release.assert_called_once()

    def test_unclaimed_confirmed_cookie_is_erased_without_browser_poll(self):
        login=QRLogin()
        with patch.object(login,'_flow',side_effect=lambda:login._update('confirmed',cookie='private')),patch.object(login.cancelled,'wait',return_value=False),patch('qr_login.SLOTS.release') as release:
            login._run()
        self.assertEqual(login.status,'expired');self.assertFalse(login.cookie)
        release.assert_called_once()


class QRTenantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=MultiAdmin(Path(self.temp.name))
        self.owner=self.app.create('owner','test-password-123')
        self.member=self.app.create('member','test-password-456',self.owner['id'])

    def start(self,user):
        with patch('qr_login.QRLogin.start'):
            return self.app.action(user,'qr-login-start',{})

    def test_per_user_session_save_and_no_browser_cookie(self):
        info=self.start(self.owner)
        with self.assertRaises(ValueError):self.app.action(self.member,'qr-login-status',{'id':info['id']})
        current=self.app.qr_logins[self.owner['id']]
        current._update('confirmed',cookie='unb=1001; _m_h5_tk=private')
        result=self.app.action(self.owner,'qr-login-status',{'id':info['id']})
        self.assertEqual(result['status'],'saved');self.assertNotIn('private',str(result))
        self.assertEqual(self.app.manager(self.owner).seller(),'1001')
        self.assertFalse(self.app.manager(self.member).state()['configured'])
        self.assertFalse(current.cookie)
        self.assertEqual(self.app.action(self.owner,'qr-login-status',{'id':info['id']})['status'],'saved')

    def test_existing_owner_conflict_and_running_service_refuse_update(self):
        self.app.action(self.owner,'cookie',{'cookie':'unb=1001; _m_h5_tk=private'})
        info=self.start(self.member);current=self.app.qr_logins[self.member['id']]
        current._update('confirmed',cookie='unb=1001; _m_h5_tk=private')
        with self.assertRaises(ValueError):self.app.action(self.member,'qr-login-status',{'id':info['id']})
        self.assertFalse(current.cookie)
        info=self.start(self.owner);current=self.app.qr_logins[self.owner['id']]
        current._update('confirmed',cookie='unb=1001; _m_h5_tk=private')
        with patch.object(self.app.manager(self.owner),'running',return_value=True):
            with self.assertRaises(ValueError):self.start(self.owner)
            with self.assertRaises(ValueError):self.app.action(self.owner,'qr-login-status',{'id':info['id']})
        self.assertFalse(current.cookie)

    def test_cancel_disable_stop_and_duplicate_start(self):
        info=self.start(self.member);current=self.app.qr_logins[self.member['id']]
        current._update('waiting')
        self.assertEqual(self.start(self.member)['id'],info['id'])
        self.app.action(self.member,'qr-login-cancel',{'id':info['id']})
        self.assertEqual(current.status,'cancelled')
        self.start(self.member);current=self.app.qr_logins[self.member['id']]
        self.app.set_active(self.owner,self.member['id'],False)
        self.assertTrue(current.cancelled.is_set())
        self.start(self.owner);current=self.app.qr_logins[self.owner['id']]
        self.app.stop();self.assertTrue(current.cancelled.is_set())
