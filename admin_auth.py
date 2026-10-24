"""Local accounts and revocable sessions; never store plaintext passwords/tokens."""
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class AuthenticationError(ValueError):
    pass


class AccountStore:
    SESSION_SECONDS=8*3600

    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY,username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    salt TEXT NOT NULL,password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','member')),
                    active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
                    csrf TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS failures(key TEXT PRIMARY KEY,count INTEGER NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bindings(user_id TEXT PRIMARY KEY REFERENCES users(id),seller TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS seller_owners(seller TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id));
                INSERT OR IGNORE INTO seller_owners(seller,user_id) SELECT seller,user_id FROM bindings;
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def password(password,salt):
        return hashlib.scrypt(password.encode('utf-8'),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32).hex()

    @staticmethod
    def validate(username,password):
        if not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,32}',username):
            raise ValueError('用户名需为3–32位字母、数字、下划线、点或短横线')
        if not isinstance(password,str) or not 10<=len(password)<=128:
            raise ValueError('密码长度需为10–128位')

    def needs_setup(self):
        with self.connect() as db:return db.execute('SELECT 1 FROM users LIMIT 1').fetchone() is None

    def create(self,username,password,actor=None,prepare=None):
        self.validate(username,password)
        salt=secrets.token_hex(16)
        password_hash=self.password(password,salt)
        user={'id':secrets.token_hex(16),'username':username,'role':'member','active':1}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            first=db.execute('SELECT 1 FROM users LIMIT 1').fetchone() is None
            if actor is None:
                if not first:raise PermissionError('账号已初始化，请登录后由管理员创建新账号')
                user['role']='admin'
            else:
                admin=db.execute("SELECT 1 FROM users WHERE id=? AND role='admin' AND active=1",(actor,)).fetchone()
                if not admin:raise PermissionError('只有管理员可以创建账号')
            try:
                db.execute('INSERT INTO users VALUES (?,?,?,?,?,1)',(user['id'],username,salt,password_hash,user['role']))
            except sqlite3.IntegrityError:
                raise ValueError('用户名已存在') from None
            seller=prepare(user,first) if prepare else None
            if seller:
                db.execute('INSERT INTO bindings VALUES (?,?)',(user['id'],seller))
                db.execute('INSERT INTO seller_owners VALUES (?,?)',(seller,user['id']))
        return user

    def login(self,username,password,client='local'):
        if not isinstance(username,str) or not isinstance(password,str) or len(username)>128 or len(password)>128:
            raise AuthenticationError('用户名或密码错误')
        key=hashlib.sha256((client+':'+username.lower()).encode()).hexdigest()
        now=time.time()
        with self.connect() as db:
            # Keep password verification and session creation atomic against revoke.
            db.execute('BEGIN IMMEDIATE')
            failure=db.execute('SELECT count,expires FROM failures WHERE key=?',(key,)).fetchone()
            if failure and failure['expires']>now and failure['count']>=8:
                raise AuthenticationError('登录失败次数过多，请5分钟后再试')
            user=db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',(username,)).fetchone()
            salt=user['salt'] if user else '00'*16
            candidate=self.password(password,salt)
            valid=user is not None and user['active'] and hmac.compare_digest(candidate,user['password_hash'])
            if not valid:
                count=failure['count']+1 if failure and failure['expires']>now else 1
                db.execute('INSERT OR REPLACE INTO failures VALUES (?,?,?)',(key,count,now+300))
            else:
                db.execute('DELETE FROM failures WHERE key=?',(key,))
                db.execute('DELETE FROM sessions WHERE expires<?',(now,))
                token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                           (hashlib.sha256(token.encode()).hexdigest(),user['id'],csrf,now+self.SESSION_SECONDS))
                result={k:user[k] for k in ('id','username','role','active')}
                return token,{**result,'csrf':csrf}
        raise AuthenticationError('用户名或密码错误')

    def session(self,token):
        if not isinstance(token,str) or not token or len(token)>128:return None
        with self.connect() as db:
            row=db.execute('''SELECT u.id,u.username,u.role,u.active,s.csrf FROM sessions s
                JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1''',
                (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
            return dict(row) if row else None

    def logout(self,token):
        with self.connect() as db:db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

    def users(self,actor):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=? AND role='admin' AND active=1",(actor,)).fetchone():
                raise PermissionError('只有管理员可以管理账号')
            return [dict(r) for r in db.execute('SELECT id,username,role,active FROM users ORDER BY rowid')]

    def change_password(self,user_id,old,new):
        self.validate('valid-user',new)
        with self.connect() as db:
            row=db.execute('SELECT salt,password_hash FROM users WHERE id=? AND active=1',(user_id,)).fetchone()
            if not row or not hmac.compare_digest(self.password(old,row['salt']),row['password_hash']):
                raise ValueError('当前密码不正确')
            salt=secrets.token_hex(16)
            db.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?',(salt,self.password(new,salt),user_id))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))

    def set_active(self,actor,user_id,active):
        self.users(actor)
        if actor==user_id:raise ValueError('不能停用当前管理员账号')
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM users WHERE id=?',(user_id,)).fetchone():raise ValueError('账号不存在')
            db.execute('UPDATE users SET active=? WHERE id=?',(int(bool(active)),user_id))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))

    def bind(self,user_id,seller,save):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM users WHERE id=? AND active=1',(user_id,)).fetchone():
                raise PermissionError('账号已停用，请重新登录')
            other=db.execute('SELECT user_id FROM seller_owners WHERE seller=? AND user_id!=?',(seller,user_id)).fetchone()
            if other:raise ValueError('该闲鱼账号已被其他用户绑定，不能重复接入发货')
            db.execute('INSERT OR IGNORE INTO seller_owners VALUES (?,?)',(seller,user_id))
            db.execute('INSERT INTO bindings VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET seller=excluded.seller',(user_id,seller))
            return save()
