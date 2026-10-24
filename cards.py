"""Local CLI: import, stock, recovery and run. Does not require a model key."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from card_store import CardStore
from utils.xianyu_utils import trans_cookies


ROOT = Path(__file__).resolve().parent


async def run_managed(agent):
    """A child started by the admin must stop if the owning admin disappears."""
    owner=os.getenv('CARD_ADMIN_PID')
    if not owner:
        return await agent.run()
    if not owner.isdigit():
        raise ValueError('后台进程标识格式错误')
    async def watch_owner():
        while os.getppid()==int(owner):
            await asyncio.sleep(1)
    tasks=[asyncio.create_task(agent.run()),asyncio.create_task(watch_owner())]
    try:
        done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def demo():
    """Exercise allocation and delivery offline with visibly fictional data."""
    import tempfile
    import time
    from card_agent import DeliveryService
    from card_orders import PaidOrder
    class LocalTransport:
        async def create_chat(self, buyer, item):
            return "7001@goofish"
        async def verify(self, order, since):
            return True
        async def send_card(self, cid, buyer, text):
            print("[本地模拟收件箱] " + text)
    with tempfile.TemporaryDirectory(prefix="xianyu-card-demo-") as directory:
        store = CardStore(Path(directory) / "demo.db")
        store.import_cards("1001", "2001", ["DEMO-NOT-REAL-A", "DEMO-NOT-REAL-B"])
        service = DeliveryService(store, "1001", None, dry_run=False)
        transport = LocalTransport()
        order = PaidOrder("9001", "2001", "3001", time.time())
        print("离线演示：使用虚构订单和卡密，不连接闲鱼，不占用真实库存。")
        print("首次付款订单：" + await service.deliver(order, transport))
        print("重复同一订单：" + await service.deliver(order, transport))
        restarted = DeliveryService(CardStore(store.path), "1001", None, dry_run=False)
        print("模拟重启后同一订单：" + await restarted.deliver(order, transport))
        print("下一付款订单：" + await restarted.deliver(PaidOrder("9002", "2001", "3002", time.time()), transport))
        print("库存耗尽后的订单：" + await restarted.deliver(PaidOrder("9003", "2001", "3003", time.time()), transport))
        print("演示结束，临时数据已自动清理。")


def main():
    os.umask(0o077)
    os.chdir(ROOT)
    workspace=Path(os.getenv('CARD_WORKSPACE',str(ROOT))).resolve()
    load_dotenv(workspace / ".env")
    parser = argparse.ArgumentParser(description="闲鱼卡密库存与付款后发卡（不使用AI）")
    parser.add_argument("--db", default="data/cards.db", help="库存数据库路径")
    parser.add_argument("--seller-id", help="导入/查看库存的账号ID；默认读取本机Cookie的unb")
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import", help="按商品导入UTF-8文本，一行一个卡密")
    imp.add_argument("--item-id", required=True)
    imp.add_argument("--file", required=True)
    commands.add_parser("stock", help="查看库存和订单状态，不显示卡密")
    reveal = commands.add_parser("reveal", help="本地显示订单已分配卡密，用于人工核对")
    reveal.add_argument("--order-id", required=True)
    confirm = commands.add_parser("confirm-sent", help="人工核对聊天已收到后，标记本地记录已发送")
    confirm.add_argument("--order-id", required=True)
    retry = commands.add_parser('retry-shipping', help='仅重试平台确认发货，不重发卡密')
    retry.add_argument('--order-id', required=True)
    commands.add_parser("run", help="启动订单监听；默认演练，不发卡")
    commands.add_parser("demo", help="离线演示虚构订单，无需Cookie，不发真实消息")
    commands.add_parser("mock-chat", help="真实聊天测试：买家发“已支付”后回复不可使用的MOCK卡密")
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
    if args.command == "demo":
        asyncio.run(demo())
        return
    cookie = os.getenv("COOKIES_STR", "")
    seller = args.seller_id or trans_cookies(cookie).get("unb", "")
    if not seller.isdigit():
        parser.error("请先填写 .env 的闲鱼 Cookie，或用 --seller-id 指定账号 unb")
    if args.command == 'mock-chat' and args.db != 'data/cards.db':
        parser.error('聊天模拟使用独立 data/mock-cards.db，不允许指定其他数据库')
    database=Path('data/mock-cards.db' if args.command == 'mock-chat' else args.db)
    store = CardStore(database if database.is_absolute() else workspace/database)
    if args.command == "import":
        result = store.import_cards(seller, args.item_id, Path(args.file).read_text(encoding="utf-8-sig").splitlines())
        print(f"新增 {result['added']} 条；跳过重复 {result['duplicates']} 条；按商品 {args.item_id} 保存。")
    elif args.command == "stock":
        print(json.dumps(store.report(seller), ensure_ascii=False, indent=2))
    elif args.command == "reveal":
        print(store.reveal(seller, args.order_id))
    elif args.command == "confirm-sent":
        store.confirm_sent(seller, args.order_id)
        print("已标记本地记录为已发送；此操作没有发送消息或修改闲鱼订单。")
    elif args.command == 'retry-shipping':
        store.retry_shipping(seller, args.order_id)
        print('已恢复平台发货重试队列；下次正式模式先核对订单再处理，不重发卡密。')
    else:
        from card_agent import CardAgent
        # Ignore --seller-id for live access; it must match the authenticated account.
        if args.seller_id and seller != trans_cookies(cookie).get("unb"):
            parser.error("运行账号必须与 Cookie 中 unb 一致")
        if args.command == 'mock-chat':
            from card_mock import MockCardAgent
            asyncio.run(run_managed(MockCardAgent(cookie, store, template_path=workspace/'data/delivery-template.json')))
            return
        mode = os.getenv("CARD_DRY_RUN", "true").lower()
        if mode not in {"true", "false"}:
            parser.error("CARD_DRY_RUN 只能为 true 或 false")
        agent = CardAgent(cookie, store, dry_run=mode == "true", template_path=workspace/'data/delivery-template.json')
        asyncio.run(run_managed(agent))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止卡密服务。")
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"无法继续：{exc}", file=sys.stderr)
        sys.exit(1)
