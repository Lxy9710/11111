# 训练与评测报告：combined-batch-v1

日期：2026-11-11。数据批次：`chainmodel/data/processed/combined-batch-v1`（25 个数据对）。
运行目录：`chainmodel/runs/local_cage_v2`。

> ⚠️ `runs\local_cage_v1` 用的是**有缺陷的第一版划分**（验证集对训练词表不闭合），
> 其数值已作废，目录已删除。本文件记录的是修正后的 `v2`。

---

## 1. 划分（本轮新增）

原先 25 个案例**全部**落在 `train`，`validate-data` 在非 smoke 模式下会直接报错
（`Independent validation family required`）。本轮新增 `scripts/assign_splits.py`，
按**族**（family）而非案例划分。

当前族结构（19 族 / 25 案例）：

| 族 | 案例数 | 归属 |
|---|---:|---|
| `outer_ring-series-207` | 1 | train |
| `cage-series-205` | 5 | train |
| `cage-series-204` | 2 | train |
| `cage-series-306` | 2 | train |
| 其余 15 个 cage 族 | 各 1 | 混合 |

### 1.1 划分必须满足**三道**检查

| 检查 | 脚本 | 失败时 |
|---|---|---|
| 族不跨 split | `assign_splits.py` | 抛错 |
| 设计指纹不跨 split | `assign_splits.py` | 抛错 |
| **验证/测试集对 train 词表闭合** | `check_split_closure.py` | **退出码 1** |

第三条是**第一版划分的真实缺陷**。`Vocabulary.build` 只用 train 构建词表，
而第一版把 `cage-series-1`（`6006FBC1/P4YA6.66`）放进了 validation。
该族是唯一能提供 `调质` 工序与 9 个参数的族，于是验证集里出现：

```
NOT CLOSED, unseen operations  : ['调质']
NOT CLOSED, unseen parameters : ['S-S1', 'S1-S2', 'Vbc', 'Ve', '倒铆钉孔角',
                                 '内外倒角', '圆周等分', '孔数+Mc', '销子数量']
```

后果有两层：**模型无法输出** `调质`（不是能力问题，是词表里没这个 token），
且这个**永远降不下去**的常数项会进入 `best_validation_loss`，
使 best checkpoint 的选择被"能否猜对那 9 个参数"主导。

> 这个缺陷在 `validate-data` 里**是可见的**
> （`vocabulary_coverage.validation.unknown_parameters = 9`），
> 但第一版划分时只看了 `status: valid`，没逐 split 读覆盖数字。
> 教训：**发布前必须逐 split 读覆盖数字，不能只看整体状态**。

### 1.2 最终划分

| split | 案例数 | 族 | 口袋形状 |
|---|---:|---|---|
| train | 20 | 15 族 | 11 圆 / 8 椭圆 / 1 外圈 |
| validation | 3 | `cage-series-108`, `cage-series-1000905`, `cage-series-304` | 1 圆 / 2 椭圆 |
| test | 2 | `cage-series-1000909`, `cage-series-840207` | 1 圆 / 1 椭圆 |

`validate-data` 三个 split 的 `unknown_operations` 与 `unknown_parameters` **全部为 0**。

**两个必须记住的约束**：

1. `outer_ring-series-207`（`D207NQ.01`）**必须留在 train**。
   它是全批次唯一有 `sources`（跨工序来源）监督（31 条）、
   且独占全部 29 个磨工类工序与 19 个参数的族。
2. `cage-series-1`（`6006FBC1/P4YA6.66`）**必须留在 train**。
   它独占 `调质` 与 9 个参数。
   代价是：全批次**唯一**可交叉验证的真实保持架路线因此在 train 里，
   验证集无法检验"模板路线 vs 真实路线"这一差异——这一点必须写进论文限制。

---

## 2. 训练运行

```powershell
cd chainmodel
python run.py train --manifest "data\processed\combined-batch-v1\manifest.json" `
  --config configs\local.json --output "runs\local_cage_v2"
