# R1/R2/R3 路线与参数条件化设计（2026-11-11 12:40）

本文是「让保持架监督真正依赖设计图」的实现方案。动机见
[v3 结果与目标级泄漏诊断](v3-results-and-target-leakage-20261111.md)：
19/25 案例共享同一粗粒度监督模式，换图后指标不掉，模型没在读设计图。

---

## 0. 现状诊断

| 事实 | 证据 |
|---|---|
| 保持架用**固定类别路线模板**生成目标 | 19 案例的 (工序→参数集合) 结构完全相同（27 工序 / 36 参数） |
| 数值随图纸变，但**结构不变** | 粗粒度监督指纹 7 种，其中一种覆盖 19 例、跨 train/val/test |
| 兜孔几何差异**不改变参数集合** | 圆孔(`pocket_diameter`) 与椭圆孔(`pocket_radius`) 的参数列表相同 |
| 模型因此退化为查表 | 换图后 features 仍 9/9、requirements 仍 6/6 |

**根本矛盾**：路线模板是「类别先验」，但**类别先验本身不该是全部监督**。
若所有同类零件的路线与参数结构完全相同，那么「设计图→工艺」这个映射
在保持架这一类上就是**常量函数**，无从学习、无从评测。

---

## 1. 三条条件化规则

### R1 — 备料段按投料方式条件化

**证据**：`design["context"]` 的技术要求原文。

| 触发条件 | 动作 |
|---|---|
| 含「锻件投料」 | **删除**备料段 4 道：`原材料检验` / `下料` / `锻造` / `锻造检验` |
| 含「棒料投料」 | 保留备料段 |
| 无投料声明 | 保留备料段（缺省） |

**物理依据**：锻件投料意味着毛坯由锻件厂供给，本厂不负责下料与锻造。
这与 `20260917` 部署的 24 道路线（无备料段）一致。

### R2 — 热处理工序名按技术要求条件化

| 触发条件 | 动作 |
|---|---|
| 技术要求含「调质」 | 热处理工序名改为 `调质` |
| 技术要求含「热处理」但无「调质」 | 保持 `热处理` |

**物理依据**：`调质`（淬火+高温回火）是特定工艺，与泛化的 `热处理` 不是一回事。
图纸写明硬度要求（如 33–37 HRC）即证明是调质。

### R3 — 参数集合按几何与工艺条件条件化 ⭐

R1/R2 只改变**工序序列**，不改变**参数集合**，因此**不足以**打破监督同质。
R3 才是关键：让几何差异真正改变某道工序带哪些参数。

#### R3.1 SDc/Sdc 条件形位公差（上游已有，我们的构建器漏掉了）

上游 `NOVbcj/smartCAD/CAD_Process_Hub/src/BJ/cage/sdc_condition.py` 已实现：

```python
# 条件：前置工序 Bc >= 6
{
  "process_name": "粗磨外径",  "inspection_project": "SDc",
  "when": {"all": [{"parameter": "前置工序Bc", "operator": ">=", "value": 6}]}
}
```

同样规则也适用于 `均车两平面及倒角`（SDc）与 `细车内径及倒角`（Sdc）。

**上游行为**（`engine.py`）：

```python
if not condition["include"]:
    resolved.pop(project, None)   # Bc < 6 时把 SDc/Sdc 从参数集中移除
```

**我们的构建器没有走这条路径**——`build_cage_case.py` 调 `engine.resolve_process()`，
而条件逻辑在 `resolve_with_context()` 里（需要 `prior_bc`）。
结果：**Bc 在 3.5–8.0 之间变化，SDc/Sdc 却恒定存在**。

按阈值重新计算，7 个案例应当**移除** SDc/Sdc：

| case | Bc | 应有状态 |
|---|---:|---|
| `C1000906NW1(U).56` | 3.5 | 无 SDc/Sdc |
| `D1000905NW1(H).56` | 3.6 | 无 SDc/Sdc |
| `C1000909NW1(U).66` | 4.85 | 无 SDc/Sdc |
| `6006FBC1/P4YA6.66` | 5.45 | 无 SDc/Sdc |
| `3C205NW1(H).56` | 5.5 | 无 SDc/Sdc |
| `D106NKW1(H).56` | 5.7 | 无 SDc/Sdc |
| `61911X2FBC1/P4YA1.66` | 5.8 | 无 SDc/Sdc |

其余 16 个（Bc ≥ 6）保持不变。

**这 7 个案例的参数集合将与其他案例不同** —— 监督模式终于开始依几何分化。

#### R3.2 兜孔几何差异改变参数集合

上游 `process_card_rules.py` 的 `钻铣孔` 规则已有分支：

```python
if _is_oval_pocket(design):
    field_order = OVAL_DRILL_MILL_FIELD_ORDER      # Lc / Wc / R(pocket_radius)
    ...
else:
    pocket = design_nominal(design, "pocket_diameter")
    targets["兜孔尺寸"] = _item("兜孔尺寸", nominal=float(pocket), ...)
```

即：**椭圆兜孔** → 参数 `{Lc, Wc, R}`；**圆兜孔** → 参数 `{兜孔尺寸}`。
两者参数**符号完全不同**，是一条天然的几何→参数集合映射。

> 但 `钻铣孔` 在本批次只有 2 个案例带参数（其余走 `parameters_unavailable`），
> 所以这条规则对监督多样性的贡献有限。**保留但标注为证据不足**。

