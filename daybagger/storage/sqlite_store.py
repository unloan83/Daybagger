import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

class SQLiteControlStore:
    def __init__(self, path:Path): self.path=path
    def initialize(self)->None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL;')
            conn.execute("""CREATE TABLE IF NOT EXISTS system_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL,created_at_utc TEXT NOT NULL,payload_json TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS runtime_boots(boot_id INTEGER PRIMARY KEY AUTOINCREMENT,created_at_utc TEXT NOT NULL,app_name TEXT NOT NULL,environment TEXT NOT NULL,trading_mode TEXT NOT NULL,goldenrules_sha256 TEXT NOT NULL)""")
            conn.commit()
    def record_event(self,event_type:str,payload:dict[str,Any])->None:
        if not event_type.strip(): raise ValueError('event_type is required')
        with self._connect() as conn:
            conn.execute('INSERT INTO system_events(event_type,created_at_utc,payload_json) VALUES (?,?,?)',(event_type,datetime.now(timezone.utc).isoformat(),json.dumps(payload,sort_keys=True,default=str))); conn.commit()
    def record_boot(self,*,app_name:str,environment:str,trading_mode:str,goldenrules_sha256:str)->None:
        with self._connect() as conn:
            conn.execute('INSERT INTO runtime_boots(created_at_utc,app_name,environment,trading_mode,goldenrules_sha256) VALUES (?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),app_name,environment,trading_mode,goldenrules_sha256)); conn.commit()
    def recent_events(self,limit:int=20)->list[dict[str,Any]]:
        with self._connect() as conn:
            rows=conn.execute('SELECT event_type,created_at_utc,payload_json FROM system_events ORDER BY event_id DESC LIMIT ?',(limit,)).fetchall()
        return [{'event_type':r[0],'created_at_utc':r[1],'payload':json.loads(r[2])} for r in rows]
    def _connect(self)->sqlite3.Connection: return sqlite3.connect(self.path)
