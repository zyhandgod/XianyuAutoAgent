"""Fixed card delivery; no LLM imports, keys, prompts or chat replies."""
import asyncio
import base64
import fcntl
import json
import os
import time
from pathlib import Path

import websockets
from loguru import logger

from XianyuApis import XianyuApis
from card_template import DEFAULT_TEMPLATE, load_template, validate_template
from card_orders import PaidOrder, SellerOrders, merge_response_cookies
from utils.xianyu_utils import generate_device_id, generate_sign, generate_mid, generate_uuid, trans_cookies


class DeliveryService:
    def __init__(self, store, seller, orders, dry_run=True, template=DEFAULT_TEMPLATE, template_path=None):
        validate_template(template)
        self.store, self.seller, self.orders = store, seller, orders
        self.dry_run, self.template = dry_run, template
        self.template_path = template_path
        self.since = store.start_at(seller)

    def current_template(self):
        return load_template(self.template_path) if self.template_path is not None else self.template

    @staticmethod
    def render_message(template, cards):
        content=cards[0] if len(cards)==1 else '\n'+'\n'.join(f'{index}. {card}' for index,card in enumerate(cards,1))
        text=template.replace('{card}',content)
        if len(text.encode('utf-8'))>20000:
            raise RuntimeError('该单完整发货消息超过20000字节上限，请缩短文案或人工处理；未分配卡密')
        return text

    async def deliver(self, order, transport):
        if order.buyer == self.seller or self.store.delivered(self.seller, order.order_id):
            return "skipped"
        if not self.store.has_stock(self.seller, order.item, order.quantity):
            return "no_stock"
        if self.dry_run:
            logger.info("演练：订单 {} 商品 {} 可发 {} 张；未分配、未发送", order.order_id, order.item,order.quantity)
            return "dry_run"
        template = self.current_template()
        # Establish the recipient conversation first; never infer buyer ID from cid.
        cid = await transport.create_chat(order.buyer, order.item)
        if not await transport.verify(order, self.since):
            logger.warning("订单 {} 二次核验未通过，跳过", order.order_id)
            return "unverified"
        cards = self.store.reserve_many(self.seller, order.order_id, order.item, order.buyer,order.quantity,
            validate_message=lambda values:self.render_message(template,values))
        if cards is None:
            return 'skipped' if self.store.delivered(self.seller,order.order_id) else 'no_stock'
        try:
            await transport.send_card(cid, order.buyer, self.render_message(template,cards))
        except BaseException:
            # Sending may have succeeded even when the acknowledgment was lost.
            # Never automatically resend or reuse this card after an uncertain result.
            self.store.finish(self.seller, order.order_id, "uncertain")
            raise
        self.store.finish(self.seller, order.order_id, "sent", paid_at=order.paid_at)
        logger.success("订单 {} 的 {} 张卡密已合并发送，消息已获接口确认", order.order_id,len(cards))
        return "sent"


