# -*- coding: utf-8 -*-
"""各脚本共用的工具。

拆出来的原因：sanitize / dec / parse_count / read_epub_meta / 切片生成这几段逻辑，
原先在 3~4 个脚本里各写一遍，改一处忘另一处就会让行为对不上
（典型：书号长度判定分别写成 10 / 10 / 15）。

这里只有纯函数和常量，不产生副作用，可以被任意脚本 import。
"""
import json
import os
import re
import sqlite3
import time
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))

# 番茄 book_id 是 19 位数字。epub 的 dc:identifier 若短于这个长度，多半是
# UUID / 时间戳之类的伪标识，不能当书号用。
MIN_BOOK_ID_LEN = 15


def db_path():
    return os.environ.get("FQ_DB") or os.path.join(BASE, "fq_books.db")


def map_path():
    return os.environ.get("FQ_MAP") or os.path.join(BASE, "mapping.json")


def plan_path():
    return os.environ.get("FQ_PLAN") or os.path.join(BASE, "cats.json")


# 各脚本直接 import 这三个用（都在 import 时读一次环境变量，与原行为一致）
DB = db_path()
MAP = map_path()
PLAN = plan_path()


# --------------------------------------------------------------------------
# 表结构
# --------------------------------------------------------------------------
# 全部列集中在这里定义。新装用户直接建出完整表；老库由 ensure_columns() 补齐
# （原先这些后加的列散落在 4 个脚本的 ALTER TABLE 里，新装用户跑到一半才报错）。
DDL = """
CREATE TABLE IF NOT EXISTS books (
    book_id          TEXT PRIMARY KEY,
    title            TEXT,
    author           TEXT,
    gender           INTEGER,
    category_id      TEXT,
    category_name    TEXT,
    word_count       INTEGER DEFAULT 0,
    create_status    INTEGER,
    last_chapter_ts  INTEGER,
    source_slice     TEXT,
    status           TEXT DEFAULT 'new',
    enqueued_at      INTEGER,
    finished_at      INTEGER,
    note             TEXT,
    abstract_raw     TEXT,
    read_raw         TEXT,
    word_raw         TEXT,
    cover_url        TEXT,
    status_raw       TEXT,
    title_raw        TEXT,
    author_raw       TEXT,
    abstract         TEXT,
    word_count_real  INTEGER,
    read_count_real  INTEGER,
    first_chapter_ts INTEGER,
    downloaded       INTEGER DEFAULT 0,
    downloaded_at    INTEGER,
    file_rel         TEXT,
    top_category     TEXT,
    tags             TEXT,
    meta_fetched_at  INTEGER
);

CREATE TABLE IF NOT EXISTS slice_progress (
    slice_key  TEXT PRIMARY KEY,
    gender     INTEGER,
    cat_id     TEXT,
    cat_name   TEXT,
    word       INTEGER,
    next_page  INTEGER DEFAULT 1,
    total_got  INTEGER DEFAULT 0,
    done       INTEGER DEFAULT 0,
    updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_status  ON books(status);
CREATE INDEX IF NOT EXISTS idx_read    ON books(read_count_real);
CREATE INDEX IF NOT EXISTS idx_word    ON books(word_count_real);
CREATE INDEX IF NOT EXISTS idx_ts      ON books(last_chapter_ts);
CREATE INDEX IF NOT EXISTS idx_cat     ON books(category_name);
CREATE INDEX IF NOT EXISTS idx_title   ON books(title);
CREATE INDEX IF NOT EXISTS idx_dl      ON books(downloaded);
CREATE INDEX IF NOT EXISTS idx_meta    ON books(meta_fetched_at);
"""

# 后加的列：老库升级时需要补
_EXTRA_COLUMNS = [
    ("downloaded", "INTEGER DEFAULT 0"),
    ("downloaded_at", "INTEGER"),
    ("file_rel", "TEXT"),
    ("top_category", "TEXT"),
    ("tags", "TEXT"),
    ("meta_fetched_at", "INTEGER"),
    ("first_chapter_ts", "INTEGER"),
    ("abstract", "TEXT"),
    ("word_count_real", "INTEGER"),
    ("read_count_real", "INTEGER"),
    ("title_raw", "TEXT"),
    ("author_raw", "TEXT"),
    ("abstract_raw", "TEXT"),
    ("read_raw", "TEXT"),
    ("word_raw", "TEXT"),
    ("cover_url", "TEXT"),
    ("status_raw", "TEXT"),
    ("source_slice", "TEXT"),
]


