"""Explicit chat-triggered MOCK delivery. Never queries paid orders or real inventory."""
import asyncio
import base64
import hashlib
import json
import re
import time

from loguru import logger

from card_agent import CardAgent
from utils.xianyu_utils import decrypt


def mock_triggers(envelope, seller, started_ms, now_ms=None):
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    try:
        entries = envelope['body']['syncPushPackage']['data']
    except (KeyError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    triggers = []
    for entry in entries:
        try:
            data = entry['data']
            if not isinstance(data, str) or len(data) > 1000000:
                continue
            try:
                message = json.loads(base64.b64decode(data).decode('utf-8'))
            except (ValueError, UnicodeError):
                message = json.loads(decrypt(data))
            info = message['1']
            metadata = info['10']
            text = metadata.get('reminderContent', '')
            buyer = str(metadata.get('senderUserId', ''))
            cid = info['2']
            stamp = int(info['5'])
            keyword=re.fullmatch(r'已支付(?:\s*([1-9][0-9]?|100))?',text.strip()) if isinstance(text,str) else None
            if (keyword is None
                    or not buyer.isdigit() or buyer == seller
                    or not isinstance(cid, str) or not cid.endswith('@goofish')
                    or not cid.split('@')[0].isdigit()
                    or stamp < started_ms or not now_ms - 300000 <= stamp <= now_ms + 60000):
                continue
            digest = hashlib.sha256(f'{seller}:{cid}:{buyer}:{stamp}'.encode()).hexdigest()
            triggers.append({'key': 'mock:' + digest, 'buyer': buyer, 'cid': cid,
                             'code': 'MOCK-TEST-' + digest[:16].upper(), 'stamp': stamp,
                             'quantity':int(keyword.group(1) or 1)})
        except (KeyError, TypeError, ValueError, AttributeError, UnicodeError):
            continue
    return triggers


class MockCardAgent(CardAgent):
    def __init__(self, cookie_string, store, template_path=None):
        super().__init__(cookie_string, store, dry_run=True, template_path=template_path)
        self.started_ms = int(time.time() * 1000)
        self.queue = asyncio.Queue(maxsize=100)
        self.mode_label = '聊天模拟：收到“已支付”仅回复 MOCK 测试卡密'
        self.startup_note = '模拟模式会发送测试消息；发“已支付 2”可测试一条消息发两张；不判断付款，不读正式库存；Ctrl+C 停止'

    async def handle_push(self, message):
        for trigger in mock_triggers(message, self.seller, self.started_ms):
            try:
                self.queue.put_nowait(trigger)
            except asyncio.QueueFull:
                logger.warning('模拟队列已满，本条不处理，请稍后重发测试关键词')

    async def respond(self, trigger):
        store = self.service.store
        if store.delivered(self.seller, trigger['key']):
            return 'duplicate'
        if time.time() * 1000 - trigger['stamp'] > 300000:
            return 'expired'
        template = self.service.current_template()
        # Item 0 is an internal mock bucket; this store is separate from cards.db.
        quantity=trigger.get('quantity',1)
        store.validate_quantity(quantity)
        codes=[trigger['code']] if quantity==1 else [f"{trigger['code']}-{i}" for i in range(1,quantity+1)]
        self.service.render_message(template,codes)
        store.import_cards(self.seller, '0', codes)
        codes = store.reserve_many(self.seller, trigger['key'], '0', trigger['buyer'],quantity,
                                  validate_message=lambda values:self.service.render_message(template,values))
        if codes is None:
            return 'duplicate'
        text = self.service.render_message(template,codes)
        try:
            await self.send_card(trigger['cid'], trigger['buyer'], text)
        except BaseException:
            store.finish(self.seller, trigger['key'], 'uncertain')
            raise
        store.finish(self.seller, trigger['key'], 'sent')
        logger.success('已合并回复 {} 张 MOCK 测试卡密，消息接口已确认',len(codes))
        return 'sent'

    async def poll_orders(self):
        # Same lifecycle as the real agent's poller, but NEVER calls the order API.
        logger.info('已就绪：用另一个闲鱼账号发“已支付”测试一张，发“已支付 2”测试一条消息发两张')
        while True:
            trigger = await self.queue.get()
            try:
                await self.respond(trigger)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('模拟回复未确认（{}），不自动重发此消息', type(exc).__name__)
            finally:
                self.queue.task_done()
