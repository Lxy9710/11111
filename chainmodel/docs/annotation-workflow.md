# 人工标注往返流程

版本：`farmcad-t2-v1`（标注协议）。本文档写给实际动手标注的工程师。

## 1. 一句话说明

你在上游 ProcessCAD 前端跑实验、改参数，标注结果通过**表格**回到数据集。
中间不需要你碰 JSON，不需要你改代码，任何时候都能追溯是谁在什么时候确认了什么。

## 2. 为什么要绕一圈用表格

因为标注发生在数据集之外。你的编辑器（Excel / WPS）和上游前端都不认识本仓库的
内部结构。表格是双方都认的中间格式，同时它把"人改了什么"变成一条可审计的记录，
而不是一次静默的文件覆盖。

## 3. 完整流程

```
                                          上游 ProcessCAD
                                                │
                                    跑实验 → 在前端改参数
                                                │
                                                ▼
                              temp/modifier/<任务号>/*.json
                              output/_task_runtime/<任务号>/task_snapshot.json
                                                │
                                                ▼  ③ 桥接脚本读取
┌───────────────────────────────────────────────────────────┐
│  ① 导出标注表      run.py annotate-export                   │
│     → params.csv / geometry.csv / binding.json / README.md │
│                          ↓                                 │
│  ② 模型辅助        run.py annotate-assist                  │
│     → params.csv 增加 suggest_* / agree_* / 优先级列       │
│                          ↓                                 │
│  ③ 人工核对填写    在 Excel 里改 action 列和要纠正的列      │
│                          ↓                                 │
│  ④ 导入合并        run.py annotate-import                   │
│     → 新案例目录（target.json 里该字段变成 gold）           │
│                          ↓                                 │
│  ⑤ 组装数据集      assemble_annotated_dataset.py            │
│     → manifest.json 里这个案例带 gold                       │
└───────────────────────────────────────────────────────────┘
```

### 步骤 ①：导出标注表

```powershell
cd chainmodel
& D:\conda\envs\smartcad38\python.exe run.py annotate-export `
  --case-dir "data\processed\combined-batch-v1\cases\cage-18" `
  --output "runs\annotate\cage-18"
```

生成四个文件：

| 文件 | 作用 |
|---|---|
| `params.csv` | **主标注表**，一行一个加工参数 |
| `geometry.csv` | 特征与图纸实体的对应关系 |
| `binding.json` | 绑定指纹，把表和它对应的数据版本锁在一起 |
| `README.md` | 填写说明（也会随表一起发给工程师） |

### 步骤 ②：模型辅助（可选但推荐）

```powershell
& D:\conda\envs\smartcad38\python.exe run.py annotate-assist `
  --case-dir "data\processed\combined-batch-v1\cases\cage-18" `
  --checkpoint "runs\local\roles-fix-v1\checkpoints\best" `
  --output "runs\annotate\cage-18"
