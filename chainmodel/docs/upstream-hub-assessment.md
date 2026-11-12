# 上游推理平台（CAD_Process_Hub）复用评估

最后核对时间：2026-11-10

本文记录对 `NOVbcj/smartCAD/CAD_Process_Hub`（下文称"上游"）的核对结果，以及它如何补足本项目的数据推理缺口。全部结论均在本机 `smartcad38` 环境实测验证，未验证项显式标注。

---

## 1. 上游是什么

上游是完整的工业 CAD 工艺生成系统，Flask 后端 + 规则引擎 + GNN 检索 + LLM 受约束推理 + CAD 写回。主流程：

```
DWG 上传 → 产品图解析/结构化配置 → GNN 检索相似工艺 → 工序匹配
→ 规则 RAG + LLM 受约束推理 → 工艺卡 / CAD / 报告写回
```

关键目录：

| 目录 | 内容 |
|---|---|
| `src/TQ/mogong/` | 磨工规则引擎（`engine.py` 98KB、`rules_store.py`、`slots.py` 94KB、`turning_source.py`） |
| `src/TQ/table_process/product_geometry_extraction/` | 设计图参数抽取（`design_part_params.py` 44KB、`ra_binding.py` 58KB） |
| `src/TQ/process_flow/excel_process_flow.py` | 工序选择 Excel 解析 |
| `src/TQ/gnn_infer/` | 图构建 + 4 种编码器 + 检索 |
| `src/TQ/application/` | 8 阶段编排（`automation_workflow.py` 55KB） |
| `dataset/TQ/rules/MoRules.json` | 磨工留量规则库 |
| `dataset/config/{内圈,外圈,保持架,滚动体,装配}/` | **历史工艺模板库** |
| `dataset/XIN_Reference/` | CAPP 历史快照（23 份 D207NQ 制造工艺 JSON） |

---

## 2. 与本项目数据工程的关系

本项目 `scripts/` 的数据管线已经**直接复用上游**（不是重写）：

```
scripts/build_pilot_pair.py      → build_design_part_params / load_excel_processes_for_part
                                   / MogongRulesStore / ProcessRuleEngine / enrich_design_part_params_for_turning
scripts/build_outer_dataset.py   → 同上 + build_process_chain
```

上游提供的是**确定性参数求解能力**，本项目提供的是**结构化、可追溯、可训练的数据契约**。两者不是竞争关系。

---

## 3. 关键核对结果

### 3.1 NOVbcj 版本 vs 20260917 基线

NOVbcj 是上游的**更新版本**，但相对 `20260917` 基线：

| 资产 | 状态 |
|---|---|
| `dataset/TQ/rules/MoRules.json` | **完全一致**（`dff0f1c197`） |
| `dataset/TQ/rules/allowance_cells_supplement.json` | **完全一致**（`b2d687d7e6`） |
| `dataset/TQ/excel/工序选择1.xlsx`（16240 行） | **完全一致** |
| `dataset/TQ/excel/工序选择（测试流程完整版）.xlsx` | **完全一致**（`5f5a60c5610e`） |
| `dataset/TQ/excel/工序选择.xlsx`（活动表） | **不同**：20260917 有 173 行 / 8 图号；NOVbcj 27 行 / 1 图号（`D208N4Q`） |
| `dataset/excel/检验规范含义表.xlsx`、`锻件尺寸检索.xlsx` | 完全一致 |
| `config/application.yml`、`application-dev.yml` | 不同（配置演进） |
| `dataset/` 全树 | 20260917 = 587 文件；NOVbcj = 1185 文件（611 新增 / 13 删除 / 90 内容变更） |
| `dataset/config/` 工艺库 | NOVbcj 多了 `滚动体/`（26 json）和 `装配/`（21 json） |

`E:\AAhz\NOVbcj\smartCAD\CAD_Process_Hub` 与工作区 NOVbcj 的 `dataset/` **逐文件哈希一致**（仅差 2 个 `.dwl` 锁文件）。两者是同一份数据。

**结论**：NOVbcj 的磨工规则库与 20260917 基线同源，可以安全作为数据生成时��的规则来源；但**活动工序表在 NOVbcj 里被裁剪过**，不能直接用 `PROCESS_SELECTION_EXCEL_PATH` 取路线。

### 3.2 路线供给的真实缺口（最重要的发现）

用 `load_excel_processes_for_part` 逐图实测（活动表 + 全部备选表）：

