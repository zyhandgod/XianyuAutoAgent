import asyncio
import json
import sqlite3
import tempfile
import time
import threading
import unittest
import contextlib
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from requests.cookies import RequestsCookieJar

from card_agent import CardAgent, DeliveryService
from card_orders import PaidOrder, SellerOrders, merge_response_cookies, parse_paid_order
from card_store import CardStore


SELLER, ITEM, BUYER = '1001', '2001', '3001'


def raw_order(order_id='9001', status='待发货'):
    return {'commonData': {'orderId': order_id, 'itemId': ITEM,
                          'orderStatus': status, 'paySuccessTime': '2026-10-17 15:00:00'},
            'buyerInfoVO': {'buyerId': BUYER}, 'priceVO': {'buyNum': '1'}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CardStore(Path(self.temp.name) / 'cards.db')

    def test_import_deduplicates_including_across_items(self):
        self.assertEqual(self.store.import_cards(SELLER, ITEM, [' A ', 'B', 'A', '']),
                         {'added': 2, 'duplicates': 1})
        self.assertEqual(self.store.import_cards(SELLER, '2002', ['A'])['added'], 0)
        self.assertEqual(self.store.report(SELLER)['inventory'][0]['available'], 2)
        self.assertNotIn('secret', json.dumps(self.store.report(SELLER)))

    def test_invalid_import_is_atomic(self):
        for seller, item, lines in [('x', ITEM, ['A']), (SELLER, 'x', ['A']),
                                     (SELLER, ITEM, ['A', 'x' * 2001])]:
            with self.assertRaises(ValueError):
                self.store.import_cards(seller, item, lines)
        self.assertEqual(self.store.report(SELLER)['inventory'], [])

    def test_cutoff_survives_restart(self):
        start = self.store.start_at(SELLER)
        other = CardStore(self.store.path)
        self.assertEqual(start, other.start_at(SELLER))
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_reservation_restarts_and_states(self):
        self.store.import_cards(SELLER, ITEM, ['A', 'B'])
        self.assertEqual(self.store.reserve(SELLER, '9001', ITEM, BUYER), 'A')
        restarted = CardStore(self.store.path)
        self.assertTrue(restarted.delivered(SELLER, '9001'))
        self.assertIsNone(restarted.reserve(SELLER, '9001', ITEM, BUYER))
        self.store.finish(SELLER, '9001', 'uncertain')
        self.assertEqual(self.store.reveal(SELLER, '9001'), 'A')
        self.store.confirm_sent(SELLER, '9001')
        self.assertEqual(self.store.report(SELLER)['deliveries'][0]['state'], 'sent')
        self.assertEqual(self.store.reserve(SELLER, '9002', ITEM, BUYER), 'B')
        self.store.finish(SELLER, '9002', 'sent')
        self.assertIsNone(self.store.reserve(SELLER, '9003', ITEM, BUYER))
        self.assertFalse(self.store.has_stock(SELLER, ITEM))
        for fn in [lambda: self.store.finish(SELLER, '9001', 'free'),
                   lambda: self.store.reveal(SELLER, 'missing'),
                   lambda: self.store.confirm_sent(SELLER, 'missing')]:
            with self.assertRaises(ValueError):
                fn()

    def test_product_and_account_isolation(self):
        self.store.import_cards(SELLER, ITEM, ['A'])
        self.assertIsNone(self.store.reserve(SELLER, '9001', '2002', BUYER))
        self.assertIsNone(self.store.reserve('1002', '9001', ITEM, BUYER))

    def test_concurrent_same_order_only_one_wins(self):
        self.store.import_cards(SELLER, ITEM, ['A', 'B', 'C'])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: CardStore(self.store.path).reserve(SELLER, '9001', ITEM, BUYER), range(16)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_concurrent_different_orders_no_reused_card(self):
        self.store.import_cards(SELLER, ITEM, ['A', 'B', 'C'])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: CardStore(self.store.path).reserve(SELLER, str(9000 + n), ITEM, BUYER), range(16)))
        assigned = [r for r in results if r is not None]
        self.assertEqual(len(assigned), 3)
        self.assertEqual(len(set(assigned)), 3)


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.paid = datetime(2026, 10, 17, 15, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

    def test_exact_paid_status_time_and_ids(self):
        self.assertIsNotNone(parse_paid_order(raw_order(), self.paid - 10, now=self.paid))
        for status in ['待付款', '交易关闭', '退款中', '已发货', '我已付款', 'WAIT_SELLER_SEND_GOODS']:
            self.assertIsNone(parse_paid_order(raw_order(status=status), 0, now=self.paid))
        self.assertIsNone(parse_paid_order(raw_order(), self.paid + 1, now=self.paid))
        self.assertIsNone(parse_paid_order(raw_order(), 0, now=self.paid - 61))
        for bad in [None, {}, {'1': {'10': {'reminderContent': '我已付款'}}}]:
            self.assertIsNone(parse_paid_order(bad, 0))
        for field in ['orderId', 'itemId', 'paySuccessTime']:
            raw = raw_order()
            raw['commonData'].pop(field)
            self.assertIsNone(parse_paid_order(raw, 0, now=self.paid))
        raw = raw_order()
        raw['buyerInfoVO']['buyerId'] = 'wrong'
        self.assertIsNone(parse_paid_order(raw, 0, now=self.paid))

    def api(self, response):
        api = Mock()
        api.session.cookies.get.return_value = 'token_123'
        api.session.post.return_value.json.return_value = response
        return api

    def test_signed_order_request_and_schema(self):
        api = self.api({'ret': ['SUCCESS::调用成功'], 'data': {'module': {'items': [], 'nextPage': False}}})
        orders = SellerOrders(api)
        self.assertEqual(orders.page(order_id='9001'), ([], False))
        kwargs = api.session.post.call_args.kwargs
        self.assertEqual(json.loads(kwargs['data']['data'])['orderIds'], '9001')
        self.assertEqual(kwargs['timeout'], 25)
        self.assertTrue(kwargs['params']['sign'])
        api.session.cookies.get.return_value = ''
        with self.assertRaisesRegex(RuntimeError, '缺少'):
            orders.page()

    def test_cookie_rotation_does_not_leave_conflicting_scopes(self):
        session = Mock()
        session.cookies = RequestsCookieJar()
        session.cookies.set('_m_h5_tk', 'old_123')
        session.cookies.set('_m_h5_tk', 'refreshed_456', domain='.goofish.com', path='/')
        response = Mock()
        response.cookies = RequestsCookieJar()
        response.cookies.set('_m_h5_tk', 'refreshed_456', domain='.goofish.com', path='/')
        merge_response_cookies(session, response)
        self.assertEqual(session.cookies.get('_m_h5_tk'), 'refreshed_456')
        response.cookies.set('_m_h5_tk', 'hostonly_789', domain='h5api.m.goofish.com', path='/')
        session.cookies.set('_m_h5_tk', 'hostonly_789', domain='h5api.m.goofish.com', path='/')
        merge_response_cookies(session, response)
        self.assertEqual(session.cookies.get('_m_h5_tk'), 'hostonly_789')

    def test_reject_api_failure_or_unknown_schema(self):
        for body in [{'ret': ['FAIL::TOKEN_EXPIRED']}, {'ret': ['SUCCESS::调用成功'], 'data': {}}, None]:
            with self.subTest(body=body), self.assertRaises(RuntimeError):
                SellerOrders(self.api(body)).page()

    def test_recent_pagination_dedupe_and_limit(self):
        orders = SellerOrders(Mock())
        with patch.object(orders, 'page', side_effect=[([raw_order()], True), ([raw_order()], False)]), patch('card_orders.time.time', return_value=self.paid):
            self.assertEqual(len(orders.recent(0)), 1)
        with patch.object(orders, 'page', return_value=([], True)), self.assertRaisesRegex(RuntimeError, '100'):
            orders.recent(0)

    def test_verification_rejects_changed_or_missing_order(self):
        orders = SellerOrders(Mock())
        expected = parse_paid_order(raw_order(), 0, now=self.paid)
        for rows, valid in [([raw_order()], True), ([], False), ([raw_order(status='退款中')], False),
                            ([raw_order('9002')], False), ([raw_order(), raw_order()], False),
                            ([raw_order(), raw_order(status='退款中')], False)]:
            with patch.object(orders, 'page', return_value=(rows, False)), patch('card_orders.time.time', return_value=self.paid):
                self.assertEqual(orders.verify(expected, 0), valid)


class FakeTransport:
    def __init__(self, valid=True, fail=False):
        self.valid, self.fail = valid, fail
        self.sent = []
        self.chats = []

    async def create_chat(self, buyer, item):
        self.chats.append((buyer, item))
        return '7001@goofish'

    async def verify(self, order, since):
        return self.valid

    async def send_card(self, cid, buyer, text):
        self.sent.append((cid, buyer, text))
        if self.fail:
            raise TimeoutError('simulated lost acknowledgment')


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CardStore(Path(self.temp.name) / 'cards.db')
        self.store.import_cards(SELLER, ITEM, ['TEST-CARD-A', 'TEST-CARD-B'])
        self.order = PaidOrder('9001', ITEM, BUYER, time.time())

    async def test_dry_run_never_opens_chat_allocates_or_sends(self):
        transport = FakeTransport()
        service = DeliveryService(self.store, SELLER, None)
        self.assertEqual(await service.deliver(self.order, transport), 'dry_run')
        self.assertEqual(transport.sent, [])
        self.assertEqual(transport.chats, [])
        self.assertFalse(self.store.delivered(SELLER, '9001'))
        self.assertEqual(self.store.report(SELLER)['inventory'][0]['available'], 2)

    async def test_duplicate_delivery_and_restart(self):
        transport = FakeTransport()
        service = DeliveryService(self.store, SELLER, None, dry_run=False)
        self.assertEqual(await service.deliver(self.order, transport), 'sent')
        restarted = DeliveryService(CardStore(self.store.path), SELLER, None, dry_run=False)
        self.assertEqual(await restarted.deliver(self.order, transport), 'skipped')
        self.assertEqual(len(transport.sent), 1)
        self.assertIn('TEST-CARD-A', transport.sent[0][2])
        self.assertNotIn('TEST-CARD-B', transport.sent[0][2])
        self.assertEqual(self.store.report(SELLER)['deliveries'][0]['state'], 'sent')

    async def test_failure_after_send_never_reallocates(self):
        transport = FakeTransport(fail=True)
        service = DeliveryService(self.store, SELLER, None, dry_run=False)
        with self.assertRaises(TimeoutError):
            await service.deliver(self.order, transport)
        self.assertEqual(self.store.report(SELLER)['deliveries'][0]['state'], 'uncertain')
        self.assertEqual(await service.deliver(self.order, transport), 'skipped')
        self.assertEqual(self.store.reveal(SELLER, '9001'), 'TEST-CARD-A')
        self.assertEqual(self.store.report(SELLER)['inventory'][0]['available'], 1)

    async def test_failed_verification_allocates_nothing(self):
        transport = FakeTransport(valid=False)
        service = DeliveryService(self.store, SELLER, None, dry_run=False)
        self.assertEqual(await service.deliver(self.order, transport), 'unverified')
        self.assertFalse(self.store.delivered(SELLER, '9001'))
        self.assertEqual(transport.sent, [])

    async def test_missing_inventory_and_seller_as_buyer(self):
        service = DeliveryService(self.store, SELLER, None, dry_run=False)
        self.assertEqual(await service.deliver(PaidOrder('9002', '2002', BUYER, time.time()), FakeTransport()), 'no_stock')
        self.assertEqual(await service.deliver(PaidOrder('9003', ITEM, SELLER, time.time()), FakeTransport()), 'skipped')
        with self.assertRaises(ValueError):
            DeliveryService(self.store, SELLER, None, template='missing placeholder')

    async def test_cancellation_during_send_keeps_original_card(self):
        from unittest.mock import AsyncMock
        transport = FakeTransport()
        transport.send_card = AsyncMock(side_effect=asyncio.CancelledError)
        service = DeliveryService(self.store, SELLER, None, dry_run=False)
        with self.assertRaises(asyncio.CancelledError):
            await service.deliver(self.order, transport)
        self.assertEqual(self.store.report(SELLER)['deliveries'][0]['state'], 'uncertain')
        self.assertEqual(await service.deliver(self.order, FakeTransport()), 'skipped')


class FakeWebsocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
        self.code = 200

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return json.dumps(message)

    async def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        if request.get('lwp') in {'/reg', '/r/SyncStatus/ackDiff'}:
            # Real startup must not wait for replies to these one-way messages.
            return
        if 'lwp' in request:
            body = {'singleChatConversation': {'cid': '7001@goofish'}} if request['lwp'].endswith('/create') else {}
            await self.messages.put({'headers': {'mid': request['headers']['mid']}, 'code': self.code, 'body': body})


class RpcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CardStore(Path(self.temp.name) / 'cards.db')
        self.agent = CardAgent(f'unb={SELLER}; _m_h5_tk=token_123', self.store)
        self.ws = FakeWebsocket()
        self.agent.ws = self.ws

    async def test_rpc_ack_create_and_send_payload(self):
        reader = asyncio.create_task(self.agent.read_messages())
        try:
            cid = await self.agent.create_chat(BUYER, ITEM)
            self.assertEqual(cid, '7001@goofish')
            await self.agent.send_card(cid, BUYER, 'TEST-CARD')
            self.assertEqual(self.ws.sent[-1]['body'][1]['actualReceivers'], [BUYER + '@goofish', SELLER + '@goofish'])
            self.assertEqual(self.agent.pending, {})
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def test_rejected_send_and_unrelated_chat(self):
        reader = asyncio.create_task(self.agent.read_messages())
        self.ws.code = 500
        try:
            with self.assertRaises(RuntimeError):
                await self.agent.send_card('7001@goofish', BUYER, 'TEST-CARD')
            await self.ws.messages.put({'headers': {'mid': 'buyer-chat'}, 'body': {'text': '我已付款'}})
            await asyncio.sleep(0.01)
            self.assertEqual(self.ws.sent[-1]['code'], 200)
            self.assertEqual(self.store.report(SELLER)['deliveries'], [])
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def test_disconnect_fails_pending_request(self):
        future = asyncio.get_running_loop().create_future()
        self.agent.pending['inflight'] = future
        await self.ws.messages.put(None)
        await self.agent.read_messages()
        with self.assertRaises(ConnectionError):
            await future

    async def test_bad_cookie_and_conversation_fail_closed(self):
        for cookie in ['', 'unb=x; _m_h5_tk=token']:
            with self.assertRaises(ValueError):
                CardAgent(cookie, self.store)
        from unittest.mock import AsyncMock
        with patch.object(self.agent, 'rpc', new=AsyncMock(return_value={'code': 200, 'body': {}})):
            with self.assertRaises(RuntimeError):
                await self.agent.create_chat(BUYER, ITEM)

    async def test_auth_is_bounded_noninteractive(self):
        response = Mock()
        response.json.return_value = {'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': 'TEST-TOKEN'}}
        with patch.object(self.agent.api.session, 'post', return_value=response) as post:
            self.assertEqual(self.agent.login_token(), 'TEST-TOKEN')
            self.assertEqual(post.call_args.kwargs['timeout'], 25)
            for result in [None, {'ret': ['FAIL::TOKEN_EXPIRED']}, {'ret': ['SUCCESS::调用成功'], 'data': {}}]:
                response.json.return_value = result
                with self.assertRaises(RuntimeError):
                    self.agent.login_token()

    async def test_cancelled_thread_holds_session_lock_until_finished(self):
        started, release = threading.Event(), threading.Event()
        def blocking_request():
            started.set()
            release.wait(timeout=2)
            return 1
        task = asyncio.create_task(self.agent.api_call(blocking_request))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertTrue(self.agent.api_lock.locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.agent.api_lock.locked())

    async def test_registration_sync_and_cleanup(self):
        from unittest.mock import AsyncMock
        class Connection:
            async def __aenter__(inner):
                return self.ws
            async def __aexit__(inner, *args):
                return False
        async def rotate_now():
            await asyncio.sleep(0.01)
        with patch.object(self.agent, 'login_token', return_value='TEST-TOKEN'), \
             patch('card_agent.websockets.connect', return_value=Connection()), \
             patch.object(self.agent, 'rotate', side_effect=rotate_now), \
             patch.object(self.agent, 'poll_orders', new=AsyncMock(side_effect=lambda: None)):
            await self.agent.connect_once()
        self.assertIsNone(self.agent.ws)
        self.assertEqual(self.agent.pending, {})
        self.assertIn('/reg', [m.get('lwp') for m in self.ws.sent])

    async def test_no_space_cookie_separator(self):
        agent = CardAgent(f'unb={SELLER};_m_h5_tk=token_123', self.store)
        self.assertEqual(agent.seller, SELLER)

    async def test_api_thread_error_during_cancel_is_drained(self):
        started, release = threading.Event(), threading.Event()
        def failing_request():
            started.set()
            release.wait(timeout=2)
            raise RuntimeError('offline error')
        task = asyncio.create_task(self.agent.api_call(failing_request))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.agent.api_lock.locked())

    async def test_poll_handles_missing_stock_and_api_failure(self):
        from unittest.mock import AsyncMock
        order = PaidOrder('9001', ITEM, BUYER, time.time())
        for result in [[order], RuntimeError('订单接口拒绝请求'), ValueError('schema')]:
            recent = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            with patch.object(self.agent.orders, 'recent', recent), \
                 patch('card_agent.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await self.agent.poll_orders()
        self.assertEqual(self.store.report(SELLER)['deliveries'], [])

    async def test_run_reconnects_then_can_stop_and_prevents_double_process(self):
        from unittest.mock import AsyncMock
        with patch.object(self.agent, 'connect_once', new=AsyncMock(side_effect=RuntimeError('offline'))), \
             patch('card_agent.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.agent.run()
        with patch.object(self.agent, 'connect_once', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.agent.run()
        with patch('card_agent.fcntl.flock', side_effect=BlockingIOError):
            with self.assertRaisesRegex(RuntimeError, '已有'):
                await self.agent.run()

    async def test_rotate_and_verify(self):
        from unittest.mock import AsyncMock
        order = PaidOrder('9001', ITEM, BUYER, time.time())
        with patch.object(self.agent.orders, 'verify', return_value=True):
            self.assertTrue(await self.agent.verify(order, 0))
        with patch('card_agent.asyncio.sleep', new=AsyncMock()) as sleep:
            await self.agent.rotate()
            sleep.assert_awaited_once_with(3500)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / 'cli.db')
        self.cookie = f'unb={SELLER}; _m_h5_tk=token_123'

    def cli(self, argv, env=None):
        import cards
        output = io.StringIO()
        with patch('sys.argv', ['cards.py', '--db', self.db] + argv), \
             patch('cards.load_dotenv'), patch.dict('os.environ', env or {}, clear=True), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            cards.main()
        return output.getvalue()

    def test_import_stock_and_recovery_without_model_key(self):
        source = Path(self.temp.name) / 'batch.txt'
        source.write_text('TEST-A\nTEST-B\nTEST-A\n', encoding='utf-8-sig')
        prefix = ['--seller-id', SELLER]
        out = self.cli(prefix + ['import', '--item-id', ITEM, '--file', str(source)])
        self.assertIn('新增 2 条', out)
        self.assertIn('"available": 2', self.cli(prefix + ['stock']))
        store = CardStore(self.db)
        store.reserve(SELLER, '9001', ITEM, BUYER)
        self.assertIn('TEST-A', self.cli(prefix + ['reveal', '--order-id', '9001']))
        self.cli(prefix + ['confirm-sent', '--order-id', '9001'])
        self.assertEqual(store.report(SELLER)['deliveries'][0]['state'], 'sent')

    def test_missing_login_wrong_account_or_bad_mode_rejected(self):
        for argv, env in [(['stock'], {}), (['--seller-id', '1002', 'run'], {'COOKIES_STR': self.cookie}),
                          (['run'], {'COOKIES_STR': self.cookie, 'CARD_DRY_RUN': 'typo'})]:
            with self.assertRaises(SystemExit):
                self.cli(argv, env)

    def test_run_has_no_ai_requirement_and_defaults_to_dry_run(self):
        from unittest.mock import AsyncMock
        with patch('card_agent.CardAgent.run', new=AsyncMock()) as run:
            self.cli(['run'], {'COOKIES_STR': self.cookie})
            run.assert_awaited_once()

    def test_offline_demo_without_credentials(self):
        out = self.cli(['demo'])
        self.assertIn('DEMO-NOT-REAL-A', out)
        self.assertIn('重复同一订单：skipped', out)
        self.assertIn('模拟重启后同一订单：skipped', out)
        self.assertIn('库存耗尽后的订单：no_stock', out)
        self.assertFalse(Path(self.db).exists())


if __name__ == '__main__':
    unittest.main()
