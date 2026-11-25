"""配置存储（供应商/阶段模型/音色/鉴权/执行开关）与 SQLite 会话存储。密钥只存服务端。"""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
PROJECTS_DIR = BASE / "projects"
ASSETS_DIR = BASE / "assets"
DATA_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)
ASSETS_DIR.mkdir(exist_ok=True)

SETTINGS_PATH = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "app.db"

DEFAULT_SETTINGS = {
    "providers": [],
    "stage_models": {
        "clarify": None, "routing": None, "concept": None, "storyboard": None,
        "factcheck": None, "visuals": None, "ingest": None,
    },
    "voice": {"voice_id": "", "speed": 0.925,
              "model": "minimax-speech-2.8-hd", "minimax_group_id": "",
              "minimax_api_key": "", "minimax_base_url": "https://ai.ctaigw.cn/v1"},
    "feishu": {"app_id": "", "app_secret": ""},
    # 单用户 token 鉴权：token 即登录密码；为空表示未配置（除 login 外一律 401）
    "auth": {"token": ""},
    # 执行开关：关闭后阶段 6/7/10（生成代码执行）直接失败
    "security": {"allow_generated_code_exec": True},
    # 创作规范（B2-3 ~ B2-6、B2-8）：逐条可关，默认全开
    "content_rules": {
        "huawei_od": True, "no_suspense_opening": True, "no_repeat_example": True,
        "complexity_ending": True, "integer_time": True,
    },
    # 流水线参数：关键帧阶段估算语速（字/秒），默认 5.6 = skill 实测 0.925 倍速
    "pipeline": {"tts_chars_per_second": 5.6},
    # BGM：assets 目录下的文件名；文件不存在时自动关闭并写进 QC 说明
    "bgm": {"enabled": True, "volume": 0.08, "path": "bgm.mp3"},
}

# 项目 id 只允许十六进制（new_id() 为 uuid4 hex[:12]），任何其他形态一律 404
PID_RE = re.compile(r"^[0-9a-f]{8,32}$")

_lock = threading.Lock()


def load_settings():
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        merged["stage_models"] = {**DEFAULT_SETTINGS["stage_models"], **data.get("stage_models", {})}
        merged["voice"] = {**DEFAULT_SETTINGS["voice"], **data.get("voice", {})}
        merged["auth"] = {**DEFAULT_SETTINGS["auth"], **data.get("auth", {})}
        merged["security"] = {**DEFAULT_SETTINGS["security"], **data.get("security", {})}
        return merged
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def save_settings(s):
    with _lock:
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)


def masked_settings():
    """返回给前端的配置：api_key 脱敏。"""
    s = load_settings()
    out = json.loads(json.dumps(s))
    for p in out["providers"]:
        k = p.get("api_key", "")
        p["api_key"] = (k[:6] + "…" + k[-4:]) if len(k) > 12 else ("*" * len(k))
        p["has_key"] = bool(k)
    fs = out.get("feishu", {})
    fs["has_secret"] = bool(fs.get("app_secret"))
    if fs.get("app_secret"):
        fs["app_secret"] = "********"
    if out["voice"].get("minimax_api_key"):
        out["voice"]["minimax_api_key"] = "********"
        out["voice"]["has_minimax_key"] = True
    else:
        out["voice"]["has_minimax_key"] = False
    # 访问密码（token 即密码）：只回显是否已配置，绝不明文回传
    auth = out.setdefault("auth", {})
    tok = auth.get("token", "")
    auth["has_token"] = bool(tok)
    auth["token"] = "********" if tok else ""
    return out


def get_provider(pid):
    for p in load_settings()["providers"]:
        if p["id"] == pid:
            return p
    return None


# 阶段模型解析：指定档位未配置时按回退链取值（routing/ingest → clarify → 任意可用供应商）
def provider_for_key(key):
    s = load_settings()
    sm = s["stage_models"]
    chain = [key]
    if key in ("routing", "ingest"):
        chain.append("clarify")
    for k in chain:
        pid = sm.get(k)
        if pid:
            p = get_provider(pid)
            if p:
                return p
    return s["providers"][0] if s["providers"] else None


