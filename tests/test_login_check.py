import unittest
from unittest.mock import Mock, patch
from login_check import verification_url, LoginRejected, check_login

class LoginCheckTests(unittest.TestCase):
    def test_only_official_https_links(self):
        for value in ['https://h5api.m.goofish.com/check?a=1','https://login.taobao.com/check']:
            self.assertEqual(verification_url(value),value)
        for value in ['javascript:alert(1)','https://goofish.com.evil.test/a','https://evil.test','https://u:p@goofish.com/','http://goofish.com/', 'https://goofish.com:8443/']:
            self.assertIsNone(verification_url(value))

    def test_diagnostic_hides_tokens_and_other_response_fields(self):
        body={'ret':['FAIL_SYS_USER_VALIDATE::private-text','RGV587_ERROR::private-text'], 'data':{'url':'https://h5api.m.goofish.com/check','accessToken':'PRIVATE'}}
        agent=Mock();agent.login_token.side_effect=LoginRejected(body)
        with patch('card_agent.CardAgent',return_value=agent):
            result=check_login('PRIVATE',Mock())
        self.assertEqual(result['status'],'verification_required')
        self.assertEqual(result['verification_url'],body['data']['url'])
        self.assertNotIn('PRIVATE',str(result))
        agent.api.session.close.assert_called_once()

    def test_success_and_malformed_response(self):
        with patch('card_agent.CardAgent'):
            self.assertEqual(check_login('cookie',Mock())['status'],'ok')
        for body in [None,[],{'ret':None},{'data':{'url':'https://evil.test'}}]:
            self.assertIsNone(LoginRejected(body).url)
