# 保持架/内外圈样本扩充（2026-11-10）

本轮解决的问题：保持架/滚动体样本只有 1–2 个图号，阶梯模型没有统计意义。
结论：**保持架从 1 个图号扩充到 24 个可训练数据对，套圈外圈/内圈各有 1 条精确路线 + 家族路线候选**。

## 1. 先看结论

| 指标 | 本轮之前 | 本轮之后 |
|---|---:|---:|
| 可训练数据对 | 1（D207NQ.01） | **25**（D207NQ.01 + 24 保持架） |
| 保持架图号 | 1 | **24** |
| 保持架实体数 | 428 | 12,532 |
| 保持架工序记录 | 32 | 620 |
| 带参数的工序 | 32 | 207 |
| 事件数 | 169 | 2,196 |
| 人工 gold | 0 | 0 |

数据主目录：[combined-batch-v1](../data/processed/combined-batch-v1)。

**所有保持架监督都是 system 级弱标**，来源是上游平台的规则引擎与历史工艺卡，
没有任何工程师审核，因此不能报告泛化性能，只能做管线验证。

## 2. 图纸在哪里、怎么分类

`E:\AAhz\测试数据` 共 35 张 DWG：

```text
测试数据/
    3D1000095X3.01.dwg        套圈外圈
    3D1000095X3.02.dwg        套圈内圈
    C110K1QT2U(H).01.dwg      套圈外圈
    D207NQ.01.dwg              套圈外圈
    E207N4QU.02 JP.dwg         套圈内圈（文件名带空格，需人工确认）
    （内部）C1000904NW1U(H).02.dwg   套圈内圈
    （内部）D209NW1U1(H).01.dwg      套圈外圈
    （内部）D209NW1U1(H).02.dwg      套圈内圈
    （内部）D210NW1U(H).01.dwg       套圈外圈
    （内部）D210NW1U(H).02.dwg       套圈内圈
    （内部）E207N4QU.01.dwg         套圈外圈
    正圆/   11 张，保持架（兜孔为正圆）
    椭圆/   13 张，保持架（兜孔为椭圆）
```

分类规则按团队约定：**文件名尾号 `.56` / `.66` 是保持架，`.01` 是套圈外圈，`.02` 是套圈内圈**。
脚本 `scripts/classify_test_data.py` 实现该规则，并额外用上游标题栏解析器交叉验证。

标题栏解析结果与文件名规则完全一致，24 张保持架图纸的 `产品名称` 全部是
`保持架` 或 `半保持架`，材料牌号全部为 `40CrNiMoA-GJB1951` 或 `40CrNiMoA-MAS2011`，
**没有缺材料的样本**。这是本轮能一次拿到 24 个数据对的直接原因。

## 3. 路线从哪来：三档证据强度

关键实测结论：**保持架的工艺路线在本厂基本是固定的**。

| 证据 | 内容 |
|---|---|
| `D106NW1(H).56` 表格行（`NOVbcj` / 20260918 部署的 `dataset/BJ/excel/工序选择.xlsx`） | 28 道工序，与我们的 `route_templates_v1.json` 保持架固定路线**逐道完全一致**（工序号、工序名、工序类型全同）——因为模板就是从这张表导出的 |
| `D106NW1(H).56` 表格行（**20260917 部署**的同名表） | **24 道**，无备料段，热处理工序名为 `调质`；与 `6006FBC1` 的路线**逐道完全相同** |
| `6006FBC1/P4YA6.66` 表格行 | 24 道工序，机器加工段与固定路线一致，差异在热处理（`调质` vs `热处理`）和备料段 |
| 上游 `NOVbcj` 已生成的保持架工艺卡 | `6006FBC1/P4YA6.66` 25 个卡文件、`C206NW1U(H).56` 18 个、`C180506K1NW1R3U(H).56` 10 个、`C205NW1U(H).56` 8 个 |
| 24 张保持架图纸 | **只有 1 张**（`6006FBC1-P4YA6.66`）在 22 张工序表里有精确图号行 |

> ⚠️ **2026-11-11 更正**：上表第一行的"逐道完全一致"是**同源**的
> （模板从该表导出，自然一致），**不构成独立验证**。
> 真正的独立交叉验证是 `6006FBC1`（`excel_exact` + `card_exact` 双源印证），
> 它只有 **23 道机加段**与模板一致，**前段备料与热处理命名不一致**。
> 且车间实际使用的 24 道版本（20260917 部署）**恰好不是模板采用的那一版**。
> 详见 [划分闭合性与路线证据核查](split-closure-and-route-evidence-20261111.md) §6。

所以保持架的路线来源分三档，脚本 `scripts/build_route_registry.py` 逐档记录、显式选择、不合并：

1. `excel_exact` — 工序表中有精确图号行。**1 个**（`6006FBC1/P4YA6.66`，24 道）。
2. `card_exact` — 从上游历史工艺卡工作目录恢复。**3 个**：
   - `C206NW1U(H).56` 18 道，16 道带 `cage_patch`；
   - `C180506K1NW1R3U(H).56` 10 道；
   - `C205NW1U(H).56` 8 道。
