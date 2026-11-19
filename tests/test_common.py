"""fq_common 的单元测试（不依赖第三方库，CI 里能直接跑）"""
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fq_common as C  # noqa: E402


# ---------------------------------------------------------------- 数字解析
@pytest.mark.parametrize("raw,suffix,want", [
    ("154.8万字", ("字",), 1548000),
    ("1.2万人在读", ("人在读", "在读", "人"), 12000),
    ("未满1000人在读", ("人在读",), 1000),
    ("3亿字", ("字",), 300000000),
    ("8000字", ("字",), 8000),
    ("", ("字",), None),
    ("暂无", ("字",), None),
])
def test_parse_count(raw, suffix, want):
    assert C.parse_count(raw, suffix) == want


# ---------------------------------------------------------------- 文件名清洗
def test_sanitize_strips_illegal_chars():
    assert C.sanitize('a/b\\c:d*e?f"g<h>i|j') == "a b c d e f g h i j"


def test_sanitize_fallback_and_truncate():
    assert C.sanitize("", "佚名") == "佚名"
    assert len(C.sanitize("字" * 500)) == C.MAX_NAME


# ---------------------------------------------------------------- 切片计划
def test_build_slices_splits_big_category():
    rows = [[0, 1, "小类", 500], [1, 2, "大类", 25000], [1, 3, "边界", 9999]]
    slices = C.build_slices(rows)
    assert sum(1 for s in slices if s[1] == 1) == 1        # 小类不拆
    assert sum(1 for s in slices if s[1] == 2) == 5        # 大类按字数档拆 5 片
    assert sum(1 for s in slices if s[1] == 3) == 1        # 9999 不拆
    assert slices[0][1] == 2                               # 书多的排前面
    assert C.slice_key(1, 851, -1) == "g1-c851-w-1"


def test_load_plan_rejects_garbage(tmp_path):
    p = tmp_path / "cats.json"
    p.write_text('{"not": "a list"}', encoding="utf-8")
    assert C.load_plan(str(p)) is None
    p.write_text("[[0, 1, \"科幻\", 100]]", encoding="utf-8")
    assert C.load_plan(str(p)) == [[0, 1, "科幻", 100]]


# ---------------------------------------------------------------- epub 元数据
def _make_epub(path, opf_xml):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("OEBPS/content.opf", opf_xml)


def test_read_epub_meta_ok(tmp_path):
    p = tmp_path / "a.epub"
    _make_epub(p, """<?xml version="1.0"?><package>
      <dc:identifier xmlns:dc="x">9787000000123456789</dc:identifier>
      <dc:title xmlns:dc="x">测试书名</dc:title>
      <dc:creator xmlns:dc="x">某作者</dc:creator>
      <dc:subject xmlns:dc="x">科幻|已完结</dc:subject>
    </package>""")
    m = C.read_epub_meta(str(p))
    assert m["book_id"] == "9787000000123456789"
    assert m["title"] == "测试书名"
    assert m["subject"] == "科幻|已完结"


def test_read_epub_meta_rejects_short_identifier(tmp_path):
    """UUID / 时间戳之类的短数字不能当书号"""
    p = tmp_path / "b.epub"
    _make_epub(p, '<dc:identifier xmlns:dc="x">12345</dc:identifier>')
    assert C.read_epub_meta(str(p))["book_id"] is None


def test_read_epub_meta_broken_file(tmp_path):
    p = tmp_path / "c.epub"
    p.write_bytes(b"not a zip")
    assert C.read_epub_meta(str(p)) is None


# ---------------------------------------------------------------- 表结构
def test_ensure_db_creates_all_columns(tmp_path):
    conn = C.ensure_db(str(tmp_path / "t.db"))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
        for c in ("downloaded", "downloaded_at", "file_rel", "top_category",
                  "tags", "meta_fetched_at", "first_chapter_ts", "title_raw"):
            assert c in cols
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_dl'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_ensure_columns_upgrades_old_db(tmp_path):
    import sqlite3
    p = str(tmp_path / "old.db")
    o = sqlite3.connect(p)
    o.execute("CREATE TABLE books (book_id TEXT PRIMARY KEY, title TEXT)")
    o.commit()
    o.close()

    conn = sqlite3.connect(p)
    added = C.ensure_columns(conn)
    conn.close()
    assert "downloaded" in added and "file_rel" in added


# ---------------------------------------------------------------- 状态文件
def test_status_file_roundtrip(tmp_path):
    s = C.Status(str(tmp_path / "status.json"))
    s.log("hello")
    s.set(running=True, done=3)
    data = s.read()
    assert data["running"] is True
    assert data["done"] == 3
    assert any("hello" in line for line in data["log"])
    s.clear_running()
    assert s.read()["running"] is False


def test_status_log_is_bounded(tmp_path):
    s = C.Status(str(tmp_path / "status.json"), max_lines=5)
    for i in range(20):
        s.log(f"line {i}")
    assert len(s.read()["log"]) == 5


# ---------------------------------------------------------------- 跨进程锁
def test_lock_is_exclusive(tmp_path):
    p = str(tmp_path / "x.lock")
    fh, ok = C.try_lock(p)
    assert ok
    _, again = C.try_lock(p)
    assert again is False          # 同进程内也已占用
    assert C.lock_held(p)
    fh.close()
    assert not C.lock_held(p)