```

这条命令会把局部模型对**每一个参数**的预测写进 `params.csv`，新增这些列：

| 列 | 含义 |
|---|---|
| `suggest_features` | 模型认为这个参数指向哪个特征 |
| `suggest_feature_slots` | **分槽判定**：每个指针槽各自选了什么、概率多少 |
| `suggest_requirements` / `suggest_requirement_slots` | 同上，对应技术要求 |
| `suggest_sources` | 模型认为来源是哪道工序的哪个参数 |
| `suggest_roles` / `suggest_role_scores` | 模型认为这是什么作用（四个分数） |
| `suggest_update` / `suggest_update_score` | 模型认为用什么算子更新 |
| `agree_features` / `agree_roles` / `agree_update` | **模型和数据集是否一致**（是/否） |
| `review_priority` | `gap` / `high` / `medium` / `low`——这行值不值得看 |
| `model_confidence` | 模型置信度（**未校准**，不是概率） |

**为什么看 `suggest_feature_slots` 而不是只看 `suggest_features`**：

指针有多个槽，每个槽独立选自己的目标。合并成一句 `outer_diameter` 会掩盖结构：
一个槽说 `NONE`、另一个槽说 `outer_diameter`，是**两个不同的决定**。分槽判定把
这句话摊开：

```
槽0=inner_outer_chamfer(0.195);槽1=NONE(0.792);槽2=NONE(0.552);槽3=NONE(0.561)
```

槽 0 找到了目标，槽 1–3 认为这里本来就没有绑定。这说明模型的依据只有一条，
你需要看那条依据是否站得住，而不是把 0.195 当成一个可靠的置信度。

**复核优先级有四档**：

| 优先级 | 含义 | 你要做什么 |
|---|---|---|
| `gap` | 数据集这一行**没有特征绑定**，模型提了一个 | **最高价值**。判一条提案，比仲裁一个冲突容易。确认后这一行从无监督变成 gold |
| `high` | 两边都有但不同，且模型很自信 | 最费时间。标签错了还是模型错了，两个都得查 |
| `medium` | 两边都有但不同，模型自己也不确定 | 模型靠不住，以标签为准，顺手看一眼 |
| `low` | 三方一致 | 最省事，扫一眼即可 |

`gap` 单独分档是有意的。把"数据集没有"和"数据集有但不一样"混在一起，会让你
把填空和纠错当成同一件事，而它们的难度完全不同。

**数值不会被建议。** `lookup` / `derive` 的值需要规则引擎执行，模型给的是从未被执行过的数字，
让人去"确认"一个没人算过的数是不负责任的。所以 `value_*` 列永远由你自己填。

**建议永远不会被自动采用。** 模型的输出只是放在旁边给你看，最终进 gold 的只有你在
可编辑列里写的、且 `action` 标了 `confirm`/`value`/`pointer` 的内容。

这一步可以重复跑，**已有的决定不会被覆盖**。

### 步骤 ③：人工填写

打开 `params.csv`（Excel 会自动正确识别 UTF-8 中文）。表里已经填好了系统自动
生成的结果，你在 `current_*` 列能看到"系统认为是什么"。

**只需要改两处**：
1. `action` 列 —— 声明你核对的范围
2. 需要纠正的列

`action` 列的四个取值：

| 取值 | 含义 | 适用场景 |
|---|---|---|
| `skip` | 我没核对（**默认值**） | 大部分行 |
| `confirm` | 整行都对，全部确认 | 系统结果完全正确 |
| `value` | 只确认数值 | 关联关系不用管，数值对了 |
| `pointer` | 只确认关联关系 | 特征/要求/来源/作用/算子对了，数值不管 |
**空格的含义（关键）**：

- 关联类字段（特征 / 要求 / 来源 / 作用 / 算子）填了 ID 列表 = 确认是这样
- 填 `NONE` = **确认这里确实没有**（例如这道工序不复制任何前序参数）
- 留空 = 我对这一项**没有表态**，不影响训练

数值类字段（名义值 / 偏差 / 单位）在 `action` 含 `value` 时是整体确认的，
留空表示"确实没有这个分量"。

**数值怎么填**：`value_lower_dev` / `value_upper_dev` 填**相对偏差**，不是绝对值。
图纸写 `Φ52.6 0.15/0` → `value_nominal=52.6`、`value_lower_dev=0.15`、
`value_upper_dev=0`、`value_unit=mm`。

**几何成员表**（`geometry.csv`）：`members` 要填**完整的**成员集合，不是"新增的
那几个"。留空表示不表态。

### 核对 `roles` 列时看什么

`roles` 决定这道工序和这个参数**是什么关系**，是四个取值之一：

| 取值 | 含义 | 判断依据 |
|---|---|---|
| `machining_target` | 这道工序切削/成形了这个特征 | 工序是切削类（磨、车、钻、倒角） |
| `reference_copy` | 工序没改这个尺寸，只是沿用/记录 | 工序是辅助类（包装、镀银、光饰、打标记） |
| `inspection_target` | 工序检验/测量这个尺寸但没改 | 工序是检验类（终检、探伤） |
| `datum_use` | 这个特征被用作定位基准 | 工序是切削类但参数是**沿用前序**的 |

最容易标错的是 `datum_use` 和 `reference_copy` 的区分。判据只有一条：
**这个参数是本工序"加工出来的"，还是"从前面拿过来用的"**。

- 从前面拿过来 + 这道工序在切削 → `datum_use`（切削是拿它当基准定位的）
- 从前面拿过来 + 这道工序不切削 → `reference_copy`（只是记录/测量）

举例：`均粗磨平面` 用前面 `粗磨外径` 得到的 `D` 来定位平面 → `D` 是 `datum_use`；
`终检` 里的 `Dc` 只是量一下 → `inspection_target`。

**当前状态**：系统自动判定已经把 16 条检验/辅助工序的错误标签（原先标成
`machining_target`）全部修正，四个角色现在都有真实样本。你标的时候主要是
复核这部分，以及把 `system` 提升到 `gold`。

### 步骤 ④：桥接上游前端的修改

如果你是在 ProcessCAD 前端里改的参数（而不是在表里改），改完执行：

```powershell
& D:\conda\envs\smartcad38\python.exe scripts\bridge_upstream_edits.py `
  --case-dir "chainmodel\data\processed\combined-batch-v1\cases\cage-18" `
  --modifier-dir "NOVbcj\smartCAD\CAD_Process_Hub\temp\modifier\<任务号>" `
  --task-dir "NOVbcj\smartCAD\CAD_Process_Hub\output\_task_runtime\<任务号>" `
  --output "chainmodel\runs\annotate\cage-18\upstream.csv" `
  --merge-into "chainmodel\runs\annotate\cage-18\params.csv"
```