```

> 注：`runs\local_cage_v1` 用的是**有缺陷的第一版划分**（验证集不闭合），
> 其数值已作废，目录已删除。此处记录的是修正后的 `v2`。

配置：`architecture=local`，hidden 128，3 层关系传播，4 指针槽，
lr 3e-4，patience 10，seed 17，CPU。

训练损失（每 epoch 20 步）：

| epoch | 步 | 训练 loss |
|---:|---:|---:|
| 0 | 1 | 2.154 |
| 3 | ~60 | ~1.10 |
| 8 | 174 | 0.642（保持架），1.676（`D207NQ.01`） |
| 11 | 221 | 0.564 |

外圈案例的损失显著高于保持架，因为它是全批次唯一带 `sources` 指针监督、
带 104 条参数状态、跨工序跨度最长（10 跳）的案例。

**这本身就是一个结论**：域内（保持架）收敛快，跨域（外圈）收敛慢，
说明当前模型对**族内模板化结构**依赖较强，对**真实历史工艺链结构**泛化不足。
这正是论文需要用跨工序寻址能力来支撑的地方。

---

## 3. 验证集评测

> ⚠️ **2026-11-11 重大更正**。下表原为 `v1`（作废划分）数值。
> 后续审计发现**评估协议本身存在系统性高估**，
> 详见 [评估协议缺陷与参数头天花板诊断](eval-protocol-audit-20261111.md)。
> 本节已按审计结论重写；**原有数字不得作为论文结论**。

命令（`--include-weak` 必需，否则全部为 `null`）：

```powershell
python run.py evaluate --checkpoint "runs\local_cage_v2\checkpoints\best" `
  --manifest "data\processed\combined-batch-v1\manifest.json" `
  --output "runs\local_cage_v2\eval_validation.json" --split validation --include-weak
```

### 3.1 三处必须修正的解读

**（1）`evaluate` 默认只评 gold，而保持架数据没有任何 gold。**

全部 25 个 case 的 event 质量分布都是 `{'system': 73, 'unknown': 20}`。
默认评估因此返回 `evaluated = 0` / `accuracy: null`。
`null` 的含义是**没有监督信号**，不是"预测完美"。
**必须加 `--include-weak`。**

**（2）`parameter` 的 0.222 是架构天花板，不是欠拟合。**

`LocalPredictor.step` 中参数 logits 只依赖 `(design.context, operation)`：

```python
hidden = self.query(torch.cat([design.context, op]))
parameter_logits = self.parameter_head(hidden)
```

与已发出参数、历史状态**完全无关** → 同一工序内 logits **恒定**
（实测 train/validation/test 全部 100% 恒定）→ 每道工序只能预测一个参数
→ 上限 = 8 个有参工序 / 36 个参数 = **0.2222**。

但数据本身**可恢复**：给出"本工序已发出的参数前缀"，下一个参数
在三个 split 上都是 **100% 确定性**。所以这是架构问题，不是数据问题。

**（3）指针与 update 指标被金标回灌系统性高估。**

`teacher_forced` 把**金标参数**注入 query，而被评估的 pointer / update 头
正是从这个 query 预测的。同一 checkpoint 两种喂法：

| case | 喂法 | features | requirements | update |
|---|---|---|---|---|
| `C1000909NW1U(H).66` | gold 回灌 | 9/9 | 6/6 | **36/36** |
| `C1000909NW1U(H).66` | 模型自预测 | 7/9 | 6/6 | **21/36** |
| `C840207NW1U(H).56` | gold 回灌 | 9/9 | 6/6 | **36/36** |
| `C840207NW1U(H).56` | 模型自预测 | 8/9 | 4/6 | **11/36** |

`update` 头虚高最严重（36/36 vs 11/36）——它学会了从金标参数里读答案。

### 3.2 诚实可用的口径

| 结论 | 状态 |
|---|---|
| 词表闭包性（三 split OOV 全 0） | ✅ **结构性结论，不受回灌影响** |
| `parameter` = 0.222 | ✅ 可引用，但**必须说明是架构天花板且数据可恢复** |
| features / requirements / update | ⚠️ 只能用 `self_predicted` 口径（7–8/9、4–6/6、11–21/36） |
| `sources` / `roles` | ❌ 保持架数据**完全没有监督**，`null` 是未标注 |

### 3.3 修复

已在 `local.py` 加入 `emitted_state()`，把「本工序已发出的参数序列」编码进 query
（`query` 输入由 `h*2` 扩为 `h*3`）；`evaluate_local` 新增 `mode`
（`teacher_forced` / `self_predicted`），CLI 新增 `--eval-mode`。

单样本过拟合探针验证：

```
overfit one train case: loss 2.1564 -> 0.0003
  teacher_forced   param=36/36  features=9/9  update=36/36
  self_predicted   param=36/36  features=9/9  update=36/36