3. `template_category` — 类别固定路线模板。**20 个**，28 道工序。

模板档位是 system 级：它来自一条真实表格行，但从未为这 20 个图号单独确认过。
数据集里 `route_source` 字段逐例记录了实际用了哪一档，`quality_status` 明确写出档位。

### 3.1 工艺卡恢复的关键细节

`temp/modifier/<task_id>/` 下的卡片 JSON 顶部带完整工序身份：

```json
{
  "filename": "6006FBC1-P4YA6.66_115_粗磨外径",
  "part_number": "6006FBC1/P4YA6.66",
  "process_no": "115",
  "process_name": "粗磨外径",
  "process_type": "保持架机加",
  "logical_process_order": "14",
  "cage_patch": { "粗磨外径": { "parameters": { "Dc": { "nominal_mm": 45.22, "source_type": "product_drawing_plus_allowance", "evidence": "design.Dc+0.3" } } } }
}
```

两个必须处理的坑：

- **分页要合并**：`..._第1页` / `..._第2页` 是同一道工序的两个页面，不是两道工序。
  合并后 `6006FBC1/P4YA6.66` 是 24 道而不是 25 个文件。
- **图号分隔符不一致**：标题栏是 `6006FBC1/P4YA6.66`，文件名是 `6006FBC1-P4YA6.66`。
  查表和查卡都必须按规范化键（`/` → `-`）匹配，否则会漏掉 1 张图纸。

`scripts/recover_route_from_cards.py` 负责这两件事，并在多个部署目录之间比对同一图号的路线，
一致才标记 `corroborated`。

## 4. 参数监督从哪来

保持架走的是平台自己的 `src.BJ.cage` 适配器，和套圈的 `src.TQ.mogong` 是两套代码：

| | 套圈外圈 | 保持架 |
|---|---|---|
| 设计参数提取 | `build_design_part_params` | `build_cage_design_params` |
| 规则引擎 | `ProcessRuleEngine` | `CageProcessRuleEngine` |
| 规则资产 | `dataset/TQ/rules/MoRules.json` | `dataset/BJ/rules/CageRules.json` |
| 参数来源类型 | 12 类 | 8 类 |

保持架规则引擎只覆盖 8 道工序（`粗磨外径`、`终磨外径`、`终车内径及倒角`、
`均车两平面及倒角`、`细车内径及倒角`、`切断`、`倒铆钉孔角`、`拆销子`），
其余 20 道保留路线槽位并显式标记 `parameters_unavailable`——
**没有算出来不等于这道工序没有参数**，这个区分写进了 `generation_status`。

24 张图纸的提取全部成功，每张 12–16 个设计参数、8 道带参数的工序、36 个参数记录：

```text
Dc    nom=41.6999  upd=derive      feat=[Dc]     req=[Dc:requirement:0]     role=[machining_target]
VDcp  dir=0.02     upd=lookup      feat=None
Ra    dir=1.25     upd=design_copy feat=None
dc    nom=36.5     upd=design_copy feat=[dc]     req=[dc:requirement:0]     role=[machining_target]
内倒角 nom=0.3     upd=derive      feat=[inner_outer_chamfer]
每组销子数 dir=12.0 upd=derive     feat=None
```

`update` 字段之前全是 `unknown`——`make_target` 只认套圈外圈的 4 个来源类型。
已在 `chainmodel/src/farmcad/data/prepare.py` 中把保持架的 8 类来源类型并入映射表，
同时把映射抽成 `update_operator()`，两套引擎共用一张表。

## 5. 特征锚点是真 handle

保持架参数提取器对每个值都带 `source.handle`，指向图上的具体实体：

```text
dc          -> 26D9A    Dc          -> 26B5D
Bc          -> 26C24    Po/Pm       -> 26D9D
inner_outer_chamfer -> 27747        pocket_diameter -> 26C1D
rivet_hole_count   -> field_map:技术要求
```

这直接缓解了 `candidate` 权重 0.0 的瓶颈：保持架的 `features` 指针有真实实体支撑，
不再是悬空的角色名。但**物理特征分组（`physical_object_id`）仍然缺失**——
同一条沟道的多个尺寸还没有合并成一个物理对象，这是下一轮要补的。

## 6. 套圈外圈/内圈：精确路线只有 3 个

| 图号 | 类别 | 路线 | 工序数 | 来源 |
|---|---|---|---:|---|
| `3D1000095X3.01` | 外圈 | 精确 | 30 | `工序选择（测试流程完整版）.xlsx` |
| `3D1000095X3.02` | 内圈 | 精确 | 29 | 同上 |
| `D207NQ.01` | 外圈 | 精确 | 32 | `dataset/excel/工序选择.xlsx` |
| `C110K1QT2U(H).01` | 外圈 | 工艺卡恢复 | 37 | `temp/modifier/767fefb3` |
| `E207N4QU.02 JP` | 内圈 | 工艺卡恢复 | 41 | `temp/modifier/d881e781` / `20260918/a96eb911` |
| `（内部）C1000904NW1U(H).02` | 内圈 | 无 | – | – |
| `（内部）D209NW1U1(H).01/.02` | 外/内圈 | 无 | – | – |
| `（内部）D210NW1U(H).01/.02` | 外/内圈 | 无 | – | – |
| `（内部）E207N4QU.01` | 外圈 | 无 | – | – |

