"""Local monitoring journal. Callers provide controlled market/status fields only."""
from datetime import datetime
import json
import sqlite3
from pathlib import Path
from trader import KST


def ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS watch_events (id INTEGER PRIMARY KEY,stamp TEXT,kind TEXT,payload TEXT)')
    db.execute('CREATE INDEX IF NOT EXISTS watch_events_time ON watch_events(stamp)')
    db.execute('CREATE TABLE IF NOT EXISTS watch_settings (name TEXT PRIMARY KEY,value TEXT)')


def append(database,kind,payload,now=None):
    db=sqlite3.connect(database,timeout=0.25)
    try:
        ensure(db)
        with db:db.execute('INSERT INTO watch_events(stamp,kind,payload) VALUES (?,?,?)',((now or datetime.now(KST)).isoformat(),kind,json.dumps(payload,ensure_ascii=False)))
    finally:db.close()


def status(database):
    db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='watch_events'").fetchone():return None
        row=db.execute("SELECT stamp,payload FROM watch_events WHERE kind='HEARTBEAT' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(at=row[0],**json.loads(row[1])) if row else None
    finally:db.close()
