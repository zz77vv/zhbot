"""Offline signed provider fixtures; no real payment/network calls."""
import hashlib
import hmac
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from test_domain import Fixture
from zhbot.okpay import OKPayGateway, OKPayError
from zhbot.vendor import okpay_sign as sdk
from zhbot.payments import Payments

TOKEN='offline-test-token'
SHOP='10001'

def signed(data, **root):
    p={'status':'success','code':200,'id':10001,'data':data,**root}
    p['sign']=sdk.sign(p,TOKEN)
    return p

class Provider:
    def __init__(self):
        self.calls=[]; self.paid=False; self.change={}; self.bad_signature=False; self.timeout_create=False
    def __call__(self,request,timeout):
        params={k:v[0] for k,v in parse_qs(request.data.decode()).items()}
        self.calls.append((request,params,timeout))
        assert sdk.verify(params,TOKEN)
        assert 'token' not in params
        if request.full_url.endswith('/payLink'):
            if self.timeout_create: raise TimeoutError('provider may have created this order')
            self.unique=params['unique_id']
            self.amount=params['amount']
            data={'order_id':'platform-1','pay_url':'https://t.me/okpay?start=offline','status':0}
        else:
            data={'order_id':'platform-1','unique_id':self.unique,'status':int(self.paid),'amount':self.amount}
            if request.full_url.endswith('checkTransferByTxid'):
                data.update(coin='USDT',pay_user_id=987654321)  # third-party payer
            data.update(self.change)
        p=signed(data)
        if self.bad_signature: p['sign']='0'*64
        response=io.BytesIO(json.dumps(p).encode()); response.status=200
        return response

class SDKTests(unittest.TestCase):
    def test_base_preserves_zero_false_amount_and_unicode(self):
        self.assertEqual('amount=100.50&data.coin=USDT&data.flag=false&data.status=0&name=充值 空格',sdk.build_base({'sign':'skip','amount':'100.50','data':{'coin':'USDT','status':0,'flag':False,'empty':'','none':None},'name':'充值 空格'}))
    def test_hmac_sha256_against_rfc4231_case_2(self):
        # Known RFC4231 output independently validates the Python HMAC primitive;
        # official A/B/C vectors are not included in the uploaded archive.
        self.assertEqual('5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843',hmac.new(b'Jefe',b'what do ya want for nothing?',hashlib.sha256).hexdigest())
    def test_sdk_sign_matches_independent_hmac(self):
        base=b'amount=1&id=10001&nonce=abc12345&timestamp=1700000000'
        p={'id':10001,'amount':'1','timestamp':1700000000,'nonce':'abc12345'}
        expected=hmac.new(TOKEN.encode(),base,hashlib.sha256).hexdigest().upper()
        self.assertEqual(expected,sdk.sign(p,TOKEN)); self.assertTrue(sdk.verify(dict(p,sign=expected),TOKEN))
        self.assertFalse(sdk.verify(dict(p,sign='0'*64),TOKEN))

