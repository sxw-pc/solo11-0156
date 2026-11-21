from .source import record
from .store import history
def overlap(a,b):return a['start']<=b['end'] and b['start']<=a['end']
def decide(db,batch):
    allocations=db.execute('SELECT * FROM code_allocation WHERE batch=? ORDER BY revision DESC,id',(batch,)).fetchall()
    if not allocations:return None
    a=allocations[0]
    contract=db.execute('SELECT * FROM code_contract WHERE id=?',(a['contract_id'],)).fetchone()
    collisions=[r for r in db.execute('SELECT * FROM code_allocation WHERE batch<>? AND revoked=0',(batch,)) if overlap(a,r)]
    used=db.execute('SELECT count(*) FROM physical_unit WHERE batch=? AND voided=0',(batch,)).fetchone()[0]
    hold=db.execute('SELECT * FROM future_code_hold WHERE batch=?',(batch,)).fetchone()
    if not contract or contract['product']!=a['product'] or contract['version']!=a['version'] or contract['signed_by']!='brand_owner' or not(contract['valid_from']<=a['at']<=contract['valid_to']):state,reason='unauthorized','product-version contract not signed or not valid'
    elif a['start']<contract['start'] or a['end']>contract['end'] or a['start']>a['end']:state,reason='outside_range','allocated codes outside signed contract'
    elif collisions:state,reason='overlap','same code numbers allocated to another batch'
    elif a['revoked'] and used:state,reason='revoked_used','used codes remain traceable but future printing blocked'
    elif a['revoked']:state,reason='revoked_unused','unused numbers returned to pool'
    elif hold and hold['state']=='blocked' and a['revision']>1:state,reason='shipment_hold','failed shipment requires code manager review before new allocation'
    else:state,reason='allocated','exclusive authorized code segment'
    old=db.execute('SELECT * FROM allocation_decision WHERE batch=?',(batch,)).fetchone()
    if not old or (old['state'],old['reason'],old['allocation_id'])!=(state,reason,a['id']):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO allocation_decision VALUES(?,?,?,?,?) ON CONFLICT(batch) DO UPDATE SET state=excluded.state,reason=excluded.reason,allocation_id=excluded.allocation_id,version=excluded.version',(batch,state,reason,a['id'],ver))
        history(db,'codes',batch,state,reason,a['id'])
    from .units import reconcile_batch
    reconcile_batch(db,batch)
    return dict(db.execute('SELECT * FROM allocation_decision WHERE batch=?',(batch,)).fetchone())
def import_data(db,data):
    touched=set()
    with db:
        for c in data.get('contracts',[]):
            if record(db,'contract',c):db.execute('INSERT INTO code_contract VALUES(?,?,?,?,?,?,?,?)',(c['id'],c['product'],c['version'],c['start'],c['end'],c['valid_from'],c['valid_to'],c['signed_by']))
            touched.update(r['batch'] for r in db.execute('SELECT batch FROM code_allocation WHERE contract_id=?',(c['id'],)))
        for a in data.get('allocations',[]):
            if record(db,'allocation',a):db.execute('INSERT INTO code_allocation VALUES(?,?,?,?,?,?,?,?,?,?)',(a['id'],a['batch'],a['contract_id'],a['product'],a['version'],a.get('revision',1),a['start'],a['end'],a['at'],int(a.get('revoked',False))))
            touched.add(a['batch'])
            touched.update(r['batch'] for r in db.execute('SELECT batch FROM code_allocation WHERE batch<>? AND revoked=0 AND start<=? AND end>=?',(a['batch'],a['end'],a['start'])))
        return [decide(db,b) for b in sorted(touched)]
