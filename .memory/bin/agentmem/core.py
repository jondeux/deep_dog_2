"""Core store: event-sourced JSONL source of truth + rebuildable SQLite index.

Layout of a memory root (default ./.memory):
  config.json              settings (see DEFAULT_CONFIG)
  log/raw-YYYY-MM.jsonl    F2 verbatim capture (append-only): {id, ts, role, text, session, agent, user, project, source}
  log/memories.jsonl       curated memory ops (append-only): add / supersede / invalidate / forget / purge
  log/decisions.jsonl      F11 write-decision log (every candidate, incl. NOOP/REJECT)
  log/state.json           cursors (last extracted raw id, transcript offsets)
  skills/<name>.md         approved procedural memory (versions kept as <name>.vN.md)
  skills/proposed/*.md     proposals awaiting the human gate
  scratch/<session>.md     session scratchpad (layer 1)
  MEMORY.md                generated always-loaded core (layer 1)
  index.db                 SQLite index (FTS5 + vectors + graph); delete it and run `reindex` to rebuild
"""
import contextlib, datetime as dt, hashlib, json, os, re, sqlite3, time

TIERS = {"external": 0, "agent": 1, "observed": 2, "operator": 3, "user": 4}  # strict precedence, never a weight
KINDS = ("fact", "rule", "pref", "entity", "relation", "episode", "summary", "artifact", "case")
# F7: per-kind half-life in days for recency decay; None = no decay (until superseded)
HALF_LIFE = {"episode": 30, "summary": 90, "case": 180, "artifact": 180, "fact": 365, "relation": 365,
             "rule": None, "pref": None, "entity": None}
DEFAULT_CONFIG = {
    "version": 2,
    "core_budget_tokens": 700,         # wake-up core (layer 1)
    "inject_budget_tokens": 1200,      # per recall/inject
    "embed": {"provider": "none", "url": "", "model": "", "dims": 0, "api_key_env": "MEM_EMBED_KEY"},
    "llm": {"url": "", "model": "", "api_key_env": "MEM_LLM_KEY"},  # optional, for unattended extraction
    "auto_approve_skills": False,
    "raw_retention_days": 0,           # 0 = keep raw log forever
    "redact": True,
    "dedup_threshold": 0.85,
}
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED:private-key]"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:api-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack-token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED:jwt]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
]


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def age_days(ts):
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - parse_ts(ts)).total_seconds() / 86400)


def new_id(prefix, text=""):
    h = hashlib.sha1(f"{text}{time.time_ns()}{os.getpid()}{os.urandom(6).hex()}".encode()).hexdigest()
    return f"{prefix}-{h[:10]}"


def redact(text, enabled=True):
    if not enabled or not text:
        return text
    for pat, rep in SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


