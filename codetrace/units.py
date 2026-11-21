from .source import record
from .store import history
def decide(db,unit_id):
    unit=db.execute('SELECT * FROM physical_unit WHERE id=?',(unit_id,)).fetchone()
    if not unit:return None
    alloc=db.execute('SELECT * FROM allocation_decision WHERE batch=?',(unit['batch'],)).fetchone()
    allocation=db.execute('SELECT * FROM code_allocation WHERE id=?',(alloc['allocation_id'],)).fetchone() if alloc else None
    scans=db.execute('SELECT * FROM scan_observation WHERE unit_id=? ORDER BY at DESC,id',(unit_id,)).fetchall()
    scan=scans[0] if scans else None
    cert=db.execute('SELECT * FROM device_cert WHERE id=?',(scan['device_id'],)).fetchone() if scan else None
    duplicates=db.execute('SELECT count(*) FROM physical_unit WHERE code=? AND id<>? AND voided=0',(unit['code'],unit_id)).fetchone()[0]
    old_unit=db.execute('SELECT * FROM physical_unit WHERE code=? AND voided=1',(unit['old_code'],)).fetchone() if unit['old_code'] is not None else None
    if not alloc or alloc['state']!='allocated':state,reason='allocation_hold','batch lacks exclusive valid code allocation'
    elif unit['code']<allocation['start'] or unit['code']>allocation['end'] or unit['product']!=allocation['product']:state,reason='code_outside_range','unit code or product mismatches allocation'
    elif unit['voided']:state,reason='voided','scrapped print remains traceable but cannot ship'
    elif duplicates:state,reason='duplicate_code','same active serial appears on multiple physical units'
    elif unit['old_code'] is not None and not old_unit:state,reason='reprint_unlinked','reprint lacks voided old physical identity'
    elif not scan:state,reason='unscanned','physical unit has no online observation'
    elif not cert or not(cert['valid_from']<=scan['at']<=cert['valid_to']):state,reason='device_invalid','scanner calibration does not cover observation'
    elif scan['vision_code']!=scan['code'] or scan['code']!=unit['code'] or scan['result']!='pass':state,reason='vision_mismatch','visual code and scanner disagree'
    else:state,reason='active','one calibrated scan identifies one physical unit'
    old=db.execute('SELECT * FROM unit_decision WHERE unit_id=?',(unit_id,)).fetchone()
    if not old or (old['state'],old['reason'],old['code'])!=(state,reason,unit['code']):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO unit_decision VALUES(?,?,?,?,?) ON CONFLICT(unit_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,code=excluded.code,version=excluded.version',(unit_id,state,reason,unit['code'],ver))
        history(db,'unit',unit_id,state,reason,scan['id'] if scan else unit_id)
    from .pack import reconcile_batch
    reconcile_batch(db,unit['batch'])
    return dict(db.execute('SELECT * FROM unit_decision WHERE unit_id=?',(unit_id,)).fetchone())
def reconcile_batch(db,batch):
    return [decide(db,r['id']) for r in db.execute('SELECT id FROM physical_unit WHERE batch=? ORDER BY id',(batch,)).fetchall()]
def import_data(db,data):
    touched=set()
    with db:
        for c in data.get('devices',[]):
            if record(db,'device',c):db.execute('INSERT INTO device_cert VALUES(?,?,?,?,?)',(c['id'],c['device'],c['valid_from'],c['valid_to'],c['operator']))
            touched.update(r['unit_id'] for r in db.execute('SELECT unit_id FROM scan_observation WHERE device_id=?',(c['id'],)))
        for u in data.get('units',[]):
            if record(db,'unit',u):db.execute('INSERT INTO physical_unit VALUES(?,?,?,?,?,?,?)',(u['id'],u['batch'],u['product'],u['code'],u['printed_at'],u.get('old_code'),int(u.get('voided',False))))
            touched.add(u['id'])
            touched.update(r['id'] for r in db.execute('SELECT id FROM physical_unit WHERE code=?',(u['code'],)))
        for s in data.get('scans',[]):
            if record(db,'scan',s):db.execute('INSERT INTO scan_observation VALUES(?,?,?,?,?,?,?)',(s['id'],s['unit_id'],s['code'],s['vision_code'],s['device_id'],s['at'],s['result']))
            touched.add(s['unit_id'])
        return [decide(db,u) for u in sorted(touched)]
