"""Seller order and no-logistics shipping adapter; unknown states fail closed.

Protocol reference (not a guarantee of availability for every account):
https://github.com/GuDong2003/xianyu-auto-reply-fix/blob/main/utils/order_history_sync.py
"""
import json
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from requests.cookies import RequestsCookieJar
from loguru import logger

from utils.xianyu_utils import generate_sign


def merge_response_cookies(session, response):
    """Replace earlier values across scopes when a trusted Goofish API rotates cookies."""
    updates = getattr(response, "cookies", None)
    if not isinstance(updates, RequestsCookieJar):
        return
    for update in updates:
        for old in list(session.cookies):
            if old.name == update.name:
                session.cookies.clear(old.domain, old.path, old.name)
        session.cookies.set(update.name, update.value, domain=".goofish.com", path="/", secure=update.secure)


@dataclass(frozen=True)
class PaidOrder:
    order_id: str
    item: str
    buyer: str
    paid_at: float
    quantity: int = 1


def order_status(raw):
    common = raw.get('commonData') if isinstance(raw, dict) else None
    if not isinstance(common, dict):
        return 'unknown'
    # Refunding must win even if the display status has not changed yet.
    if common.get('inRefund') not in (None, False, 0, '', 'false', '0'):
        return 'blocked'
    return {'待发货': 'pending_ship', 'pending_ship': 'pending_ship',
            '待收货': 'shipped', '等待买家收货': 'shipped', '已发货': 'shipped', 'shipped': 'shipped',
            '交易成功': 'completed', '已完成': 'completed', 'completed': 'completed',
            '退款中': 'blocked', '交易关闭': 'blocked', '已关闭': 'blocked',
            'cancelled': 'blocked', 'refunding': 'blocked', '待付款': 'blocked'}.get(common.get('orderStatus'), 'unknown')


def parse_paid_order(raw, since, now=None):
    if not isinstance(raw, dict):
        return None
    common = raw.get("commonData")
    buyer_info = raw.get("buyerInfoVO")
    if not isinstance(common, dict) or not isinstance(buyer_info, dict):
        return None
    # Exact seller-side states only. Never use buyer chat text as payment evidence.
    if order_status(raw) != 'pending_ship':
        return None
    ids = [str(common.get("orderId") or ""), str(common.get("itemId") or ""),
           str(buyer_info.get("buyerId") or "")]
    if not all(value.isdigit() for value in ids):
        return None
    try:
        paid_at = datetime.strptime(common["paySuccessTime"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    except (KeyError, ValueError, TypeError):
        return None
    if not since <= paid_at <= (time.time() if now is None else now) + 60:
        return None
    price = raw.get('priceVO')
    quantity = price.get('buyNum') if isinstance(price,dict) else None
    if isinstance(quantity,str) and quantity.isascii() and quantity.isdigit():
        quantity=int(quantity)
    if type(quantity) is not int or not 1<=quantity<=100:
        logger.warning('订单 {} 购买数量缺失、异常或超过100张，已跳过发卡，请人工核对',ids[0])
        return None
    return PaidOrder(*ids, paid_at, quantity)


class SellerOrders:
    API = "mtop.taobao.idle.trade.merchant.sold.get"

    def __init__(self, api):
        self.api = api

    def post(self, api_name, payload, merchant=True):
        token = self.api.session.cookies.get("_m_h5_tk", "").split("_")[0]
        if not token:
            raise RuntimeError("Cookie 缺少 _m_h5_tk，请更新完整 Cookie")
        payload = json.dumps(payload, separators=(",", ":"))
        stamp = str(int(time.time() * 1000))
        params = {"jsv": "2.7.2", "appKey": "34839810", "t": stamp,
                  "sign": generate_sign(stamp, token, payload), "v": "1.0", "type": "json",
                  "accountSite": "xianyu", "dataType": "json", "timeout": "20000",
                  "api": api_name, "valueType": "string", "sessionOption": "AutoLoginOnly"}
        response = self.api.session.post(f"https://h5api.m.goofish.com/h5/{api_name}/1.0/",
                                        params=params, data={"data": payload}, timeout=25,
                                        headers={"origin": "https://seller.goofish.com" if merchant else "https://www.goofish.com",
                                                 "referer": "https://seller.goofish.com/?site=COMMONPRO#/seller-trade/order-manage",
                                                 "idle_site_biz_code": "COMMONPRO"})
        response.raise_for_status()
        merge_response_cookies(self.api.session, response)
        result = response.json()
        if not isinstance(result, dict) or not any(
                str(r).startswith("SUCCESS::") for r in result.get("ret", [])):
            # Do not log raw responses, which may include login URLs or private data.
            raise RuntimeError("订单接口拒绝请求；请检查卖家中心权限、Cookie 或平台验证")
        return result

    def page(self, number=1, order_id=""):
        result = self.post(self.API, {"pageNumber": number, "rowsPerPage": 20, "orderIds": order_id,
                                    "queryCode": "ALL", "orderSearchParam": "{}"})
        data = result.get("data")
        module = data.get("module") if isinstance(data, dict) else None
        if not isinstance(module, dict) or not isinstance(module.get("items"), list):
            raise RuntimeError("订单接口格式不匹配，已停止本轮发卡，需核对真实接口")
        return module["items"], str(module.get("nextPage", "false")).lower() == "true"

    def recent(self, since):
        candidates = {}
        for number in range(1, 6):
            items, more = self.page(number)
            for raw in items:
                order = parse_paid_order(raw, since)
                if order:
                    candidates[order.order_id] = order
            if not more:
                return list(candidates.values())
        # Partial scans could miss an old state change. Surface the limit explicitly.
        raise RuntimeError("订单超过100条扫描范围；本轮不发卡，请调整订单查询范围")

    def verify(self, expected, since):
        raw = self.lookup(expected)
        return raw is not None and parse_paid_order(raw, since) == expected

    def lookup(self, expected):
        items, _ = self.page(order_id=expected.order_id)
        matches = [raw for raw in items if isinstance(raw, dict)
                   and isinstance(raw.get("commonData"), dict)
                   and str(raw["commonData"].get("orderId")) == expected.order_id]
        if len(matches) != 1:
            return None
        raw = matches[0]
        common, buyer = raw['commonData'], raw.get('buyerInfoVO') or {}
        if str(common.get('itemId')) != expected.item or str(buyer.get('buyerId')) != expected.buyer:
            return None
        return raw

    def confirm_shipped(self, expected):
        """Read before every retry; only mutate the exact card recipient's paid order."""
        raw = self.lookup(expected)
        status = order_status(raw)
        if status in {'shipped', 'completed'}:
            return 'done'
        if status != 'pending_ship' or parse_paid_order(raw, 0) != expected:
            return 'blocked'
        result = self.post('mtop.taobao.idle.logistic.consign.dummy', {
            'orderId': expected.order_id, 'tradeText': '', 'picList': [], 'newUnconsign': True}, merchant=False)
        if isinstance(result.get('data'), dict) and result['data'].get('success') is False:
            raise RuntimeError('平台拒绝确认发货；卡密不会重发')
        # An API success alone is not evidence that the displayed order changed.
        status = order_status(self.lookup(expected))
        if status in {'shipped', 'completed'}:
            return 'done'
        return 'blocked' if status in {'blocked', 'unknown'} else 'pending'
