"""Short-lived official Goofish QR sessions; credentials never leave the backend."""
import base64
import io
import json
import logging
import re
import secrets
import threading
import time

import requests

HOST = 'https://passport.goofish.com'
TOKEN_URL = 'https://h5api.m.goofish.com/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/'
SLOTS = threading.BoundedSemaphore(2)


class QRLoginError(ValueError):
    def __init__(self, code):
        super().__init__('扫码登录未完成')
        self.code = code


class QRLogin:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.status = 'idle'
        self.image = ''
        self.cookie = ''
        self.created = time.monotonic()
        self.thread = None
        self.stage = 'initializing'
        self.error_code = ''
        self.session_id = secrets.token_urlsafe(24)

    def start(self):
        if not SLOTS.acquire(blocking=False):
            raise ValueError('当前扫码登录人数较多，请稍后再试')
        self.status = 'preparing'
        self.thread = threading.Thread(target=self._run, daemon=True)
        try:
            self.thread.start()
        except BaseException:
            SLOTS.release()
            raise

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            self.status = 'cancelled'
            self.cookie = self.image = ''

    def snapshot(self):
        with self.lock:
            if time.monotonic()-self.created >= 300:
                self.cancelled.set()
                self.status = 'expired'
                self.cookie = self.image = ''
            return {'id':self.session_id, 'status':self.status, 'image':self.image,
                    'error_code':self.error_code, 'stage':self.stage}

    def _update(self, status, image=None, cookie=None):
        with self.lock:
            if self.cancelled.is_set():
                return
            self.status = status
            if image is not None:self.image = image
            if cookie is not None:self.cookie = cookie

    @staticmethod
    def _request(session, method, url, **kwargs):
        # Only fixed official endpoints; never follow arbitrary risk-control URLs.
        response = session.request(method, url, timeout=(8,15), allow_redirects=False, **kwargs)
        response.raise_for_status()
        if response.is_redirect:
            raise ValueError('平台要求额外验证')
        return response

    @staticmethod
    def _cookie_string(session):
        cookies = {}
        for cookie in session.cookies:
            domain = cookie.domain.lstrip('.')
            if domain == 'goofish.com' or domain.endswith('.goofish.com'):
                cookies[cookie.name] = cookie.value
        if not cookies.get('unb','').isdigit():
            raise QRLoginError('missing_account')
        if not cookies.get('_m_h5_tk'):
            raise QRLoginError('missing_token')
        return '; '.join(f'{key}={value}' for key,value in cookies.items())

    def _run(self):
        try:
            self._flow()
            # A disconnected browser must not leave confirmed credentials in RAM
            # indefinitely. Saving/cancelling wakes this bounded cleanup wait.
            if self.status == 'confirmed' and not self.cancelled.wait(max(0,300-(time.monotonic()-self.created))):
                self._update('expired', image='', cookie='')
        except Exception as exc:
            # Errors can contain tokenized URLs, QR secrets or response cookies.
            if isinstance(exc,QRLoginError):
                code=exc.code
            elif isinstance(exc,requests.Timeout):
                code='network_timeout'
            elif isinstance(exc,requests.HTTPError):
                code='http_error'
            elif isinstance(exc,requests.RequestException):
                code='network_error'
            else:
                code='invalid_response'
            with self.lock:self.error_code=code
            logging.warning('QR login failed stage=%s code=%s',self.stage,code)
            self._update('error', image='', cookie='')
        finally:
            SLOTS.release()

    def _flow(self):
        import qrcode
        import qrcode.image.svg
        with requests.Session() as session:
            session.headers.update({'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/133.0.0.0 Safari/537.36',
                                    'Referer':'https://www.goofish.com/', 'Origin':'https://www.goofish.com'})
            self.stage='initializing'
            self._request(session,'GET',TOKEN_URL)
            self.stage='login_page'
            page = self._request(session,'GET',HOST+'/mini_login.htm',params={
                'lang':'zh_cn','appName':'xianyu','appEntrance':'web','styleType':'vertical',
                'isMobile':'false','qrCodeFirst':'true','fromSite':77})
            match = re.search(r'window\.viewData\s*=\s*(\{.*?\});',page.text,re.S)
            if not match:raise ValueError('登录页格式已变化或需要验证')
            params = json.loads(match.group(1))['loginFormData']
            params['umidTag'] = 'SERVER'
            self.stage='generate_qr'
            result = self._request(session,'GET',HOST+'/newlogin/qrcode/generate.do',params=params).json()
            if result.get('content',{}).get('success') is not True:
                raise ValueError('无法生成二维码')
            data = result['content']['data']
            params.update({'t':data['t'],'ck':data['ck']})
            content = data['codeContent']
            if not isinstance(content,str) or not 1 <= len(content) <= 8192:
                raise ValueError('二维码格式错误')
            output = io.BytesIO()
            qrcode.make(content,image_factory=qrcode.image.svg.SvgPathImage).save(output)
            self._update('waiting',image='data:image/svg+xml;base64,'+base64.b64encode(output.getvalue()).decode())
            while not self.cancelled.is_set() and time.monotonic()-self.created < 300:
                self.stage='poll_status'
                result = self._request(session,'POST',HOST+'/newlogin/qrcode/query.do',data=params).json()
                data = result.get('content',{}).get('data',{})
                status = data.get('qrCodeStatus')
                if status == 'CONFIRMED':
                    if data.get('iframeRedirect'):
                        self._update('verification_required',image='')
                        return
                    self.stage='collect_credentials'
                    # Confirmation already returns login cookies. Do not make an
                    # unnecessary anonymous bootstrap call invalidate a valid login.
                    try:
                        cookie=self._cookie_string(session)
                    except QRLoginError as exc:
                        if exc.code!='missing_token':raise
                        self.stage='refresh_token'
                        self._request(session,'GET',TOKEN_URL)
                        cookie=self._cookie_string(session)
                    self._update('confirmed',image='',cookie=cookie)
                    return
                if status in {'EXPIRED','CANCELED','CANCELLED'}:
                    self._update('expired' if status=='EXPIRED' else 'cancelled',image='')
                    return
                if status not in {'NEW','SCANED','SCANNED'}:
                    raise QRLoginError('unknown_scan_status')
                self._update('scanned' if status in {'SCANED','SCANNED'} else 'waiting')
                if self.cancelled.wait(2):return
            self._update('expired',image='',cookie='')
