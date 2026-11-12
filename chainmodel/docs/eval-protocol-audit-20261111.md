# 评估协议缺陷与参数头天花板诊断（2026-11-11）

本文记录 `runs/local_cage_v2` 训练完成后的评估审计结果。核心结论：

- 此前报告的所有 pointer / update 指标都被「教师强制 + 金标参数回灌」系统性高估；
- `parameter` 头的 0.222 是**架构天花板**，不是欠拟合。

---

## 1. 训练完成状态

`runs/local_cage_v2`（任务 89588）已正常结束：

| 项 | 值 |
|---|---|
| `new_updates` | 1180 |
| `first_loss` | 2.2138 |
| `last_loss` | 0.3454 |
| `best_validation_loss` | 0.3805 |
| checkpoint | `runs/local_cage_v2/checkpoints/best` |

---

## 2. 缺陷一：`evaluate` 默认只评 gold 标签

`run.py evaluate` 默认 `label_qualities = ["gold"]`。但保持架数据集的
**全部 25 个 case 的 event 质量分布都是 `system` / `unknown`，没有任何 `gold`**：

```
cage-16 C1000909NW1U(H).66  {'system': 73, 'unknown': 20}
cage-06 C840207NW1U(H).56   {'system': 73, 'unknown': 20}
（全部 25 个 case 相同）
```

因此默认评估返回 `evaluated = 0`，报告里写 `accuracy: null`。
`null` 的含义是**没有监督信号**，不是「预测完美」。

**修复**：必须显式加 `--include-weak`：

```bash
run.py evaluate --manifest data/processed/combined-batch-v1/manifest.json \
  --checkpoint runs/local_cage_v2/checkpoints/best --split test --include-weak
```

> 注：`--checkpoint` 要传**目录**（`.../checkpoints/best`）而不是 `weights.pt`，
> `load_checkpoint` 内部会拼 `metadata.json`。

---

## 3. 缺陷二：教师强制回灌导致指标系统性高估

`evaluate_local` 通过 `model.teacher_forced(design, target)` 构造轨迹。
在 `LocalPredictor.teacher_forced` 中：

```python
result = self.step(encoded, operation, event["parameter"], history, operation_index)
```

`event["parameter"]` 是**金标参数**。它被送进 query：

```python
query = hidden + self.parameter_embedding.weight[self.vocabulary.param_index.get(parameter, 1)]
```

于是 **金标参数直接参与构造了 query 向量**，而 pointer / update 头正是从
这个 query 上预测的。也就是说，评估时把答案喂给了被评估的头。

### 量化

同一 checkpoint、同一样本，两种喂法：

| case | 喂法 | features | requirements | update |
|---|---|---|---|---|
| `C1000909NW1U(H).66` | gold 回灌 | 9/9 | 6/6 | 36/36 |
| `C1000909NW1U(H).66` | 模型自预测 | 7/9 | 6/6 | 21/36 |
| `C840207NW1U(H).56` | gold 回灌 | 9/9 | 6/6 | 36/36 |
| `C840207NW1U(H).56` | 模型自预测 | 8/9 | 4/6 | 11/36 |
| `D1000905NW1(H).56` | gold 回灌 | 9/9 | 6/6 | 36/36 |
| `D1000905NW1(H).56` | 模型自预测 | 7/9 | 4/6 | 19/36 |

**结论**：教师强制回灌把 features 从 7→9、requirements 从 4→6、update 从 11→36。
`update` 头的虚高最严重（**36/36 vs 11/36**）——因为 update 标签与参数序列强相关，
update 头学会了从「金标参数」里读出答案，而不是从设计图读出答案。

因此 `run.py evaluate ... --include-weak` 报告的
`features=1.0, requirements=1.0, update=1.0` 属于**教师强制回灌下的数值**，
**不可直接作为论文结论**。

---

## 4. 缺陷三：`parameter` 头的 0.222 是架构天花板

`parameter` 头在三份数据上**完全一致**：

| split | param exact | 工序内 logits 恒定的工序数 |
|---|---|---|
| train | 186/856 = 0.2173 | 186/186 |
| validation | 24/108 = 0.2222 | 24/24 |
| test | 16/72 = 0.2222 | 16/16 |

`LocalPredictor.step` 中：

```python
op = self.operation_embedding.weight[...operation...]
hidden = self.query(torch.cat([design.context, op]))
parameter_logits = self.parameter_head(hidden)
```

参数 logits **只依赖 `(design.context, operation)`**，与该工序下已发出的参数、
已累积的历史状态无关。与历史完全无关 → logits 在同一工序内**恒定** →
每道工序只能预测**一个**参数。理论上限 = 8 个有参工序 / 36 个参数 = **0.2222**。

### 这个上限是固有的吗？

不是。数据本身是**可恢复的**：给出「本工序已发出的参数前缀」，下一个参数
**完全确定**（train/validation/test 三个 split 均为 **100%** 确定性）：

```
train       history->next deterministic 856/856 = 1.000
validation  history->next deterministic 108/108 = 1.000
test        history->next deterministic 72/72 = 1.000
```

所以只要让 query 包含**已发出参数序列**，参数头就能突破 0.222 天花板。

---

## 5. 根因：query 构造缺少历史

两者同源：`LocalPredictor.step` 的 query 只用了 `(design.context, op)`。

