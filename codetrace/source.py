import hashlib,json
from pathlib import Path
def read(path):
    raw=Path(path).read_bytes();return json.loads(raw),raw
def record(db,kind,item):
    raw=json.dumps(item,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    sha=hashlib.sha256(raw).hexdigest();id=item['id']
    old=db.execute('SELECT sha FROM evidence WHERE id=?',(id,)).fetchone()
    if old:
        if old['sha']!=sha:raise ValueError('evidence identity conflict: '+id)
        return False
    db.execute('INSERT INTO evidence VALUES(?,?,?,?)',(id,kind,sha,raw));return True
def file_record(db,command,raw):
    sha=hashlib.sha256(raw).hexdigest()
    db.execute('INSERT INTO file_journal(command,sha,raw) VALUES(?,?,?)',(command,sha,raw))
