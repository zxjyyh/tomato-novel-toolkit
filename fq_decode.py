# -*- coding: utf-8 -*-
"""把字体混淆的字段翻译成正常文字，并解析成数字

前提：mapping.json（PUA → 真实字符），由 fq_fontmap3.py + 人工校正生成。

做的事：
  1. 备份原始书名/作者到 title_raw / author_raw
  2. title / author / abstract 写入解码后的正常文字
  3. word_raw → word_count_real（整数，如 154.8万字 → 1548000）
     read_raw → read_count_real（整数，如 1.2万人在读 → 12000）

可反复运行：枚举器一边加新书，这里一边补译。
已经解过的行会被跳过，所以第二次跑几乎是瞬间完成。
"""
import os
import sys
import time

from fq_common import MAP, decode, ensure_db, load_map, parse_count, read_json

CHUNK = 5000


def main():
    if not os.path.exists(MAP):
        print(f"❌ 找不到映射表 {MAP}")
        print("   先跑：python fq_fontmap3.py")
        return 1
    raw = read_json(MAP, None)
    if not isinstance(raw, dict) or not raw:
        print(f"❌ {MAP} 内容为空或格式不对")
        return 1
    tbl = load_map(MAP)

    conn = ensure_db()
    cur = conn.cursor()
    try:
        rows = cur.execute(
            "SELECT book_id, title, author, abstract, word_count_real, read_count_real,"
            "       title_raw, author_raw, abstract_raw, word_raw, read_raw FROM books"
        ).fetchall()

        t0 = time.time()
        batch, n, changed = [], 0, 0
        for (bid, title, author, abstract, word_real, read_real,
             title_raw, author_raw, ab_raw, w_raw, r_raw) in rows:
            n += 1
            # 原文优先用 *_raw（可能是 PUA 原文）；没有就把当前值当原文存下来
            src_title = title_raw or title or ""
            src_author = author_raw or author or ""
            new_traw = title_raw or title
            new_araw = author_raw or author

            new_title = decode(src_title, tbl)
            new_author = decode(src_author, tbl)
            new_abstract = decode(ab_raw, tbl) if ab_raw else abstract
            new_word = parse_count(decode(w_raw, tbl), ("字",)) if w_raw else word_real
            new_read = (parse_count(decode(r_raw, tbl), ("人在读", "在读", "人"))
                        if r_raw else read_real)

            if (new_title, new_author, new_abstract, new_word, new_read,
                    new_traw, new_araw) == (title, author, abstract, word_real, read_real,
                                            title_raw, author_raw):
                continue

            batch.append((new_traw, new_araw, new_title, new_author, new_abstract,
                          new_word, new_read, bid))
            changed += 1
            if len(batch) >= CHUNK:
                cur.executemany(
                    "UPDATE books SET title_raw=?, author_raw=?, title=?, author=?,"
                    " abstract=?, word_count_real=?, read_count_real=? WHERE book_id=?", batch)
                conn.commit()
                batch.clear()
                print(f"  已更新 {changed:,} / {n:,}", flush=True)

        if batch:
            cur.executemany(
                "UPDATE books SET title_raw=?, author_raw=?, title=?, author=?,"
                " abstract=?, word_count_real=?, read_count_real=? WHERE book_id=?", batch)
            conn.commit()

        stat = cur.execute(
            "SELECT COUNT(*), SUM(word_count_real IS NOT NULL), SUM(read_count_real IS NOT NULL) "
            "FROM books").fetchone()
        print(f"扫描 {n:,} 本，更新 {changed:,} 本，耗时 {time.time()-t0:.1f}s")
        print(f"库内 {stat[0]:,} | 有字数 {stat[1] or 0:,} | 有在读 {stat[2] or 0:,}")
        print("样例：")
        for r in cur.execute("SELECT title, author, word_count_real, read_count_real FROM books "
                             "WHERE word_count_real IS NOT NULL "
                             "ORDER BY read_count_real DESC LIMIT 6"):
            print(f"  {(r[0] or '')[:24]:<26} {(r[1] or '')[:8]:<10} "
                  f"{r[2]:>9} 字  {r[3] or 0:>7} 在读")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
