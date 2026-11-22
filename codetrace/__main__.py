import argparse,json
from pathlib import Path
from .store import connect
from .source import read,file_record
from . import codes,units,pack,report,buffer_capture,buffer_reader
HANDLERS={'code-import':codes.import_data,'scan-import':units.import_data,'pack-import':pack.import_data,'buffer-import':buffer_capture.import_data,'archive-import':buffer_reader.import_data}
def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=[*HANDLERS,'demo','report','audit','buffer-report','archive-report']);p.add_argument('--db',default='state.sqlite');p.add_argument('--input');a=p.parse_args();db=connect(a.db)
    if a.command=='archive-report':print(json.dumps(buffer_reader.snapshot(db),ensure_ascii=False,indent=2));return
    if a.command=='buffer-report':print(json.dumps(buffer_capture.snapshot(db),ensure_ascii=False,indent=2));return
    if a.command=='report':print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if a.command=='audit':print(json.dumps(report.audit(db),ensure_ascii=False,indent=2));return
    if a.command=='demo':
        for cmd,name in [('code-import','code_ok.json'),('scan-import','scan_ok.json'),('pack-import','pack_ok.json')]:
            doc,raw=read(Path(__file__).resolve().parents[1]/'fixtures'/name);HANDLERS[cmd](db,doc)
            with db:file_record(db,cmd,raw)
        print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if not a.input:p.error('--input required')
    doc,raw=read(a.input);result=HANDLERS[a.command](db,doc)
    with db:file_record(db,a.command,raw)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()