| 图号 | 20260917 活动表 | NOVbcj 活动表 | 其它备选表 | 结论 |
|---|---|---|---|---|
| `D207NQ.01` | ✅ 32 工序 | ❌ | — | 仅 20260917 可用 |
| `D208N4Q.01` | ❌ | ❌ | ✅ **Amix `dataset/excel/工序选择.xlsx` 22 工序** | 可恢复 |
| `3D1000095X3.01` | ❌ | ❌ | ✅ `工序选择（测试流程完整版）.xlsx` 30 工序 | 可恢复（需工程师批准） |
| `C110K1QT2U(H).01` | ❌ | ❌ | ❌ Excel 无 | **可从工作卡恢复**（见 3.3） |
| `E207N4QU.01 JP` | ❌ | ❌ | ❌ Excel 无 | **可从工作卡恢复**（见 3.3） |
| `D209NW1U1(H).01` | ❌ | ❌ | ❌ | 仅 DWG 上传记录，无卡 |
| `D210NW1U(H).01` | ❌ | ❌ | ❌ | 仅 DWG 上传记录，无卡 |

全量扫描 `E:\AAhz` 下 8 个部署的 6 张 `工序选择*.xlsx`，对 39 张测试 DWG 做精确图号匹配：**只有 2 张命中**（`3D1000095X3.01`、`3D1000095X3.02`）。另外 37 张在任何部署里都没有精确路线。

`dataset/TQ/excel/工序选择1.xlsx`（16240 行 / 616 图号）是历史全量库，但**不含** D207NQ / D208N4Q / E207N4QU / 3D1000095 / C110K1QT2U / D209 / D210 任一。

### 3.3 上一条结论的例外：工作卡目录里有完整路线

上游 `temp/modifier/<task_id>/` 保存了**已写回的工序卡 JSON**，每张卡带 `logical_process_order` / `logical_process_no` / `process_name` / `mogong_patch`。这些文件**本身就是路线 + 参数的完整记录**。

实测覆盖（`20260909` / `20260917` / `20260918` / `NOVbcj` 四个部署）：

| 图号 | 工作目录数 | 卡数 | 带 `mogong_patch` | 不同工序数 |
|---|---:|---:|---:|---:|
| `D207NQ.01` | 24 | 593 | 294 | 20 |
| `E207N4QU.02JP` | 3 | 123 | 60 | 17 |
| `C110K1QT2U(H).01` | 4 | 106 | 52 | 18 |
| `3D1000095X3.01` | 1 | 34 | 16 | 13 |
| `3D1000095X3.02` | 1 | 33 | 15 | 11 |
| `C106KS3W1U(H).01` | 1 | 39 | 20 | 18 |
| `C180506K1NW1R3U(H).56` | 3 | 27 | 5 | 4 |
| `6006FBC1-P4YA6.66` | 3 | 37 | 8 | 8 |
| `3C204X1W1.56` | 1 | 30 | 8 | 8 |
| `C206NW1U(H).56` | 1 | 19 | 8 | 8 |
| `C205NW1U(H).56` | 1 | 9 | 2 | 2 |

从 `C110K1QT2U(H).01` 的工作卡恢复出的路线（37 道，含 2 页装前检查 / 2 页探伤）：

```
100 总图 → 105 细车Ⅰ → 110 细车Ⅱ → 110 车外沟 → 120 提交检验 → 299 磨工总图
→ 300 均粗磨平面 → 305 退磁 → 310 粗磨外径 → 315 粗磨外圈内径 → 320 粗磨外沟
→ 325 清洗 → 330 中温补充回火 → 335 打倒角 → 340 粗光饰 → 345 终磨平面
→ 350 细磨外径 → 355 终磨外径 → 360 细磨外沟 → 365 终磨外沟 → 370 清洗
→ 375 酸洗 → 380 除氢 → 385 终磨外圈内径 → 390 去锐角 → 395 精光饰
→ 400 荧光磁粉探伤(2页) → 405 粗研外沟 → 410 精研外沟 → 415 修磨外径
→ 420 修磨平面 → 425 退磁 → 430 清洗 → 435 激光打字 → 440 装前检查(2页)
→ 445 清洗 → 450 提交
```

> ⚠️ 这条路线**来自实际出卡产物**，来源强度高于 Excel 表格，但仍需工程师确认后写入 `approved_route_source`。

