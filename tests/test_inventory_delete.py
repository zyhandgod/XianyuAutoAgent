import tempfile
import unittest
from pathlib import Path

from admin import Admin
from card_store import CardStore


class InventoryDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'.env').write_text('COOKIES_STR="unb=1001;_m_h5_tk=test"')
        self.admin=Admin(self.root);self.store=self.admin.store
        self.store.import_cards('1001','2001',['A','B','C'])

    def test_single_batch_and_explicit_reimport_restores_stable_id(self):
        rows=self.store.list_cards('1001','2001');ids=[row['id'] for row in rows[:2]]
        result=self.admin.action('delete-cards',{'item':'2001','ids':ids})
        self.assertIn('2',result['message'])
        self.assertEqual(self.store.report('1001')['inventory'][0]['available'],1)
        self.assertEqual(self.store.report('1001')['inventory'][0]['total'],1)
        for card_id in ids:
            with self.assertRaises(ValueError):self.store.card_value('1001',card_id)
            with self.assertRaises(ValueError):self.store.edit_card('1001',card_id,disabled=False)
        self.assertEqual(self.store.import_cards('1001','2001',['C'])['added'],1)
        self.assertEqual(self.store.list_cards('1001','2001')[0]['id'],rows[0]['id'])
        self.assertEqual(self.store.import_cards('1001','2001',['C'])['duplicates'],1)

    def test_allocated_after_selection_blocks_entire_batch_and_keeps_history(self):
        rows=self.store.list_cards('1001','2001')
        self.store.reserve('1001','9001','2001','3001')
        for status in ['sending','uncertain','sent']:
            if status!='sending':
                with self.store.connect() as db:db.execute('UPDATE deliveries SET state=?',(status,))
            with self.assertRaises(ValueError):self.store.delete_cards('1001','2001',[r['id'] for r in rows])
            self.assertEqual(len(self.store.list_cards('1001','2001')),3)
        self.assertEqual(self.store.delete_cards('1001','2001',all_unused=True),2)
        self.assertEqual(self.store.reveal('1001','9001'),'A')
        self.assertEqual(self.store.import_cards('1001','2001',['A'])['duplicates'],1)
        self.assertIsNone(self.store.reserve('1001','9001','2001','3001'))

    def test_all_includes_disabled_and_records_outside_visible_500(self):
        self.store.import_cards('1001','2001',[f'EXTRA-{i}' for i in range(600)])
        self.store.edit_card('1001',self.store.list_cards('1001','2001')[0]['id'],disabled=True)
        self.assertEqual(len(self.store.list_cards('1001','2001')),500)
        self.assertEqual(self.store.delete_cards('1001','2001',all_unused=True),603)
        self.assertEqual(self.store.list_cards('1001','2001'),[])
        self.assertEqual(self.store.report('1001')['inventory'],[])
        self.assertFalse(self.store.has_stock('1001','2001'))
        self.assertIsNone(self.store.reserve('1001','9001','2001','3001'))

    def test_scope_and_invalid_requests_cannot_change_inventory(self):
        self.store.import_cards('1002','2001',['OTHER'])
        self.store.import_cards('1001','2002',['PRODUCT'])
        wrong=[self.store.list_cards('1002','2001')[0]['id'],self.store.list_cards('1001','2002')[0]['id']]
        for value in [None,[],[True],['1'],[-1],[1]*501,wrong]:
            with self.assertRaises(ValueError):self.store.delete_cards('1001','2001',value)
        with self.assertRaises(ValueError):self.store.delete_cards('1001','2001',all_unused='true')
        self.assertEqual(len(self.store.list_cards('1001','2001')),3)
        self.assertEqual(self.store.delete_cards('1001','2001',all_unused=True),3)
        self.assertEqual(len(self.store.list_cards('1002','2001')),1)
        self.assertEqual(len(self.store.list_cards('1001','2002')),1)