这个脚本读上游落盘的两处记录：

- `task_snapshot.json` → `result.manual_table_edits.applied_edits`：你在前端
  直接改的表格单元格
- `stages/rule_llm/attempts/*/output.json` → `plan_details[].changes[]`：引擎
  对每个字段的提案（含 `rule_evidence`、`confidence`）

**对齐方式是精确的**，不是模糊匹配。两边都用「图号 + 工序号 + 参数符号」定位：

```
上游工艺卡文件名   C180506K1NW1R3U(H).56_115_粗磨外径.json
本项目事件 ID      C180506K1NW1R3U(H).56:state:115:Dc
```

`--merge-into` 会把前端的改动写进 `params.csv`：

- 只写**你真的动过**的行。没动过的行保持 `skip` —— 工程师没看过的字段不能变 gold
- 如果你已经在表里做了决定，**表里的决定优先**，不会被前端记录覆盖

### 步骤 ⑤：导入合并

```powershell
& D:\conda\envs\smartcad38\python.exe run.py annotate-import `
  --case-dir "data\processed\combined-batch-v1\cases\cage-18" `
  --worksheets "runs\annotate\cage-18" `
  --output "runs\annotate\cage-18-gold" `
  --reviewer "张工"
```

产出：

| 文件 | 内容 |
|---|---|
| `design.json` | 与原案例一致（输入不变） |
| `target.json` | 被确认的字段 `quality` 变成 `gold` |
| `review_log.json` | **逐条记录**哪个字段被提升为 gold、原值是什么、谁确认的 |
| `annotations.template.json` | 下一轮标注的模板 |

**原案例目录永远不会被修改。** 导入写到一个新目录，这是有意的：标注是增量，
不覆盖历史。

### 步骤 ⑥：组装数据集

```powershell
& D:\conda\envs\smartcad38\python.exe scripts\assemble_annotated_dataset.py `
  --base-dataset "chainmodel\data\processed\combined-batch-v1" `
  --annotated-case "chainmodel\runs\annotate\cage-18-gold" `
  --splits "chainmodel\data\processed\combined-batch-v1\review\cases.csv" `
  --output "chainmodel\data\processed\annotated-batch-v1"
```

组装脚本会：

- 用标注版本**替换**该案例的基础版本
- 按 `family_id` 隔离划分 train / validation / test
- **如果同一个族出现在两个划分里，直接报错**（防止物理零件泄漏）
- **如果没有 validation 划分，直接报错**（没有独立验证集的评估数字没有意义）
- 在 `review/cases.csv` 和 `dataset_summary.json` 里写明每个案例的审核状态和
  gold 比例

### 批量生成标注表

一次给多个案例准备标注表，用批量脚本：

```powershell
& D:\conda\envs\smartcad38\python.exe scripts\prepare_annotation_batch.py `
  --dataset "chainmodel\data\processed\combined-batch-v1" `
  --checkpoint "chainmodel\runs\local\roles-fix-v1\checkpoints\best" `
  --output "chainmodel\runs\annotate\batch-roles-fix-v1"