#### R3.3 铆钉孔配对分支

上游 `process_card_rules.py`：

```python
if canonical == "钻铆钉孔" and rivet_pairing_value(design) == RIVET_PAIRING_PAIRED:
    field_order = ("n", "Pm", "Mc", "VC2")
```

配对/非配对铆钉孔 → 不同参数集。同样，本批次该工序只有 3 例带参数。

---

## 2. 实现方案

### 2.1 `FeatureSignature` 扩展

```python
@dataclass
class FeatureSignature:
    ...
    # 新增：路线条件化证据
    forged_stock: bool = False       # 锻件投料 -> R1
    temper_required: bool = False    # 技术要求含调质 -> R2
    prior_bc: Optional[float] = None # 前置工序 Bc -> R3.1
    oval_pocket: bool = False        # 椭圆兜孔 -> R3.2
    rivet_paired: bool = False       # 铆钉配对 -> R3.3
```

### 2.2 `signature_from_design` 提取

```python
context = design.get("context", "")
signature.forged_stock = "锻件投料" in context
signature.temper_required = "调质" in context
signature.prior_bc = <从 design 参数解析 Bc>
signature.oval_pocket = any(f["id"] == "pocket_radius" for f in design["features"])
```

### 2.3 `generate_route` 应用 R1/R2

```python
if profile.get("fixed_route"):
    rows = [...]
    if signature.forged_stock:
        rows = [r for r in rows if r["process_name_only"] not in PREP_OPS]
    if signature.temper_required:
        rows = [dict(r, process_name_only="调质", ...) if r["process_name_only"] == "热处理" else r
                for r in rows]
```

### 2.4 R3.1 的实现（关键）

不能只改 `resolve_process`——那会丢失上游的条件语义。
应在 `build_cage_case.py` 的 `resolve_operations` 中**显式应用**同一条件资产：

```python
from src.BJ.cage.sdc_condition import resolve_sdc_requirement

# 按工序顺序跟踪 prior_bc
prior_bc = None
for row in route_rows:
    name = row["process_name_only"]
    condition = resolve_sdc_requirement(name, prior_bc)
    if condition is not None and not condition["include"]:
        entry["parameters"].pop(condition["inspection_project"], None)
        entry["conditional_exclusions"].append(condition)
    if name == "均车两平面及倒角":
        prior_bc = entry["parameters"].get("Bc", {}).get("nominal_mm")
```

**必须复用上游资产，不得自己重写阈值**——否则条件规则与工艺卡生成脱节。

---

## 3. 预期效果

| 指标 | 现状 | R1/R2/R3 后（预估） |
|---|---|---|
| 粗粒度监督模式数 | 7 | **≥ 9–12** |
| 跨 split 共享的监督模式 | 1 种覆盖 19 例 | 大幅减少 |
| 参数集合依几何变化 | ❌ | ✅（R3.1，7 例受影响） |
| 路线依设计证据变化 | ❌ | ✅（R1 删 4 道、R2 改名） |
| 换图后指标是否下降 | ❌ 不下降 | 应当下降（这才说明模型在读图） |

### 3.1 新增评估指标：设计扰动敏感度

这是判断「模型是否真的在读设计图」的**直接**指标，应成为常驻项：

| 扰动 | 期望行为 |
|---|---|
| 换成另一张图的设计 | features / requirements 指标**显著下降** |
| 打乱 `feature_id` | 指标大幅下降（现状已验证：9/9 → 0/9） |
| 改 `Bc` 使其跨过 6 的阈值 | 参数集合预测应改变（SDc 出现/消失） |

现状下「换图」不下降 → 指标无效。修复后应下降。

---

## 4. 风险与前置条件

| 风险 | 说明 | 缓解 |
|---|---|---|
| 词表变化 | `热处理` 消失、`调质` 出现（若 R2 触发）；`SDc`/`Sdc` 仍在但部分案例消失 | 重跑 `check_split_closure.py` |
| 划分需重做 | 监督模式变化后必须重新划 | `assign_splits.py` 已含第三道检查，会直接拦住泄漏 |
| R3.1 依赖上游 | 必须复用 `resolve_sdc_requirement`，不能自写阈值 | 单测锁定 |
| 部分工序无参数 | `钻铣孔`/`钻铆钉孔` 多数案例 `parameters_unavailable` | 保持显式状态，不伪造 |
| 数据量仍小 | 25 案例 → 条件化后监督模式仍有限 | 扩类别（内圈/滚动体） |

---

## 5. 实施顺序

1. **R3.1（最高优先）** — 唯一能改变参数集合的规则，且上游已有实现与资产。
2. **R1** — 简单、证据明确（锻件投料），且能对上 `20260917` 的 24 道路线。
3. **R2** — 简单、证据明确。
4. **R3.2/R3.3** — 保留但当前证据不足，标注为 `system` 级。
5. 重建数据集 → 重划划分 → 重训 → 出**可引用**的数字。
6. 加入设计扰动敏感度指标。

---

## 6. 与论文的关系

这一步不是"调数据"，而是**论文方法论的核心论证**：

> 类别路线是**先验**，设计图证据是**条件**。
> 只有当路线与参数集合随设计证据变化时，
> 「拓扑感知的几何定位」才成为**必要**而非可选的机制。

`method-section-framework.md` §3.8.5 的三条条件化规则正是这个意思，
本文把它们从"设想"落到"有上游资产支撑、可回归测试的具体实现"。
