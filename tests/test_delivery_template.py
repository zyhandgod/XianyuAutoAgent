import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from admin import MultiAdmin
from card_agent import DeliveryService
from card_mock import MockCardAgent
from card_orders import PaidOrder
from card_store import CardStore
from card_template import DEFAULT_TEMPLATE, load_template, save_template


class TemplateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'delivery-template.json'

    async def test_live_reload_real_and_mock_share_text_without_reallocation(self):
        store = CardStore(self.root/'real.db')
        store.import_cards('1001', '2001', ['REAL-A', 'REAL-B'])
        service = DeliveryService(store, '1001', None, dry_run=False, template_path=self.path)
        mock = MockCardAgent('unb=1001;_m_h5_tk=test', CardStore(self.root/'mock.db'), template_path=self.path)
        mock.send_card = AsyncMock()
        transport = AsyncMock()
        transport.create_chat.return_value = '7001@goofish'
        transport.verify.return_value = True
        for index in (1, 2):
            text = f'您好\n教程：https://example.com/{index}\n下载：xxx\n卡密：{{card}}\n\n谢谢！'
            save_template(self.path, text)
            order = PaidOrder(str(9000+index), '2001', '3001', time.time())
            self.assertEqual(await service.deliver(order, transport), 'sent')
            self.assertEqual(transport.send_card.call_args.args[2], text.replace('{card}', ['REAL-A','REAL-B'][index-1]))
            trigger = {'key':f'mock:{index}', 'buyer':'3001', 'cid':'7001@goofish',
                       'stamp':time.time()*1000, 'code':f'MOCK-TEST-{index}'}
            self.assertEqual(await mock.respond(trigger), 'sent')
            self.assertEqual(mock.send_card.call_args.args[2], text.replace('{card}', trigger['code']))
            self.assertEqual(await mock.respond(trigger), 'duplicate')
            self.assertEqual(await service.deliver(order, transport), 'skipped')

    async def test_invalid_template_preserves_saved_text_and_inventory(self):
        self.assertEqual(load_template(self.path), DEFAULT_TEMPLATE)
        save_template(self.path, '卡密：{card}')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for value in ('', '无占位符', '{card}{card}', 'x'*5001+'{card}', None):
            with self.assertRaises(ValueError):save_template(self.path, value)
        self.assertEqual(load_template(self.path), '卡密：{card}')
        store = CardStore(self.root/'cards.db')
        store.import_cards('1001','2001',['ONE'])
        service = DeliveryService(store,'1001',None,dry_run=False,template_path=self.path)
        self.path.write_text('{"template":"broken"}')
        transport = AsyncMock()
        with self.assertRaises(ValueError):
            await service.deliver(PaidOrder('9001','2001','3001',time.time()),transport)
        transport.create_chat.assert_not_awaited()
        self.assertEqual(store.report('1001')['inventory'][0]['available'],1)

    async def test_backend_saves_template_per_account_and_across_restart(self):
        app = MultiAdmin(self.root)
        owner = app.create('owner','test-password-123')
        member = app.create('member','test-password-456',owner['id'])
        app.action(owner,'delivery-template',{'template':'自定义发货：{card}'})
        self.assertEqual(app.manager(owner).state()['delivery_template'],'自定义发货：{card}')
        self.assertEqual(app.manager(member).state()['delivery_template'],DEFAULT_TEMPLATE)
        self.assertEqual(MultiAdmin(self.root).manager(owner).state()['delivery_template'],'自定义发货：{card}')
