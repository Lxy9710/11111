# 类别默认路线：实测结论与实现

最后核对时间：2026-11-10

本文回答「能否查到每种零件类别的默认路线并写入」。结论是**能，但要按
「类别 + 特征签名」建模，不能按「一类别一条路线」建模**。所有数字来自
本机 `smartcad38` 环境下对 6 张工序选择表的实测。

---

## 1. 数据基础

| 来源 | 行数 | 图号数 |
|---|---:|---:|
| `dataset/TQ/excel/工序选择1.xlsx` | 16240 | 616 |
| `dataset/TQ/excel/工序选择（测试流程完整版）.xlsx` | 856 | 25 |
| `dataset/TQ/excel/工序选择.xlsx`（活动表） | 27 | 1 |
| `dataset/BJ/excel/工序选择.xlsx` | 28 | 1 |
| `dataset/excel/工序选择GZ.xlsx` | 59 | 3 |
| `E:/AAhz/Amix/mix717/CAD_Process_Hublh/dataset/excel/工序选择.xlsx` | 308 | 12 |

| 合计 | **17417 行路线 / 645 个图号** |

按部位名称与工序名称判定类别：

| 类别 | 图号数 |
|---|---:|
| 内圈 | 378 |
| 外圈 | 264 |
| 保持架 | 1 |
| 滚动体 | 2 |
| 未判定 | 2 |

---

## 2. 关键发现：路线词表是封闭的

把工序名归一为 **(特征, 阶段)** 二元组或**脊柱工序名**后：

| 类别 | 规范 token 数 | 覆盖真实路线 token 比例 |
|---|---:|---:|
| 外圈 | 58 | **100%** |
| 内圈 | 56 | **100%** |

也就是说：**所有真实路线都是这 ~57 个 token 的组合**。词表封闭意味着路线
预测在词表层面是封闭问题，不存在开放式工序名。

归一化规则：

- 特征键：`GROOVE`（沟）、`PLANE`（平面）、`OD`（外径类）、`ID`（内径类）
- 阶段码：`M` 均磨 / `R` 粗磨 / `F` 细磨 / `S` 终磨 / `R2` 粗研 / `P` 精研 / `X` 修磨
- 脊柱工序：与特征无关的工序（酸洗、除氢、探伤、激光打字、装前检查、
  提交、退磁、清洗、回火、打倒角、去锐角、光饰、磨工总图）

---

## 3. 路线结构：每个特征一套阶段阶梯

同类别零件的路线由「**每个加工特征的阶段阶梯**」+「**脊柱工序**」决定。

### 3.1 外圈（207 件含磨加段的样本）

| 特征 | 规范阶梯 | 覆盖率 |
|---|---|---:|
| GROOVE | 粗磨外沟 → 细磨外沟 → 终磨外沟 → 粗研外沟 → 精研外沟 | 48.2% |
| OD | 粗磨外径 → 终磨外径 → 精研外径 → 修磨外径 | 56.9% |
| ID | 粗磨外圈内径 → 终磨外圈内径 | 93.2% |
| PLANE | 均粗磨平面 → 终磨平面 → 修磨平面 | 62.9% |

### 3.2 内圈（312 件）

| 特征 | 规范阶梯 | 覆盖率 |
|---|---|---:|
| GROOVE | 粗磨内沟 → 终磨内沟 → 粗研内沟 → 精研内沟 | 57.4% |
| OD | 粗磨内圈外径 → 终磨内圈外径 → 精研内圈外径 | 42.3% |
| ID | 粗磨内径 → 终磨内径 | 70.7% |
| PLANE | 均粗磨平面 → 终磨平面 → 修磨平面 | 52.6% |

每个特征都有 2–4 个变体（如外圈 GROOVE 有 `R-F-S-R2-P` 96 件、
`R-S-R2-P` 94 件、`R-F-S-P` 8 件）。模板只取最高频变体。

### 3.3 脊柱工序

脊柱工序与特征无关，按实测相对位置插入。外圈的中位相对位置：

```
磨工总图 0.00 → 中温补充回火 0.23 → 打倒角 0.28 → 粗光饰 0.30
→ 酸洗 0.48 → 除氢 0.54 → 去锐角/精光饰 0.62 → 荧光磁粉探伤 0.67
→ 退磁 0.77 → 激光打字 0.88 → 清洗 0.90 → 提交/装前检查 1.00
```

