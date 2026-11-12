# -*- coding: utf-8 -*-
"""从抽取的路线数据中学习规范阶梯与脊柱位置，生成路线模板资产。

输入 `chainmodel/data/route_templates/routes_raw.json`（由
`scripts/route_template/extract_routes.py` 生成，含每张来源表的 SHA256）。
输出 `chainmodel/data/route_templates/route_templates_v1.json`。
"""
import json, collections

ROOT = r"E:\AAA我的论文\3Topology-Aware Geometric Localization and Cascaded Modification for Multi-Operation CAD Drawings\intentCAD"
payload = json.load(open(ROOT + r"\chainmodel\data\route_templates\routes_raw.json", encoding="utf-8"))
raw = payload["routes"]


def cat_of(rows):
    for row in rows:
        text = str(row.get("part_region")) + str(row.get("process_name")) + str(row.get("process_type"))
        if "保持架" in text or "保持器" in text:
            return "cage"
        if "滚动体" in text or "钢球" in text:
            return "rolling"
        if "内圈" in text:
            return "inner"
        if "外圈" in text:
            return "outer"
    return "unknown"


STAGE_MAP = [("均粗磨", "M"), ("均磨", "M"), ("粗磨", "R"), ("粗研", "R2"), ("细磨", "F"),
             ("终磨", "S"), ("精研", "P"), ("修磨", "X")]
STAGE_ORD = ["M", "R", "F", "S", "R2", "P", "X"]
FEATS = ["GROOVE", "OD", "ID", "PLANE"]
SPINE = ["磨工总图", "退磁", "退磁、清洗", "退磁清洗", "退磁丶清洗", "清洗", "中温补充回火",
         "低温补充回火", "高温补充回火", "打倒角", "去锐角", "去各部锐角", "粗光饰", "精光饰",
         "光饰", "酸洗", "除氢", "荧光磁粉探伤", "荧光渗透探伤", "磁弹仪检测", "激光打字",
         "装前检查", "提交", "标记", "提交检查"]


def canon(name):
    n = name.strip(); st = None
    for pre, tag in STAGE_MAP:
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


def token_of(name):
    nm = name.strip()
    f, st = canon(nm)
    if f: return f"{f}|{st}"
    if nm in SPINE: return f"SPINE|{nm}"
    return None


CAGE_ROUTE = [
    ["10", "备料", "原材料检验"], ["20", "备料", "下料"], ["30", "备料", "锻造"],
    ["40", "备料", "锻造检验"], ["99", "保持架机加", "总图"],
    ["100", "保持架机加", "粗车第一平面及内径、倒角"],
    ["105", "保持架机加", "粗车第二平面及外径、倒角"],
    ["110", "热处理", "热处理"], ["115", "保持架机加", "粗磨外径"],
    ["120", "保持架机加", "切断"], ["125", "保持架机加", "均车两平面及倒角"],
    ["130", "保持架机加", "细车内径及倒角"], ["135", "保持架机加", "钻铆钉孔"],
    ["140", "保持架机加", "倒铆钉孔角"], ["145", "保持架机加", "插销子"],
    ["150", "保持架机加", "打标记"], ["155", "保持架机加", "钻铣孔"],
    ["160", "保持架机加", "终磨外径"], ["165", "保持架机加", "终车内径及倒角"],
    ["170", "保持架机加", "去毛刺"], ["175", "保持架机加", "拆销子"],
    ["180", "保持架机加", "光饰"], ["185", "其它", "清洗"],
    ["190", "其它", "荧光磁粉探伤"], ["195", "其它", "清洗"],
    ["200", "其它", "终检"], ["205", "其它", "镀银"], ["210", "其它", "包装"],
]

ROLLING_ROUTE = [
    ["10", "滚动体", "备料"], ["100", "滚动体", "切断"], ["105", "滚动体", "车料段"],
    ["110", "滚动体", "热冲压"], ["120", "滚动体", "光球"], ["125", "滚动体", "软磨一"],
    ["135", "滚动体", "软磨二"], ["145", "滚动体", "硬磨"], ["165", "滚动体", "初研"],
    ["170", "滚动体", "清洗"], ["175", "滚动体", "精研"], ["180", "滚动体", "清洗"],
    ["185", "滚动体", "100%外观"], ["190", "滚动体", "涡流探伤"],
    ["200", "滚动体", "涂油包装"], ["205", "滚动体", "超精研"], ["210", "滚动体", "清洗"],
    ["215", "滚动体", "尺寸分选及外观"], ["220", "滚动体", "终检验收"],
    ["230", "滚动体", "涂油包装"],
]

