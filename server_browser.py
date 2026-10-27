"""Short-lived, per-user browser controlled only by authenticated management APIs."""
import base64
import multiprocessing
import os
import secrets
import signal
import time

WIDTH, HEIGHT = 1100, 780


def _worker(pipe, cookie):
    # Separate process: Playwright's sync API must stay on its owning thread.
    os.setsid()
    browser = display = None
    try:
        import select
        import subprocess
        from playwright.sync_api import sync_playwright
        from utils.xianyu_utils import trans_cookies
        from XianyuApis import XianyuApis
        read_fd, write_fd = os.pipe()
        try:
            display = subprocess.Popen(['Xvfb','-displayfd',str(write_fd),'-screen','0',f'{WIDTH}x{HEIGHT}x24','-nolisten','tcp'],
                                       pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.close(write_fd)
            if not select.select([read_fd],[],[],8)[0]:raise RuntimeError('display unavailable')
            number=os.read(read_fd,64).decode().strip()
            if not number.isdigit():raise RuntimeError('display unavailable')
            os.environ['DISPLAY']=':'+number
        finally:
            os.close(read_fd)
        if os.path.isdir('/opt/card-console/ms-playwright'):
            os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH','/opt/card-console/ms-playwright')
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=False,args=['--no-sandbox','--disable-dev-shm-usage'])
            api=XianyuApis()
            ua=api.session.headers['user-agent'];api.session.close()
            context=browser.new_context(viewport={'width':WIDTH,'height':HEIGHT},user_agent=ua,locale='zh-CN')
            context.add_cookies([{'name':k,'value':v,'domain':'.goofish.com','path':'/','secure':True}
                                 for k,v in trans_cookies(cookie).items()])
            cookie=None
            page=context.new_page()
            page.set_default_timeout(8000)
            try:page.goto('https://www.goofish.com/',wait_until='domcontentloaded',timeout=30000)
            except Exception:pass
            # Obtain the challenge in this browser's cookie context, not a discarded requests session.
            import json
            from utils.xianyu_utils import generate_device_id, generate_sign
            from login_check import LoginRejected
            values={c['name']:c['value'] for c in context.cookies('https://www.goofish.com/')}
            if values.get('unb') and values.get('_m_h5_tk'):
                payload=json.dumps({'appKey':'444e9908a51d1cb236a27862abc769c9','deviceId':generate_device_id(values['unb'])},separators=(',',':'))
                stamp=str(int(time.time()*1000));name='mtop.taobao.idlemessage.pc.login.token'
                try:
                    response=context.request.post(f'https://h5api.m.goofish.com/h5/{name}/1.0/',
                        params={'jsv':'2.7.2','appKey':'34839810','t':stamp,'sign':generate_sign(stamp,values['_m_h5_tk'].split('_')[0],payload),
                                'v':'1.0','type':'originaljson','accountSite':'xianyu','dataType':'json','api':name,'sessionOption':'AutoLoginOnly'},
                        form={'data':payload},timeout=25000)
                    url=LoginRejected(response.json()).url
                    if url:page.goto(url,wait_until='domcontentloaded',timeout=20000)
                except Exception:pass
            pipe.send({'status':'ready'})
            deadline=time.monotonic()+600
            while time.monotonic()<deadline:
                if not pipe.poll(1):continue
                command=pipe.recv()
                kind=command.get('kind')
                if kind=='close':break
                try:
                    if kind=='pointer':
                        points=command['points']
                        page.mouse.move(*points[0])
                        page.mouse.down()
                        try:
                            for point in points[1:]:
                                page.mouse.move(*point)
                                page.wait_for_timeout(18)
                        finally:page.mouse.up()
                    elif kind=='wheel':page.mouse.wheel(0,command['delta'])
                    elif kind=='key':page.keyboard.press(command['key'])
                    elif kind=='text':page.keyboard.insert_text(command['text'])
                    elif kind=='home':page.goto('https://www.goofish.com/',wait_until='domcontentloaded',timeout=20000)
                    elif kind=='refresh':page.reload(wait_until='domcontentloaded',timeout=20000)
                    elif kind=='cookies':
                        # Read only Goofish cookies: this private reply never goes to HTTP clients.
                        values={c['name']:c['value'] for c in context.cookies('https://www.goofish.com/')}
                        pipe.send({'cookie':'; '.join(f'{k}={v}' for k,v in values.items())})
                        continue
                    # Popups (e.g. official Taobao login) become the displayed page.
                    pages=[p for p in context.pages if not p.is_closed()]
                    if pages:page=pages[-1]
                    image=base64.b64encode(page.screenshot(type='jpeg',quality=65,timeout=8000)).decode()
                    pipe.send({'status':'ready','image':'data:image/jpeg;base64,'+image,'width':WIDTH,'height':HEIGHT})
                except Exception:
                    pipe.send({'status':'ready','error':'页面操作未完成，请刷新画面后重试'})
            browser.close();browser=None
    except Exception:
        try:pipe.send({'status':'error','error':'服务器浏览器暂不可用，请关闭后重试'})
        except Exception:pass
    finally:
        if browser:
            try:browser.close()
            except Exception:pass
        if display:
            display.terminate()
            try:display.wait(timeout=3)
            except Exception:display.kill()
        pipe.close()