套圈剩余 6 张**本轮没有路线**，因为：

- `E:\AAhz\测试数据` 根目录只有 DWG，**没有提取后的 JSON**，不能直接进管线；
- 22 张工序表里没有这 6 个图号的精确行；
- 家族候选已由 `scripts/find_part_families.py` 列出（见下），但**同系列零件的路线不等于该零件的路线**，
  只能作为工程师审批路线时的参考起点。

家族候选示例（数字系列是保守启发式，不是工程族标签）：

```text
（内部）D209NW1U1(H).01   series=209   候选: E209.01(29道) / E209.02(28道) / ...
（内部）D210NW1U(H).01    series=210   候选: E210.01 / E210.02 / ...
（内部）E207N4QU.01       series=207   候选: D207NQ.01(32道) / D207NQ.02(41道)
```

## 7. 新增脚本

| 脚本 | 作用 |
|---|---|
| `scripts/classify_test_data.py` | 按尾号规则分类图纸，跨 22 张工序表查精确图号 |
| `scripts/scan_route_cards.py` | 扫描各部署的 `temp/modifier`，盘点可恢复路线与工艺卡 |
| `scripts/recover_route_from_cards.py` | 从工艺卡恢复路线：合并分页、规范化图号、跨部署比对 |
| `scripts/build_route_registry.py` | 按图号汇总三档路线来源，显式选择，记录 SHA256 |
| `scripts/probe_cage_batch.py` | 批量探测保持架设计参数与规则解析结果 |
| `scripts/build_cage_case.py` | 单个保持架数据对：`design.json` + `target.json` |
| `scripts/build_cage_dataset.py` | 批量构建保持架数据集 + 清单 + 审核表 |
| `scripts/assemble_combined_dataset.py` | 合并套圈试点与保持架样本，按族划分 train/validation |
| `scripts/find_part_families.py` | 为缺路线的图号找同系列候选路线 |

## 8. 验证

- 25 个数据对全部通过 `validate_design` + `validate_target`。
- `run.py validate-data --manifest ... --smoke` 通过，词表覆盖率 100%（0 个未知工序/参数）。
- `run.py train --config configs/local-smoke.json --smoke` 通过，loss 1.507 → 1.369。
- `chainmodel` 测试 65 项全过；`scripts` 测试 45 项中 44 项过。

唯一失败项 `test_build_process_chain.RealChainTests.test_cli_replay_is_byte_identical_and_refuses_overwrite`
**与本轮改动无关**：它比较的是 `provenance.json` 里的**绝对路径**，
存档的产物记录的是 `E:\AAhz\20260918\intentionCAD\...`，本机是 `E:\AAA我的论文\...\intentCAD\...`。
产物内容哈希（`association_graph.json` 等 7 个文件）逐个比对**全部一致**，
只有 `provenance.json` 的路径字符串不同。这是环境搬迁导致的，不是数据漂移。

## 9. 还没有做到的

1. **保持架物理特征分组**：参数有真 handle，但同一物理对象的多个尺寸尚未合并，
   `physical_object_id` 仍然为空。
2. **20 个保持架用的是类别固定路线**，不是这 20 个图号各自的确认路线。
   差异（热处理是 `调质` 还是 `热处理`、备料段是否保留）必须由工程师确认。
3. **套圈剩余 6 张没有路线**，`E:\AAhz\测试数据` 根目录缺提取 JSON，需要用 AutoCAD 副本重新提取。
4. **零人工 gold**。所有参数仍是 system 级，`parameters_complete` 只能标 system。
5. **保持架只覆盖了 8/28 道工序的参数计算**。粗车、钻铆钉孔、钻铣孔、光饰、终检、镀银、
   包装这些工序没有规则；平台生成了卡但没有把参数写进 `cage_patch` 的规则部分。
6. **内圈/滚动体没有样本**。上游 `src.TQ` 支持内圈，但测试数据里没有内圈保持架以外的内圈图纸可提取。

## 10. 下一步建议

优先级从高到低：

1. **给 20 个保持架图号建立逐个确认的路线**——只要工程师审一遍 `review/cases.csv`
   里的 `route_source` 档位，这 24 个样本就从"类别推断"升级为"逐图号确认"。
2. **补 `physical_object_id`**——保持架的 handle 证据已经齐了，把 `dc`/`Dc`/`Bc`/
   `inner_outer_chamfer`/`pocket_diameter` 归到物理对象，`features` 指针才能从
   `candidate` 升级到可用权重。
3. **重新提取套圈根目录的 6 张图纸**，然后用家族候选 + 工程师审批补路线，
   把外圈/内圈从 3 个提到 9 个。
4. **扩展保持架规则覆盖**：上游 `CageRules.json` 只有 8 道工序，
   粗车/钻孔/光饰等工序的工艺卡虽然生成了，但参数没有进结构化补丁。
