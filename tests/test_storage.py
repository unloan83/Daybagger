from pathlib import Path
from daybagger.storage.sqlite_store import SQLiteControlStore
def test_event_roundtrip(tmp_path:Path):
    s=SQLiteControlStore(tmp_path/'c.sqlite3'); s.initialize(); s.record_event('TEST_EVENT',{'value':7}); e=s.recent_events(1)[0]; assert e['event_type']=='TEST_EVENT' and e['payload']['value']==7
