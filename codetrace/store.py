import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS code_contract(id TEXT PRIMARY KEY,product TEXT NOT NULL,version INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS code_allocation(id TEXT PRIMARY KEY,batch TEXT NOT NULL,contract_id TEXT NOT NULL,product TEXT NOT NULL,version INTEGER NOT NULL,revision INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,at TEXT NOT NULL,revoked INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS allocation_decision(batch TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,allocation_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS device_cert(id TEXT PRIMARY KEY,device TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,operator TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS physical_unit(id TEXT PRIMARY KEY,batch TEXT NOT NULL,product TEXT NOT NULL,code INTEGER NOT NULL,printed_at TEXT NOT NULL,old_code INTEGER,voided INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS scan_observation(id TEXT PRIMARY KEY,unit_id TEXT NOT NULL,code INTEGER NOT NULL,vision_code INTEGER NOT NULL,device_id TEXT NOT NULL,at TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unit_decision(unit_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,code INTEGER,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS box(id TEXT PRIMARY KEY,batch TEXT NOT NULL,capacity INTEGER NOT NULL,seal TEXT NOT NULL,old_seal TEXT,old_seal_void INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS box_member(id TEXT PRIMARY KEY,box_id TEXT NOT NULL,unit_id TEXT NOT NULL,joined_at TEXT NOT NULL,active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS box_break(id TEXT PRIMARY KEY,box_id TEXT NOT NULL,at TEXT NOT NULL,witness TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pallet(id TEXT PRIMARY KEY,batch TEXT NOT NULL,box_ids TEXT NOT NULL,shipped_at TEXT);
CREATE TABLE IF NOT EXISTS shipment_decision(pallet_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS future_code_hold(batch TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,source_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def history(db,subsystem,object_id,state,reason,source_id=None):
    db.execute('INSERT INTO decision_history(subsystem,object_id,state,reason,source_id) VALUES(?,?,?,?,?)',(subsystem,object_id,state,reason,source_id))