### 3.4 上游规则引擎可直接复算参数（已实测）

在 `smartcad38` 下用 20260917 基线代码，对 `D208N4Q.01` 跑完整链：

```
build_design_part_params → enrich_design_part_params_for_turning
→ detect_grind_loop_from_context → infer_precision_grade
→ MogongRulesStore + ProcessRuleEngine.resolve_process
```

结果：**22 道路线中 14 道命中磨工规则**，每道产出 4–11 个带来源的参数：

```
均粗磨平面   → C, VCs, δ, D
粗磨外径     → D, VDsp, VDmp, 2△Cir, SD, C
粗磨外圈内径 → D2, VD2p, VD2mp, Ke2, C
粗磨外沟     → De, VDep, Ke, Se, R, ae
终磨外沟     → De, VDep, Ke, ae, Se, △Cur (+7)
精研外沟     → De, △Cir, Ke, △Cur, ae, Se (+5)
修磨外径     → D, VDsp, VDmp, 2△Cir, SD, Ra
```

对 `3D1000095X3.01`（备选表路线）：**30 道中 16 道命中**。

参数来源分 12 类（统计全部 42 张带 patch 的工作卡）：

| `source_type` | 计数 | 含义 |
|---|---:|---|
| `lookup_direct` | 93 | 规则库直接取值（Ra/Ke/Se/VCs/δ 等） |
| `copy_from_other_process` | 65 | 从其它工序复制（**跨工序状态依赖**） |
| `product_drawing` | 31 | 直接取自产品图 |
| `lookup_allowance_calculate` | 23 | 留量查表计算（De/D/d2/di/B/C 等） |
| `product_drawing_chamfer` | 8 | 倒角双标注 |
| `product_drawing_calculate_r` | 4 | R = 产品值 − 扣减 |
| `product_drawing_calculate_ae` / `_ai` | 2 / 2 | 沟位置 = 产品值 + (细磨−终磨)/2 |
| `product_drawing_calculate_de` / `_di` / `_rough_b` / `_rough_c` | 各 1 | De/di/粗磨 B/粗磨 C 计算 |

**这 12 类来源直接映射本项目 target 的 `update` 标签语义**：

| 上游 `source_type` | 本项目 `update` |
|---|---|
| `product_drawing*` | `design_copy` |
| `copy_from_other_process` | `state_copy` |
| `lookup_direct` / `lookup_allowance_calculate` | `lookup` |
| `product_drawing_calculate_*`（带公式） | `derive` |

`copy_from_other_process` 的 65 次命中**正是本项目 `sources` 指针头的监督来源**（前序工序 PARAM 的 state copy），上游工作卡里的 `evidence` 字段（如 `"evidence": "细磨平面.C"`）直接给出了复制目标。

### 3.5 上游工艺模板库是"工序卡几何真值"

`dataset/config/外圈/D208N4Q.01/` 有 29 个工序卡 JSON（每个 344 实体），内含：

- 工序号（`325`）、工序名（`粗磨外沟`）
- 检验符号（`De` `VDep` `Ke` `ae` `Se`）
- 磨削参数（`修整量 粗 0.05mm / 半精 0.01mm / 精 0.005mm`、`最大进给速度`、`光磨时间 0～5s`、`工件转速 15～26Hz`）
- 砂轮（`P65×13×10`、`3SG60JV60`）、量具（`H903`、`89A60K5V111T3`）
- `dimension_layout`：文字/尺寸线的写回定位（`handle`、`new_position`、`restore_text_position`）

`dataset/config/内圈/D208N4Q.02/` 30 个，`保持架/D109/` 39 个，`滚动体/滚动体-78″NH/` 26 个，`装配/D206NW1(H)/` 21 个。

这些是**带 `handle` 锚点的物理目标对象**，正是本项目 `features.anchor_entities` 与"物理对象层"缺失的那一层。

### 3.6 上游 GNN 检索资产

| 资产 | 状态 |
|---|---|
| `experiments/GNNdate48/models/best_model.pth` | ✅ 16.4 MB，`20260917` 与 NOVbcj **哈希一致** |
| `experiments/GNNdate48/models/cad_encoder.pth` | ✅ 同上 |
| `dataset/graph/` | ❌ **不存在**（两个部署都没有） |
| `experiments/GNNdate48/embs.pt` | ❌ 不存在 |
| `dataset/models/bge-large-zh-v1.5/` | ✅ BGE 中文向量模型（规则 RAG 用） |

