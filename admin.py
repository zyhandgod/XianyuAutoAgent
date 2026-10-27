"""Loopback-only local management panel. No cloud hosting or new dependencies."""
import argparse
import atexit
import fcntl
import json
import os
import secrets
import signal
import shutil
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import dotenv_values, set_key

from card_store import CardStore
from card_template import load_template, save_template
from qr_login import QRLogin
from admin_auth import AccountStore, AuthenticationError
from utils.xianyu_utils import trans_cookies

ROOT = Path(__file__).resolve().parent


def item_id(value):
    value = str(value).strip()
    if value.isdigit():
        return value
    parsed = urlparse(value)
    if parsed.hostname not in {'www.goofish.com', 'goofish.com'}:
        raise ValueError('请填写闲鱼商品完整链接或数字商品ID')
    query = parse_qs(parsed.query)
    value = (query.get('id') or query.get('itemId') or [''])[0]
    if not value.isdigit():
        raise ValueError('链接中未找到商品ID，请使用网页版商品完整链接')
    return value


def real_cards(text):
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    if not lines or len(lines)>5000:
        raise ValueError('请填写卡密，一行一个，每次最多5000行')
    if any(s.upper().startswith(('MOCK-TEST-', 'DEMO-NOT-REAL-')) for s in lines):
        raise ValueError('测试卡密不能导入正式库存，请填写可用的真实卡密')
    return lines


