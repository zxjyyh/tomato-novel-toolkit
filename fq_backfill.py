# -*- coding: utf-8 -*-
"""番茄回填器 —— 用下载好的 EPUB 把数据库里的乱码书名、作者修好

背景：
  枚举接口返回的书名/作者带有私用区字符（PUA，如 \\ue457），
  这是番茄的字体混淆。下载引擎生成 EPUB 时已经还原成正常文字，
  所以下载完从 EPUB 元数据反读回来，是最省事的修复方式。
  顺带补上真实的分类（dc:subject，形如「悬疑推理|已完结」）。

用法：
    python fq_backfill.py --dir ./books
"""
import argparse
import os
import sys

from fq_common import ensure_db, read_epub_meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "books"),
                    help="EPUB 所在目录（会递归）")
    args = ap.parse_args()

    conn = ensure_db()
    cur = conn.cursor()
    try:
        total = fixed = 0
        for root, _dirs, files in os.walk(args.dir):
            for f in files:
                if not f.lower().endswith(".epub"):
                    continue
                meta = read_epub_meta(os.path.join(root, f))
                if not meta or not meta.get("book_id"):
                    continue
                total += 1
                bid = meta["book_id"]
                title = meta["title"]
                author = meta["author"]
                subject = meta["subject"]
                cat = subject.split("|")[0].strip() if "|" in subject else ""

                row = cur.execute("SELECT title, author FROM books WHERE book_id=?",
                                  (bid,)).fetchone()
                if not row:
                    continue                      # 不是枚举库里的书，跳过
                if (row[0] or "") == title and (row[1] or "") == author and not cat:
                    continue
                cur.execute("UPDATE books SET title=?, author=? WHERE book_id=?",
                            (title, author, bid))
                if cat:
                    cur.execute("UPDATE books SET category_name=? WHERE book_id=? AND "
                                "COALESCE(category_name,'')=''", (cat, bid))
                fixed += 1

        conn.commit()
        print(f"扫描 {total} 个 EPUB，回填 {fixed} 本")

        # 检查原始字段里还有多少没解出来的 PUA 字符
        n_bad = cur.execute(
            "SELECT COUNT(*) FROM books WHERE COALESCE(title_raw,title) GLOB '*[\ue000-\uf8ff]*'"
        ).fetchone()[0]
        n_all = cur.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        print(f"库内共 {n_all} 本，仍有乱码书名 {n_bad} 本")
        if n_bad:
            print("  处理办法：重新生成映射表（fq_fontmap3.py），再跑 fq_decode.py")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