```

它会给每个案例建一个子目录，里面是 `params.csv` / `geometry.csv` /
`binding.json` / `README.md`，并且**已经带上了模型的建议**。

已经带 gold 标签的案例默认跳过（`--include-reviewed` 可以强制包含）。

`--case` 可以指定只做某几个案例，`--limit` 可以限制数量。

## 4. 安全机制

导入环节有四道闸门，任何一道不过都会**报错并停止**，不会静默产生错数据：

| 闸门 | 检查什么 | 为什么 |
|---|---|---|
| 绑定指纹 | `binding.json` 里记录的 `design.json` / `target.json` 的 SHA256 必须与当前文件一致 | 防止把旧表贴到新数据上 |
| 事件地址 | 表里的 `event_id` 必须已存在于 `target.json` | 防止凭空造记录 |
| 指针目标 | 特征 / 要求 / 来源 ID 必须真实存在 | 防止指到不存在的对象 |
| 因果顺序 | `sources` 只能指向**更早**的工序 | 防止把未来的结果当来源 |

另外：

- `reviewer` 不能为空，且必须与 `binding.json` 里的一致
- 特殊指针（`NONE` / `UNKNOWN` / `EXTERNAL`）不能和具体对象混写
- 角色和算子必须在协议允许的取值内
- 导入了多少字段、哪些字段，全部写在 `review_log.json` 里

## 5. 三态缺失语义（务必理解）

这是本数据集的核心约定，标注时直接对应：

| 表里怎么填 | 含义 | 训练效果 |
|---|---|---|
| 留空 | 我没表态 | 不监督，标签保持原级别 |
| `NONE` | 确认**确实没有** | 监督，权重按 quality 计算 |
| `UNKNOWN` | 确认**不确定** | 监督，权重按 quality 计算 |

**"没有"和"不知道"是两件事。** 留空是"我不知道"，`NONE` 是"我确认没有"。
不要用留空表达"确认没有"，那会让模型学不到"确实不存在"这个信息。

## 6. 质量分档

导入后，被确认的字段 `quality` 从原级别提升到 `gold`：

| 档位 | 训练权重 | 含义 |
|---|---|---|
| `gold` | 1.0 | 工程师核对过 |
| `silver` | 0.5 | 资深复核但未逐条确认 |
| `system` | 0.2 | 机器自动推的，没人验证 |
| `candidate` | 0.0 | 机器的猜测候选，不参与打分 |
| `unknown` | 0.0 | 未标注，不参与打分 |

**效果实测**（`cage-18`，确认 8 行后重算损失）：

| | features 监督数 | roles 监督数 | group 监督数 |
|---|---:|---:|---:|
| 标注前 | 3 | **0** | 0 |
| 标注后 | 8 | **8** | 1 |

`roles` 从 0 变 8 是关键：原来 `machining_target` 是 `candidate` 档（权重 0），
所以 `roles` 这个头根本没在学。标完之后它才真正进入训练。

## 7. 当前状态与建议

- 现有 25 个案例的标注**全部是 `system` 级**，一个 gold 都没有
- `roles` 字段经过语义修正后，监督量从 **0 → 1016 条**，四个角色都有真实样本
- 建议标注 **3–5 个案例**，每个案例挑 20–30 行核对即可
- 优先标 `roles` 字段（现在是 `system` 档，权重 0.2；确认后升到 1.0）
- 优先标 `sources` 字段（目前只有 31 条，覆盖率 3%）

各字段当前的监督量（`scripts/check_supervision.py` 输出）：

| 字段 | 监督条数 | 质量构成 |
|---|---:|---|
| `roles` | 1016 | system 1016 / unknown 14 / candidate 6 |
| `update` | 1036 | system 1036 |
| `parameter` | 1036 | — |
| `operation` | 652 | — |
| `value` | 416 | system 416 / unknown 620 |
| `features` | 203 | system 203 / unknown 687 / candidate 146 |
| `requirements` | 174 | system 174 / unknown 862 |
| `sources` | **31** | system 31 / unknown 1005 |


## 8. 当前训练基线

`combined-batch-v1`（25 案例，20/3/2 家族隔离划分）在 `configs/t2.json` 下的
100 epoch 正式训练结果（`runs/train/roles-fix-v1`）：

| 指标 | 值 |
|---|---|
| 首 epoch loss | 3.229 |
| 末 epoch loss | 0.0039 |
| 最优验证 loss | 0.0053（第 98 epoch，未触发早停） |

**注意**：验证 loss 持续下降且 `bad_epochs` 始终为 0，说明 20 个训练案例对
128 维 GRU 已经过拟合到接近记忆。teacher-forced loss 不能作为泛化指标，
真实能力要看 rollout。

rollout 指标（3 个验证案例）：

| 指标 | 值 |
|---|---|
| 路线归一化编辑距离 | 0.94 – 0.96 |
| 预测工序数 / 参考工序数 | 52–55 / 28 |
| `roles` 准确率 | 19% – 36% |
| 终止原因 | `max_events` + `max_parameters` |

**诊断**：模型在 rollout 时生成 28 道工序里的 54–55 道，且撞上参数上限。
这不是监督不足，而是**解码没有"工序结束"的停止信号**——被每道工序塞进
30+ 个参数撑爆了预算。这是下一步要修的结构性问题，与标注工作正交。

## 9. 局部模型基线（`runs/local/roles-fix-v1`）

用 `configs/local.json` 在同一个 `combined-batch-v1` 上重训的**局部模型**，
2000 步：

| 指标 | 值 |
|---|---|
| 首 epoch loss | 2.239 |
| 末 epoch loss | 0.00083 |
| 最优验证 loss | 0.00210 |

### 9.1 角色头：修正前后对比

用 `scripts/probe_head_collapse.py` 测的（train 划分，20 案例，842 个参数）：

| | 修正前 `local_cage_v3` | 修正后 `roles-fix-v1` |
|---|---|---|
| `machining_target` 召回 | 0.365 | **1.000** |
| `reference_copy` 召回 | 0.512 | **0.977** |
| `inspection_target` 召回 | 0.500 | **1.000** |
| `datum_use` 召回 | 0.000 | **0.944** |
| 预测分布 / 标签分布 熵比 | 3.83（分布乱） | **1.05**（分布对齐） |
| 阈值多标签完全匹配率 | — | **1.000** |

修正前 `datum_use` 召回是 0，且预测分布和标签分布的熵差 3.8 倍——模型在乱猜。
修正后熵比 1.05，说明模型输出的角色分布和真实标签分布几乎一致。

### 9.2 为什么需要探针工具

**loss 下降不等于头学到了东西。** 一个头可能训练得很"成功"——loss 降到很低、
teacher-forced 准确率很高——但学到的只是"永远输出同一个答案"。

`local_cage_v3` 就是这种情况：所有 `machining_target` 标签都在 `candidate` 档
（训练权重 0），模型从头到尾没见过这个类的有效监督。loss 照常下降，但
`roles` 头是废的。

`probe_head_collapse.py` 检查三件事：

1. **训练覆盖**——每个类别实际收到了多少梯度（按 loss 的 quality 权重算）
2. **每类召回 / 误报率**——区分"没信号"和"有信号但不区分"
3. **分布熵比**——预测分布和标签分布差多远

```powershell
& D:\conda\envs\smartcad38\python.exe scripts\probe_head_collapse.py `
  --checkpoint "chainmodel\runs\local\roles-fix-v1\checkpoints\best" `
  --split train