```

`parameter` 从 **8/36 → 36/36**，天花板已破；
`self_predicted` 与 `teacher_forced` 同分，说明不再依赖金标回灌。

---

## 3A. `v3` 结果与「目标级划分泄漏」

> 完整分析见 [v3 结果与目标级泄漏诊断](v3-results-and-target-leakage-20261111.md)。

### `v3` 训练

| 项 | `v2` | `v3` |
|---|---:|---:|
| `new_updates` | 1180 | 1820 |
| `last_loss` | 0.3454 | **0.0030** |
| `best_validation_loss` | 0.3805 | **0.00237** |

### `v3` 评估（`--eval-mode both`）

validation 与 test 在两种口径下**全字段 1.0**：

| split | 口径 | parameter | features | requirements | update |
|---|---|---:|---:|---:|---:|
| validation | teacher_forced | 108/108 | 27/27 | 18/18 | 108/108 |
| validation | self_predicted | 108/108 | 27/27 | 18/18 | 108/108 |
| test | teacher_forced | 72/72 | 18/18 | 12/12 | 72/72 |
| test | self_predicted | 72/72 | 18/18 | 12/12 | 72/72 |

### ⚠️ 但这个 1.0 不能写进论文

**粗粒度监督模式**（工序 + 参数 + 绑定，忽略数值）指纹统计：

```
DISTINCT COARSE SUPERVISION PATTERNS: 7
  4d50f2b36ada  n=19  {'train': 14, 'test': 2, 'validation': 3}
  （其余 6 个各 1 例，全在 train）
```

19 个案例（跨 train/val/test）**共享同一监督模式**——因为保持架用
**固定类别路线模板**生成目标，工序序列与参数绑定完全相同，只有数值不同。

**决定性实验**：把 test 的目标配上**完全错误的 train 设计**：

| 配对 | features | requirements |
|---|---:|---:|
| `C1000909NW1U(H).66` + 自己的设计 | 9/9 | 6/6 |
| `C1000909NW1U(H).66` + `3C204X1W1.56` 的设计 | **9/9** | **6/6** |
| `C840207NW1U(H).56` + `3D207NW1Y.56` 的设计 | **9/9** | **6/6** |

**换图后指标一点不掉。** 对照：把设计里的**特征 id 打乱**（几何不变），
features 立刻从 9/9 掉到 2/9、0/9。

→ 模型用的是 `feature_id` 这个**符号标识**，**不关心几何内容**。
它学到的是「按模板顺序把参数绑到按位置排好的特征槽位」，
不是「按几何语义把参数绑到正确的特征」。

### 3A.1 已加的防线

`assign_splits.py` 新增**监督模式指纹检查**（族、设计指纹之外第三道）：

```python
fingerprint = supervision_fingerprint(target)   # 工序+参数+绑定+update，忽略数值
if fingerprint in supervision and supervision[fingerprint] != splits[case["case_id"]]:
    raise ValueError("Identical supervision straddles a split: ...")