class Admin:
    def __init__(self, root=ROOT,workspace=None):
        self.root=Path(root)
        self.workspace=Path(workspace) if workspace else self.root
        self.store=CardStore(self.workspace/'data/cards.db')
        self.token=secrets.token_urlsafe(32)
        self.lock=threading.RLock()
        self.process=None
        self.mode='observe'
        self.log_path=self.workspace/'data/service.log'

    def config(self):
        return dotenv_values(self.workspace/'.env')

    def seller(self):
        cookies=trans_cookies(self.config().get('COOKIES_STR') or '')
        if not all(cookies.get(k) for k in ('unb','_m_h5_tk')) or not cookies['unb'].isdigit():
            raise ValueError('请先在运行设置中保存完整的闲鱼 Cookie')
        return cookies['unb']

    def owned_running(self):
        return self.process is not None and self.process.poll() is None

    def external_running(self):
        paths=list((self.workspace/'data').glob('agent-*.lock'))
        try:paths.append(self.root/'data/agent-locks'/f'agent-{self.seller()}.lock')
        except ValueError:pass
        for path in paths:
            if not path.exists():continue
            with path.open('a') as handle:
                try:
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
        return False

    def running(self):
        return self.owned_running() or self.external_running()

    def stop(self,owned_only=False):
        with self.lock:
            if self.owned_running():
                self.process.send_signal(signal.SIGINT)
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    self.process.wait(timeout=5)
            elif not owned_only and self.external_running():
                raise ValueError('检测到其他终端启动的发货进程，请在原终端按 Ctrl+C 停止后再操作后台')
            return {'message':'发货服务已停止'}

    def start(self, mode):
        with self.lock:
            if mode not in {'observe','live','mock'}:
                raise ValueError('无效运行模式')
            if self.running():
                raise ValueError('请先停止当前服务，再切换运行模式')
            seller=self.seller()
            report=self.store.report(seller)
            if mode=='live':
                enabled={p['item'] for p in report['products'] if p['enabled']}
                if not any(r['item'] in enabled and r['available'] for r in report['inventory']) and not self.store.pending_shipments(seller):
                    raise ValueError('请先添加商品并导入真实卡密，再启用真实发货')
            env=os.environ.copy()
            env.pop('COOKIES_STR',None)
            env['CARD_DRY_RUN']='false' if mode=='live' else 'true'
            env['CARD_ADMIN_PID']=str(os.getpid())
            env['CARD_WORKSPACE']=str(self.workspace.resolve())
            env['CARD_LOCK_DIR']=str((self.root/'data/agent-locks').resolve())
            self.log_path.parent.mkdir(exist_ok=True, mode=0o700)
            with self.log_path.open('ab') as log:
                command='mock-chat' if mode=='mock' else 'run'
                self.process=subprocess.Popen([sys.executable,'-u',str(self.root/'cards.py'),command],
                    cwd=self.root,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            self.log_path.chmod(0o600)
            self.mode=mode
            return {'message':'服务已启动，连接状态请查看运行日志'}

    def state(self):
        with self.lock:
            try:
                seller=self.seller()
                report=self.store.report(seller)
            except ValueError:
                seller=''
                report={'products':[],'inventory':[],'deliveries':[],'shipments':[]}
            logs=[]
            if self.log_path.exists():
                # Bounded reads; logs contain operational status, never card contents.
                with self.log_path.open('rb') as handle:
                    handle.seek(max(0,self.log_path.stat().st_size-16000))
                    logs=handle.read().decode('utf-8',errors='replace').splitlines()[-60:]
            return {**report,'seller':seller,'configured':bool(seller),'running':self.running(),
                    'delivery_template':load_template(self.workspace/'data/delivery-template.json'),
                    'mode':self.mode if self.owned_running() or not self.external_running() else 'external',
                    'logs':logs,'csrf':self.token}

    def action(self, action, data):
        with self.lock:
            if action=='check-login':
                from login_check import check_login
                return check_login(self.config().get('COOKIES_STR') or '',self.store)
            if action=='delivery-template':
                save_template(self.workspace/'data/delivery-template.json',data.get('template'))
                return {'message':'发货文案已保存，下一次发卡使用新文案，无需重启服务'}
            if action=='stop':
                return self.stop()
            if action=='start':
                return self.start(data.get('mode'))
            if action=='cookie':
                if self.running():
                    raise ValueError('请先停止服务再更新 Cookie')
                value=data.get('cookie','').strip()
                cookies=trans_cookies(value)
                if not cookies.get('unb','').isdigit() or not cookies.get('_m_h5_tk'):
                    raise ValueError('Cookie 不完整：需要 unb、_m_h5_tk')
                env=self.workspace/'.env'
                if not env.exists():
                    env.touch(mode=0o600)
                set_key(env,'COOKIES_STR',value)
                env.chmod(0o600)
                return {'message':'Cookie 已保存在本机，页面不会回显'}
            seller=self.seller()
            if action=='product':
                self.store.product(seller,item_id(data.get('item','')),str(data.get('name','')),data.get('enabled',True))
            elif action=='import':
                item=item_id(data.get('item',''))
                if item not in {p['item'] for p in self.store.report(seller)['products']}:
                    raise ValueError('请先添加对应商品')
                result=self.store.import_cards(seller,item,real_cards(data.get('text','')))
                return {'message':f"已导入 {result['added']} 条，跳过重复 {result['duplicates']} 条"}
            elif action=='edit-card':
                secret=data.get('secret')
                if secret is not None:
                    real_cards(secret)
                    if len(secret.splitlines())!=1:
                        raise ValueError('修改单个卡密时只能填写一行')
                self.store.edit_card(seller,int(data.get('id',0)),secret,data.get('disabled'))
            elif action=='unlock-card':
                self.store.edit_card(seller,int(data.get('id',0)),disabled=False)
            elif action=='delete-cards':
                count=self.store.delete_cards(seller,item_id(data.get('item','')),data.get('ids'),data.get('all_unused',False))
                return {'message':f'已删除 {count} 条未分配卡密'}
            elif action=='retry-shipping':
                self.store.retry_shipping(seller,str(data.get('order','')))
            else:
                raise ValueError('未知操作')
            return {'message':'已保存'}


class MultiAdmin:
    def __init__(self,root=ROOT):
        self.root=Path(root)
        self.auth=AccountStore(self.root/'data/auth.db')
        self.anonymous_csrf=secrets.token_urlsafe(32)
        self.public_url=os.getenv('ADMIN_PUBLIC_URL','').rstrip('/')
        if self.public_url:
            parsed=urlparse(self.public_url)
            if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError('ADMIN_PUBLIC_URL 必须是完整的 HTTPS 来源地址，不含路径')
        self.managers={}
        self.qr_logins={}
        self.lock=threading.RLock()

    def manager(self,user):
        user_id=user['id']
        if not isinstance(user_id,str) or len(user_id)!=32 or any(c not in '0123456789abcdef' for c in user_id):
            raise ValueError('无效账号标识')
        with self.lock:
            if user_id not in self.managers:
                path=self.root/'data/users'/user_id
                path.mkdir(parents=True,exist_ok=True,mode=0o700)
                self.managers[user_id]=Admin(self.root,path)
            return self.managers[user_id]

    def create(self,username,password,actor=None):
        def prepare(user,first):
            manager=self.manager(user)
            if first:
                # Existing data belongs only to the first owner, never to later accounts.
                legacy=Admin(self.root)
                if legacy.running():raise ValueError('请先停止旧的命令行发货进程，再初始化账号')
                if (self.root/'.env').exists():
                    shutil.copyfile(self.root/'.env',manager.workspace/'.env')
                    (manager.workspace/'.env').chmod(0o600)
                with sqlite3.connect(legacy.store.path) as source,sqlite3.connect(manager.store.path) as target:
                    source.backup(target)
                try:return manager.seller()
                except ValueError:return None
            return None
        with self.lock:return self.auth.create(username,password,actor,prepare)

    def state(self,user):
        return {**self.manager(user).state(),'csrf':user['csrf'],
                'user':{k:user[k] for k in ('username','role')}}

    def action(self,user,action,data):
        manager=self.manager(user)
        with self.lock:
            if action.startswith('qr-login-'):
                current=self.qr_logins.get(user['id'])
                if action=='qr-login-start':
                    if manager.running():raise ValueError('请先停止发货服务，再扫码登录')
                    if current and current.snapshot()['status'] in {'preparing','waiting','scanned','confirmed'}:
                        return current.snapshot()
                    current=QRLogin()
                    current.start()
                    self.qr_logins[user['id']]=current
                    return current.snapshot()
                if not current or data.get('id')!=current.session_id:
                    raise ValueError('扫码会话已失效，请重新生成二维码')
                if action=='qr-login-cancel':
                    current.cancel()
                    return current.snapshot()
                if action=='qr-login-status':
                    snapshot=current.snapshot()
                    if snapshot['status']=='confirmed':
                        if manager.running():
                            current.cancel()
                            raise ValueError('服务已启动，登录信息未更新，请停止服务后重新扫码')
                        with current.lock:
                            value=current.cookie
                            seller=trans_cookies(value).get('unb','')
                            try:
                                self.auth.bind(user['id'],seller,lambda:manager.action('cookie',{'cookie':value}))
                            finally:
                                current.cancel()
                            current.status='saved'
                        return {'id':current.session_id,'status':'saved','image':''}
                    return snapshot
                raise ValueError('未知扫码操作')
            if action=='cookie':
                if user['id'] in self.qr_logins:self.qr_logins[user['id']].cancel()
                seller=trans_cookies(data.get('cookie','')).get('unb','')
                if not seller.isdigit():raise ValueError('Cookie 中缺少有效账号标识')
                return self.auth.bind(user['id'],seller,lambda:manager.action(action,data))
            if action=='start':
                if user['id'] in self.qr_logins:self.qr_logins[user['id']].cancel()
                return self.auth.bind(user['id'],manager.seller(),lambda:manager.action(action,data))
            return manager.action(action,data)

    def set_active(self,actor,target,active):
        users=self.auth.users(actor['id'])
        user=next((u for u in users if u['id']==target),None)
        if user is None:raise ValueError('账号不存在')
        if user['id']==actor['id']:raise ValueError('不能停用当前管理员账号')
        with self.lock:
            if not active:
                self.manager(user).stop()
                if target in self.qr_logins:self.qr_logins[target].cancel()
            self.auth.set_active(actor['id'],target,active)

    def stop(self,owned_only=True):
        with self.lock:
            for login in self.qr_logins.values():login.cancel()
            for manager in self.managers.values():manager.stop(owned_only=owned_only)


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def trusted(self):
            port=self.server.server_address[1]
            allowed={f'127.0.0.1:{port}',f'localhost:{port}'}
            if app.public_url:allowed.add(urlparse(app.public_url).netloc)
            return self.headers.get('Host') in allowed

        def trusted_origin(self):
            port=self.server.server_address[1]
            allowed={app.public_url} if app.public_url else {f'http://127.0.0.1:{port}',f'http://localhost:{port}'}
            return self.headers.get('Origin') in allowed

        def session_cookie(self,token=''):
            secure='; Secure' if app.public_url else ''
            age=28800 if token else 0
            return f'local_admin_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}{secure}'

        def session_token(self):
            try:
                cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''))
                return cookie['local_admin_session'].value if 'local_admin_session' in cookie else ''
            except Exception:return ''

        def user(self):
            return app.auth.session(self.session_token())

        def send(self,status,body,content_type='application/json; charset=utf-8',cookie=None):
            payload=body.encode() if isinstance(body,str) else json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            if cookie:self.send_header('Set-Cookie',cookie)
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self.trusted():
                return self.send(403,{'error':'仅支持本机访问'})
            with app.lock:return self.get_response()

        def get_response(self):
            url=urlparse(self.path)
            try:
                user=self.user()
                if url.path=='/api/auth/status':
                    return self.send(200,{'needs_setup':app.auth.needs_setup(),'authenticated':bool(user),
                        'csrf':user['csrf'] if user else app.anonymous_csrf,
                        'user':{k:user[k] for k in ('username','role')} if user else None})
                if url.path.startswith('/api/') and not user:
                    return self.send(401,{'error':'请先登录'})
                if url.path=='/api/state':
                    return self.send(200,app.state(user))
                if url.path=='/api/users':
                    return self.send(200,app.auth.users(user['id']))
                if url.path=='/api/cards':
                    item=parse_qs(url.query).get('item',[''])[0]
                    manager=app.manager(user)
                    return self.send(200,manager.store.list_cards(manager.seller(),item))
                if url.path=='/api/card':
                    card_id=int(parse_qs(url.query).get('id',['0'])[0])
                    manager=app.manager(user)
                    return self.send(200,{'secret':manager.store.card_value(manager.seller(),card_id)})
                static={'/':'index.html','/app.js':'app.js','/auth.js':'auth.js','/style.css':'style.css'}
                if url.path not in static:
                    return self.send(404,{'error':'页面不存在'})
                types={'/':'text/html; charset=utf-8','/app.js':'application/javascript; charset=utf-8','/auth.js':'application/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}
                return self.send(200,(app.root/'admin_static'/static[url.path]).read_text(),types[url.path])
            except PermissionError as exc:
                return self.send(403,{'error':str(exc)})
            except (ValueError,TypeError) as exc:
                return self.send(400,{'error':str(exc)})

        def do_POST(self):
            port=self.server.server_address[1]
            user=self.user()
            expected=user['csrf'] if user else app.anonymous_csrf
            if (not self.trusted() or not self.trusted_origin()
                    or not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),expected)):
                return self.send(403,{'error':'请求来源校验失败，请刷新本地后台'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1024*1024:
                    raise ValueError('请求为空或文件超过1MB')
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    raise ValueError('请求格式应为JSON')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):
                    raise ValueError('请求格式错误')
                if not self.path.startswith('/api/'):
                    return self.send(404,{'error':'接口不存在'})
                if self.path in {'/api/auth/login','/api/auth/setup'}:
                    with app.lock:
                        if self.path.endswith('/setup'):
                            if app.public_url:
                                raise PermissionError('公开部署不开放首次账号初始化，请恢复已有账号数据或在本机完成初始化')
                            app.create(data.get('username'),data.get('password'))
                        token,logged_in=app.auth.login(data.get('username'),data.get('password'),self.client_address[0])
                    return self.send(200,{'message':'登录成功','csrf':logged_in['csrf']},
                        cookie=self.session_cookie(token))
                return self.dispatch_authenticated(data)
            except AuthenticationError as exc:
                return self.send(401,{'error':str(exc)})
            except PermissionError as exc:
                return self.send(403,{'error':str(exc)})
            except sqlite3.IntegrityError:
                return self.send(400,{'error':'卡密已存在，不能重复使用'})
            except (ValueError,TypeError,KeyError) as exc:
                return self.send(400,{'error':str(exc)})
            except Exception:
                return self.send(500,{'error':'操作失败，请检查本地文件权限或服务日志'})

        def dispatch_authenticated(self,data):
            # A slow request body must not preserve authority after logout/disable.
            # Revalidation and every mutation share the same lock as revocation.
            with app.lock:
                user=self.user()
                if not user:return self.send(401,{'error':'请先登录'})
                if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),user['csrf']):
                    return self.send(403,{'error':'登录状态已变化，请刷新页面'})
                if self.path=='/api/auth/logout':
                    if user['id'] in app.qr_logins:app.qr_logins[user['id']].cancel()
                    app.auth.logout(self.session_token())
                    return self.send(200,{'message':'已退出登录'},cookie=self.session_cookie())
                if self.path=='/api/auth/password':
                    app.auth.change_password(user['id'],data.get('old',''),data.get('password'))
                    if user['id'] in app.qr_logins:app.qr_logins[user['id']].cancel()
                    return self.send(200,{'message':'密码已修改，请重新登录'},cookie=self.session_cookie())
                if self.path=='/api/auth/create-user':
                    app.create(data.get('username'),data.get('password'),user['id'])
                    return self.send(200,{'message':'账号已创建，新账号拥有独立工作空间'})
                if self.path=='/api/auth/user-active':
                    app.set_active(user,str(data.get('id','')),bool(data.get('active')))
                    return self.send(200,{'message':'账号状态已更新'})
                return self.send(200,app.action(user,self.path[5:],data))
    return Handler


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description='闲鱼卡密本地管理后台')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    app=MultiAdmin()
    atexit.register(lambda:app.stop(owned_only=True))
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler_for(app))
    print(f'本地管理后台：http://127.0.0.1:{server.server_address[1]} （发货服务默认停止）',flush=True)
    def shutdown_signal(signum,frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,shutdown_signal)
    signal.signal(signal.SIGHUP,shutdown_signal)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.stop(owned_only=True)


if __name__=='__main__':
    main()
