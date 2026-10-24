import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from admin import MultiAdmin,handler_for


class PublicDeploymentTests(unittest.TestCase):
    def test_public_origin_secure_cookie_and_disabled_public_bootstrap(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict('os.environ',{'ADMIN_PUBLIC_URL':'https://cards.example.test'}):app=MultiAdmin(Path(folder))
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(app))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(method,path,data=None,origin='https://cards.example.test',token='',csrf=''):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1])
                try:
                    conn.request(method,path,json.dumps(data) if data is not None else None,
                        {'Host':'cards.example.test','Origin':origin,'X-CSRF-Token':csrf,
                         'Content-Type':'application/json','Cookie':'local_admin_session='+token})
                    response=conn.getresponse();return response.status,json.loads(response.read()),dict(response.getheaders())
                finally:conn.close()
            try:
                _,status,_=request('GET','/api/auth/status')
                self.assertTrue(status['needs_setup'])
                credentials={'username':'owner','password':'TemporaryPublicPassword123'}
                self.assertEqual(request('POST','/api/auth/setup',credentials,csrf=status['csrf'])[0],403)
                app.create(credentials['username'],credentials['password'])
                self.assertEqual(request('POST','/api/auth/login',credentials,origin='http://cards.example.test',csrf=status['csrf'])[0],403)
                self.assertEqual(request('POST','/api/auth/login',credentials,origin='https://evil.test',csrf=status['csrf'])[0],403)
                code,login,headers=request('POST','/api/auth/login',credentials,csrf=status['csrf'])
                self.assertEqual(code,200);self.assertIn('; Secure',headers['Set-Cookie'])
                token=headers['Set-Cookie'].split(';')[0].split('=',1)[1]
                self.assertEqual(request('GET','/api/state',token=token)[0],200)
                self.assertEqual(request('GET','/api/state')[0],401)
                _,_,headers=request('POST','/api/auth/logout',{},token=token,csrf=login['csrf'])
                self.assertIn('; Secure',headers['Set-Cookie'])
            finally:server.shutdown();server.server_close();thread.join()

    def test_only_a_single_https_origin_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            for url in ['http://cards.example.test','https://cards.example.test/path','https://user:pass@cards.example.test']:
                with patch.dict('os.environ',{'ADMIN_PUBLIC_URL':url}),self.assertRaises(ValueError):MultiAdmin(Path(folder))