class ServerBrowser:
    def __init__(self,cookie):
        self.id=secrets.token_urlsafe(24)
        self.created=time.monotonic()
        self.ready=False
        self.closed=False
        ctx=multiprocessing.get_context('spawn')
        self.pipe,child=ctx.Pipe()
        self.process=ctx.Process(target=_worker,args=(child,cookie),daemon=True)
        self.process.start();child.close()

    def close(self):
        if self.closed:return
        self.closed=True
        try:self.pipe.send({'kind':'close'})
        except Exception:pass
        self.process.join(timeout=2)
        if self.process.is_alive():
            try:
                if os.getpgid(self.process.pid)==self.process.pid:os.killpg(self.process.pid,signal.SIGTERM)
                else:self.process.terminate()
            except ProcessLookupError:pass
            self.process.join(timeout=2)
            if self.process.is_alive():
                try:
                    if os.getpgid(self.process.pid)==self.process.pid:os.killpg(self.process.pid,signal.SIGKILL)
                    else:self.process.kill()
                except ProcessLookupError:pass
                self.process.join(timeout=2)
        self.pipe.close()

    def exchange(self,kind='status',**kwargs):
        if self.closed or time.monotonic()-self.created>600:
            self.close();raise ValueError('验证窗口已过期，请重新打开')
        if not self.ready:
            if not self.pipe.poll():
                if not self.process.is_alive():raise ValueError('服务器浏览器启动失败，请重新打开')
                return {'id':self.id,'status':'preparing'}
            result=self.pipe.recv()
            if result.get('status')!='ready':
                self.close();return {'id':self.id,**result}
            self.ready=True
        self.pipe.send({'kind':kind,**kwargs})
        if not self.pipe.poll(30):
            self.close();raise ValueError('浏览器响应超时，请重新打开')
        result=self.pipe.recv()
        return {'id':self.id,**result}


def browser_input(data):
    kind=data.get('kind')
    if kind=='pointer':
        points=data.get('points')
        if not isinstance(points,list) or not 1<=len(points)<=120:raise ValueError('无效鼠标操作')
        for point in points:
            if (not isinstance(point,list) or len(point)!=2 or any(type(v) is not int for v in point)
                    or not 0<=point[0]<WIDTH or not 0<=point[1]<HEIGHT):raise ValueError('无效坐标')
        return {'kind':kind,'points':points}
    if kind=='wheel' and type(data.get('delta')) is int and -780<=data['delta']<=780:
        return {'kind':kind,'delta':data['delta']}
    if kind=='key' and data.get('key') in {'Enter','Tab','Backspace','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Control+A'}:
        return {'kind':kind,'key':data['key']}
    if kind=='text' and isinstance(data.get('text'),str) and 1<=len(data['text'])<=128:
        return {'kind':kind,'text':data['text']}
    if kind in {'refresh','home'}:return {'kind':kind}
    raise ValueError('无效浏览器操作')