class CardAgent:
    URL = "wss://wss-goofish.dingtalk.com/"

    def __init__(self, cookie_string, store, dry_run=True, interval=30, template_path=None):
        self.cookies = trans_cookies(cookie_string)
        if not all(self.cookies.get(k) for k in ("unb", "_m_h5_tk")):
            raise ValueError("请在 .env 填写完整闲鱼 Cookie（至少含 unb、_m_h5_tk）")
        self.seller = self.cookies["unb"]
        if not self.seller.isdigit():
            raise ValueError("Cookie 中的 unb 格式不正确")
        self.api = XianyuApis()
        for name, value in self.cookies.items():
            self.api.session.cookies.set(name, value, domain=".goofish.com", path="/")
        self.orders = SellerOrders(self.api)
        self.service = DeliveryService(store, self.seller, self.orders, dry_run, template_path=template_path)
        self.interval = max(20, interval)
        self.device = generate_device_id(self.seller)
        self.pending = {}
        self.ws = None
        self.api_lock = asyncio.Lock()
        self.mode_label = "演练（不发卡）" if dry_run else "真实发卡"
        self.startup_note = "仅处理本账号首次启动后付款的订单；不自动补发历史订单"

    async def api_call(self, function, *args):
        # requests.Session and its cookie jar are shared; serialize thread access.
        async with self.api_lock:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A worker thread cannot be cancelled. Drain it before unlocking.
                try:
                    await task
                except Exception:
                    pass
                raise

    def login_token(self):
        """Bounded, noninteractive authentication for this service only."""
        api_name = "mtop.taobao.idlemessage.pc.login.token"
        token = self.api.session.cookies.get("_m_h5_tk", "").split("_")[0]
        payload = json.dumps({"appKey": "444e9908a51d1cb236a27862abc769c9", "deviceId": self.device},
                             separators=(",", ":"))
        stamp = str(int(time.time() * 1000))
        result = self.api.session.post(f"https://h5api.m.goofish.com/h5/{api_name}/1.0/",
            params={"jsv": "2.7.2", "appKey": "34839810", "t": stamp,
                    "sign": generate_sign(stamp, token, payload), "v": "1.0", "type": "originaljson",
                    "accountSite": "xianyu", "dataType": "json", "timeout": "20000",
                    "api": api_name, "sessionOption": "AutoLoginOnly"},
            data={"data": payload}, timeout=25)
        result.raise_for_status()
        merge_response_cookies(self.api.session, result)
        body = result.json()
        if not isinstance(body, dict) or not any(str(r).startswith("SUCCESS::") for r in body.get("ret", [])):
            from login_check import LoginRejected
            raise LoginRejected(body)
        access_token = body.get("data", {}).get("accessToken")
        if not access_token:
            raise RuntimeError("Token 接口格式不匹配，未启动发卡")
        return access_token

    async def rpc(self, lwp, body, extra_headers=None):
        mid = generate_mid()
        while mid in self.pending:
            mid = generate_mid()
        future = asyncio.get_running_loop().create_future()
        self.pending[mid] = future
        try:
            request = {"lwp": lwp, "headers": {**(extra_headers or {}), "mid": mid}}
            if body is not None:
                request["body"] = body
            await self.ws.send(json.dumps(request))
            result = await asyncio.wait_for(future, timeout=20)
            if str(result.get("code")) != "200":
                raise RuntimeError("闲鱼消息接口未确认成功")
            return result
        finally:
            self.pending.pop(mid, None)

    async def read_messages(self):
        try:
            async for raw in self.ws:
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        continue
                    headers = message.get("headers") or {}
                    mid = headers.get("mid")
                    future = self.pending.get(mid)
                    if future is not None and "code" in message:
                        if not future.done():
                            future.set_result(message)
                    elif mid and "code" not in message:
                        await self.ws.send(json.dumps({"code": 200, "headers": {
                            "mid": mid, "sid": headers.get("sid", "")}}))
                        await self.handle_push(message)
                except (ValueError, TypeError, AttributeError):
                    continue
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("闲鱼连接已断开"))

    async def handle_push(self, message):
        # Real payment mode ignores buyer chats, including “I have paid”.
        pass

    async def create_chat(self, buyer, item):
        result = await self.rpc("/r/SingleChatConversation/create", [{
            "pairFirst": f"{buyer}@goofish", "pairSecond": f"{self.seller}@goofish",
            "bizType": "1", "extension": {"itemId": item},
            "ctx": {"appVersion": "1.0", "platform": "web"}}])
        body = result.get("body") or {}
        conversation = body.get("singleChatConversation") or {}
        cid = conversation.get("cid", "")
        if not isinstance(cid, str) or not cid.endswith("@goofish") or not cid.split("@")[0].isdigit():
            raise RuntimeError("会话接口格式不匹配，未发送卡密")
        return cid

    async def verify(self, order, since):
        return await self.api_call(self.orders.verify, order, since)

    async def send_card(self, cid, buyer, text):
        encoded = base64.b64encode(json.dumps({"contentType": 1, "text": {"text": text}}).encode()).decode()
        await self.rpc("/r/MessageSend/sendByReceiverScope", [{
            "uuid": generate_uuid(), "cid": cid, "conversationType": 1,
            "content": {"contentType": 101, "custom": {"type": 1, "data": encoded}},
            "redPointPolicy": 0, "extension": {"extJson": "{}"},
            "ctx": {"appVersion": "1.0", "platform": "web"}, "mtags": {},
            "msgReadStatusSetting": 1},
            {"actualReceivers": [f"{buyer}@goofish", f"{self.seller}@goofish"]}])

    async def poll_orders(self):
        while True:
            try:
                await self.finish_shipments()
                orders = await self.api_call(self.orders.recent, self.service.since)
                for order in orders:
                    result = await self.service.deliver(order, self)
                    if result == 'sent':
                        await self.finish_shipments()
                    if result == "no_stock":
                        logger.warning("订单 {} 商品 {} 需要 {} 张卡密，库存不足，整单未发；请补充库存", order.order_id, order.item,order.quantity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not print raw request URLs, chat payloads or card contents.
                logger.error("本轮发货未完成（{}），未确认的发送需人工核对", type(exc).__name__)
                if isinstance(exc, RuntimeError):
                    logger.error("{}", exc)
            await asyncio.sleep(self.interval)

    async def finish_shipments(self):
        if self.service.dry_run:
            return
        store = self.service.store
        for row in store.pending_shipments(self.seller):
            order = PaidOrder(row['order_id'], row['item'], row['buyer'], row['paid_at'],row['quantity'])
            if row['attempts'] >= 5:
                store.shipment_result(self.seller, order.order_id, 'blocked')
                logger.error('订单 {} 卡密已发，但平台发货未确认；已停止自动重试，请人工核对', order.order_id)
                continue
            store.shipment_attempt(self.seller, order.order_id)
            try:
                result = await self.api_call(self.orders.confirm_shipped, order)
                store.shipment_result(self.seller, order.order_id, result)
                if result == 'done':
                    logger.success('订单 {} 已回查确认平台已发货', order.order_id)
                elif result == 'blocked':
                    logger.error('订单 {} 状态不适合确认发货，请人工核对；不会重发卡密', order.order_id)
                else:
                    logger.warning('订单 {} 发货状态尚未更新，下轮只核对平台状态，不重发卡密', order.order_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('订单 {} 卡密已发，平台发货未确认（{}）；下轮先核对订单再重试', order.order_id, type(exc).__name__)

    async def heartbeat(self):
        while True:
            await self.rpc("/!", None)
            await asyncio.sleep(15)

    async def rotate(self):
        await asyncio.sleep(3500)

    async def connect_once(self):
        token = await self.api_call(self.login_token)
        # Use refreshed session cookies rather than the initial login snapshot.
        cookie_string = "; ".join(f"{c.name}={c.value}" for c in self.api.session.cookies)
        headers = {"Cookie": cookie_string, "Origin": "https://www.goofish.com",
                   "User-Agent": "Mozilla/5.0 Chrome/146.0.0.0 Safari/537.36"}
        async with websockets.connect(self.URL, extra_headers=headers, open_timeout=25) as ws:
            self.ws = ws
            tasks = [asyncio.create_task(self.read_messages())]
            try:
                # Registration and sync are one-way protocol messages in the upstream
                # client. Waiting for a /reg RPC reply before sync deadlocks startup.
                await ws.send(json.dumps({"lwp": "/reg", "headers": {"cache-header": "app-key token ua wv",
                    "app-key": "444e9908a51d1cb236a27862abc769c9", "token": token,
                    "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                    "dt": "j", "wv": "im:3,au:3,sy:6", "sync": "0,0;0;0;", "did": self.device,
                    "mid": generate_mid()}}))
                await asyncio.sleep(1)
                await ws.send(json.dumps({"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": generate_mid()},
                    "body": [{"pipeline": "sync", "tooLong2Tag": "PNM,1",
                    "channel": "sync", "topic": "sync", "highPts": 0,
                    "pts": int(time.time() * 1000) * 1000, "seq": 0, "timestamp": int(time.time() * 1000)}]}))
                # A correlated heartbeat confirms that the message connection responds.
                await self.rpc("/!", None)
                logger.success("闲鱼已连接，模式：{}", self.mode_label)
                tasks += [asyncio.create_task(self.heartbeat()), asyncio.create_task(self.poll_orders()),
                          asyncio.create_task(self.rotate())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.ws = None

    async def run(self):
        lock_dir=Path(os.getenv('CARD_LOCK_DIR',str(Path(__file__).resolve().parent/'data/agent-locks')))
        lock_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        lock_path = lock_dir / f"agent-{self.seller}.lock"
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("此账号已有发货进程运行，请先停止旧进程")
            logger.info("{}", self.startup_note)
            while True:
                try:
                    await self.connect_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("连接中断（{}），10秒后重试", type(exc).__name__)
                    if isinstance(exc, RuntimeError):
                        logger.warning("{}", exc)
                await asyncio.sleep(10)