```

现在重跑划分会**直接报错**：

```
ValueError: Identical supervision straddles a split: C1000909NW1U(H).66 and the case
in split 'train' share the same operation/parameter/binding pattern. Held-out cases
must differ in what is supervised, not only in numeric values.
```

新增 3 项回归测试（`scripts/test_dataset_batch.py`），全套 **116 项测试通过**。

### 3A.2 结论修正

| 表述 | 成立？ |
|---|---|
| "参数头突破了架构天花板（0.222 → 1.0）" | ✅ **成立**。同案例内区分参数实例，与跨案例泄漏无关 |
| "特征/要求指针准确率 1.0" | ❌ **不成立**。换图仍满分，是模板同质导致的退化 |
| "能从设计图恢复参数的对象归属" | ❌ **不成立**。需要先做路线条件化 |

**这直接说明 R1/R2/R3 路线条件化不是可选项，而是必要前提**：
只有当目标随设计证据变化、且不同几何走不同参数集合时，
模型才可能被"逼"去读设计图。

---

## 4. 结论：现在能训练了吗？

**能，而且已经在训。** 但要把"能训练"和"能出论文结论"分开：

| 层面 | 状态 |
|---|---|
| 管线可跑通 | ✅ 25 案例，词表覆盖 100%，非 smoke 校验通过 |
| 有合法 train/val/test 划分 | ❌ **需重划**：新增监督模式检查后当前划分被判泄漏 |
| 训练收敛 | ✅ loss 2.15 → 0.003（`v3`，1820 步） |
| 评估协议无泄漏 | ✅ 已修复并加回归测试（见 §3.1） |
| `parameter` 头突破架构天花板 | ✅ 已验证，探针 8/36 → 36/36 |
| 划分无监督泄漏 | ❌ 19/25 案例共享同一监督模式（见 §3A） |
| 能支撑论文的**定量结论** | ❌ 零 gold；24/25 是保持架；`sources`/`roles` 无监督；1.0 指标退化 |
| 能支撑论文的**机制性论证** | ⚠️ 可以（消融、跨度分桶、提交约束） |

**论文里的正确表述**：当前实验能证明的是
"在**弱监督**下，架构级约束（工序条件化查询 + 来源屏蔽 + 类型化多槽指针）
能从设计图与模板化路线中稳定恢复**参数的对象归属与来源语义**"。
不能证明的是"端到端工艺规划能力"或"工业泛化"。

**评估协议红线**（务必遵守）：

- 必须带 `--include-weak`，否则全部指标为 `null`；
- 必须区分 `teacher_forced`（上界）与 `self_predicted`（部署口径），
  **只报 `teacher_forced` 数字等于报告一个上界当结论**；
- 不得引用 `sources` / `roles` 的 `null` 作为任何方向的证据；
- **不得引用 `v3` 的 1.0 指标**——换图仍满分，说明是模板同质导致的退化，
  必须先做路线条件化（R1/R2/R3）让监督随设计变化，重划划分后再评测。

**下一位需要解决的问题**（按阻塞顺序）：

1. **R1/R2/R3 路线条件化** — 让目标随设计证据变化、不同几何走不同参数集合。
   这是让 `features` / `requirements` 变成有意义指标的前提。
2. **重划划分** — 路线条件化后，监督模式会出现真实差异，再跑
   `assign_splits.py` 的三道检查（含新增的监督模式检查）。
3. **设计扰动指标** — 把「换图 / 打乱特征 id」做成常驻指标，
   报告指标对设计输入的敏感度。这比单一准确率更能暴露退化。

---

## 5. 顺带修复：试点产物的 provenance 不可搬迁

`scripts/test_build_process_chain.py` 有一个**先前就存在**的失败
（项目从 `E:\AAhz\20260918\intentionCAD` 搬到当前目录后出现）。
本轮定位并修复了根因。

**根因**：`provenance.json` 里嵌入的是**绝对路径**
（`E:\AAhz\20260918\intentionCAD\...`）。项目一搬家，
同样的输入就会生成**字节不同**的 `provenance.json`，
replay 校验随之失败——尽管 7 个内容产物完全一致。

**修复**：provenance 改为记录**项目相对路径**
（`pilot/D207NQ.01/chain_source/...`、`scripts/build_process_chain.py`），
项目外的外部资产保持绝对路径不变。

**改动**：

| 文件 | 改动 |
|---|---|
| `scripts/build_process_chain.py` | 新增 `portable_path()`；provenance 改用相对路径 |
| `chainmodel/src/farmcad/data/prepare.py` | 新增 `resolve_recorded_path()`，按候选根目录解析相对路径 |
| `scripts/refresh_verification.py` | 新增：产物重建后刷新 `verification.json` |
| `pilot/D207NQ.01/process_chain/` | 重新生成，`verification.json` 同步刷新 |

**验证**：

- 7 个内容产物（`association_graph.json` / `feature_chains.*` / `nodes.csv` /
  `edges.csv` / `review_tasks.csv` / `summary.json`）**字节完全一致**，
  只有 `provenance.json` 变化，且变化仅限路径表示。
- 由重新生成的试点重新 `prepare` 出的 `design.json` / `target.json`
  与已发布案例**字节一致**（`0ea9cb37…` / `5c34b74b…`）。
- `scripts` 45 项 + `chainmodel` 65 项测试**全部通过**。

**教训**：provenance 记录的是"读了什么"，不是"在哪个目录读的"。
把绝对路径写进产物，等于把一次目录搬迁变成一次产物变更。

---

## 5. 下一步优先级（按对论文的阻塞程度排序）

1. **补 `roles` 与 `sources` 监督**（最高优先）
   保持架模板路线里其实有明确的角色信息（ machining_target / reference_copy ），
   `build_cage_case.py` 目前没填。这两个字段正是 Challenge 2 的直接证据。
2. **补少量人工 gold**（1–2 个数据对即可）
   把质量权重从 `system` 0.2 提升到 `gold` 1.0，
   指标才具备"人工核验"的说服力。
3. **补物理对象分组**（`physical_object_id`）
   保持架的所有参数都有真实 CAD handle，归组是半自动的。
   这会让"特征"从"参数角色候选"变成"真实物理对象"，直接支撑 Contribution 1。
4. **A/B 设计变更对**（T3 的前提）
   否则 Challenge 3 与"规划时的复用"这一段在 Method 里只能标 future work。
5. **扩充保持架以外类别**（内圈/滚子）
   否则划分始终是"保持架内部分布"，跨域结论无从谈起。
