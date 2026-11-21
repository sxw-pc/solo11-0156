import copy,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from codetrace import __file__ as imported_file
from codetrace.store import connect
from codetrace import codes,units,pack
ROOT=Path(__file__).resolve().parents[1]
def fx(name):return json.loads((ROOT/'fixtures'/(name+'.json')).read_text(encoding='utf8'))
class CodeTests(unittest.TestCase):
    def setUp(self):self.db=connect(':memory:')
    def tearDown(self):self.db.close()
    def a(self):return self.db.execute('SELECT state FROM allocation_decision WHERE batch="BATCH-1"').fetchone()[0]
    def u(self):return self.db.execute('SELECT state FROM unit_decision WHERE unit_id="UNIT-1"').fetchone()[0]
    def p(self):return self.db.execute('SELECT state FROM shipment_decision WHERE pallet_id="PALLET-1"').fetchone()[0]
    def first(self):codes.import_data(self.db,fx('code_ok'))
    def second(self):self.first();units.import_data(self.db,fx('scan_ok'))
    def good(self):self.second();pack.import_data(self.db,fx('pack_ok'))
    def test_module_local(self):
        print('imported module',imported_file)
        self.assertTrue(str(Path(imported_file).resolve()).startswith(str(ROOT.resolve())))
    def test_three_business_states(self):
        self.good();self.assertEqual((self.a(),self.u(),self.p()),('allocated','active','shippable'))
    def test_product_contract_authority(self):
        self.assertEqual(codes.import_data(self.db,fx('code_bad'))[0]['state'],'unauthorized')
        self.db.close();self.db=connect(':memory:')
        x=fx('code_ok');x['allocations'][0]['start']=900
        self.assertEqual(codes.import_data(self.db,x)[0]['state'],'outside_range')
    def test_overlapping_allocations_block_both_batches_and_unit(self):
        self.good();other=fx('code_ok');other['contracts'][0].update(id='CONTRACT-OTHER',start=1000,end=1999)
        other['allocations'][0].update(id='ALLOC-OTHER',batch='BATCH-OTHER',contract_id='CONTRACT-OTHER',start=1050,end=1150)
        codes.import_data(self.db,other)
        self.assertEqual(self.a(),'overlap');self.assertEqual(self.u(),'allocation_hold');self.assertEqual(self.p(),'blocked')
    def test_revoke_unused_vs_used_numbers(self):
        self.first();revision=fx('code_ok')['allocations'][0].copy();revision.update(id='ALLOC-REV',revision=2,revoked=True)
        codes.import_data(self.db,{'allocations':[revision]});self.assertEqual(self.a(),'revoked_unused')
        self.db.close();self.db=connect(':memory:');self.second()
        codes.import_data(self.db,{'allocations':[revision]});self.assertEqual(self.a(),'revoked_used')
    def test_vision_mismatch_and_calibration(self):
        self.first();units.import_data(self.db,fx('scan_bad'))
        self.assertEqual(self.db.execute('SELECT state FROM unit_decision WHERE unit_id="UNIT-B"').fetchone()[0],'device_invalid')
        self.db.close();self.db=connect(':memory:');self.first()
        x=fx('scan_ok');x['scans'][0]['vision_code']=1002
        self.assertEqual(units.import_data(self.db,x)[0]['state'],'vision_mismatch')
    def test_reprint_requires_voided_old_unit(self):
        self.first();x=fx('scan_ok');x['units'][0]['old_code']=1000
        self.assertEqual(units.import_data(self.db,x)[0]['state'],'reprint_unlinked')
        old={'units':[{'id':'UNIT-OLD','batch':'BATCH-1','product':'P-1','code':1000,'printed_at':'2026-02-01','voided':True}]}
        units.import_data(self.db,old)
        self.assertEqual(self.u(),'reprint_unlinked')
        newer=fx('scan_ok')['units'][0].copy();newer.update(id='UNIT-NEW',code=1003,old_code=1000)
        scan=fx('scan_ok')['scans'][0].copy();scan.update(id='SCAN-NEW',unit_id='UNIT-NEW',code=1003,vision_code=1003)
        self.assertEqual(units.import_data(self.db,{'units':[newer],'scans':[scan]})[-1]['state'],'active')
    def test_duplicate_prints_revoke_active_identity_and_shipment(self):
        self.good();duplicate=fx('scan_ok')['units'][0].copy();duplicate.update(id='UNIT-DUP')
        units.import_data(self.db,{'units':[duplicate]})
        self.assertEqual(self.u(),'duplicate_code');self.assertEqual(self.p(),'blocked')
    def test_one_unit_two_boxes_and_old_seal(self):
        self.good();x=fx('pack_ok');x['boxes'][0].update(id='BOX-2',seal='SEAL-2',old_seal='SEAL-1',old_seal_void=False)
        x['members'][0].update(id='MEMBER-2',box_id='BOX-2')
        x['pallets'][0].update(id='PALLET-2',box_ids=['BOX-2'])
        pack.import_data(self.db,x)
        self.assertEqual(self.p(),'blocked')
        state=self.db.execute('SELECT reason FROM shipment_decision WHERE pallet_id="PALLET-2"').fetchone()[0]
        self.assertIn('unit_in_two_boxes',state)
        self.assertIn('old_seal_active',state)
    def test_break_and_reseal_moves_membership_without_double_box(self):
        self.good()
        update={'breaks':[{'id':'BREAK-1','box_id':'BOX-1','at':'2026-02-04','witness':'warehouse'}],
                'boxes':[{'id':'BOX-NEW','batch':'BATCH-1','capacity':1,'seal':'SEAL-NEW','old_seal':'SEAL-1','old_seal_void':True,'state':'sealed'}],
                'members':[{'id':'MEMBER-NEW','box_id':'BOX-NEW','unit_id':'UNIT-1','joined_at':'2026-02-04','active':True}],
                'pallets':[{'id':'PALLET-NEW','batch':'BATCH-1','box_ids':['BOX-NEW']}]}
        pack.import_data(self.db,update)
        self.assertEqual(self.p(),'blocked')
        self.assertEqual(self.db.execute('SELECT state FROM shipment_decision WHERE pallet_id="PALLET-NEW"').fetchone()[0],'shippable')
        self.assertEqual(self.db.execute('SELECT count(*) FROM box_member WHERE unit_id="UNIT-1" AND active=1').fetchone()[0],1)
    def test_invalid_member_and_count_block_shipping(self):
        self.second();x=fx('pack_ok');x['boxes'][0]['capacity']=2
        pack.import_data(self.db,x)
        self.assertIn('box_count',self.db.execute('SELECT reason FROM shipment_decision').fetchone()[0])
        self.db.close();self.db=connect(':memory:');self.first();units.import_data(self.db,fx('scan_bad'))
        x=fx('pack_bad');x['boxes'][0].update(old_seal_void=True)
        pack.import_data(self.db,x)
        self.assertIn('invalid_member',self.db.execute('SELECT reason FROM shipment_decision').fetchone()[0])
    def test_shipped_bad_box_holds_future_code_allocation(self):
        self.good()
        p={'pallets':[{'id':'PALLET-SHIPPED','batch':'BATCH-1','box_ids':['BOX-MISSING'],'shipped_at':'2026-02-04'}]}
        pack.import_data(self.db,p)
        self.assertEqual(self.db.execute('SELECT state FROM future_code_hold').fetchone()[0],'blocked')
        new=fx('code_ok')['allocations'][0].copy();new.update(id='ALLOC-2',revision=2)
        self.assertEqual(codes.import_data(self.db,{'allocations':[new]})[0]['state'],'shipment_hold')
    def test_identity_conflict_and_cli_replay(self):
        self.first();before=self.db.execute('SELECT count(*) FROM evidence').fetchone()[0]
        x=fx('code_ok');x['allocations'][0]['start']=800
        with self.assertRaises(ValueError):codes.import_data(self.db,x)
        self.assertEqual(self.db.execute('SELECT count(*) FROM evidence').fetchone()[0],before)
        with tempfile.TemporaryDirectory() as d:
            db=str(Path(d)/'demo.sqlite')
            for cmd in ('demo','audit'):
                r=subprocess.run([sys.executable,'-m','codetrace',cmd,'--db',db],cwd=ROOT,capture_output=True,text=True)
                self.assertEqual(r.returncode,0,r.stderr)
                if cmd=='audit':self.assertTrue(json.loads(r.stdout)['ok'])
if __name__=='__main__':unittest.main()
