import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from card_mock import MockCardAgent, mock_triggers
from card_store import CardStore


SELLER, BUYER = '1001', '3001'


def envelope(text='已支付', buyer=BUYER, stamp=None, cid='7001@goofish'):
    message = {'1': {'2': cid, '5': int(time.time() * 1000) if stamp is None else stamp,
                      '10': {'reminderContent': text, 'senderUserId': buyer}}}
    data = base64.b64encode(json.dumps(message).encode()).decode()
    return {'headers': {'mid': 'test-push'}, 'body': {'syncPushPackage': {'data': [{'data': data}]}}}


class ParserTests(unittest.TestCase):
    def test_only_exact_keyword_from_another_buyer_after_startup(self):
        self.assertEqual(len(mock_triggers(envelope(), SELLER, 0)), 1)
        for kwargs in [{'text': '已支付请发货'}, {'text': '已付款'}, {'buyer': SELLER},
                       {'buyer': 'bad'}, {'cid': 'not-a-cid'}, {'stamp': 1},
                       {'stamp': int(time.time() * 1000) + 120000}]:
            self.assertEqual(mock_triggers(envelope(**kwargs), SELLER, 0), [])
        now = int(time.time() * 1000)
        self.assertEqual(mock_triggers(envelope(stamp=now - 1), SELLER, now), [])

    def test_malformed_and_msgpack_batches(self):
        for raw in [None, {}, {'body': {'syncPushPackage': {'data': {}}}},
                    {'body': {'syncPushPackage': {'data': [{'data': 'bad'}, {}, {'data': None}]}}}]:
            self.assertEqual(mock_triggers(raw, SELLER, 0), [])
        message = {'1': {'2': '7001@goofish', '5': int(time.time() * 1000),
                          '10': {'reminderContent': '已支付', 'senderUserId': BUYER}}}
        raw = envelope()
        raw['body']['syncPushPackage']['data'][0]['data'] = 'not-json-messagepack'
        with patch('card_mock.decrypt', return_value=json.dumps(message)):
            self.assertEqual(len(mock_triggers(raw, SELLER, 0)), 1)


class MockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CardStore(Path(self.temp.name) / 'mock.db')
        self.agent = MockCardAgent('unb=1001;_m_h5_tk=test_token', self.store)
        self.agent.started_ms -= 10
        self.agent.send_card = AsyncMock()
        self.raw = envelope()

    async def test_receives_keyword_sends_only_labeled_mock_and_dedupes(self):
        await self.agent.handle_push(self.raw)
        trigger = self.agent.queue.get_nowait()
        self.assertEqual(await self.agent.respond(trigger), 'sent')
        text = self.agent.send_card.call_args.args[2]
        self.assertIn('MOCK-TEST-', text)
        self.assertIn('您好，感谢光临小店，以下是发货内容：', text)
        self.assertIn('请尽快使用，谢谢！', text)
        self.assertEqual(self.agent.send_card.call_args.args[:2], ('7001@goofish', BUYER))
        self.assertEqual(await self.agent.respond(trigger), 'duplicate')
        self.agent.send_card.assert_awaited_once()
        restarted = MockCardAgent('unb=1001;_m_h5_tk=test_token', CardStore(self.store.path))
        restarted.send_card = AsyncMock()
        self.assertEqual(await restarted.respond(trigger), 'duplicate')
        restarted.send_card.assert_not_awaited()

    async def test_send_timeout_keeps_mock_uncertain(self):
        self.agent.send_card.side_effect = TimeoutError
        trigger = mock_triggers(self.raw, SELLER, 0)[0]
        with self.assertRaises(TimeoutError):
            await self.agent.respond(trigger)
        self.assertEqual(self.store.report(SELLER)['deliveries'][0]['state'], 'uncertain')
        self.assertEqual(await self.agent.respond(trigger), 'duplicate')

    async def test_worker_never_queries_orders_and_keeps_reader_free(self):
        self.agent.orders.recent = AsyncMock(side_effect=AssertionError('must not query orders'))
        await self.agent.handle_push(self.raw)
        worker = asyncio.create_task(self.agent.poll_orders())
        await asyncio.wait_for(self.agent.queue.join(), 1)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.agent.send_card.assert_awaited_once()
        self.agent.orders.recent.assert_not_called()

    async def test_expired_or_overflow_queue_does_not_send(self):
        trigger = mock_triggers(self.raw, SELLER, 0)[0]
        trigger['stamp'] = 1
        self.assertEqual(await self.agent.respond(trigger), 'expired')
        self.agent.queue = asyncio.Queue(maxsize=1)
        self.agent.queue.put_nowait(trigger)
        await self.agent.handle_push(self.raw)
        self.assertEqual(self.agent.queue.qsize(), 1)
        self.agent.send_card.assert_not_awaited()

    async def test_worker_errors_do_not_stop_following_messages(self):
        trigger = mock_triggers(self.raw, SELLER, 0)[0]
        self.agent.respond = AsyncMock(side_effect=[RuntimeError('mock failure'), 'duplicate'])
        await self.agent.queue.put(trigger)
        await self.agent.queue.put(trigger)
        worker = asyncio.create_task(self.agent.poll_orders())
        await asyncio.wait_for(self.agent.queue.join(), 1)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertEqual(self.agent.respond.await_count, 2)

    async def test_rpc_reader_can_process_trigger_and_later_send_ack(self):
        from test_cards import FakeWebsocket
        ws = FakeWebsocket()
        self.agent.ws = ws
        # Use the actual send serializer and RPC implementation for this test.
        from card_agent import CardAgent
        self.agent.send_card = CardAgent.send_card.__get__(self.agent)
        reader = asyncio.create_task(self.agent.read_messages())
        worker = asyncio.create_task(self.agent.poll_orders())
        try:
            await ws.messages.put(self.raw)
            for _ in range(100):
                records = self.store.report(SELLER)['deliveries']
                if records and records[0]['state'] == 'sent':
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(records[0]['state'], 'sent')
            sent = [m for m in ws.sent if m.get('lwp') == '/r/MessageSend/sendByReceiverScope']
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]['body'][1]['actualReceivers'][0], BUYER + '@goofish')
        finally:
            worker.cancel()
            reader.cancel()
            await asyncio.gather(reader, worker, return_exceptions=True)


class MockCliTests(unittest.TestCase):
    def test_explicit_mock_mode_forces_isolated_store_without_ai_key(self):
        import cards
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as folder:
            store = CardStore(Path(folder) / 'mock.db')
            with patch('sys.argv', ['cards.py', 'mock-chat']), patch('cards.load_dotenv'), \
                 patch.dict('os.environ', {'COOKIES_STR': 'unb=1001;_m_h5_tk=test_token'}, clear=True), \
                 patch('cards.CardStore', return_value=store) as constructor, \
                 patch('card_mock.MockCardAgent.run', new=AsyncMock()) as run, \
                 contextlib.redirect_stderr(io.StringIO()):
                cards.main()
            constructor.assert_called_once_with(cards.ROOT/'data/mock-cards.db')
            run.assert_awaited_once()

    def test_mock_cannot_point_at_formal_inventory(self):
        import cards
        import contextlib
        import io
        with patch('sys.argv', ['cards.py', '--db', 'data/formal.db', 'mock-chat']), \
             patch('cards.load_dotenv'), \
             patch.dict('os.environ', {'COOKIES_STR': 'unb=1001;_m_h5_tk=test_token'}, clear=True), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cards.main()


if __name__ == '__main__':
    unittest.main()
