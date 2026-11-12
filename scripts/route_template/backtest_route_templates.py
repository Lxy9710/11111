# -*- coding: utf-8 -*-
"""回测：用真实路线反推特征签名 -> 生成器 -> 与真实路线比对。

对照基线是「随机取同类别另一件」，因为这才是「靠类别猜路线」的诚实对照。
"""
import sys, json, collections, re, random
from pathlib import Path

ROOT = Path(r"E:\AAA我的论文\3Topology-Aware Geometric Localization and Cascaded Modification for Multi-Operation CAD Drawings\intentCAD")
sys.path.insert(0, str(ROOT / "chainmodel" / "src"))
from farmcad.data.route_templates import (FeatureSignature, generate_route, load_templates,
                                          CATEGORY_OUTER, CATEGORY_INNER)

payload = json.load(open(ROOT / "chainmodel" / "data" / "route_templates" / "routes_raw.json", encoding="utf-8"))
raw = payload["routes"]


def cat_of(rows):
    for row in rows:
        text = str(row.get("part_region")) + str(row.get("process_name")) + str(row.get("process_type"))
        if "保持架" in text or "保持器" in text: return "cage"
        if "滚动体" in text or "钢球" in text: return "rolling"
        if "内圈" in text: return "inner"
        if "外圈" in text: return "outer"
    return "unknown"


def canon(name):
    n = name.strip(); st = None
    for pre, tag in [("均粗磨", "M"), ("均磨", "M"), ("粗磨", "R"), ("粗研", "R2"),
                     ("细磨", "F"), ("终磨", "S"), ("精研", "P"), ("修磨", "X")]:
        if n.startswith(pre):
            st = tag; n = n[len(pre):]; break
    n = n.replace("加工", "").replace("处理", "").replace("、", "").replace("，", "").replace(",", "")
    n = n.replace("外径圆", "").replace("内孔", "").replace("内经", "内径").replace("外圈内经", "外圈内径")
    n = n.replace("端面", "平面").replace("外沟道", "外沟").replace("内沟道", "内沟")
    if "沟" in n: return ("GROOVE", st)
    if "平面" in n: return ("PLANE", st)
    if "圈内径" in n: return ("ID", st)
    if "圈外径" in n: return ("OD", st)
    if "外径" in n: return ("OD", st)
    if "内径" in n: return ("ID", st)
    return (None, n)


SPINE_TEMPER = {"中温补充回火", "低温补充回火", "高温补充回火", "补充回火"}


def sig_from_real(rows, cat):
    feats = set(); spine = set(); temper = None
    for row in rows:
        nm = row["process_name"].strip()
        f, st = canon(nm)
        if f:
            feats.add(f)
        else:
            spine.add(nm)
            if nm in SPINE_TEMPER:
                temper = nm
    catkey = CATEGORY_OUTER if cat == "outer" else CATEGORY_INNER
    return FeatureSignature(
        category=catkey,
        groove="GROOVE" in feats, od="OD" in feats, id="ID" in feats, plane="PLANE" in feats,
        lapping=any("光饰" in s for s in spine),
        regrind=any("锐角" in s for s in spine),
        chamfer=any("倒角" in s for s in spine),
        pickling="酸洗" in spine,
        dehydrogenation="除氢" in spine,
        magnetic_particle_inspection=any("探伤" in s or "磁弹仪" in s for s in spine),
        laser_marking=any("激光" in s or s == "标记" for s in spine),
        preassembly_check="装前检查" in spine,
        submit="提交" in spine,
        temper=temper,
    )


templates = load_templates()
random.seed(0)

for cat in ("outer", "inner"):
    real = {}
    for pt, rows in raw.items():
        if cat_of(rows) != cat:
            continue
        names = [row["process_name"].strip() for row in rows if row["process_no"] >= 299]
        if names:
            real[pt] = names
    n = 0; exact = 0; js = []; miss = collections.Counter(); extra = collections.Counter()
    for pt, names in real.items():
        sig = sig_from_real(raw[pt], cat)
        try:
            g = generate_route(sig, templates)
        except Exception as exc:
            print(f"   [skip] {pt}: {exc}")
            continue
        gen = [r["process_name_only"] for r in g.rows]
        n += 1
        if gen == names: exact += 1
        sr, sg = set(names), set(gen)
        js.append(len(sr & sg) / max(1, len(sr | sg)))
        for x in sr - sg: miss[x] += 1
        for x in sg - sr: extra[x] += 1
    js.sort()
    keys = list(real)
    base = []
    for pt in keys:
        other = random.choice([k for k in keys if k != pt])
        a, b = set(real[pt]), set(real[other])
        base.append(len(a & b) / max(1, len(a | b)))
    base.sort()
    print(f"\n########## {cat}: parts={n}")
    print(f"   生成器  Jaccard mean={sum(js)/n:.3f} median={js[n//2]:.3f} p10={js[int(n*0.1)]:.3f} p90={js[int(n*0.9)]:.3f}")
    print(f"   基线(随机同类另一件) Jaccard mean={sum(base)/n:.3f} median={base[n//2]:.3f}")
    print(f"   提升 = {sum(js)/n - sum(base)/n:+.3f}")
    print(f"   精确匹配 = {exact}/{n} ({100*exact/n:.1f}%)")
    print(f"   集合完全一致 = {sum(1 for j in js if j >= 0.999)}/{n}")
    print("   MISSING:", ", ".join(f"{k}:{v}" for k, v in miss.most_common(6)))
    print("   EXTRA  :", ", ".join(f"{k}:{v}" for k, v in extra.most_common(6)))