GNN 检索库 `dataset/graph/` 缺失 → `infer_similar_process` 阶段不可用。但 `build_graph_library` 可从 `dataset/config/` 现场重建（`test_retrieve_all.py --auto_graph_from_config`），图库只需外圈/内圈/保持架 5 个工艺目录。

GNN 图构建与本项目 `design.json` 的实体图**结构上同源**：都是"实体 + 边"，上游 7 类边（共享端点 / 同组 / KNN / 自环 / 特征-实体链接 / 特征间关系 / 其他）、40 维节点特征；本项目 3 类边（自环 / 邻近 / 共端点）、12 维 `numeric`。**本项目的 12 维是被裁剪过的简化版**，缺 type one-hot、缺特征节点、缺 group 边。

### 3.7 上游 CAPP 历史快照

`dataset/XIN_Reference/capp_output_temp/history_json/` 有 23 份 D207NQ 制造工艺 JSON（`manufacturingProcess` → `subProcesses` → `operations`），每道工序带：

```json
{
  "hz3_opNo": "60", "opNum": "OP-60", "hz3_opType": "磨加工",
  "processParameters": [{"hz3_SpecSym": "1", "hz3_req1": "砂轮转速", "hz3_req2": "1000r/min"}],
  "inspectionRequirements": [{"hz3_Number": "1", "hz3_SpecSym": "CⅠ",
                              "hz3_MeasuringIns": "G904/G905", "hz3_InstrumentPre": "0.01/0.005",
                              "hz3_samplingRatio": "每盘至少5件"}],
  "equipmentResources": [{"hz3_drawNo": "砂轮", "hz3_req": "N450×125×380"}]
}
```

**`hz3_SpecSym` 就是检验符号（`C` `D` `De` `CⅠ` `VD2p` `2△Cir`…）**，与磨工规则的参数符号同构。这是"工序 → 参数符号 → 检验要求 → 器具 → 精度"的完整链路，可直接构造本项目的 `requirements` / `sources` / 数值标签。

注意：这些快照的 `object_desc` 显示是"由 12 张工序卡自动生成"，即**从工作卡反向汇总**，与 3.3 的工作卡同源，不是独立真值。

### 3.8 自动化接口（可用于批量补数）

`/api/automation/run` + `/api/automation/batch`（≤100 项/批），8 个阶段：

```
json_extract → dwg_extract → product_context_confirm → infer_similar_process
→ process_matching → process_drawing_parameter → rule_llm → card_generation
```

契约由 `GET /api/automation/contract` 自动发现。`options.process_selection_excel_path` + `options.process_selection_excel_part_number` 支持**指定备选 Excel 和参考图号**——这正是 `3D1000095X3.01` 那种"精确图号在非活动表"的情况。

> ⚠️ 未验证：`rule_llm` / `card_generation` 阶段依赖 LLM 服务和 AutoCAD COM，本机未验证。`infer_similar_process` 因 `dataset/graph/` 缺失不可用。

---

## 4. 结论：上游能补足什么、不能补足什么

### 4.1 能补足

| 缺口 | 上游提供 | 状态 |
|---|---|---|
| **路线供给**（4 张 `blocked_route`） | Amix 308 行表（D208N4Q.01）+ 工作卡恢复（C110K1QT2U、E207N4QU.02JP） | ✅ 已实测可用 |
| **参数值监督**（`update` / `sources` / 数值） | `ProcessRuleEngine` 复算 + 12 类 `source_type` | ✅ 已实测 |
| **物理对象层**（`features.anchor_entities`） | `dataset/config/` 工艺库 147 个工序卡的 `handle` 锚点 | ✅ 资产在 |
| **检验符号词表** | `MoRules.json` + CAPP 快照 `hz3_SpecSym` | ✅ |
| **跨工序状态依赖**（`sources` 指针） | `copy_from_other_process` 65 处 + `evidence` 目标名 | ✅ |
| **同款/相似件的工序对照** | `D208N4Q.01` 外圈 29 卡 vs `D207NQ.01` 外圈 32 路线 | ✅ |

### 4.2 不能补足

