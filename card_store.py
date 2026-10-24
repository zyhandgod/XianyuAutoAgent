"""Local card inventory. Never return an allocated card to the available pool."""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class CardStore:
    def __init__(self, path="data/cards.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cards (
                    id INTEGER PRIMARY KEY, seller TEXT NOT NULL, item TEXT NOT NULL,
                    secret TEXT NOT NULL, UNIQUE(seller, secret)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    seller TEXT NOT NULL, order_id TEXT NOT NULL,
                    item TEXT NOT NULL, buyer TEXT NOT NULL,
                    card_id INTEGER NOT NULL UNIQUE REFERENCES cards(id),
                    state TEXT NOT NULL CHECK(state IN ('sending','sent','uncertain')),
                    updated REAL NOT NULL, PRIMARY KEY(seller, order_id)
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    seller TEXT PRIMARY KEY, start_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shipments (
                    seller TEXT NOT NULL, order_id TEXT NOT NULL,
                    paid_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','done','blocked')),
                    attempts INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
                    PRIMARY KEY(seller, order_id),
                    FOREIGN KEY(seller,order_id) REFERENCES deliveries(seller,order_id)
                );
                CREATE TABLE IF NOT EXISTS products (
                    seller TEXT NOT NULL, item TEXT NOT NULL, name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(seller,item)
                );
                CREATE TABLE IF NOT EXISTS delivery_cards (
                    seller TEXT NOT NULL, order_id TEXT NOT NULL,
                    card_id INTEGER PRIMARY KEY REFERENCES cards(id), position INTEGER NOT NULL,
                    FOREIGN KEY(seller,order_id) REFERENCES deliveries(seller,order_id),
                    UNIQUE(seller,order_id,position)
                );
            """)
            db.execute('''INSERT OR IGNORE INTO delivery_cards(seller,order_id,card_id,position)
                SELECT seller,order_id,card_id,0 FROM deliveries''')
            if 'disabled' not in {r[1] for r in db.execute('PRAGMA table_info(cards)')}:
                db.execute('ALTER TABLE cards ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0')
            if 'deleted' not in {r[1] for r in db.execute('PRAGMA table_info(cards)')}:
                db.execute('ALTER TABLE cards ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0')
            if 'editable_after_sale' not in {r[1] for r in db.execute('PRAGMA table_info(cards)')}:
                db.execute('ALTER TABLE cards ADD COLUMN editable_after_sale INTEGER NOT NULL DEFAULT 0')
            db.execute("INSERT OR IGNORE INTO products(seller,item,name) SELECT DISTINCT seller,item,'商品 ' || item FROM cards")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def start_at(self, seller):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts VALUES (?, ?)", (seller, time.time()))
            return db.execute("SELECT start_at FROM accounts WHERE seller=?", (seller,)).fetchone()[0]

    def import_cards(self, seller, item, lines):
        cards = [line.strip() for line in lines if line.strip()]
        if not seller.isdigit() or not item.isdigit():
            raise ValueError("卖家和商品 ID 必须为数字")
        if any(len(card) > 2000 for card in cards):
            raise ValueError("单行卡密超过 2000 字，请检查文件格式")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = db.total_changes
            db.executemany("""INSERT INTO cards(seller,item,secret) VALUES (?,?,?)
                ON CONFLICT(seller,secret) DO UPDATE SET item=excluded.item,deleted=0,disabled=0,editable_after_sale=0
                WHERE cards.deleted=1 AND NOT EXISTS(SELECT 1 FROM delivery_cards d WHERE d.card_id=cards.id)""",
                           ((seller, item, card) for card in cards))
            added = db.total_changes - before
            db.execute("INSERT OR IGNORE INTO products(seller,item,name) VALUES (?,?,?)", (seller,item,'商品 '+item))
        return {"added": added, "duplicates": len(cards) - added}

    def has_stock(self, seller, item, quantity=1):
        self.validate_quantity(quantity)
        with self.connect() as db:
            return db.execute("""SELECT COUNT(*) FROM (SELECT 1 FROM cards c WHERE seller=? AND item=?
                AND disabled=0 AND deleted=0 AND EXISTS(SELECT 1 FROM products p WHERE p.seller=c.seller AND p.item=c.item AND p.enabled=1)
                AND NOT EXISTS(SELECT 1 FROM delivery_cards d WHERE d.card_id=c.id) LIMIT ?)""",
                              (seller, item,quantity)).fetchone()[0] == quantity

    def delivered(self, seller, order_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM deliveries WHERE seller=? AND order_id=?",
                              (seller, order_id)).fetchone() is not None

    def reserve(self, seller, order_id, item, buyer):
        cards=self.reserve_many(seller,order_id,item,buyer,1)
        return cards[0] if cards else None

    @staticmethod
    def validate_quantity(quantity):
        if type(quantity) is not int or not 1<=quantity<=100:
            raise ValueError('每单数量必须为1至100的整数')

    def reserve_many(self, seller, order_id, item, buyer, quantity, validate_message=None):
        """Allocate the complete order atomically; never commit a partial batch."""
        self.validate_quantity(quantity)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM deliveries WHERE seller=? AND order_id=?",
                          (seller, order_id)).fetchone():
                return None
            cards = db.execute("""SELECT id,secret FROM cards c WHERE seller=? AND item=?
                AND disabled=0 AND deleted=0 AND EXISTS(SELECT 1 FROM products p WHERE p.seller=c.seller AND p.item=c.item AND p.enabled=1)
                AND NOT EXISTS(SELECT 1 FROM delivery_cards d WHERE d.card_id=c.id)
                ORDER BY id LIMIT ?""", (seller, item,quantity)).fetchall()
            if len(cards)!=quantity:
                return None
            secrets=[card['secret'] for card in cards]
            if validate_message:validate_message(secrets)
            db.execute("INSERT INTO deliveries(seller,order_id,item,buyer,card_id,state,updated) VALUES (?,?,?,?,?,'sending',?)",
                       (seller, order_id, item, buyer, cards[0]['id'], time.time()))
            db.executemany('INSERT INTO delivery_cards(seller,order_id,card_id,position) VALUES (?,?,?,?)',
                           [(seller,order_id,card['id'],position) for position,card in enumerate(cards)])
            return secrets

    def finish(self, seller, order_id, state, paid_at=None):
        if state not in {"sent", "uncertain"}:
            raise ValueError("无效发货状态")
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state=?,updated=? WHERE seller=? AND order_id=? AND state='sending'",
                       (state, time.time(), seller, order_id))
            if state == 'sent' and paid_at is not None:
                # Same transaction: a crash cannot lose shipping work after marking the card sent.
                db.execute("""INSERT OR IGNORE INTO shipments(seller,order_id,paid_at,state,updated)
                    SELECT seller,order_id,?,'pending',? FROM deliveries
                    WHERE seller=? AND order_id=? AND state='sent'""", (paid_at, time.time(), seller, order_id))

    def pending_shipments(self, seller):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT s.order_id,s.paid_at,s.attempts,d.item,d.buyer,
                (SELECT COUNT(*) FROM delivery_cards a WHERE a.seller=d.seller AND a.order_id=d.order_id) AS quantity
                FROM shipments s JOIN deliveries d ON d.seller=s.seller AND d.order_id=s.order_id
                WHERE s.seller=? AND s.state='pending' AND d.state='sent' ORDER BY s.updated""", (seller,))]

    def shipment_attempt(self, seller, order_id):
        with self.connect() as db:
            db.execute("UPDATE shipments SET attempts=attempts+1,updated=? WHERE seller=? AND order_id=? AND state='pending'",
                       (time.time(), seller, order_id))

    def shipment_result(self, seller, order_id, state):
        if state not in {'pending', 'done', 'blocked'}:
            raise ValueError('无效平台发货状态')
        with self.connect() as db:
            db.execute("UPDATE shipments SET state=?,updated=? WHERE seller=? AND order_id=?",
                       (state, time.time(), seller, order_id))

    def retry_shipping(self, seller, order_id):
        with self.connect() as db:
            cursor = db.execute("UPDATE shipments SET state='pending',attempts=0,updated=? WHERE seller=? AND order_id=? AND state!='done'",
                                (time.time(), seller, order_id))
            if cursor.rowcount != 1:
                raise ValueError('没有可重试的平台发货记录（必须已确认卡密发送）')

    def report(self, seller):
        with self.connect() as db:
            inventory = [dict(r) for r in db.execute("""SELECT c.item, COUNT(*) AS total,
                SUM(CASE WHEN d.card_id IS NULL AND c.disabled=0 THEN 1 ELSE 0 END) AS available
                FROM cards c LEFT JOIN delivery_cards d ON d.card_id=c.id
                WHERE c.seller=? AND c.deleted=0 GROUP BY c.item""", (seller,))]
            deliveries = [dict(r) for r in db.execute("""SELECT d.order_id,d.item,d.buyer,d.state,d.updated,
                (SELECT COUNT(*) FROM delivery_cards a WHERE a.seller=d.seller AND a.order_id=d.order_id) AS quantity
                FROM deliveries d WHERE d.seller=? ORDER BY d.updated DESC""", (seller,))]
            shipments = [dict(r) for r in db.execute('SELECT order_id,state,attempts,updated FROM shipments WHERE seller=?', (seller,))]
            products = [dict(r) for r in db.execute('SELECT item,name,enabled FROM products WHERE seller=? ORDER BY rowid DESC', (seller,))]
        return {"inventory": inventory, "deliveries": deliveries, 'shipments': shipments, 'products':products}

    def product(self, seller, item, name, enabled=True):
        if not seller.isdigit() or not item.isdigit() or not name.strip() or len(name)>120:
            raise ValueError('请填写有效的商品ID和名称（120字以内）')
        with self.connect() as db:
            db.execute('''INSERT INTO products(seller,item,name,enabled) VALUES (?,?,?,?)
                ON CONFLICT(seller,item) DO UPDATE SET name=excluded.name,enabled=excluded.enabled''',
                (seller,item,name.strip(),int(bool(enabled))))

    def list_cards(self, seller, item):
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT c.id,c.secret,c.disabled,c.editable_after_sale,d.order_id,d.state,
                length(c.secret) AS length FROM cards c LEFT JOIN delivery_cards a ON a.card_id=c.id
                LEFT JOIN deliveries d ON d.seller=a.seller AND d.order_id=a.order_id
                WHERE c.seller=? AND c.item=? AND c.deleted=0 ORDER BY c.id DESC LIMIT 500''', (seller,item))]

    def card_value(self, seller, card_id):
        with self.connect() as db:
            row=db.execute('SELECT secret FROM cards WHERE seller=? AND id=? AND deleted=0', (seller,card_id)).fetchone()
            if row is None:
                raise ValueError('卡密不存在')
            return row[0]

    def edit_card(self, seller, card_id, secret=None, disabled=None):
        if secret is not None and (not secret.strip() or len(secret)>2000):
            raise ValueError('卡密不能为空且不可超过2000字')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('''SELECT c.id,c.disabled,c.editable_after_sale,
                EXISTS(SELECT 1 FROM delivery_cards d WHERE d.card_id=c.id) AS allocated
                FROM cards c WHERE seller=? AND id=? AND deleted=0''', (seller,card_id)).fetchone()
            if row is None:
                raise ValueError('卡密不存在')
            if row['allocated']:
                # Re-enable means releasing the old assignment back to stock.
                # For a multi-card order, release the complete batch so the
                # old delivery can never remain as a partial allocation.
                if secret is not None or disabled is not False:
                    raise ValueError('已分配或已发送的卡密只能重新启用')
                orders = [r['order_id'] for r in db.execute(
                    'SELECT DISTINCT order_id FROM delivery_cards WHERE seller=? AND card_id=?', (seller,card_id))]
                if not orders:
                    raise ValueError('卡密分配记录不完整，请人工核对')
                placeholders=','.join('?' for _ in orders)
                assigned = [r['card_id'] for r in db.execute(
                    f'SELECT card_id FROM delivery_cards WHERE seller=? AND order_id IN ({placeholders})', [seller,*orders])]
                db.execute(f'DELETE FROM shipments WHERE seller=? AND order_id IN ({placeholders})', [seller,*orders])
                db.execute(f'DELETE FROM delivery_cards WHERE seller=? AND order_id IN ({placeholders})', [seller,*orders])
                db.execute(f'DELETE FROM deliveries WHERE seller=? AND order_id IN ({placeholders})', [seller,*orders])
                db.executemany('UPDATE cards SET disabled=0,editable_after_sale=0 WHERE id=?',
                               [(card,) for card in assigned])
                return
            if secret is not None:
                db.execute('UPDATE cards SET secret=? WHERE id=?', (secret.strip(),card_id))
            if disabled is not None:
                db.execute('UPDATE cards SET disabled=? WHERE id=?',(int(bool(disabled)),card_id))

    def delete_cards(self, seller, item, ids=None, all_unused=False):
        if not isinstance(all_unused,bool):raise ValueError('无效删除范围')
        if not all_unused and (not isinstance(ids,list) or not 1<=len(ids)<=500
                              or any(type(value) is not int or value<=0 for value in ids)):
            raise ValueError('请选择要删除的卡密，每次最多500条')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            scope='seller=? AND item=? AND deleted=0'
            params=[seller,item]
            if not all_unused:
                ids=list(set(ids))
                scope+=' AND id IN ('+','.join('?' for _ in ids)+')'
                params+=ids
                rows=db.execute('SELECT id FROM cards WHERE '+scope,params).fetchall()
                if len(rows)!=len(ids):raise ValueError('部分卡密不存在或不属于当前商品，请刷新库存')
                if db.execute('SELECT 1 FROM cards c WHERE '+scope+
                              ' AND EXISTS(SELECT 1 FROM delivery_cards d WHERE d.card_id=c.id)',params).fetchone():
                    raise ValueError('已分配或已发送的卡密请先重新启用，再执行此操作')
            # Keep stable IDs and delivery deduplication history. Reimporting a
            # removed unused secret explicitly restores it to the inventory.
            result=db.execute('UPDATE cards AS c SET deleted=1,disabled=1 WHERE '+scope+
                ' AND NOT EXISTS(SELECT 1 FROM delivery_cards d WHERE d.card_id=c.id)',params)
            return result.rowcount

    def reveal(self, seller, order_id):
        """Explicit local recovery only; normal reports never expose cards."""
        with self.connect() as db:
            rows = db.execute("""SELECT c.secret FROM delivery_cards a JOIN cards c ON c.id=a.card_id
                WHERE a.seller=? AND a.order_id=? ORDER BY a.position""", (seller, order_id)).fetchall()
            if not rows:
                raise ValueError("没有此订单的卡密分配记录")
            return '\n'.join(row[0] for row in rows)

    def confirm_sent(self, seller, order_id):
        with self.connect() as db:
            cur = db.execute("UPDATE deliveries SET state='sent',updated=? WHERE seller=? AND order_id=?",
                             (time.time(), seller, order_id))
            if cur.rowcount != 1:
                raise ValueError("没有此订单的卡密分配记录")