profiles = {}
for cat in ("outer", "inner"):
    parts = [pt for pt, seq in raw.items() if cat_of(seq) == cat and any(x["process_no"] >= 299 for x in raw[pt])]
    # token -> 规范名
    names = collections.defaultdict(collections.Counter)
    # 每特征每件的阶段集合
    per_feat = collections.defaultdict(collections.Counter)
    rel = collections.defaultdict(list)
    for pt in parts:
        ts = []
        for row in raw[pt]:
            no = row["process_no"]
            if no < 299: continue
            nm = row["process_name"].strip()
            t = token_of(nm)
            if t:
                names[t][nm] += 1
                ts.append(t)
            else:
                ts.append(f"OTHER|{nm}")
        L = len(ts)
        feats = collections.defaultdict(set)
        for i, t in enumerate(ts):
            if t.startswith("SPINE|"):
                rel[t.split("|", 1)[1]].append(i / max(1, L - 1))
            elif t.startswith("OTHER|"):
                continue
            else:
                f, st = t.split("|", 1)
                if f in FEATS and st in STAGE_ORD:
                    feats[f].add(st)
        for f, s in feats.items():
            per_feat[f][tuple(sorted(s, key=STAGE_ORD.index))] += 1

    ladders = {}
    variants_out = {}
    for f in FEATS:
        if f not in per_feat:
            continue
        c = per_feat[f]
        total = sum(c.values())
        variants = [{"stages": list(k), "count": v, "pct": round(100 * v / total, 1)}
                    for k, v in c.most_common()]
        variants_out[f] = {"total": total, "variants": variants}
        dominant = variants[0]["stages"]
        canonical = []
        for st in dominant:
            t = f"{f}|{st}"
            canonical.append(names[t].most_common(1)[0][0] if t in names else f)
        ladders[f] = {
            "stages": dominant,
            "canonical": canonical,
            "support": variants[0]["count"],
            "total": total,
            "coverage_pct": variants[0]["pct"],
            "stage_names": {st: (names[f"{f}|{st}"].most_common(1)[0][0] if f"{f}|{st}" in names else None)
                            for st in dominant},
        }

    profiles[cat] = {
        "label": "外圈" if cat == "outer" else "内圈",
        "sample_parts": len(parts),
        "grind_feature_ladder": ladders,
        "ladder_variants": variants_out,
        "spine_relative_position": {k: round(sorted(v)[len(v) // 2], 4) for k, v in rel.items()},
        "spine_frequency": {k: len(v) for k, v in rel.items()},
        "spine_canonical_names": {k: names[f"SPINE|{k}"].most_common(1)[0][0]
                                  for k in rel if f"SPINE|{k}" in names},
    }

out = {
    "schema_version": "farmcad-route-templates-v1",
    "generated_at": "2026-11-10",
    "purpose": "按零件类别 + 图纸特征签名生成默认工艺路线，替代按图号查 Excel。",
    "evidence": {
        "route_tables_scanned": payload["sources"],
        "total_route_rows": payload["total_rows"],
        "distinct_parts": payload["distinct_parts"],
        "category_distribution": payload["category_counts"],
        "closed_vocabulary": {"outer_ring": 58, "inner_ring": 56},
        "note": "路线词表封闭：把工序名归一为 (特征, 阶段) 或脊柱名后，"
                "全类别观察到的规范 token 集合覆盖 100% 的真实路线 token。",
    },
    "stage_order": STAGE_ORD,
    "stage_codes": {"M": "均磨", "R": "粗磨", "R2": "粗研", "F": "细磨", "S": "终磨",
                     "P": "精研", "X": "修磨"},
    "feature_keys": {"GROOVE": "沟槽", "OD": "外径类", "ID": "内径类", "PLANE": "平面"},
    "category_profiles": {
        "outer_ring": profiles["outer"],
        "inner_ring": profiles["inner"],
        "cage": {
            "label": "保持架", "sample_parts": 1,
            "source": "dataset/BJ/excel/工序选择.xlsx (D106NW1(H).56) + dataset/config/保持架/D109/",
            "fixed_route": CAGE_ROUTE,
            "caveat": "仅 1 个图号样本，证据强度不足，不做阶梯展开。",
            "route_version_provenance": {
                "derived_from_deployment": "NOVbcj (20260918 版)",
                "derived_from_part": "D106NW1(H).56",
                "derived_from_rows": 28,
                "note": "同一图号在 20260917 部署的表中为 24 道路线（无备料段、热处理工序名"
                        "为『调质』），与车间实际使用的 6006FBC1/P4YA6.66 路线逐道完全一致；"
                        "本模板取的是 28 道版本，故机加段 23 道一致而前段存在差异。",
                "known_divergence": [
                    "备料段（原材料检验/下料/锻造/锻造检验）仅在锻件非投料时成立；"
                    "图纸技术要求写明『锻件投料』时应整段删除。",
                    "热处理工序名应按技术要求取『调质』而非泛化的『热处理』。",
                ],
            },
        },
        "rolling_element": {
            "label": "滚动体", "sample_parts": 2,
            "source": "dataset/excel/工序选择GZ.xlsx (C276927S3K10W1, C276927NK1W1(H))",
            "fixed_route": ROLLING_ROUTE,
            "caveat": "仅 2 个图号样本，证据强度不足，不做阶梯展开。",
        },
    },
    "generation_rules": {
        "description": "阶段主序 + 特征门控 + 脊柱按实测相对位插入。",
        "steps": [
            "1. 识别零件类别（外圈/内圈/保持架/滚动体）",
            "2. 从设计图提取特征签名：沟槽/外径/内径/平面 + 工艺条件",
            "3. 对每个存在特征，按阶段阶梯展开工序（阶段主序 M→R→F→S→R2→P→X）",
            "4. 按 spine_relative_position 插入脊柱工序",
            "5. 分配工序号槽位（磨加段 300-450，步长 5）",
        ],
        "quality_status": "system_derived_not_human_verified",
        "note": "本文件为 system 级路线模板，不是人工 gold。"
                "使用本模板生成的路线，其 OP 事件 quality 必须为 system。",
    },
    "validated_performance": {
        "method": "以真实路线反推特征签名，再生成路线，与真实路线比对 Jaccard；"
                  "基线为随机取同类别另一件。",
        "outer_ring": {"jaccard_mean": 0.79, "baseline": 0.60, "gain": 0.19},
        "inner_ring": {"jaccard_mean": 0.76, "baseline": 0.57, "gain": 0.19},
        "oracle_ceiling": {
            "note": "已知真实槽位集合时，规范空间 Jaccard = 1.000，"
                    "但精确顺序匹配仅 1%，说明工序集合可预测而顺序不可完全确定。",
        },
    },
    "known_limitations": [
        "同阶段内不同特征的先后在实测中接近 50/50 随机，本模板不保证精确顺序。",
        "车加段（工序号 100-130）差异极大，受毛坯形态与成型方式支配，未纳入本模板。",
        "保持架仅 1 个图号、滚动体仅 2 个图号，其固定路线证据强度不足。",
        "路线版本在不同部署间不一致（D208N4Q.01 在 Amix 表为 22 道，D207NQ.01 在 20260917 活动表为 32 道）。",
        "各特征的阶段阶梯存在多个变体（如外圈 GROOVE 有 4 种变体），本模板只取最高频变体。",
        "少数图纸只含备料段、无可识别加工特征，模板无法生成，须回退到按图号查表或人工补录。",
        "保持架路线版本分歧未消解：同一图号 D106NW1(H).56 在 20260917 部署为 24 道、"
        "在 20260918/NOVbcj 部署为 28 道；本模板取后者，而车间实际使用的 6006FBC1 路线"
        "与前者逐道完全一致。差异集中在备料段与热处理工序命名，详见 "
        "category_profiles.cage.route_version_provenance。",
        "保持架模板的源图号 D106NW1(H).56 与测试图纸 D106NKW1(H).56 不同，"
        "后者在全部署的路线表中均无对应行，只能依靠本模板覆盖。",
    ],
}

path = (ROOT + r"\chainmodel\data\route_templates\route_templates_v1.json")
json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("written")
for cat in ("outer_ring", "inner_ring"):
    p = out["category_profiles"][cat]
    print(f"\n### {cat} ({p['sample_parts']} 件)")
    for f, v in p["grind_feature_ladder"].items():
        print(f"  {f:<7} [{v['coverage_pct']}%] {' → '.join(v['canonical'])}")