---

## 4. 顺序不可完全确定（重要的负面结论）

我最初假设路线是「固定骨架 + 特征门控」，回测证明这个假设是错的：

| 模型 | 外圈 Jaccard | 内圈 Jaccard |
|---|---:|---:|
| 逐特征串行拼接 + 脊柱追加 | 0.650 | 0.576 |
| 阶段主序 + 脊柱追加 | 0.666 | 0.634 |
| 规范骨架槽位 | 0.418 | — |
| **阶段主序 + 特征门控 + 脊柱按实测位插入** | **0.791** | **0.760** |
| *基线：随机取同类别另一件* | *0.604* | *0.570* |

决定性实验：**已知每件真实路线的 token 集合（oracle）**，

- 规范空间 Jaccard = **1.000** → 工序**集合**完全可预测；
- 但精确顺序匹配仅 **1%** → 工序**顺序**不可由单条固定规则确定。

进一步的分解定位了原因：**同一阶段内不同特征的先后在实测中接近 50/50 随机**。

外圈 stage=S 阶段内成对先后统计（共 1580 对）：

```
GROOVE>OD = 200    OD>GROOVE = 200      # 完全对半
PLANE>OD  = 161    OD>PLANE  = 161
PLANE>GROOVE = 159 GROOVE>PLANE = 159
```

内圈 stage=S（共 2724 对）同样是 `ID>GROOVE=297 / GROOVE>ID=297` 对半。

**这就是路线生成的固有难度**：工艺上"终磨外径"和"终磨外沟"本就可以交换，
真实工艺员凭经验决定，而不是由规则唯一确定。对本项目而言这是**好消息**——
它说明路线预测的目标应该是**工序集合 + 阶段归属**，而不是精确序列。

### 4.1 阶梯变体不可由特征签名预测

尝试用「其它特征是否存在」来预测某特征的阶梯变体：

| 类别 | 特征 | 最高频基线 | 其它特征条件化 | 提升 |
|---|---|---:|---:|---:|
| 外圈 | GROOVE | 0.482 | 0.543 | +0.060 |
| 外圈 | OD | 0.569 | 0.627 | +0.059 |
| 内圈 | GROOVE | 0.574 | 0.577 | +0.003 |
| 内圈 | OD | 0.423 | 0.427 | +0.003 |

提升极小。用**图号前缀（系列号）**做条件化可达 0.86–0.97，但那等价于记忆
图号，不能用于新图纸。**结论：阶梯变体需要图纸之外的额外信息（精度要求、
工艺习惯、具体工艺卡），不能从设计图本身可靠推出。**

---

## 5. 实现

### 5.1 资产

`chainmodel/data/route_templates/route_templates_v1.json`

按类别记录：特征阶梯（含规范工序名与各变体频次）、脊柱相对位置与频次、
阶段主序、固定路线（保持架/滚动体）、实测性能与已知局限。

### 5.2 生成器

`chainmodel/src/farmcad/data/route_templates.py`

```python
from farmcad.data.route_templates import (
    FeatureSignature, generate_route, signature_from_design,
    CATEGORY_OUTER, CATEGORY_INNER,
)

signature = signature_from_design(CATEGORY_OUTER, design_parameters, field_map)
route = generate_route(signature)
route.rows        # 工序行，结构与 load_excel_processes_for_part 一致
route.provenance  # 生成依据：用了哪些阶梯、哪些脊柱、证据等级
```

`route_templates.py` 与 `route_source.py` 一起提供 `RouteResolver`：

```python
resolver = RouteResolver(excel_loader=load_excel_processes_for_part)
resolved = resolver.resolve("D208N4Q.01", CATEGORY_OUTER, signature)
resolved.source   # "excel_exact" 或 "template_category"
```

查表命中优先；查不到才回退到类别模板，回退时在 `warnings` 里显式记录。

### 5.3 质量等级

模板路线是 **system 级**，不是 gold：

- `resolved.is_human_verified` 恒为 `False`；
- `provenance.quality_status = "system_derived_not_human_verified"`；
- 用模板路线生成的 target，其 OP 事件 `quality` 必须为 `system`。