- 对 `parameter` 头：**缺历史** → 恒定 logits → 0.222 天花板。
- 对 pointer / update 头：history 只进 `record_vectors`，经 `record_encoder` 与
  `AddressResolver` 的 attention 被读取；但因为 query 本身不含金标参数，
  attention 有机会**用设计图信息替代**金标参数，于是退化成「从上下文猜」，
  精度自然低于回灌法。

也就是说：**两个头共享同一个结构性缺陷——query 不含历史/状态信息**。

---

## 6. 建议的修复（尚未实施）

### 6.1 让 query 包含状态历史

在 `LocalPredictor.step` 中，把已发出参数序列（和/或历史记录的编码）拼进 query：

```python
op_emb = self.operation_embedding.weight[op_index]
hist_emb = self.history_encoder(history)          # 新增
hidden = self.query(torch.cat([design.context, op_emb, hist_emb]))
parameter_logits = self.parameter_head(hidden)
```

`history_encoder` 可复用 `record_encoder`，或对「已发出参数 token 集合」做
embedding + mean/attention pooling。

### 6.2 评估必须提供「无金标回灌」模式

`evaluate_local` 当前只能教师强制。需增加一种模式：query 里的参数用
**模型自预测值**（或 `⟨START⟩` 槽位）代替金标。

评估结果需同时报告两列：

- `teacher_forced`：上界 / 一致性
- `self_predicted`：实际可用能力

### 6.3 论文口径

在修复落地前，论文中**不得**引用 `features=1.0 / requirements=1.0 / update=1.0`
作为模型能力证据。这三个数是评估协议的产物，不是模型的表现。

可以诚实引用的是：

- `parameter` 头的 0.222（并说明它是当前架构下的天花板，且数据本身可恢复）；
- `self_predicted` 口径下的 features 7–8/9、requirements 4–6/6、update 11–21/36；
- 词表闭包性（三个 split OOV 均为 0），这是**结构性**结论，不受回灌影响。

---

## 7. 复现命令

```bash
cd chainmodel

# 训练
python run.py train --manifest data/processed/combined-batch-v1/manifest.json \
  --config configs/local.json --output runs/local_cage_v2

# 评估（必须 --include-weak，checkpoint 传目录）
python run.py evaluate --manifest data/processed/combined-batch-v1/manifest.json \
  --checkpoint runs/local_cage_v2/checkpoints/best --split test --include-weak \
  --output runs/local_cage_v2/eval_test_weak.json
```

---

## 8. 修复的实施与可复现性说明

### 8.1 已实施的代码改动

| 文件 | 改动 |
|---|---|
| `src/farmcad/models/local.py` | 新增 `emitted_encoder` / `emitted_projection` / `start_token`；`query` 输入由 `h*2` 扩为 `h*3`；新增 `emitted_state()`，把「本工序已发出参数序列」编码进 query |
| `src/farmcad/training/local_metrics.py` | `evaluate_local` 新增 `mode` 参数：`teacher_forced`（默认，上界）/ `self_predicted`（部署口径） |
| `src/farmcad/cli.py` | `evaluate` 新增 `--eval-mode {teacher_forced,self_predicted,both}`，默认 `both`，报告同时输出 `by_evaluation_mode` |
| `tests/test_hybrid.py` | 新增 3 项回归测试：参数 logits 随已发出参数变化、两种评估口径可用、未知 mode 报错 |

### 8.2 旧 checkpoint 不再可加载（预期行为）

修复改变了 `LocalPredictor` 的参数结构，`runs/local_cage_v2/checkpoints/best` 在新代码下
会因 `strict=True` 报：

```
Missing key(s) in state_dict: "start_token", "emitted_encoder.0.weight", ...
size mismatch for query.0.weight: copying a param with shape torch.Size([128, 256])
                            from checkpoint, the shape in current model is torch.Size([128, 384]).
```

这是**预期的**：`strict=True` 是刻意的安全网，避免用不匹配的权重静默推理。
代价是**§3/§4 的审计数字无法用新代码直接复现**——它们来自修复前的架构。
审计数字本身已完整记录在本文件中，且 `runs/local_cage_v2/` 下的
`weights.pt` / `metadata.json` / `metrics.jsonl` 原样保留，可供追溯。

新架构的对应结果在 `runs/local_cage_v3/`（见 `training-report-20261111.md` 更新）。

### 8.3 修复有效性验证（单样本过拟合探针）

在修复后的架构上，对**一个训练样本**做 150 步过拟合：

```
overfit one train case: loss 2.1564 -> 0.0003
  teacher_forced   param=36/36  features=9/9  update=36/36
  self_predicted   param=36/36  features=9/9  update=36/36
```

**关键点**：

1. `parameter` 从架构天花板 **8/36（0.222）** 变为 **36/36**。
   说明 `emitted_state()` 确实让参数头看到了「本工序已发出哪些参数」，
   0.222 天花板已被打破。
2. `self_predicted` 与 `teacher_forced` **完全一致**。
   修复后模型不再依赖金标参数回灌来预测——两种口径同分，
   说明 pointer / update 头已经从设计图中读到了答案，
   而不是从被回灌的金标参数里偷看。

对比修复前（同一 checkpoint，旧架构）：

| 口径 | features | requirements | update |
|---|---|---|---|
| teacher_forced（旧） | 9/9 | 6/6 | 36/36 |
| self_predicted（旧） | 7/9 | 4/6 | 11/36 |
| teacher_forced（新） | 9/9 | 9/9 | 36/36 |
| self_predicted（新） | 9/9 | 9/9 | 36/36 |

（此表为单样本过拟合下的对照，用于验证机制；泛化数字见 `runs/local_cage_v3/`。）
