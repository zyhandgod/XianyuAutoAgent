import asyncio
import fcntl
import http.client
import json
import os
import signal
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from admin import Admin, MultiAdmin, handler_for, item_id, real_cards
from card_agent import CardAgent, DeliveryService
from card_orders import PaidOrder, SellerOrders, order_status, parse_paid_order
from card_store import CardStore
from test_cards import raw_order


class ShippingTests(unittest.TestCase):
    def setUp(self):
        self.orders=SellerOrders(Mock())
        self.raw=raw_order()
        self.expected=parse_paid_order(self.raw,0,now=time.time()+86400)

    def test_refund_overrides_paid_status(self):
        self.raw['commonData']['inRefund']=True
        self.assertEqual(order_status(self.raw),'blocked')
        self.assertIsNone(parse_paid_order(self.raw,0))
        self.assertEqual(order_status(None),'unknown')

    def test_confirm_ship_readback_and_exact_payload(self):
        shipped=raw_order(status='待收货')
        with patch.object(self.orders,'lookup',side_effect=[self.raw,shipped]),patch.object(self.orders,'post',return_value={'data':{}}) as post:
            self.assertEqual(self.orders.confirm_shipped(self.expected),'done')
            self.assertEqual(post.call_args.args[0],'mtop.taobao.idle.logistic.consign.dummy')
            self.assertEqual(post.call_args.args[1],{'orderId':'9001','tradeText':'','picList':[],'newUnconsign':True})

    def test_already_shipped_refund_or_wrong_buyer_never_posts(self):
        for raw,result in [(raw_order(status='交易成功'),'done'),(raw_order(status='待收货'),'done'),
                           (raw_order(status='退款中'),'blocked'),(None,'blocked')]:
            with patch.object(self.orders,'lookup',return_value=raw),patch.object(self.orders,'post') as post:
                self.assertEqual(self.orders.confirm_shipped(self.expected),result)
                post.assert_not_called()
        wrong=raw_order();wrong['buyerInfoVO']['buyerId']='9999'
        with patch.object(self.orders,'page',return_value=([wrong],False)):
            self.assertIsNone(self.orders.lookup(self.expected))

    def test_mutation_success_not_enough_without_readback(self):
        with patch.object(self.orders,'lookup',return_value=self.raw),patch.object(self.orders,'post',return_value={'data':{}}):
            self.assertEqual(self.orders.confirm_shipped(self.expected),'pending')
        with patch.object(self.orders,'lookup',side_effect=[self.raw,None]),patch.object(self.orders,'post',return_value={'data':{}}):
            self.assertEqual(self.orders.confirm_shipped(self.expected),'blocked')
        with patch.object(self.orders,'lookup',return_value=self.raw),patch.object(self.orders,'post',return_value={'data':{'success':False}}):
            with self.assertRaises(RuntimeError):self.orders.confirm_shipped(self.expected)


class StoreAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'.env').write_text("COOKIES_STR='unb=1001;_m_h5_tk=test_token'\n")
        self.app=Admin(self.root)
        self.store=self.app.store

    def test_product_disable_and_locked_card_edit(self):
        self.store.import_cards('1001','2001',['REAL-A','REAL-B'])
        self.store.product('1001','2001','月卡',False)
        self.assertFalse(self.store.has_stock('1001','2001'))
        self.assertIsNone(self.store.reserve('1001','9001','2001','3001'))
        self.store.product('1001','2001','月卡',True)
        rows=self.store.list_cards('1001','2001')
        card_id=rows[0]['id']
        self.store.edit_card('1001',card_id,secret='REAL-NEW')
        self.assertEqual(self.store.card_value('1001',card_id),'REAL-NEW')
        self.store.edit_card('1001',card_id,disabled=True)
        self.store.reserve('1001','9001','2001','3001')
        allocated=[r for r in self.store.list_cards('1001','2001') if r['order_id']][0]
        self.assertIn(allocated['secret'], {'REAL-A','REAL-NEW'})
        with self.assertRaises(ValueError):self.store.edit_card('1001',allocated['id'],secret='BAD')
        # Re-enabling a sold card releases its delivery and restores stock.
        with self.store.connect() as db:db.execute('UPDATE cards SET disabled=1 WHERE id=?',(allocated['id'],))
        self.store.edit_card('1001',allocated['id'],disabled=False)
        row=[r for r in self.store.list_cards('1001','2001') if r['id']==allocated['id']][0]
        self.assertEqual(row['disabled'],0)
        self.assertIsNone(row['order_id'])
        self.assertFalse(self.store.delivered('1001','9001'))
        self.assertTrue(self.store.has_stock('1001','2001'))
        self.store.edit_card('1001',card_id,disabled=False)
        self.assertTrue(self.store.has_stock('1001','2001'))

    def test_unlock_action_releases_sent_card_for_reallocation(self):
        self.store.import_cards('1001','2002',['SENT-A'])
        card_id=self.store.list_cards('1001','2002')[0]['id']
        self.assertEqual(self.store.reserve('1001','9100','2002','3001'),'SENT-A')
        self.store.finish('1001','9100','sent',paid_at=123)
        self.app.action('unlock-card',{'id':card_id})
        row=self.store.list_cards('1001','2002')[0]
        self.assertIsNone(row['order_id'])
        self.assertFalse(self.store.delivered('1001','9100'))
        self.assertTrue(self.store.has_stock('1001','2002'))
        self.assertEqual(self.store.reserve('1001','9101','2002','3001'),'SENT-A')

    def test_sent_and_pending_shipping_committed_together(self):
        self.store.import_cards('1001','2001',['REAL-A'])
        self.store.reserve('1001','9001','2001','3001')
        self.store.finish('1001','9001','sent',paid_at=123)
        restarted=CardStore(self.store.path)
        self.assertEqual(restarted.pending_shipments('1001')[0]['paid_at'],123)
        restarted.shipment_attempt('1001','9001')
        self.assertEqual(restarted.pending_shipments('1001')[0]['attempts'],1)
        restarted.shipment_result('1001','9001','blocked')
        self.assertEqual(restarted.pending_shipments('1001'),[])
        restarted.retry_shipping('1001','9001')
        self.assertEqual(restarted.pending_shipments('1001')[0]['attempts'],0)
        restarted.shipment_result('1001','9001','done')
        with self.assertRaises(ValueError):restarted.retry_shipping('1001','9001')
        with self.assertRaises(ValueError):restarted.shipment_result('1001','9001','bad')

    def test_admin_import_edit_and_cookie_never_echo(self):
        self.app.action('product',{'item':'https://www.goofish.com/item?id=2001','name':'月卡'})
        result=self.app.action('import',{'item':'2001','text':'REAL-A\nREAL-B\nREAL-A'})
        self.assertIn('导入 2 条',result['message'])
        card=self.store.list_cards('1001','2001')[0]
        self.app.action('edit-card',{'id':card['id'],'secret':'REAL-NEW'})
        self.app.action('edit-card',{'id':card['id'],'disabled':True})
        state=self.app.state()
        self.assertTrue(state['configured']);self.assertFalse(state['running'])
        self.assertNotIn('test_token',json.dumps(state));self.assertNotIn('REAL-',json.dumps(state))
        self.app.action('cookie',{'cookie':'unb=1002;_m_h5_tk=new_token'})
        self.assertEqual(self.app.state()['products'],[])
        self.assertEqual((self.root/'.env').stat().st_mode&0o777,0o600)

    def test_invalid_inputs_and_live_without_stock_fail(self):
        for value in ['bad','https://evil.example/?id=2001','https://www.goofish.com/item']:
            with self.assertRaises(ValueError):item_id(value)
        self.assertEqual(item_id('2001'),'2001')
        for text in ['', 'MOCK-TEST-123','DEMO-NOT-REAL-A']:
            with self.assertRaises(ValueError):real_cards(text)
        for action,data in [('start',{'mode':'live'}),('start',{'mode':'bad'}),('import',{'item':'2001','text':'REAL-A'}),
                            ('cookie',{'cookie':'bad'}),('unknown',{}),('edit-card',{'id':1,'secret':'A\nB'})]:
            with self.assertRaises(ValueError):self.app.action(action,data)
        (self.root/'.env').unlink()
        self.assertFalse(self.app.state()['configured'])

    def test_owned_start_and_stop_uses_child_env_not_secret_arguments(self):
        child=Mock();child.poll.return_value=None
        with patch('admin.subprocess.Popen',return_value=child) as popen:
            self.app.action('start',{'mode':'observe'})
            self.assertTrue(self.app.running())
            kwargs=popen.call_args.kwargs
            self.assertEqual(kwargs['env']['CARD_DRY_RUN'],'true')
            self.assertEqual(kwargs['env']['CARD_ADMIN_PID'],str(os.getpid()))
            self.assertNotIn('COOKIES_STR',kwargs['env'])
            with self.assertRaises(ValueError):self.app.action('cookie',{'cookie':'unb=1002;_m_h5_tk=x'})
            with self.assertRaises(ValueError):self.app.start('observe')
            self.app.stop();child.send_signal.assert_called_once_with(signal.SIGINT)
            child.poll.return_value=0

    def test_external_lock_is_not_reported_stopped_or_overwritten(self):
        path=self.root/'data/agent-1001.lock'
        with path.open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertTrue(self.app.state()['running'])
            self.assertEqual(self.app.state()['mode'],'external')
            for action,data in [('start',{'mode':'observe'}),('stop',{}),('cookie',{'cookie':'unb=1002;_m_h5_tk=x'})]:
                with self.assertRaises(ValueError):self.app.action(action,data)
            self.app.stop(owned_only=True)
        self.assertFalse(self.app.running())

    def test_logs_cookie_creation_live_start_and_stop_timeout(self):
        import subprocess
        self.app.log_path.write_text('ready\n'*100)
        self.assertEqual(len(self.app.state()['logs']),60)
        (self.root/'.env').unlink()
        self.app.action('cookie',{'cookie':'unb=1001;_m_h5_tk=new_token'})
        self.store.import_cards('1001','2001',['REAL-A'])
        child=Mock();child.poll.return_value=None
        with patch('admin.subprocess.Popen',return_value=child) as popen:
            self.app.start('live')
            self.assertEqual(popen.call_args.kwargs['env']['CARD_DRY_RUN'],'false')
            child.wait.side_effect=[subprocess.TimeoutExpired('child',30),0]
            self.app.stop();child.terminate.assert_called_once()
        child.poll.return_value=0
        self.store.reserve('1001','9001','2001','3001');self.store.finish('1001','9001','sent',paid_at=123)
        self.store.shipment_result('1001','9001','blocked')
        self.app.action('retry-shipping',{'order':'9001'})

    def test_chat_mock_uses_explicit_command_without_requiring_real_stock(self):
        child=Mock();child.poll.return_value=None
        with patch('admin.subprocess.Popen',return_value=child) as popen:
            self.app.start('mock')
            self.assertEqual(popen.call_args.args[0][-1],'mock-chat')
            self.assertEqual(popen.call_args.kwargs['env']['CARD_DRY_RUN'],'true')
            self.assertEqual(popen.call_args.kwargs['env']['CARD_WORKSPACE'],str(self.app.workspace.resolve()))
            self.assertEqual(self.app.state()['mode'],'mock')
            with self.assertRaises(ValueError):self.app.start('live')
            self.app.stop()
        child.poll.return_value=0
        self.assertEqual(self.store.report('1001')['deliveries'],[])
        with self.assertRaises(ValueError):self.app.start('live')

    def test_admin_main_handles_shutdown_without_running_delivery(self):
        import admin
        server=Mock();server.server_address=('127.0.0.1',8765)
        server.serve_forever.side_effect=KeyboardInterrupt
        with patch('sys.argv',['admin.py']),patch('admin.MultiAdmin',return_value=self.app), \
             patch('admin.ThreadingHTTPServer',return_value=server),patch('admin.atexit.register'), \
             patch('admin.signal.signal'),patch('builtins.print'):
            admin.main()
        server.server_close.assert_called_once()


class ShippingWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_pending_shipping_after_restart_never_resends_card(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CardStore(Path(folder)/'cards.db')
            store.import_cards('1001','2001',['REAL-A'])
            store.reserve('1001','9001','2001','3001');store.finish('1001','9001','sent',paid_at=123)
            agent=CardAgent('unb=1001;_m_h5_tk=token',store,dry_run=True)
            agent.orders.confirm_shipped=Mock(return_value='done')
            await agent.finish_shipments();agent.orders.confirm_shipped.assert_not_called()
            agent.service.dry_run=False;agent.send_card=AsyncMock()
            agent.orders.confirm_shipped.side_effect=TimeoutError
            await agent.finish_shipments()
            self.assertEqual(store.pending_shipments('1001')[0]['attempts'],1)
            agent.orders.confirm_shipped.side_effect=None
            await agent.finish_shipments()
            self.assertEqual(store.report('1001')['shipments'][0]['state'],'done')
            agent.send_card.assert_not_awaited()

    async def test_dead_admin_owner_cancels_delivery_and_leaves_no_task(self):
        from cards import run_managed
        agent=Mock();agent.run=AsyncMock(side_effect=lambda:None)
        with patch.dict('os.environ',{'CARD_ADMIN_PID':'100'}),patch('cards.os.getppid',return_value=101):
            await run_managed(agent)
        with patch.dict('os.environ',{'CARD_ADMIN_PID':'bad'}),self.assertRaises(ValueError):
            await run_managed(agent)

    async def test_shipping_readback_states_and_maximum_attempts(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CardStore(Path(folder)/'cards.db');store.import_cards('1001','2001',['REAL-A'])
            store.reserve('1001','9001','2001','3001');store.finish('1001','9001','sent',paid_at=123)
            agent=CardAgent('unb=1001;_m_h5_tk=token',store,dry_run=False)
            for result in ['pending','blocked']:
                agent.orders.confirm_shipped=Mock(return_value=result)
                await agent.finish_shipments()
                self.assertEqual(store.report('1001')['shipments'][0]['state'],result)
            store.retry_shipping('1001','9001')
            for _ in range(5):store.shipment_attempt('1001','9001')
            await agent.finish_shipments()
            self.assertEqual(store.report('1001')['shipments'][0]['state'],'blocked')


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        (cls.root/'.env').write_text("COOKIES_STR='unb=1001;_m_h5_tk=test_token'")
        (cls.root/'admin_static').mkdir();(cls.root/'admin_static/index.html').write_text('<h1>管理后台</h1>')
        cls.platform=MultiAdmin(cls.root)
        cls.user=cls.platform.create('test-owner','test-password-long')
        cls.session_token,cls.session=cls.platform.auth.login('test-owner','test-password-long')
        cls.app=cls.platform.manager(cls.user)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(cls.platform))
        cls.port=cls.server.server_address[1]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()

    def request(self,method,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port)
        try:
            connection.request(method,path,body=body,headers={'Cookie':'local_admin_session='+self.session_token,**(headers or {})})
            response=connection.getresponse();return response.status,response.read(),dict(response.getheaders())
        finally:connection.close()

    def test_state_and_static_do_not_expose_secret_files(self):
        status,body,headers=self.request('GET','/api/state')
        self.assertEqual(status,200);self.assertNotIn(b'test_token',body)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(self.request('GET','/.env')[0],404)
        self.assertEqual(self.request('GET','/')[0],200)
        self.assertEqual(self.request('GET','/api/state',headers={'Host':'evil.example'})[0],403)

    def test_mutations_require_both_origin_and_csrf(self):
        body=json.dumps({'item':'2001','name':'月卡'})
        self.assertEqual(self.request('POST','/api/product',body)[0],403)
        valid={'Origin':f'http://127.0.0.1:{self.port}','X-CSRF-Token':self.session['csrf'],'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/product',body,{**valid,'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/product',body,valid)[0],200)
        self.assertEqual(self.request('POST','/api/product','[]',valid)[0],400)

    def test_card_routes_and_error_statuses(self):
        self.app.store.import_cards('1001','2001',['REAL-A','REAL-B'])
        self.assertEqual(self.request('GET','/api/cards?item=2001')[0],200)
        card=self.app.store.list_cards('1001','2001')[0]['id']
        self.assertEqual(self.request('GET','/api/card?id='+str(card))[0],200)
        self.assertEqual(self.request('GET','/api/card?id=bad')[0],400)
        valid={'Origin':f'http://127.0.0.1:{self.port}','X-CSRF-Token':self.session['csrf'],'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/unknown','{}',valid)[0],400)
        self.assertEqual(self.request('POST','/missing','{}',valid)[0],404)
        self.assertEqual(self.request('POST','/api/product','',valid)[0],400)
        self.assertEqual(self.request('POST','/api/product','{}',{**valid,'Content-Type':'text/plain'})[0],400)
        self.assertEqual(self.request('POST','/api/edit-card',json.dumps({'id':card,'secret':'REAL-A'}),valid)[0],400)
