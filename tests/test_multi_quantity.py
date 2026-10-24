import asyncio
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock,Mock

from card_agent import CardAgent,DeliveryService
from card_mock import MockCardAgent,mock_triggers
from card_orders import PaidOrder,SellerOrders,parse_paid_order
from card_store import CardStore
from test_cards import raw_order
from test_mock_cards import envelope


class QuantityTests(unittest.TestCase):
    def test_only_trusted_buy_num_positive_integer_is_accepted(self):
        for value in (2,'2',100,'100'):
            raw=raw_order();raw['priceVO']['buyNum']=value
            self.assertEqual(parse_paid_order(raw,0).quantity,int(value))
        for value in (None,0,-1,True,2.0,'2.0','2x','',101,' 2','٢'):
            raw=raw_order();raw['priceVO']['buyNum']=value
            self.assertIsNone(parse_paid_order(raw,0))
        raw=raw_order();raw.pop('priceVO');raw['commonData']['quantity']=2
        self.assertIsNone(parse_paid_order(raw,0))

    def test_verification_and_shipping_reject_changed_quantity(self):
        raw=raw_order();raw['priceVO']['buyNum']='2'
        expected=parse_paid_order(raw,0)
        api=SellerOrders(Mock());api.page=Mock(return_value=([raw],False));api.post=Mock()
        self.assertTrue(api.verify(expected,0))
        raw['priceVO']['buyNum']='1'
        self.assertFalse(api.verify(expected,0))
        self.assertEqual(api.confirm_shipped(expected),'blocked');api.post.assert_not_called()


class BatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=CardStore(Path(self.temp.name)/'cards.db')
        self.store.import_cards('1001','2001',['A','B','C'])

    def test_atomic_shortage_and_all_secondary_allocations_are_locked(self):
        self.assertIsNone(self.store.reserve_many('1001','9001','2001','3001',4))
        self.assertEqual(self.store.report('1001')['deliveries'],[])
        self.assertEqual(self.store.reserve_many('1001','9001','2001','3001',2),['A','B'])
        self.assertEqual(self.store.report('1001')['inventory'][0]['available'],1)
        self.assertEqual(self.store.report('1001')['deliveries'][0]['quantity'],2)
        self.assertEqual(self.store.reveal('1001','9001'),'A\nB')
        rows=self.store.list_cards('1001','2001')
        for row in rows:
            if row['order_id']:
                with self.assertRaises(ValueError):self.store.edit_card('1001',row['id'],secret='OTHER')
                with self.assertRaises(ValueError):self.store.delete_cards('1001','2001',[row['id']])
        self.assertEqual(self.store.delete_cards('1001','2001',all_unused=True),1)
        self.assertEqual(self.store.import_cards('1001','2001',['B'])['duplicates'],1)

    def test_reenable_one_card_releases_complete_multi_card_order(self):
        self.assertEqual(self.store.reserve_many('1001','9001','2001','3001',2),['A','B'])
        allocated={row['secret']:row for row in self.store.list_cards('1001','2001') if row['order_id']}
        self.assertEqual(set(allocated),{'A','B'})
        self.store.finish('1001','9001','sent',paid_at=123)
        self.store.edit_card('1001',allocated['A']['id'],disabled=False)
        self.assertFalse(self.store.delivered('1001','9001'))
        self.assertEqual(self.store.pending_shipments('1001'),[])
        self.assertTrue(self.store.has_stock('1001','2001',3))
        self.assertTrue(all(row['order_id'] is None and row['disabled']==0
                            for row in self.store.list_cards('1001','2001')))
        self.assertEqual(self.store.reserve_many('1001','9002','2001','3001',3),['A','B','C'])

    def test_concurrent_orders_cannot_split_or_share_stock(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda i:self.store.reserve_many('1001',str(9000+i),'2001','3001',2),range(4)))
        self.assertEqual(sum(value is not None for value in results),1)
        self.assertEqual(self.store.report('1001')['inventory'][0]['available'],1)
        restarted=CardStore(self.store.path)
        row=restarted.report('1001')['deliveries'][0]
        self.assertIsNone(restarted.reserve_many('1001',row['order_id'],'2001','3001',2))
        self.assertEqual(row['quantity'],2)

    def test_legacy_migration_preserves_history(self):
        path=Path(self.temp.name)/'legacy.db'
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE cards(id INTEGER PRIMARY KEY,seller TEXT,item TEXT,secret TEXT,UNIQUE(seller,secret));
                CREATE TABLE deliveries(seller TEXT,order_id TEXT,item TEXT,buyer TEXT,card_id INTEGER UNIQUE,state TEXT,updated REAL,PRIMARY KEY(seller,order_id));
                INSERT INTO cards VALUES(1,'1001','2001','OLD');
                INSERT INTO deliveries VALUES('1001','9000','2001','3001',1,'sent',1);''')
        legacy=CardStore(path);CardStore(path)
        self.assertEqual(legacy.report('1001')['deliveries'][0]['quantity'],1)
        self.assertFalse(legacy.has_stock('1001','2001'))
        self.assertEqual(legacy.reveal('1001','9000'),'OLD')


class MultiDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=CardStore(Path(self.temp.name)/'cards.db')
        self.store.import_cards('1001','2001',['A','B','C'])
        self.order=PaidOrder('9001','2001','3001',time.time(),2)
        self.transport=AsyncMock();self.transport.create_chat.return_value='7001@goofish';self.transport.verify.return_value=True
        self.service=DeliveryService(self.store,'1001',None,dry_run=False,template='教程：xxx\n卡密：{card}\n谢谢')

    async def test_one_message_for_two_cards_with_one_shipping_job(self):
        self.assertEqual(await self.service.deliver(self.order,self.transport),'sent')
        self.transport.send_card.assert_awaited_once()
        text=self.transport.send_card.call_args.args[2]
        self.assertEqual(text,'教程：xxx\n卡密：\n1. A\n2. B\n谢谢')
        self.assertEqual(self.store.pending_shipments('1001')[0]['quantity'],2)
        agent=CardAgent('unb=1001;_m_h5_tk=example',self.store,dry_run=False)
        agent.orders.confirm_shipped=Mock(return_value='done')
        await agent.finish_shipments()
        self.assertEqual(agent.orders.confirm_shipped.call_args.args[0].quantity,2)
        self.assertEqual(await self.service.deliver(self.order,self.transport),'skipped')
        self.transport.send_card.assert_awaited_once()

    async def test_shortage_and_message_limit_allocate_nothing(self):
        order=PaidOrder('9002','2001','3001',time.time(),4)
        self.assertEqual(await self.service.deliver(order,self.transport),'no_stock')
        self.transport.create_chat.assert_not_awaited()
        self.service.template='x'*19999+'{card}'
        with self.assertRaises(RuntimeError):await self.service.deliver(self.order,self.transport)
        self.assertEqual(self.store.report('1001')['deliveries'],[])
        self.assertEqual(self.store.report('1001')['inventory'][0]['available'],3)
        self.transport.send_card.assert_not_awaited()

    async def test_uncertain_batch_is_never_resent_or_reused(self):
        self.transport.send_card.side_effect=TimeoutError
        with self.assertRaises(TimeoutError):await self.service.deliver(self.order,self.transport)
        self.assertEqual(self.store.report('1001')['deliveries'][0]['state'],'uncertain')
        restarted=DeliveryService(CardStore(self.store.path),'1001',None,dry_run=False)
        self.assertEqual(await restarted.deliver(self.order,self.transport),'skipped')
        self.assertEqual(self.store.report('1001')['inventory'][0]['available'],1)
        self.assertEqual(self.store.pending_shipments('1001'),[])

    async def test_mock_quantity_two_one_message_and_separate_inventory(self):
        mock=MockCardAgent('unb=1001;_m_h5_tk=example',CardStore(Path(self.temp.name)/'mock.db'))
        mock.send_card=AsyncMock()
        trigger=mock_triggers(envelope(text='已支付 2'),'1001',0)[0]
        self.assertEqual(trigger['quantity'],2)
        self.assertEqual(await mock.respond(trigger),'sent')
        mock.send_card.assert_awaited_once()
        self.assertEqual(mock.send_card.call_args.args[2].count('MOCK-TEST-'),2)
        self.assertEqual(await mock.respond(trigger),'duplicate')
        self.assertEqual(self.store.report('1001')['inventory'][0]['available'],3)
        self.assertEqual(mock.service.store.pending_shipments('1001'),[])