class OKPayTests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp(); self.provider=Provider()
        self.g=OKPayGateway(SHOP,TOKEN,opener=self.provider)
        self.p=Payments(self.s,self.g,SHOP)
    def test_real_adapter_builds_signed_form_and_persists_platform_id(self):
        o=self.p.create(1,2)
        row=self.s.one('SELECT * FROM payments WHERE id=?',(o['id'],))
        self.assertEqual('platform-1',row['platform_id']); self.assertEqual('pending',row['state'])
        req,params,timeout=self.provider.calls[0]
        self.assertEqual('https://api.okaypay.me/shop/payLink',req.full_url)
        self.assertEqual('POST',req.method); self.assertIn('application/x-www-form-urlencoded',req.get_header('Content-type'))
        self.assertEqual('2',params['amount']); self.assertEqual('USDT',params['coin'])
        self.assertEqual(o['id'],params['unique_id']); self.assertGreaterEqual(len(params['nonce']),8)
    def test_third_party_paid_credit_to_order_creator_exactly_once(self):
        o=self.p.create(1,2); self.provider.paid=True
        self.p.check(1,o['id']); self.p.check(1,o['id'])
        self.assertEqual((12,0),self.s.balance(1)); self.assertEqual((0,0),self.s.balance(987654321))
        self.assertTrue(self.provider.calls[-1][0].full_url.endswith('checkTransferByTxid'))
        self.assertEqual('platform-1',self.provider.calls[-1][1]['txid'])
    def test_pending_response_cannot_credit(self):
        o=self.p.create(1,2); self.p.check(1,o['id'])
        self.assertEqual((10,0),self.s.balance(1))
        self.assertEqual(2,len(self.provider.calls))
    def test_bad_signature_and_signed_mismatches_cannot_credit(self):
        for changes in [{'order_id':'another'},{'unique_id':'another'},{'amount':'3'},{'coin':'TRX'},{'status':True}]:
            with self.subTest(changes=changes):
                o=self.p.create(1,2); self.provider.paid=True; self.provider.change=changes
                with self.assertRaises(OKPayError): self.p.check(1,o['id'])
                self.provider.change={}
        self.provider.bad_signature=True
        with self.assertRaises(OKPayError): self.p.check(1,o['id'])
        self.assertEqual((10,0),self.s.balance(1))
    def test_create_timeout_recover_via_same_unique_without_recreating(self):
        self.provider.timeout_create=True
        with self.assertRaises(Exception): self.p.create(1,2)
        o=self.s.rows('SELECT * FROM payments')[0]
        self.provider.unique=o['id']; self.provider.amount='2'; self.provider.paid=True
        self.p.check(1,o['id']); self.assertEqual((12,0),self.s.balance(1))
        self.assertEqual(1,sum(req.full_url.endswith('payLink') for req,_,_ in self.provider.calls))
    def test_repeated_create_reuses_pending_order(self):
        a=self.p.create(1,2); b=self.p.create(1,2)
        self.assertEqual(a['id'],b['id']); self.assertEqual(1,len(self.provider.calls))
    def test_unknown_order_blocks_recreation_but_can_be_checked(self):
        self.provider.timeout_create=True
        with self.assertRaises(Exception): self.p.create(1,2)
        with self.assertRaises(Exception): self.p.create(1,2)
        self.assertEqual(1,len(self.provider.calls))
    def test_numeric_lexeme_preserved_and_duplicate_keys_rejected(self):
        body=signed({'order_id':'platform-1','unique_id':'local','amount':'2.00','coin':'USDT','status':1})
        raw=json.dumps(body).replace('"amount": "2.00"','"amount": 2.00').encode()
        self.g.opener=lambda *a,**kw:io.BytesIO(raw)
        evidence=self.g.verify_query({'id':'local','amount':2,'currency':'USDT','merchant':SHOP,'platform_id':'platform-1'})
        self.assertTrue(evidence['paid'])
        duplicate=raw.replace(b'"code": 200',b'"code": 200, "code": 200')
        self.g.opener=lambda *a,**kw:io.BytesIO(duplicate)
        with self.assertRaises(OKPayError): self.g.verify_query({'id':'local','amount':2,'currency':'USDT','merchant':SHOP,'platform_id':'platform-1'})
    def test_unsigned_warning_is_error_and_never_credits(self):
        o=self.p.create(1,2)
        self.g.opener=lambda *a,**kw:io.BytesIO(b'{"status":"warning","msg":"untrusted"}')
        with self.assertRaises(OKPayError): self.p.check(1,o['id'])
        self.assertEqual((10,0),self.s.balance(1))

    def test_signed_wrong_merchant_and_missing_signature_are_rejected(self):
        o=self.p.create(1,2)
        for body in [signed({'order_id':'platform-1','unique_id':o['id'],'amount':'2','status':1},id=99999),
                     {'status':'success','code':200,'id':10001,'data':{'status':1}}]:
            self.g.opener=lambda *args,body=body,**kwargs:io.BytesIO(json.dumps(body).encode())
            with self.assertRaises(OKPayError): self.p.check(1,o['id'])
        self.assertEqual((10,0),self.s.balance(1))
    def test_second_query_requires_coin_and_same_platform_id(self):
        o=self.p.create(1,2); self.provider.paid=True
        for change in ['missing_coin','wrong_platform','pending']:
            def opener(request,timeout):
                raw=self.provider(request,timeout)
                body=json.loads(raw.getvalue())
                if request.full_url.endswith('checkTransferByTxid'):
                    if change=='missing_coin': body['data'].pop('coin')
                    elif change=='wrong_platform': body['data']['order_id']='other-platform'
                    else: body['data']['status']=0
                    body['sign']=sdk.sign(body,TOKEN)
                return io.BytesIO(json.dumps(body).encode())
            self.g.opener=opener
            with self.assertRaises(OKPayError): self.p.check(1,o['id'])
        self.assertEqual((10,0),self.s.balance(1))
    def test_http_errors_limits_and_response_text_do_not_leak(self):
        o=self.p.create(1,2)
        cases=[b'not-json SECRET',b'x'*(256*1024+1),b'{"status":"success","code":200,"id":10001,"data":[]}']
        for raw in cases:
            self.g.opener=lambda *args,raw=raw,**kwargs:io.BytesIO(raw)
            with self.assertRaises(OKPayError) as error: self.p.check(1,o['id'])
            self.assertNotIn('SECRET',str(error.exception)); self.assertNotIn(TOKEN,str(error.exception))
    def test_no_redirects_and_new_nonce_per_query(self):
        from zhbot.okpay import NoRedirect
        with self.assertRaises(OKPayError): NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example')
        o=self.p.create(1,2); self.p.check(1,o['id']); self.p.check(1,o['id'])
        nonces=[params['nonce'] for _,params,_ in self.provider.calls]
        self.assertEqual(len(nonces),len(set(nonces)))

class FixedSDKCases(unittest.TestCase):
    def test_custom_cross_language_cases(self):
        from pathlib import Path
        cases=json.loads((Path(__file__).parent/'fixtures/okpay_sdk_cases.json').read_text())
        for case in cases:
            with self.subTest(params=case['params']):
                self.assertEqual(case['base'],sdk.build_base(case['params']))
                self.assertEqual(case['signature'],sdk.sign(case['params'],'offline-cross-language'))
