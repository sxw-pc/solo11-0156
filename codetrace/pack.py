import json
from .source import record
from .store import history
def decide(db,pallet_id):
    pallet=db.execute('SELECT * FROM pallet WHERE id=?',(pallet_id,)).fetchone()
    if not pallet:return None
    box_ids=json.loads(pallet['box_ids']);boxes=[db.execute('SELECT * FROM box WHERE id=?',(bid,)).fetchone() for bid in box_ids]
    all_members=[];bad=[]
    for box in boxes:
        if not box:bad.append('box_missing');continue
        members=db.execute('SELECT * FROM box_member WHERE box_id=? AND active=1',(box['id'],)).fetchall()
        if len(members)!=box['capacity']:bad.append('box_count')
        if box['old_seal']:
            prior=db.execute('SELECT * FROM box WHERE seal=?',(box['old_seal'],)).fetchone()
            broken=db.execute('SELECT * FROM box_break WHERE box_id=?',(prior['id'],)).fetchone() if prior else None
            if not box['old_seal_void'] or not prior or prior['state']!='broken' or not broken:bad.append('old_seal_active')
        if box['state']!='sealed':bad.append('box_unsealed')
        all_members.extend(members)
    if len({m['unit_id'] for m in all_members})!=len(all_members):bad.append('unit_in_two_boxes')
    for m in all_members:
        decision=db.execute('SELECT * FROM unit_decision WHERE unit_id=?',(m['unit_id'],)).fetchone()
        unit=db.execute('SELECT * FROM physical_unit WHERE id=?',(m['unit_id'],)).fetchone()
        if not decision or decision['state']!='active' or not unit or unit['batch']!=pallet['batch']:bad.append('invalid_member')
        other=db.execute('SELECT count(*) FROM box_member WHERE unit_id=? AND active=1',(m['unit_id'],)).fetchone()[0]
        if other>1:bad.append('unit_in_two_boxes')
    allocation=db.execute('SELECT * FROM allocation_decision WHERE batch=?',(pallet['batch'],)).fetchone()
    if not allocation or allocation['state']!='allocated':bad.append('allocation_hold')
    state,reason=('blocked',','.join(sorted(set(bad)))) if bad else ('shippable','all sealed boxes contain uniquely active units')
    old=db.execute('SELECT * FROM shipment_decision WHERE pallet_id=?',(pallet_id,)).fetchone()
    if not old or (old['state'],old['reason'])!=(state,reason):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO shipment_decision VALUES(?,?,?,?) ON CONFLICT(pallet_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,version=excluded.version',(pallet_id,state,reason,ver))
        history(db,'shipment',pallet_id,state,reason,pallet_id)
        if state=='blocked' and pallet['shipped_at']:
            db.execute('INSERT INTO future_code_hold VALUES(?,?,?) ON CONFLICT(batch) DO UPDATE SET state=excluded.state,reason=excluded.reason',(pallet['batch'],'blocked','shipped pallet later found with invalid member or seal'))
    return dict(db.execute('SELECT * FROM shipment_decision WHERE pallet_id=?',(pallet_id,)).fetchone())
def reconcile_batch(db,batch):
    return [decide(db,r['id']) for r in db.execute('SELECT id FROM pallet WHERE batch=? ORDER BY id',(batch,)).fetchall()]
def import_data(db,data):
    touched=set()
    with db:
        for br in data.get('breaks',[]):
            if record(db,'box-break',br):
                db.execute('INSERT INTO box_break VALUES(?,?,?,?)',(br['id'],br['box_id'],br['at'],br['witness']))
                db.execute('UPDATE box SET state="broken" WHERE id=?',(br['box_id'],))
                db.execute('UPDATE box_member SET active=0 WHERE box_id=?',(br['box_id'],))
            prior=db.execute('SELECT batch FROM box WHERE id=?',(br['box_id'],)).fetchone()
            if prior:touched.add(prior['batch'])
        for b in data.get('boxes',[]):
            if record(db,'box',b):db.execute('INSERT INTO box VALUES(?,?,?,?,?,?,?)',(b['id'],b['batch'],b['capacity'],b['seal'],b.get('old_seal'),int(b.get('old_seal_void',False)),b['state']))
            touched.add(b['batch'])
        for m in data.get('members',[]):
            if record(db,'member',m):db.execute('INSERT INTO box_member VALUES(?,?,?,?,?)',(m['id'],m['box_id'],m['unit_id'],m['joined_at'],int(m.get('active',True))))
            box=db.execute('SELECT batch FROM box WHERE id=?',(m['box_id'],)).fetchone()
            if box:touched.add(box['batch'])
        for p in data.get('pallets',[]):
            if record(db,'pallet',p):db.execute('INSERT INTO pallet VALUES(?,?,?,?)',(p['id'],p['batch'],json.dumps(p['box_ids']),p.get('shipped_at')))
            touched.add(p['batch'])
        return [x for batch in sorted(touched) for x in reconcile_batch(db,batch)]