def ensure_columns(conn):
    """给老库补上缺失的列，返回新增的列名列表"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
    added = []
    for name, typ in _EXTRA_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE books ADD COLUMN {name} {typ}")
            added.append(name)
    if added:
        conn.commit()
    return added


def ensure_db(path=None, create=True):
    """建库 + 建表 + 补列。create=False 时库不存在就抛 FileNotFoundError。

    返回连接（调用方负责 close）。
    """
    p = path or db_path()
    if not create and not os.path.exists(p):
        raise FileNotFoundError(p)
    parent = os.path.dirname(os.path.abspath(p))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(p, timeout=60)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    ensure_columns(conn)
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# JSON / 状态文件
# --------------------------------------------------------------------------
def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj, atomic=True):
    """原子写：先写 .tmp 再 replace，避免面板读到半截 JSON"""
    try:
        if atomic:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
        return True
    except Exception:
        return False


class Status:
    """status_*.json 的读写 + 日志追加。

    约定：`running` 由进程写，但**权威来源是「进程还在不在」**——
    异常退出（kill -9 / 容器重启）时这个字段会残留 true，读的一方必须自己校验。
    """

    def __init__(self, path, max_lines=120):
        self.path = path
        self.max_lines = max_lines

    def read(self):
        v = read_json(self.path, {})
        return v if isinstance(v, dict) else {}

    def _write(self, st):
        write_json(self.path, st)

    def set(self, **kw):
        st = self.read()
        st.update(kw)
        self._write(st)

    def clear_running(self):
        st = self.read()
        if not st:
            return
        st["running"] = False
        self._write(st)

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        st = self.read()
        st.setdefault("log", []).append(line)
        st["log"] = st["log"][-self.max_lines:]
        self._write(st)
        return line


# --------------------------------------------------------------------------
# 字体混淆解码
# --------------------------------------------------------------------------
def load_map(path=None):
    """{0xE4E7: '大', ...}；映射表不存在或损坏时返回空 dict（调用方按直通处理）"""
    p = path or map_path()
    raw = read_json(p, None)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[int(k[2:], 16)] = v
        except (ValueError, TypeError):
            continue
    return out


def decode(s, table):
    if not s or not table:
        return s
    return "".join(table.get(ord(c), c) for c in s)


# --------------------------------------------------------------------------
# 「154.8万字」「1.2万人在读」→ 整数
# --------------------------------------------------------------------------
_UNIT = {"万": 10000, "亿": 100000000}


def parse_count(s, suffix_words=()):
    if not s:
        return None
    t = str(s)
    for w in suffix_words:
        t = t.replace(w, "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万亿]?)", t.strip())
    if not m:
        return None
    try:
        return int(float(m.group(1)) * _UNIT.get(m.group(2), 1))
    except (ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------
# 文件名清洗
# --------------------------------------------------------------------------
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
# Windows 单段路径上限 255；分类目录 + "-作者.epub" 还要占位，留够余量。
# 注意 Windows 整体还有 MAX_PATH 260 的限制，深层目录下仍可能超长。
MAX_NAME = 100


def sanitize(name, fallback="未知", max_len=MAX_NAME):
    s = _BAD_CHARS.sub(" ", str(name or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return (s or fallback)[:max_len]


# --------------------------------------------------------------------------
# epub 元数据
# --------------------------------------------------------------------------
_RE_ID = re.compile(r"<dc:identifier[^>]*>(.*?)</dc:identifier>", re.S)
_RE_TITLE = re.compile(r"<dc:title[^>]*>(.*?)</dc:title>", re.S)
_RE_CREATOR = re.compile(r"<dc:creator[^>]*>(.*?)</dc:creator>", re.S)
_RE_SUBJECT = re.compile(r"<dc:subject[^>]*>(.*?)</dc:subject>", re.S)


def read_epub_meta(path):
    """读 OPF，返回 dict（book_id / title / author / subject）。

    book_id 为 None 表示打不开或读不出合法书号。其余字段可能为空串。
    """
    try:
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
            if not name:
                return None
            xml = z.read(name).decode("utf-8", "ignore")
    except Exception:
        return None

    out = {}
    for key, rx in (("book_id", _RE_ID), ("title", _RE_TITLE),
                    ("author", _RE_CREATOR), ("subject", _RE_SUBJECT)):
        m = rx.search(xml)
        out[key] = m.group(1).strip() if m else ""

    digits = re.sub(r"\D", "", out["book_id"])
    out["book_id"] = digits if len(digits) >= MIN_BOOK_ID_LEN else None
    return out


def _close(a, b, tolerance=2):
    """两个字符串是否只差 1~2 个字（列表接口的形近字污染）"""
    if not a or not b:
        return False
    if a == b:
        return True
    if abs(len(a) - len(b)) > tolerance:
        return False
    diff = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
    return diff <= tolerance


same_enough = _close


# --------------------------------------------------------------------------
# 导出格式化（TSV 清单用）
# --------------------------------------------------------------------------
def fmt_word(n):
    """字数：0 -> '', 12345 -> '1.23万字', 8000 -> '8000字'"""
    if not n or n <= 0:
        return ""
    if n >= 10000:
        v = n / 10000
        return f"{v:.2f}".rstrip("0").rstrip(".") + "万字"
    return f"{n}字"


def fmt_date(ts):
    if not ts or ts <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except (ValueError, OSError):
        return ""


def clean_field(s):
    """去掉会破坏 TSV 的字符"""
    if s is None:
        return ""
    s = str(s).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# 切片计划
# --------------------------------------------------------------------------
def slice_key(gender, cid, word):
    return f"g{gender}-c{cid}-w{word}"


def load_plan(path=None):
    """读 cats.json，返回 [[gender, cid, name, total], ...]；不存在/格式错返回 None"""
    rows = read_json(path or plan_path(), None)
    if not isinstance(rows, list) or not rows:
        return None
    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 4:
            return None
        try:
            out.append([int(r[0]), int(r[1]), str(r[2]), int(r[3] or 0)])
        except (TypeError, ValueError):
            return None
    return out


def build_slices(rows):
    """[[gender, cid, name, total]] → [(gender, cid, name, word), ...]

    总量 >= 10000 的分类会被「每类最多 1 万本」截断，必须按字数档拆开；
    小分类一个切片就够，避免重复查 6 个档。
    返回顺序按总量降序（书多的先跑，问题早暴露）。
    """
    tmp = []
    for g, cid, name, total in rows:
        total = int(total or 0)
        if total >= 10000:
            for w in (0, 1, 2, 3, 4):
                tmp.append((total, int(g), int(cid), name, w))
        else:
            tmp.append((total, int(g), int(cid), name, -1))
    tmp.sort(key=lambda x: -x[0])
    return [(g, cid, name, w) for _t, g, cid, name, w in tmp]


# --------------------------------------------------------------------------
# 跨进程锁
# --------------------------------------------------------------------------
def try_lock(path):
    """非阻塞排他锁，进程退出/崩溃时由系统自动释放（无僵尸锁文件）。

    返回 (handle, True)；已被占用返回 (None, False)。handle 需要一直持有。
    """
    try:
        fh = open(path, "a+")
    except Exception:
        return None, False
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return fh, True
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return None, False


def lock_held(path):
    """只探测不写入：是否有别的进程占着这个锁。

    用于面板判断「扫描是不是外部进程在跑」——比"数据库最近 3 分钟有没有写入"
    可靠得多（限流冷却期间可能好几分钟不写库，但那不算停止）。
    """
    if not os.path.exists(path):
        return False
    try:
        fh = open(path, "r+")
    except Exception:
        return False
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return True
    try:
        fh.close()
    except Exception:
        pass
    return False