# ---------- SQLite（会话 / 消息 / 项目元数据） ----------
# D2：WAL + busy_timeout + 线程级连接复用，消除并发写 locked 与连接堆积

_thread_local = threading.local()


def _conn():
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _thread_local.conn = conn
    return conn


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
            id TEXT PRIMARY KEY, title TEXT, project_id TEXT,
            created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, kind TEXT DEFAULT 'text',
            payload TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY, title TEXT, tier TEXT,
            state TEXT, stage INTEGER DEFAULT 0,
            created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS references_(
            id TEXT PRIMARY KEY, title TEXT, source_url TEXT,
            ingested_via TEXT, summary TEXT, key_points TEXT,
            usable_facts TEXT, suggested_angles TEXT,
            raw_chars INTEGER DEFAULT 0, created REAL);
        CREATE TABLE IF NOT EXISTS project_refs(
            project_id TEXT, ref_id TEXT, created REAL,
            PRIMARY KEY(project_id, ref_id));
        CREATE TABLE IF NOT EXISTS project_events(
            pid TEXT, seq INTEGER, type TEXT, payload TEXT, ts REAL,
            PRIMARY KEY(pid, seq));
        """)
    # 摄取资料标记不可信来源（D4）：旧库补列
    try:
        with _conn() as c:
            c.execute("ALTER TABLE references_ ADD COLUMN untrusted INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass


def now():
    return time.time()


def new_id():
    return uuid.uuid4().hex[:12]


def create_session(title="新对话"):
    sid = new_id()
    with _conn() as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (sid, title, None, now(), now()))
    return sid


def add_message(session_id, role, content, kind="text", payload=None):
    from .redact import redact
    mid = new_id()
    with _conn() as c:
        c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                  (mid, session_id, role, redact(content), kind,
                   json.dumps(payload, ensure_ascii=False) if payload else None, now()))
        c.execute("UPDATE sessions SET updated=? WHERE id=?", (now(), session_id))
    return mid


def list_sessions():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM sessions ORDER BY updated DESC").fetchall()]


def get_session(sid):
    with _conn() as c:
        r = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None


def session_messages(sid):
    with _conn() as c:
        rows = c.execute("SELECT * FROM messages WHERE session_id=? ORDER BY created", (sid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else None
        out.append(d)
    return out


def rename_session(sid, title):
    title = (title or "").strip()[:50]
    if not title:
        return False
    with _conn() as c:
        cur = c.execute("UPDATE sessions SET title=?, updated=? WHERE id=?",
                        (title, now(), sid))
        return cur.rowcount > 0


def bind_project(session_id, project_id):
    with _conn() as c:
        c.execute("UPDATE sessions SET project_id=?, updated=? WHERE id=?",
                  (project_id, now(), session_id))


def create_project(title, tier):
    pid = new_id()
    with _conn() as c:
        c.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?)",
                  (pid, title, tier, "created", 0, now(), now()))
    return pid


def update_project(pid, **kw):
    kw["updated"] = now()
    sets = ",".join(f"{k}=?" for k in kw)
    with _conn() as c:
        c.execute(f"UPDATE projects SET {sets} WHERE id=?", (*kw.values(), pid))


def get_project(pid):
    with _conn() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def list_projects():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY updated DESC").fetchall()]


def project_dir(pid):
    """项目目录（读路径）：id 必须合法且项目必须存在，否则 404。
    读路径不建目录——避免 ".." / 前缀碰撞等任意路径被自动创建或读取。"""
    from fastapi import HTTPException
    if not isinstance(pid, str) or not PID_RE.match(pid) or get_project(pid) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return PROJECTS_DIR / pid


def create_project_dir(pid):
    """新建项目时显式建目录（唯一会写文件系统的入口）。"""
    d = project_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 资料库（M6 摄取结果） ----------

def create_reference(title, source_url, ingested_via, summary, key_points,
                     usable_facts, suggested_angles, raw_chars):
    rid = new_id()
    with _conn() as c:
        c.execute(
            "INSERT INTO references_(id,title,source_url,ingested_via,summary,"
            "key_points,usable_facts,suggested_angles,raw_chars,created,untrusted) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
            (rid, title, source_url, ingested_via, summary,
             json.dumps(key_points, ensure_ascii=False),
             json.dumps(usable_facts, ensure_ascii=False),
             json.dumps(suggested_angles, ensure_ascii=False),
             raw_chars, now()))
    return rid


def get_reference(rid):
    with _conn() as c:
        r = c.execute("SELECT * FROM references_ WHERE id=?", (rid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("key_points", "usable_facts", "suggested_angles"):
        d[k] = json.loads(d[k] or "[]")
    return d


def list_references():
    with _conn() as c:
        rows = c.execute("SELECT * FROM references_ ORDER BY created DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("key_points", "usable_facts", "suggested_angles"):
            d[k] = json.loads(d[k] or "[]")
        out.append(d)
    return out


def attach_reference(pid, rid):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO project_refs VALUES(?,?,?)", (pid, rid, now()))


def project_references(pid):
    with _conn() as c:
        rows = c.execute(
            "SELECT r.* FROM references_ r JOIN project_refs p ON r.id=p.ref_id "
            "WHERE p.project_id=? ORDER BY p.created", (pid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("key_points", "usable_facts", "suggested_angles"):
            d[k] = json.loads(d[k] or "[]")
        out.append(d)
    return out


# ---------- 流水线事件落表（D1：断线重连/进度回放的事实来源） ----------

def append_project_event(pid, etype, payload) -> int:
    """事件落表并返回递增 seq（seq 以当前表内最大值 +1 为准，跨进程一致）。"""
    with _conn() as c:
        row = c.execute("SELECT COALESCE(MAX(seq), 0) FROM project_events WHERE pid=?",
                        (pid,)).fetchone()
        seq = int(row[0]) + 1
        c.execute("INSERT INTO project_events VALUES(?,?,?,?,?)",
                  (pid, seq, etype, json.dumps(payload, ensure_ascii=False), now()))
        return seq


def list_project_events(pid, after=0, limit=500):
    """按游标回放：after=0 返回全部（最多 limit 条），否则只返回 seq>after 的增量。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT seq, type, payload, ts FROM project_events WHERE pid=? AND seq>? "
            "ORDER BY seq LIMIT ?", (pid, after, limit)).fetchall()
    return [{"seq": r["seq"], "type": r["type"],
             "payload": json.loads(r["payload"]), "ts": r["ts"]} for r in rows]


def latest_event_seq(pid) -> int:
    with _conn() as c:
        row = c.execute("SELECT COALESCE(MAX(seq), 0) FROM project_events WHERE pid=?",
                        (pid,)).fetchone()
        return int(row[0])


def session_for_project(pid):
    """绑定到该项目的最近会话（单写者落盘用：流水线事件只由执行者写一次）。"""
    with _conn() as c:
        r = c.execute(
            "SELECT id FROM sessions WHERE project_id=? ORDER BY updated DESC LIMIT 1",
            (pid,)).fetchone()
        return r["id"] if r else None


def add_project_note(pid, content):
    """把流水线事件写成一条会话消息。只由执行者（Pipeline 侧）调用，
    避免多订阅者重复落盘（C-REL-09）。"""
    sid = session_for_project(pid)
    if not sid:
        return None
    return add_message(sid, "assistant", content)


def maybe_autoname_session(sid, first_text):
    """首条用户消息自动命名（仅当仍是默认名）。"""
    s = get_session(sid)
    if not s or s["title"] != "新对话":
        return None
    import re
    text = re.sub(r"https?://\S+", "", first_text)
    title = text.strip().split("\n")[0].strip(" ，。：")[:20] or "新对话"
    rename_session(sid, title)
    return title
