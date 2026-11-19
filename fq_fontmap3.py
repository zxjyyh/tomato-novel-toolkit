# -*- coding: utf-8 -*-
"""字体混淆还原 v3 —— 多字体集成比对（灰度 + 更高分辨率）

v2 用单一字体二值图，形近字会错（犬/大、宇/字、往/在）。
v3 同时用微软雅黑/黑体/宋体三种字形打分求平均，形近字靠多字体共识分开。

用法：
    1. 打开 https://fanqienovel.com/library，在 Network 面板里把字体请求另存为
       obf.woff2，放在本脚本同目录
    2. python fq_fontmap3.py            # 产出 mapping.json
    3. 人工核对低置信项（脚本会列出）

所有路径都基于脚本所在目录，不受当前工作目录影响。
"""
import glob
import json
import os
import sys
import time

import numpy as np
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
SIZE = 64

# 参照字体：优先用系统里的中文字体，找不到就报错提示装一个
_CANDIDATES = [
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Linux（apt install fonts-noto-cjk）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/**/NotoSansCJK*.ttc",
]
FONTS = []
for _p in _CANDIDATES:
    for _hit in glob.glob(_p, recursive=True):
        if os.path.isfile(_hit) and _hit not in FONTS:
            FONTS.append(_hit)
if not FONTS:
    sys.exit("找不到中文字体，请先安装（Linux: apt install fonts-noto-cjk）")

OBF_WOFF2 = os.path.join(BASE, "obf.woff2")
OBF_TTF = os.path.join(BASE, "obf.ttf")
OUT = os.environ.get("FQ_MAP") or os.path.join(BASE, "mapping.json")


def ensure_obf_ttf():
    """PIL 不支持 woff2，先把抓到的混淆字体转成 ttf"""
    if os.path.isfile(OBF_TTF):
        return
    if not os.path.isfile(OBF_WOFF2):
        sys.exit(f"缺 {OBF_WOFF2}：打开书库页，从 Network 里另存字体请求的 woff2 文件")
    f = TTFont(OBF_WOFF2)
    f.flavor = None
    f.save(OBF_TTF)
    print("obf.woff2 -> obf.ttf 转换完成")


def render(font, ch):
    box = 120
    img = Image.new("L", (box, box), 255)
    ImageDraw.Draw(img).text((14, 8), ch, font=font, fill=0)
    a = np.array(img)
    mask = a < 220
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    img = img.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)).resize(
        (SIZE, SIZE), Image.LANCZOS)
    v = (255 - np.array(img).astype(np.float32)).ravel()
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n else None


def main():
    t0 = time.time()
    out_path = OUT
    for i, a in enumerate(sys.argv):
        if a == "--out" and i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]

    ensure_obf_ttf()
    obf_fonts = [ImageFont.truetype(OBF_TTF, 72) for _ in FONTS]
    sys_fonts = [ImageFont.truetype(p, 72) for p in FONTS]

    cands, seen = [], set()
    for b1 in range(0xB0, 0xF8):
        for b2 in range(0xA1, 0xFF):
            try:
                ch = bytes([b1, b2]).decode("gb2312")
            except Exception:
                continue
            if ch not in seen:
                seen.add(ch)
                cands.append(ch)
    for ch in ("0123456789.%+-·—…、，。！？：；（）《》“”‘’"
               "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if ch not in seen:
            seen.add(ch)
            cands.append(ch)

    # 候选矩阵：每字体一个。渲染失败的字符留全零行，打分时必须屏蔽，
    # 否则余弦相似度全为负时，全零行的点积 0 反而是最高分，会选中一个根本没有字形的候选。
    mats, valid = [], []
    for sf in sys_fonts:
        m = np.zeros((len(cands), SIZE * SIZE), dtype=np.float32)
        ok_row = np.zeros(len(cands), dtype=bool)
        for i, ch in enumerate(cands):
            v = render(sf, ch)
            if v is not None:
                m[i] = v
                ok_row[i] = True
        mats.append(m)
        valid.append(ok_row)
    print(f"候选 {len(cands)} 个字 × {len(FONTS)} 字体，耗时 {time.time()-t0:.0f}s", flush=True)

    cps = sorted(TTFont(OBF_WOFF2).getBestCmap())
    mapping, weak = {}, []
    for cp in cps:
        score = np.zeros(len(cands), dtype=np.float32)
        ok = 0
        for fi, of in enumerate(obf_fonts):
            v = render(of, chr(cp))
            if v is None:
                continue
            s = mats[fi] @ v
            # 该字体渲染失败的候选给一个远低于有效余弦值的分（区间是 [-1, 1]）
            score += np.where(valid[fi], s, -9.0)
            ok += 1
        if not ok:
            continue
        score /= ok
        order = np.argsort(-score)
        b = order[0]
        if score[b] <= -9.0:
            continue
        mapping[f"U+{cp:04X}"] = cands[b]
        s2 = score[order[1]]
        if score[b] - s2 < 0.02:
            weak.append((f"U+{cp:04X}", cands[b], cands[order[1]],
                         round(float(score[b] - s2), 4)))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=1)
    print(f"完成 {len(mapping)} 个 → {out_path}，低置信 {len(weak)} 个，"
          f"总耗时 {time.time()-t0:.0f}s")
    for w in weak[:12]:
        print("  ", w)
    if weak:
        print("  低置信项需要人工核对后手动改 mapping.json")

    # 冒烟测试：解几个已知样本
    table = {int(k[2:], 16): v for k, v in mapping.items()}
    for s in ("\ue4e7\ue53c.\ue47a\ue3f7\ue4a0",
              "\ue4f6\ue556\ue556\ue504\ue41c\ue53f读",
              "\ue457限：\ue50d级\ue440佬\ue4e4亿\ue4df\ue534"):
        print("解码:", "".join(table.get(ord(c), c) for c in s))


if __name__ == "__main__":
    main()