| 缺口 | 原因 |
|---|---|
| **人工 gold 标注** | 上游全部是系统生成/规则推导，无人工确认层。本项目 `human_gold_fields = 0` 的问题**上游解决不了** |
| **`D209NW1U1` / `D210NW1U` / `E207N4QU.01` 路线** | 全网无 Excel 精确路线、无工作卡。仅 DWG 上传记录 |
| **GNN 检索库** | `dataset/graph/` 不存在，需从 `dataset/config/` 重建 |
| **阶段二 LLM 推理真值** | 上游的 LLM 改参本身是"受约束推理"，不能当 ground truth |
| **`features` 指针的强监督** | 上游只给"属性 ↔ 尺寸标注实体"，不给"属性 ↔ 几何成员集合" |

### 4.3 关键风险

1. **NOVbcj 的活动工序表被裁剪**（27 行 / 1 图号）。若继续用 NOVbcj 作为数据生成基线，D207NQ.01 的路线会直接查不到。**必须改用 20260917 的活动表，或显式传 `excel_path`。**
2. **Amix 表里的路线是旧版**（D208N4Q.01 22 道 vs D207NQ.01 32 道，无 `磨工总图` 之外的 `细磨平面` 差异，且缺 `打倒角` 之后的清洗/除氢序列）。作为训练目标前需工程师确认路线版本。
3. **上游数值是"系统推导"不是"人工确认"**。按本项目质量分层规则，这些只能进 `silver` / `system`，**不能进 `gold`**，也不能把 `candidate` 权重从 0 提上来。
4. **`copy_from_other_process` 的目标识别**有历史坑（上游经验：同名精磨工序重复时，425 的 D 必须关联到 415 实例）。本项目 `build_process_chain.py` 的三重校验（来源工序号更早 + 值一致 + 参数状态类型）必须继续生效。

---

## 5. 建议的下一步（按优先级）

> **2026-11-10 更新**：第 1 项（路线资产清单）已用另一种方式解决——
> 不再按图号查表，而是按「类别 + 特征签名」生成默认路线。详见
> [`route-template-analysis.md`](route-template-analysis.md)。原第 1、2 项
> （`route_assets_index.json` / `recover_route_from_cards.py`）降级为可选：
> 它们能恢复**精确**路线，强度高于模板路线，但仍不如工程师确认。

1. ~~**建立路线资产清单**~~ → 已由 `scripts/route_template/extract_routes.py` +
   `route_templates_v1.json` 覆盖（含每张来源表 SHA256）。
2. ~~**写 `recover_route_from_cards.py`**~~ → 降级为可选增强。工作卡恢复的路线
   仍是**精确路线**，可作为 `route_templates` 的补充来源写入 `routes_raw.json`。
3. **升级 `build_pilot_pair.py` 使其接受 `excel_path`**：把"路线来源"从隐式全局改为显式参数 + 哈希记录。
   （`build_outer_dataset.py` 已接入 `RouteResolver`，查表失败自动回退到类别模板并在
   `route_source` 字段记录来源与降级警告。）
4. **把上游 12 类 `source_type` 映射写进 `data-contract.md`** 的 `update` 字段定义，并新增 `derive` 的公式证据要求（上游 `evidence` 字段格式如 `"product=8.5+(C细磨-C终磨)/2=8.5+0.08/2"`）。
5. **建 `dataset/config/` 工艺库的 handle 索引**：147 个工序卡 → 工序名 → 符号 → handle 集合，作为 `features.anchor_entities` 与几何成员分组候选的来源。
6. **重建 GNN 图库**：从 `dataset/config/` 5 个工艺目录用 `build_graph_library` 生成 `dataset/graph/`，恢复 `infer_similar_process`，并对比上游 40 维特征与本项目 12 维的差异，决定是否对齐。

---

## 6. 复核命令

```powershell
# 路线覆盖核对
python -c "import pandas as pd; f=pd.read_excel(r'E:\AAhz\Amix\mix717\CAD_Process_Hublh\dataset\excel\工序选择.xlsx'); print(f[f['部件型号（图号）']=='D208N4Q.01'][['工序号','工序名称']].to_string())"

# 上游规则引擎复算（smartcad38）
& 'D:\conda\envs\smartcad38\python.exe' <script>

# 工作卡路线恢复
Get-ChildItem 'E:\AAhz\20260909\smartCAD\CAD_Process_Hub\temp\modifier\767fefb3' -Filter '*.json'
```

所有 Python 实测均在 `smartcad38`（Python 3.8.20 / PyTorch 1.13.1+cpu）下执行。