按 `data-contract.md` 的五级质量分层（gold 1.0 / silver 0.5 / system 0.2），
模板路线只能拿 0.2 的权重。**这不能替代人工标注**，只是让 `blocked_route`
不再是数据管线的硬阻塞。

---

## 6. 对数据集的直接影响

`outer-batch-v2` 里有 3 个 `blocked_route` 样本（`C110K1QT2U(H).01`、
`D208N4Q.01`、`3D1000095X3.01`），另有 `3D1000095X3.02`。这些图号在任何
部署的工序表里都没有精确行。现在它们可以：

1. 用 `signature_from_design` 从设计图提取特征签名；
2. 用 `generate_route` 得到类别默认路线；
3. 用上游 `ProcessRuleEngine` 在该路线上复算参数。

但产出的路线与参数都必须标为 `system`，且**不能替代**已找到的精确路线：
`3D1000095X3.01` 在 `工序选择（测试流程完整版）.xlsx` 里有精确的 30 道路线，
应当优先用精确路线，模板只作兜底。

---

## 7. 已知局限

1. **同阶段内特征顺序不可确定**（实测 50/50）。模板给的是一种合理排列，不是真值。
2. **车加段（工序号 100–130）未纳入模板**。该段差异极大，受毛坯形态
   （棒料/管料/锻件）与成型方式（一次成型/成型/细车A面B面）支配，
   现有 645 件样本无法用单一模板覆盖。
3. **阶梯变体需要图纸外信息**（见 4.1）。
4. **保持架仅 1 个图号、滚动体仅 2 个图号**，其固定路线证据强度不足，
   模板中标记为 `fixed_route` 并附 caveat。
5. **少数图纸只有备料段、无可识别加工特征**（实测 2 件：
   `C2116K5NW1U(H).04`、`D24M02U.04`），模板无法生成，
   `generate_route` 会显式抛错，要求回退到按图号查表或人工补录，
   **不用空模板冒充**。
6. **路线版本在部署间不一致**：`D208N4Q.01` 在 Amix 表是 22 道，
   `D207NQ.01` 在 20260917 活动表是 32 道。使用前需确认版本。
7. **路线只覆盖磨加段（工序号 ≥ 299）**。备料段（10–40）与车加段（100–130）
   仍依赖按图号查表。

---

## 8. 复核命令

```powershell
# 1. 抽取全部路线数据（含来源表 SHA256）
& 'D:\conda\envs\smartcad38\python.exe' scripts\route_template\extract_routes.py `
    --output chainmodel\data\route_templates\routes_raw.json --include-external

# 2. 从路线数据学习阶梯与脊柱，生成模板资产
& 'D:\conda\envs\smartcad38\python.exe' scripts\route_template\build_route_templates.py

# 3. 回测：生成器 vs 随机同类别基线
& 'D:\conda\envs\smartcad38\python.exe' scripts\route_template\backtest_route_templates.py

# 4. 端到端：真实 blocked_route 图号走通「查表失败 -> 类别模板」
& 'D:\conda\envs\smartcad38\python.exe' scripts\route_template\e2e_route_resolution.py
```

全部 Python 实测均在 `smartcad38`（Python 3.8.20 / PyTorch 1.13.1+cpu）下执行。

---

## 9. 与已有数据集的衔接

`chainmodel/data/route_templates/` 下的三个文件构成完整链路：

| 文件 | 角色 |
|---|---|
| `routes_raw.json` | 抽取的原始路线 + 每张来源表的 SHA256 |
| `route_templates_v1.json` | 学习到的默认路线资产（生成器输入） |
| `../../../scripts/route_template/*.py` | 抽取 / 学习 / 回测 / 端到端验证脚本 |

生成器与解析器的接口：

```python
from farmcad.data.route_source import RouteResolver
from farmcad.data.route_templates import CATEGORY_OUTER, signature_from_design

resolver = RouteResolver(excel_loader=load_excel_processes_for_part)
resolved = resolver.resolve(case_id, CATEGORY_OUTER, signature)
resolved.source       # "excel_exact" | "template_category"
resolved.is_human_verified   # 恒为 False
```

`scripts/build_outer_dataset.py` 已接入该解析器：查表失败时自动回退到
类别模板，并在 `process_parameters.json` 的 `route_source` 字段记录来源、
生成依据与降级警告。**模板路线产生的 OP 事件质量等级为 `system`，
不得当作 `gold`。**
