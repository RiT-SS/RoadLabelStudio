import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .domain import KEYS, CLASSES, RULES

def now():
    return datetime.now(timezone.utc).isoformat()

def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ['videos', 'frames', 'imports']:
            (self.root/name).mkdir(exist_ok=True)
        self.path = self.root/'studio.sqlite3'
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY, name TEXT NOT NULL, meta TEXT NOT NULL, created TEXT NOT NULL, active_run TEXT);
              CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id), status TEXT NOT NULL, config TEXT NOT NULL, done INTEGER DEFAULT 0, total INTEGER DEFAULT 0, error TEXT DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS observations(run_id TEXT NOT NULL REFERENCES jobs(id), frame_index INTEGER NOT NULL, scores TEXT NOT NULL, note TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(run_id,frame_index));
              CREATE TABLE IF NOT EXISTS edits(video_id TEXT NOT NULL REFERENCES videos(id), frame_index INTEGER NOT NULL, scores TEXT NOT NULL, note TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL, updated TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(video_id,frame_index));
              CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, video_id TEXT, frame_index INTEGER, payload TEXT NOT NULL, actor TEXT NOT NULL, created TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY, name TEXT NOT NULL, instructions TEXT NOT NULL, enabled INTEGER NOT NULL, revision INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS presence(token_hash TEXT NOT NULL, tab_id TEXT NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(token_hash,tab_id));
              CREATE INDEX IF NOT EXISTS jobs_video ON jobs(video_id,created);
              CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(video_id) WHERE status IN ('queued','running','cancelling');
            ''')
            if 'stage' not in {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN stage TEXT NOT NULL DEFAULT ''")
            for key, rule in zip(KEYS[:7], RULES):
                db.execute('INSERT OR IGNORE INTO skills VALUES(?,?,?,?,?)', (key, CLASSES[key], rule, 1, 1))

    def online_sessions(self, timeout=90):
        current = time.time()
        with self.db() as db:
            return db.execute('''SELECT count(DISTINCT p.token_hash) FROM presence p
                LEFT JOIN sessions s ON s.token_hash=p.token_hash
                WHERE p.last_seen>? AND (p.token_hash='local' OR s.expires>?)''',
                (current-timeout,current)).fetchone()[0]

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def video(self, video_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
        if row is None:
            raise KeyError(video_id)
        return {**dict(row), 'meta': json.loads(row['meta'])}

    def rows(self, video_id, run_id=None):
        v = self.video(video_id)
        run_id = run_id or v['active_run']
        with self.db() as db:
            base = db.execute('SELECT * FROM observations WHERE run_id=? ORDER BY frame_index', (run_id,)).fetchall()
            edits = db.execute('SELECT * FROM edits WHERE video_id=?', (video_id,)).fetchall()
        result = {}
        for row in base:
            result[row['frame_index']] = {'frame_index': row['frame_index'], 'scores': json.loads(row['scores']), 'note': row['note'], 'kind': row['kind'], 'status': 'pending', 'revision': 0, 'ai_scores': json.loads(row['scores']), 'ai_note': row['note'], 'run_id': run_id}
        for row in edits:
            i = row['frame_index']
            previous = result.get(i, {'frame_index': i, 'ai_scores': None, 'ai_note': '', 'run_id': run_id})
            result[i] = {**previous, 'scores': json.loads(row['scores']), 'note': row['note'], 'kind': 'human', 'status': row['status'], 'revision': row['revision'], 'updated': row['updated']}
        return [result[i] for i in sorted(result)]
