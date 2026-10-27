"""Authenticated login diagnostics; never expose cookies or access tokens."""
import re
from urllib.parse import urlsplit


def verification_url(value):
    if not isinstance(value,str) or len(value)>16000:return None
    if value.startswith('//'):value='https:'+value
    try:
        parsed=urlsplit(value)
        host=parsed.hostname or ''
        if (parsed.scheme=='https' and not parsed.username and not parsed.password
                and parsed.port in (None,443)
                and any(host==domain or host.endswith('.'+domain)
                        for domain in ('goofish.com','taobao.com','alibaba.com'))):
            return value
    except ValueError:pass
    return None


class LoginRejected(RuntimeError):
    def __init__(self,body):
        super().__init__('登录失败；请在运行设置检测登录状态并完成闲鱼验证')
        body=body if isinstance(body,dict) else {}
        ret=body.get('ret',[])
        self.codes=[value.split('::')[0] for value in ret
                    if isinstance(value,str) and re.fullmatch(r'[A-Z0-9_]+',value.split('::')[0])][:5] if isinstance(ret,list) else []
        data=body.get('data',{})
        self.url=None
        if isinstance(data,dict):
            for key in ('url','redirectUrl','redirectURL','punishUrl'):
                self.url=verification_url(data.get(key))
                if self.url:break


def check_login(cookie,store):
    from card_agent import CardAgent
    try:
        agent=CardAgent(cookie,store)
        try:agent.login_token()
        finally:agent.api.session.close()
        return {'status':'ok','message':'服务器登录检测通过，可以启动发货服务','codes':[]}
    except LoginRejected as exc:
        required=bool(exc.url or 'FAIL_SYS_USER_VALIDATE' in exc.codes or 'RGV587_ERROR' in exc.codes)
        return {'status':'verification_required' if required else 'login_failed',
                'message':'闲鱼要求额外验证，请打开官方验证页面，完成后重新检测' if required else '登录未通过，请重新保存完整 Cookie 后检测',
                'codes':exc.codes,'verification_url':exc.url}
    except ValueError:
        return {'status':'missing_cookie','message':'请先保存完整的闲鱼 Cookie','codes':[]}
    except Exception:
        return {'status':'error','message':'登录检测请求失败，请稍后重试','codes':[]}