```

### 9.3 角色头是**多标签**，不是分类

每个角色有独立的 sigmoid 分数，部署时按阈值（`role_threshold`）取所有过线的角色。
**不能当成 argmax 读**——独立 sigmoid 不让角色互相竞争，分数最高的角色不等于
"那个角色"。探针工具按阈值读，和部署时的规则一致。

### 9.4 划分的一个已知弱点

validation / test 划分的角色标签**全部是 `machining_target`**（108 / 72 条），
没有 `reference_copy` / `inspection_target` / `datum_use`。所以在这两个划分上
"完全匹配率 1.0"只说明模型对单一角色没问题，**不能证明它会区分四种角色**。

区分能力只在 train 划分上测到了（那里四类齐全）。要真正评估角色泛化，需要
一个含四种角色的独立验证集——现在还没有。

### 9.5 特征指针的现状

| | 值 |
|---|---|
| 数据集里不同的特征绑定 | 11 种 |
| 模型给出的不同绑定 | 19 种 |
| 熵比 | 1.00 |

模型在**填数据集的空白**（316 行里 187 行是标签为空、模型给���提案）。这正是
局部模型该做的事：规则填不出来的绑定，模型给候选，人来判。

## 10. 文件清单


| 文件 | 作用 |
|---|---|
| `chainmodel/src/farmcad/data/annotation.py` | 导出 / 导入的核心实现 |
| `chainmodel/src/farmcad/data/assist.py` | 把局部模型的建议写进标注表 |
| `chainmodel/src/farmcad/data/prepare.py` | `operation_role` / `parameter_role`：角色的判定规则 |
| `chainmodel/src/farmcad/cli.py` | `annotate-export` / `annotate-assist` / `annotate-import` 命令 |
| `scripts/bridge_upstream_edits.py` | 上游前端改动 → 标注表 |
| `scripts/prepare_annotation_batch.py` | 批量生成标注表（一步到位） |
| `scripts/assemble_annotated_dataset.py` | 基础数据 + 标注 → 可训练数据集 |
| `scripts/retag_roles.py` | 角色规则变更后，重刷已有数据集的角色标签 |
| `scripts/check_supervision.py` | 统计每个头真正收到多少监督 |
| `scripts/probe_head_collapse.py` | 检查某个头是不是塌成了一个常数答案 |
| `chainmodel/tests/test_annotation.py` | 往返协议测试 |
| `chainmodel/tests/test_upstream_bridge.py` | 桥接测试 |
| `chainmodel/tests/test_operation_semantics.py` | 角色判定规则测试 |
| `chainmodel/tests/test_assist.py` | 标注辅助表测试 |

## 9. 相关文件
