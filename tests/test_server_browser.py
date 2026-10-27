import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from admin import MultiAdmin
from server_browser import browser_input


class BrowserInputTests(unittest.TestCase):
    def test_live_pointer_events_are_bounded_and_ordered(self):
        events=[{'type':'down','x':1,'y':2},{'type':'move','x':20,'y':2},{'type':'up','x':30,'y':2}]
        self.assertEqual(browser_input({'kind':'pointer-events','events':events})['events'],events)
        for events in [[],[{'type':'click','x':1,'y':1}],[{'type':'move','x':True,'y':1}],
                       [{'type':'down','x':1100,'y':1}],[{'type':'down','x':1,'y':-1}]]:
            with self.assertRaises(ValueError):browser_input({'kind':'pointer-events','events':events})

    def test_manual_input_bounds(self):
        self.assertEqual(browser_input({'kind':'pointer','points':[[0,0],[1099,779]]})['points'],[[0,0],[1099,779]])
        for data in [{'kind':'pointer','points':[]},{'kind':'pointer','points':[[1100,1]]},
                     {'kind':'pointer','points':[[True,1]]},{'kind':'key','key':'F12'},
                     {'kind':'wheel','delta':10000},{'kind':'text','text':'a'*129},{'kind':'navigate','url':'http://localhost'}]:
            with self.assertRaises(ValueError):browser_input(data)


class BrowserTenantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=MultiAdmin(Path(self.temp.name))
        self.owner=self.app.create('owner','temporary-password-123')
        self.member=self.app.create('member','temporary-password-456',self.owner['id'])
        self.session=Mock(id='private-session',closed=False)
        self.session.process.is_alive.return_value=True
        import time
        self.session.created=time.monotonic()
        self.session.exchange.return_value={'cookie':'unb=1001; _m_h5_tk=PRIVATE'}
        self.app.server_browsers[self.owner['id']]=self.session

    def test_other_user_cannot_control_or_export(self):
        for action in ['browser-status','browser-save','browser-input','browser-close']:
            with self.assertRaises(ValueError):self.app.action(self.member,action,{'id':'private-session'})
        self.session.exchange.assert_not_called()

    def test_save_binds_seller_without_returning_cookie(self):
        with patch('login_check.check_login',return_value={'status':'ok','message':'ok'}):
            result=self.app.action(self.owner,'browser-save',{'id':'private-session'})
        self.assertNotIn('PRIVATE',str(result))
        self.assertTrue(result['saved'])
        self.assertEqual(self.app.manager(self.owner).seller(),'1001')
        with self.assertRaises(ValueError):self.app.action(self.member,'cookie',{'cookie':'unb=1001; _m_h5_tk=OTHER'})

    def test_seller_bound_to_another_user_is_not_overwritten(self):
        self.app.action(self.member,'cookie',{'cookie':'unb=1001; _m_h5_tk=OTHER'})
        with self.assertRaises(ValueError):self.app.action(self.owner,'browser-save',{'id':'private-session'})
        self.assertFalse((self.app.manager(self.owner).workspace/'.env').exists())

    def test_close_and_disable_cleanup(self):
        result=self.app.action(self.owner,'browser-close',{'id':'private-session'})
        self.assertEqual(result['status'],'closed');self.session.close.assert_called_once()
        self.assertNotIn(self.owner['id'],self.app.server_browsers)
        self.app.server_browsers[self.member['id']]=self.session
        self.app.set_active(self.owner,self.member['id'],False)
        self.assertNotIn(self.member['id'],self.app.server_browsers)

    def test_capacity_and_running_guards(self):
        with self.assertRaises(ValueError):self.app.action(self.member,'browser-start',{})
        with patch.object(self.app.manager(self.owner),'running',return_value=True):
            with self.assertRaises(ValueError):self.app.action(self.owner,'browser-start',{})
            with self.assertRaises(ValueError):self.app.action(self.owner,'browser-save',{'id':'private-session'})
