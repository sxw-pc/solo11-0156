import json
from .store import connect
from . import codes,units,pack,buffer_capture,buffer_reader
def build(db):
    batches=[r[0] for r in db.execute('SELECT DISTINCT batch FROM code_allocation UNION SELECT DISTINCT batch FROM physical_unit UNION SELECT DISTINCT batch FROM pallet ORDER BY batch')]
    return [{'batch':b,'allocation':dict(x) if (x:=db.execute('SELECT * FROM allocation_decision WHERE batch=?',(b,)).fetchone()) else None,'units':[dict(x) for x in db.execute('SELECT d.* FROM unit_decision d JOIN physical_unit u ON u.id=d.unit_id WHERE u.batch=? ORDER BY d.unit_id',(b,))],'pallets':[dict(x) for x in db.execute('SELECT p.*,s.state,s.reason FROM pallet p LEFT JOIN shipment_decision s ON s.pallet_id=p.id WHERE p.batch=? ORDER BY p.id',(b,))],'history':[dict(x) for x in db.execute('SELECT subsystem,object_id,state,reason,source_id FROM decision_history WHERE object_id=? OR object_id IN (SELECT id FROM physical_unit WHERE batch=?) OR object_id IN (SELECT id FROM pallet WHERE batch=?) ORDER BY id',(b,b,b))]} for b in batches]
def audit(db):
    replay=connect(':memory:');handlers={'code-import':codes.import_data,'scan-import':units.import_data,'pack-import':pack.import_data,'buffer-import':buffer_capture.import_data,'archive-import':buffer_reader.import_data}
    for r in db.execute('SELECT command,raw FROM file_journal ORDER BY id'):handlers[r['command']](replay,json.loads(r['raw']))
    equal=build(db)==build(replay) and buffer_capture.snapshot(db)==buffer_capture.snapshot(replay) and buffer_reader.snapshot(db)==buffer_reader.snapshot(replay);replay.close();return {'ok':equal,'files':db.execute('SELECT count(*) FROM file_journal').fetchone()[0]}