def est_tokens(text):
    return len(text) // 4 + 1


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokens(text):
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def jaccard(a, b):
    sa, sb = set(tokens(a)), set(tokens(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


@contextlib.contextmanager
def file_lock(path, timeout=10.0):
    """Portable advisory lock via O_EXCL lockfile (multi-agent safe appends)."""
    lock = path + ".lock"
    start = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            if time.time() - start > timeout:
                try:  # stale lock (crashed writer): break it
                    if time.time() - os.path.getmtime(lock) > 60:
                        os.remove(lock)
                        continue
                except OSError:
                    pass
                raise TimeoutError(f"lock busy: {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(lock)


SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
  id TEXT PRIMARY KEY, kind TEXT, key TEXT, text TEXT, tier TEXT, tier_rank INT,
  user TEXT, agent TEXT, project TEXT, session TEXT,
  source_ids TEXT, derived_from TEXT, entities TEXT, quote TEXT,
  valid_from TEXT, valid_to TEXT, created TEXT, supersedes TEXT, superseded_by TEXT,
  importance REAL DEFAULT 0.5, pinned INT DEFAULT 0, ttl_days REAL, forgotten INT DEFAULT 0,
  access_count INT DEFAULT 0, last_access TEXT, meta TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id UNINDEXED, text, key, entities, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS kb(id TEXT PRIMARY KEY, path TEXT, chunk INT, text TEXT, sha TEXT, created TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(id UNINDEXED, text, path, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY, dims INT, vec BLOB, text_sha TEXT);
CREATE TABLE IF NOT EXISTS raw(id TEXT PRIMARY KEY, ts TEXT, role TEXT, session TEXT, agent TEXT, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(id UNINDEXED, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS entities(name TEXT PRIMARY KEY, kind TEXT, first_seen TEXT, last_seen TEXT, mentions INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS mentions(entity TEXT, memory_id TEXT, PRIMARY KEY(entity, memory_id));
CREATE TABLE IF NOT EXISTS stats(id TEXT PRIMARY KEY, access_count INT DEFAULT 0, last_access TEXT);
CREATE INDEX IF NOT EXISTS idx_mem_key ON memories(key);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(kind);
"""


def find_root():
    """Where the store is when no --root is given. A hook starts in whatever directory the session is in, so ".memory" under the
    working directory is not enough. In order: MEM_ROOT; the store this script is vendored in (<store>/bin/agentmem/core.py), so a
    hook command anchored to ${CLAUDE_PROJECT_DIR}/.memory/bin/mem.py always uses that project's store, even from inside a nested
    package that has a store of its own; ".memory" in the working directory (the plugin copy, which sits in no store); the nearest
    ".memory" above the working directory, the way git finds ".git". Only a directory with a config.json counts, and with none
    found the answer is still ".memory", so the error stays "no memory at ...". `init` does not call this: it creates a store
    where it is told to."""
    env = os.environ.get("MEM_ROOT")
    if env:
        return env
    own = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if os.path.isfile(os.path.join(own, "config.json")):
        return own
    if os.path.isfile(os.path.join(".memory", "config.json")):
        return ".memory"
    here = os.path.abspath(os.getcwd())
    while os.path.dirname(here) != here:
        here = os.path.dirname(here)
        if os.path.isfile(os.path.join(here, ".memory", "config.json")):
            return os.path.join(here, ".memory")
    return ".memory"


class Store:
    def __init__(self, root=None):
        self.root = os.path.abspath(root or find_root())
        self._db = None
        self.scope = {"user": os.environ.get("MEM_USER", ""), "agent": os.environ.get("MEM_AGENT", ""),
                      "project": os.environ.get("MEM_PROJECT", ""), "session": os.environ.get("MEM_SESSION", "")}

    # ---------- paths / config ----------
    def p(self, *a):
        return os.path.join(self.root, *a)

    def exists(self):
        return os.path.isfile(self.p("config.json"))

    def need(self):
        if not self.exists():
            raise SystemExit(f"no memory at {self.root}; run: mem init")

    @property
    def config(self):
        if not hasattr(self, "_cfg"):
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            if self.exists():
                user = json.load(open(self.p("config.json"), encoding="utf-8"))
                for k, v in user.items():
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
            self._cfg = cfg
        return self._cfg

    def init_dirs(self):
        for d in ("log", "skills", os.path.join("skills", "proposed"), "scratch"):
            os.makedirs(self.p(d), exist_ok=True)
        if not self.exists():
            with open(self.p("config.json"), "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        for f in ("memories.jsonl", "decisions.jsonl"):
            open(self.p("log", f), "a").close()
        self.db  # create schema

    # ---------- JSONL ----------
    def append(self, rel, rec):
        path = self.p(rel)
        with file_lock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def read_jsonl(self, rel):
        path = self.p(rel)
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # tolerate a torn last line
        return out

    def raw_files(self):
        d = self.p("log")
        return sorted(os.path.join("log", f) for f in os.listdir(d) if f.startswith("raw-") and f.endswith(".jsonl")) if os.path.isdir(d) else []

    def state(self):
        path = self.p("log", "state.json")
        return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}

    def save_state(self, st):
        path = self.p("log", "state.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, path)

    # ---------- SQLite index ----------
    @property
    def db(self):
        if self._db is None:
            os.makedirs(self.root, exist_ok=True)
            self._db = sqlite3.connect(self.p("index.db"), timeout=30)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        return self._db

    def reindex(self):
        """Rebuild memories + raw tables from JSONL (source of truth). KB/code graph are rebuilt by ingest."""
        db = self.db
        for t in ("memories", "memories_fts", "raw", "raw_fts", "entities", "mentions"):
            db.execute(f"DELETE FROM {t}")
        for rel in self.raw_files():
            for r in self.read_jsonl(rel):
                self._index_raw(r)
        for ev in self.read_jsonl(os.path.join("log", "memories.jsonl")):
            self._apply_event(ev)
        db.commit()

    # ---------- raw capture (F2) ----------
    def capture(self, role, text, source="cli", session=None, ts=None, meta=None):
        text = redact(text, self.config["redact"])
        if not text or not text.strip():
            return None
        rec = {"id": new_id("r", text), "ts": ts or now(), "role": role, "text": text,
               "session": session or self.scope["session"], "agent": self.scope["agent"],
               "user": self.scope["user"], "project": self.scope["project"], "source": source}
        if meta:
            rec["meta"] = meta
        self.append(os.path.join("log", f"raw-{rec['ts'][:7]}.jsonl"), rec)
        self._index_raw(rec)
        self.db.commit()
        return rec

    def _index_raw(self, r):
        self.db.execute("INSERT OR REPLACE INTO raw(id, ts, role, session, agent, text) VALUES(?,?,?,?,?,?)",
                        (r["id"], r["ts"], r["role"], r.get("session", ""), r.get("agent", ""), r["text"]))
        self.db.execute("INSERT INTO raw_fts(id, text) VALUES(?,?)", (r["id"], r["text"]))

    def raw_by_ids(self, ids):
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return [dict(r) for r in self.db.execute(f"SELECT * FROM raw WHERE id IN ({q}) ORDER BY ts", list(ids))]

    def raw_since(self, after_ts=None, limit=400):
        if after_ts:
            rows = self.db.execute("SELECT * FROM raw WHERE ts > ? ORDER BY ts LIMIT ?", (after_ts, limit))
        else:
            rows = self.db.execute("SELECT * FROM raw ORDER BY ts LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---------- curated memory events (F1, F5, F6) ----------
    def emit(self, ev):
        ev.setdefault("ts", now())
        self.append(os.path.join("log", "memories.jsonl"), ev)
        self._apply_event(ev)
        self.db.commit()
        return ev

    def _apply_event(self, ev):
        db, op = self.db, ev["op"]
        if op == "add":
            m = ev["memory"]
            db.execute("""INSERT OR REPLACE INTO memories(id,kind,key,text,tier,tier_rank,user,agent,project,session,
                source_ids,derived_from,entities,quote,valid_from,valid_to,created,supersedes,superseded_by,
                importance,pinned,ttl_days,forgotten,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                       (m["id"], m["kind"], m.get("key", ""), m["text"], m["tier"], TIERS[m["tier"]],
                        m.get("user", ""), m.get("agent", ""), m.get("project", ""), m.get("session", ""),
                        json.dumps(m.get("source_ids", [])), json.dumps(m.get("derived_from", [])),
                        json.dumps(m.get("entities", [])), m.get("quote", ""), m.get("valid_from") or ev["ts"],
                        m.get("valid_to"), ev["ts"], m.get("supersedes"), None, float(m.get("importance", 0.5)),
                        int(bool(m.get("pinned"))), m.get("ttl_days"), json.dumps(m.get("meta", {}))))
            db.execute("DELETE FROM memories_fts WHERE id=?", (m["id"],))
            db.execute("INSERT INTO memories_fts(id,text,key,entities) VALUES(?,?,?,?)",
                       (m["id"], m["text"], m.get("key", ""), " ".join(m.get("entities", []))))
            for e in m.get("entities", []):
                name = e.strip()
                if not name:
                    continue
                db.execute("""INSERT INTO entities(name,kind,first_seen,last_seen,mentions) VALUES(?,?,?,?,1)
                              ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen, mentions=mentions+1""",
                           (name, "", ev["ts"], ev["ts"]))
                db.execute("INSERT OR IGNORE INTO mentions(entity, memory_id) VALUES(?,?)", (name, m["id"]))
            if m.get("supersedes"):  # F5: close the old version, never delete it
                db.execute("UPDATE memories SET valid_to=?, superseded_by=? WHERE id=? AND valid_to IS NULL",
                           (m.get("valid_from") or ev["ts"], m["id"], m["supersedes"]))
        elif op == "invalidate":
            db.execute("UPDATE memories SET valid_to=? WHERE id=?", (ev.get("valid_to") or ev["ts"], ev["id"]))
        elif op in ("forget", "purge"):
            db.execute("UPDATE memories SET forgotten=1, valid_to=COALESCE(valid_to, ?) WHERE id=?", (ev["ts"], ev["id"]))
            db.execute("DELETE FROM memories_fts WHERE id=?", (ev["id"],))
            db.execute("DELETE FROM vectors WHERE id=?", (ev["id"],))
        elif op == "verify":  # OKF v0.2-style verification signal: verified[{by, at}]
            r = db.execute("SELECT meta FROM memories WHERE id=?", (ev["id"],)).fetchone()
            if r:
                meta = json.loads(r[0] or "{}")
                meta.setdefault("verified", []).append({"by": ev.get("by", "human:user"), "at": ev["ts"]})
                db.execute("UPDATE memories SET meta=? WHERE id=?", (json.dumps(meta), ev["id"]))
        elif op == "pin":
            db.execute("UPDATE memories SET pinned=? WHERE id=?", (int(ev.get("value", 1)), ev["id"]))

    def get(self, mid):
        r = self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        return self.row(r) if r else None

    @staticmethod
    def row(r):
        d = dict(r)
        for k in ("source_ids", "derived_from", "entities", "meta"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    pass
        return d

    def current(self, where="", args=(), as_of=None):
        """Valid, non-forgotten memories (optionally as of a past time) - F5 as-of queries."""
        cond = ["forgotten=0"]
        if as_of:
            cond.append("valid_from<=? AND (valid_to IS NULL OR valid_to>?)")
            args = (as_of, as_of) + tuple(args)
        else:
            cond.append("valid_to IS NULL")
        if where:
            cond.append(where)
        rows = self.db.execute(f"SELECT * FROM memories WHERE {' AND '.join(cond)}", args)
        out = []
        for r in rows:
            d = self.row(r)
            if d.get("ttl_days") and age_days(d["valid_from"]) > float(d["ttl_days"]):
                continue  # F7 TTL for temporary information
            out.append(d)
        return out

    def scope_ok(self, m, scope=None):
        """F-scopes: a record is visible if each of its non-empty scope fields matches the active scope
        (empty active scope = no restriction). user/project are shared across agents; session is not a filter."""
        s = scope or self.scope
        for k in ("user", "project"):
            if m.get(k) and s.get(k) and m[k] != s[k]:
                return False
        return True

    def touch(self, ids):
        t = now()
        for i in ids:
            self.db.execute("""INSERT INTO stats(id, access_count, last_access) VALUES(?,1,?)
                               ON CONFLICT(id) DO UPDATE SET access_count=access_count+1, last_access=excluded.last_access""",
                            (i, t))
        self.db.commit()

    def access(self, mid):
        r = self.db.execute("SELECT access_count FROM stats WHERE id=?", (mid,)).fetchone()
        return r[0] if r else 0

    def decide(self, rec):
        """F11: log every write decision, including NOOP/REJECT."""
        rec.setdefault("ts", now())
        self.append(os.path.join("log", "decisions.jsonl"), rec)
