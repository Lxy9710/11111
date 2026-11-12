# Method 章节框架（IntentCAD）

日期：2026-11-11。用途：为 `intentCAD.docx` 的 Method 一节提供章节骨架、每节要讲什么、
每节的证据来源，以及哪些内容目前**不能写**（避免把未完成的工作写成已验证的贡献）。

配套阅读：
- [混合架构说明](hybrid-architecture.md)——实现层的权威描述
- [数据契约](data-contract.md)——输入/目标隔离与标签质量
- [保持架样本扩充](cage-sample-expansion-20261110.md)——数据集构建
- [训练与评测报告 2026-11-11](training-report-20261111.md)——划分、训练曲线与验证集实测指标
- [划分闭合性与路线证据核查 2026-11-11](split-closure-and-route-evidence-20261111.md)——闭包缺陷与路线证据强度的更正
- [特征—要求—工序状态关联方案](../../docs/2-process-chain-model-plan.md)——任务定义 T1/T2/T3

`paper.txt` 是**已完成的旧工作**，只作为相关工作的定位参照，不是本文内容。

---

## 0. 写作前的三条硬约束

这三条决定 Method 每句话能不能落地，写之前必须先接受：

1. **不能声称恢复真实工艺历史**。单一设计图通常不能唯一确定路线，Method 里要显式写
   "评价允许多个有效方案"，而不是"预测正确路线"。
2. **不能把 system 弱标写成 gold**。当前 25 个数据对**零人工 gold**，
   所有监督来自上游平台规则引擎与历史工艺卡。Method 的实验节必须写明这一点，
   且报告的指标必须带 `--include-weak` 标记。
3. **不能声称已完成 LLM 闭环**。Qwen3.6-27B 权重已下载但**未加载**，
   `reasoning/backend.py` 仍是 placeholder。Method 里 LLM 部分要写成**架构 + 工具协议**，
   实测证据用离线响应闭环（`hybrid-step`）来支撑，不能声称有多轮自主规划结果。

---

## 1. 章节总览

```text
3. Method
   3.1 Task formulation and scope
   3.2 Framework overview
   3.3 Design perception and semantic feature representation
       3.3.1 Drawing entity graph construction
       3.3.2 Geometry-text joint encoding
       3.3.3 Feature-anchored aggregation and geometric membership
   3.4 Operation-conditioned local association prediction
       3.4.1 Operation-conditioned query formulation
       3.4.2 Typed multi-slot pointer addressing
       3.4.3 Role, update and value heads
       3.4.4 Explicit parameter-state records and source masking
   3.5 Explicit process-state memory
       3.5.1 Storage schema and append-only plan branches
       3.5.2 Transactional commit and compatibility checks
       3.5.3 Provenance and revision control
   3.6 Evidence-grounded planning with a tool protocol
       3.6.1 Request construction and context budgeting
       3.6.2 Planner actions and state transitions
       3.6.3 Review gates and engineering-verification boundary
   3.7 Learning objective
       3.7.1 Quality-weighted event supervision
       3.7.2 Pointer, role, update and value objectives
       3.7.3 Absence semantics and masking
   3.8 Dataset construction
       3.8.1 Route recovery and evidence tiers
       3.8.2 Input/target isolation
       3.8.3 Splits and leakage control
   3.9 Experimental protocol
```

**为什么这样切**：3.1–3.7 是方法本体（审稿人主要看这里），3.8 是数据工程
（这是本文相对已有工作的实质差异之一，值得独立成节），3.9 是实验设置。

---

## 2. 3.1 Task formulation and scope

**要讲什么**：把三个任务的形式化定义写清楚，并明确本文的主任务是 T2。

需要的符号（建议固定一套，全文一致）：

| 符号 | 含义 | 对应代码 |
|---|---|---|
| $D$ | 设计图 | `design.json` |
| $O=(o_1,\dots,o_T)$ | 工序序列 | `target.json` 的 `OP` 事件 |
| $K$ | 制造上下文（材料、精度、热处理条件） | `design.json` 的 `context` |
| $F=\{f_i\}$ | 特征对象集合 | `design.json` 的 `features` |
| $R=\{r_j\}$ | 设计要求集合 | `design.json` 的 `requirements` |
| $S=\{(s,o,a)\}$ | 工序状态：对象 × 工序 × 属性 | `memory/store.py` 的 `states` |
| $A$ | 关联图 | `association_graph.json` |

**建议写法**（英文骨架）：

> We formulate intent-aware process-chain modeling as a structured prediction problem over
> triples $(\mathcal{F}, \mathcal{R}, \mathcal{S})$ rather than over a flat sequence of
> machining parameters. A design drawing $D$ and a manufacturing context $K$ determine a set of
> design objects $\mathcal{F}$ carrying requirements $\mathcal{R}$; a process chain
> $O=(o_1,\dots,o_T)$ induces a sequence of states $\mathcal{S}$ in which each state records
> the value of one attribute of one object at one operation. We distinguish three tasks with
> different input conditions and different supervision: **T1 association diagnosis** (given
> $D$, $O$, $K$; predict the associations), **T2 joint route–association generation**
> (given $D$, $K$; predict candidate $O$ and the association graph), and **T3 change
> propagation** (given a drawing pair $A$, $B$ and a prior plan; predict the affected
> operations and parameters).

**必须写明的边界**（放在本节末尾，一句话）：

> Because a single drawing generally does not determine a unique process chain, T2 is
> evaluated as a set-valued prediction: a candidate route is correct if it is a valid
> realization of the design requirements, not if it matches one reference history.

**不能写**：不能说"我们恢复了真实工艺路线"。

---

## 3. 3.2 Framework overview

**要讲什么**：一段话说清信息流，不是模块清单。Intro 第五段已经有一句话版本，这里展开成图。

建议的结构（图 2）：

```text
                    ┌──────────────────────────────────────┐
   Design drawing D │  Design perception (3.3)             │
   ───────────────▶ │  entities → features → requirements   │
                    └───────────────┬──────────────────────┘
                                    │ feature/requirement tensors
                    ┌───────────────▼──────────────────────┐
   Operation query  │  Local association model (3.4)        │
   (o_t, a)         │  typed pointers → roles/update/value  │
   ───────────────▶ └───────────────┬──────────────────────┘
                                    │ addresses (IDs only)
   ┌────────────────────────────────▼─────────────────────────┐
   │  Process-state store (3.5)  — SQLite, exact values,     │
   │  operations / states / bindings / sources / audit         │
   └───────┬───────────────────────────────┬─────────────────┘
           │ read-only evidence packets     │ validated commits
   ┌───────▼──────────────┐      ┌─────────▼──────────────────┐
   │ LLM planner (3.6)    │─────▶│ Tool protocol (list/read/  │
   │ Qwen3.6-27B          │      │ query/predict/commit)      │
   └──────────────────────┘      └────────────────────────────┘
```

**关键区分**（Intro 提过，这里必须再强调一次，否则读者会以为只有一个数据库）：

- **Process-state store**：当前方案的事实、来源与状态。数值是权威的，不可被 LLM 改写。
- **Planning experience memory**（尚未实现，标注为 future work）：跨任务可检索的关联知识。

**建议写法**：

> IntentCAD separates *perception*, *state*, and *planning*. The perception model is a small
> local network that never proposes executable numeric values; the state store is an
> append-only, transactional database that holds exact numerical records and their
> provenance; the planner is a language model that may only act through a constrained tool
> protocol. This separation is deliberate: each component is allowed to be wrong in a
> detectable way, and the boundary between them is what makes the resulting associations
> auditable.

---

## 4. 3.3 Design perception and semantic feature representation

**要讲什么**：从 DWG 提取的实体图，如何变成带要求的特征对象表示。

### 3.3.1 Drawing entity graph construction

- 实体类型白名单：`LINE, ARC, CIRCLE, DIMENSION, TEXT, MTEXT, POLYLINE, LWPOLYLINE, ELLIPSE, INSERT, OTHER`
- 每个实体一个 **12 维有限数值向量**（`numeric`）+ 文本 + 类型
- 三类边：0 自环、1 k-NN（4 近邻）、2 端点共享

**12 维向量的逐维定义**（来自 `adapt_design`，必须原样进正文表格，否则不可复现）：

| 维 | 含义 | 归一化 |
|---:|---|---|
| 0–1 | 实体包围盒中心 (x, y) | 按图纸有效中心范围线性缩放到 [0,1] |
| 2–3 | 包围盒尺寸 (w, h) | 除以对应方向跨度 |
| 4 | 实体长度（LINE/ARC 端点距离） | `scaled()` |
| 5 | 半径（ARC/CIRCLE） | `scaled()`，缺失取 0 |
| 6–7 | 端点方向 $(\sin\theta,\cos\theta)$ | 无方向时取 0 |
| 8 | 标注测量值 | `scaled()` |
| 9 | 是否为有效标注测量 | 0/1 |
| 10 | 是否有有效几何（中心可算） | 0/1 |
| 11 | 是否有起止点 | 0/1 |

后三位是**存在性标志**，不是连续量。维度 8–11 的存在性编码让"没有测量值"
与"测量值为 0"在数值上可区分——这在工艺图里是常见混淆。

**边的定义**：

| 类型 | 含义 |
|---:|---|
| 0 | 自环 |
| 1 | k-NN 空间邻接，每个实体取 4 个最近邻（按归一化中心距离） |
| 2 | 端点共享：两个 LINE/ARC 共享端点坐标 |

**必须写**：

> Entity geometry is normalized per drawing so that the encoder sees a comparable range
> across parts of very different physical size. The three trailing dimensions are
> existence flags rather than measurements: they let the model distinguish "no measured
> value" from "measured value zero", a distinction that is routine in manufacturing
> drawings and would otherwise be lost in a single scalar.

### 3.3.2 Geometry-text joint encoding

对应 `models/encoder.py` 的 `DesignEncoder`：

- 实体表示 = $h_e = W_{num}\,\mathbf{n}_e + E_{type}[\tau_e] + \mathrm{ByteText}(t_e)$
- 3 层关系传播（`RelationLayer`），关系嵌入 3 类
- 文本编码器是**字节级轻量卷积**（`ByteTextEncoder`），无外部 tokenizer 下载

**必须诚实写**：

> The text encoder is a byte-level convolutional baseline rather than a pretrained
> transformer. This keeps the model fully offline and reproducible, and it isolates the
> contribution of the association machinery from backbone choice; we regard replacing it
> with a pretrained encoder as an orthogonal improvement.

### 3.3.3 Feature-anchored aggregation and geometric membership

- 特征表示 = 数值 + 文本 + 单位 + **锚定实体聚合**（`anchor_entities`）
- 通过 attention（`feature_query @ entity_key^T`）把特征与实体软连接
- 产出 `membership_logits`：特征↔实体的成员关系打分（Dice + BCE 监督）

**这一节对应论文的核心主张之一**：特征不是孤立向量，而是与其承载要求绑定的对象。

---

## 5. 3.4 Operation-conditioned local association prediction

这是方法的**技术核心**，对应 `models/local.py` + `models/pointer.py`。

### 3.4.1 Operation-conditioned query formulation

关键设计：**模型不生成整条路线**，而是在"给定工序 + 给定参数"的条件下预测关联。

```text
设计上下文 c + 工序嵌入 e_{o_t} → MLP → 查询向量 q
q + 参数嵌入 e_a → 条件化后的查询
```

**为什么这样设计**（这正是论文 Challenge 2 的答案）：

> Geometric matching cannot resolve cross-operation addressing, because the information
> required for addressing—operation identity, state provenance, and the role of each
> operation—is absent from the design geometry. We therefore make the operation an explicit
> conditioning variable rather than an output to be decoded.

### 3.4.2 Typed multi-slot pointer addressing

对应 `AddressResolver` + `TypedPointer`：

- 三个**类型化**指针头：`features` / `requirements` / `sources`
- 每个头是 $K$ 个 slot（`pointer_slots=4`），每个 slot 有独立 query
- 打分 = $(\mathrm{W}q + s_k) \cdot \mathrm{W}k(\text{candidate}) / \sqrt{d}$
- 特殊列 `NONE` / `UNKNOWN` / `EXTERNAL` 是**可学习参数**，不是 ID

**这一节必须写"为什么多槽"**：

> A single-slot pointer can express only one binding per label. Because one parameter record
> may legitimately bind to several objects and inherit from several prior states, we use a
> multi-slot pointer with a learned slot-query bank. The special columns let the model
> abstain explicitly instead of being forced to pick the nearest candidate.

### 3.4.3 Role, update and value heads

- `role_head`：4 类（`machining_target`, `reference_copy`, `inspection_target`, `datum_use`）
- `update_head`：6 类（`unknown`, `design_copy`, `state_copy`, `external`, `lookup`, `derive`）
- `value_head` / `value_mask_head` / `unit_head`：数值与缺失

**必须写**：`update` 的 6 类是**数据契约的一部分**，
其中 `state_copy` 是跨工序来源的**唯一合法表示**，
`external` 表示"知道来源名称但无法定位到前序实例"。

### 3.4.4 Explicit parameter-state records and source masking

- 历史状态由 `record_vectors` 重新编码，**不依赖 RNN 隐状态**，不读未来记录
- `sources` 指针在当前工序状态上被**硬屏蔽**（`valid = [r["operation_index"] < operation_index]`）
- 记录向量 = 特征池化 ⊕ 要求池化 ⊕ (工序+参数+值+单位+角色+更新) 属性嵌入

**这直接对应论文 Challenge 2 的"因果性"要求**，要写清楚：

> Source pointers are masked to strictly earlier operations. A model that wished to
> explain a parameter by itself, or by a later operation, cannot express it. The
> constraint is enforced at the architecture level rather than by post-hoc filtering.

---

## 6. 3.5 Explicit process-state memory

对应 `memory/store.py`。这一节要强调的**不是数据库技术，而是"数值不被模型改写"这条边界**。

### 3.5.1 Storage schema and append-only plan branches

六张表：`meta` / `operations` / `states` / `bindings` / `sources` / `requests` / `audit`。
一个数据库 = 一张设计图的一条方案分支。状态地址在 `plan_id` 内唯一。

### 3.5.2 Transactional commit and compatibility checks

提交前检查（每条都要在 Method 里列出，这是可验证的工程贡献）：

1. 工序/参数在局部模型词表内
2. 对象与要求存在，且要求属于已绑定对象
3. 来源必须是当前计划中**更早**工序的记录
4. 同工序不得对同一对象的同一属性提交相互覆盖的多条变更
5. `design_copy` 恰好一个对象且无状态来源
6. `state_copy` 恰好一个来源，且对象绑定集合与参数名称相同
7. **禁止传入任意 `value` 字段绕过执行器**

**建议写法**：

> A commit is accepted only if it is internally consistent with the objects, requirements,
> and prior states already in the store. Crucially, the planner cannot supply a value:
> the commit API rejects any payload containing a numeric value, so every recorded value
> originates either from the design input or from an executed rule. This is what allows a
> downstream reviewer to reconstruct the provenance of every number in the plan.

### 3.5.3 Provenance and revision control

`revision` 单调递增，陈旧 revision 被拒绝；所有接受/拒绝写 `audit`；
`close_plan` 返回**尚未工程核验的要求列表**，不宣布要求已满足。

---

## 7. 3.6 Evidence-grounded planning with a tool protocol

**先说清楚状态**：Qwen3.6-27B 权重已下载（15 分片，索引与长度已核对），
但**尚未加载**，`backend.py` 是 placeholder。本节要写成**架构 + 协议**，
把"已验证"限定在工具闭环上。

### 3.6.1 Request construction and context budgeting

- 请求**不包含**完整实体图与完整参数历史
- 包含：设计 ID/hash、plan_id、revision、request_id、分页候选、最近三道已提交工序、
  当前工具结果、工具协议、预算说明
- 上下文预算按**序列化 Unicode 字符数**计量，**不是 token 数**（必须写明，审稿人会问）
- 超限**报错**，不从尺寸/公差/要求中静默删字段

### 3.6.2 Planner actions and state transitions

| 动作 | 用途 |
|---|---|
| `list` | 分页读 features / requirements / operations / parameters |
| `read` | 按类型与 ID 读 feature / requirement / entity / state |
| `read_design_context` | 按偏移读技术要求等文字，每片 ≤1000 字符 |
| `query_states` | 查询某对象的历史记录，保留分页游标 |
| `predict_local` | 指定工序+参数+对象范围，取局部预测 |
| `commit_operation` | 提交一道工序及其参数关联（**不接受 value**） |
| `finish` | 关闭提案，仍需专家审核 |

### 3.6.3 Review gates and engineering-verification boundary

记录三态：`planned_change` / `planned_observation` / `reference`。
检测记录**不覆盖**当前计划属性；引用记录同样不覆盖；历史全部保留。

**写作要点**：这一节是回应"LLM 会不会乱来"的正面回答。
核心句：

> The planner's proposals are never engineering truth. Every committed record is marked
> `engineering_verified: false`, and closing a plan returns the set of requirements that
> remain unverified rather than a claim of satisfaction.

---

## 8. 3.7 Learning objective

对应 `training/losses.py`。

### 3.7.1 Quality-weighted event supervision

五档标签质量与权重（这是本文数据工程的核心机制，要写进 Method）：

| 质量 | 权重 | 含义 |
|---|---:|---|
| `gold` | 1.0 | 工程师审核确认 |
| `silver` | 0.5 | 部分审核 |
| `system` | 0.2 | 确定性规则/引擎输出 |
| `candidate` | 0.0 | 几何匹配/语义抽取/LLM 推断候选 |
| `unknown` | 0.0 | 未覆盖，**掩码** |

`unknown` 权重必须为 0（`validate_weights` 强制），`gold` 必须 > 0。

### 3.7.2 Pointer, role, update and value objectives

12 个损失项及默认权重：

```text
action 1.0 | operation 1.0 | parameter 1.0 | features 1.0 | requirements 1.0
sources 1.0 | roles 0.5 | update 0.5 | value 0.1 | value_mask 0.1 | unit 0.1 | group 1.0
```

指针损失用 `cross_entropy`（slot 级），成员分组用 **BCE + Dice**。

### 3.7.3 Absence semantics and masking

**三态缺席语义**是本文数据契约的关键设计，必须单列：

| 表示 | 含义 | 训练处理 |
|---|---|---|
| `null` | 未标注 | **掩码，不产生负例** |
| `[]` / `["NONE"]` | 已确认缺席 | 负例 |
| `["UNKNOWN"]` | 不确定 | 不强制 |

**为什么重要**（可直接写进 Method）：

> Treating an unannotated absence as a negative would teach the model that a binding does
> not exist when in fact nobody looked. We therefore separate *not annotated*, *confirmed
> absent*, and *uncertain* at the label level, and mask the first from the loss.

---

## 9. 3.8 Dataset construction

这一节是本文**相对已有工作的实质差异**，建议给足篇幅（1.5–2 页）。

### 3.8.1 Route recovery and evidence tiers

**这是本轮最新的工作**，也是最有说服力的部分。三档证据：

| 档位 | 含义 | 当前数量 | 证据强度 |
|---|---|---:|---|
| `excel_exact` | 工序表中存在该图号的精确行 | 2 | **最高**（双源印证时） |
| `template_category` | 类别固定路线模板 | 20 | 中（**仅 1 个样本导出，证据不足**） |
| `card_exact` | 从上游历史工艺卡恢复 | 3 | **最低**（是"实际执行到的工序集合"，非完整路线） |

⚠️ **强度排序与直觉相反**：`card_exact` 证据最弱。
它恢复的是某次任务**实际执行到的工序集合**，可能是残缺的。
实测：`C180506K1NW1R3U(H).56` 只有 10 道（缺 14 道），
`C205NW1U(H).56` 只有 8 道（缺 15 道），
`C206NW1U(H).56` 只有 18 道（尾部缺 `清洗`/`荧光磁粉探伤`/`终检`/`镀银`/`包装`）。
**必须如实写这一点**，否则"逐案记录证据档位让审阅者区分精确与推断"这句话站不住。

**唯一可交叉验证的样本**：`6006FBC1/P4YA6.66` 是全批次唯一有 `excel_exact`
真实路线的保持架，且 `card_exact` 独立恢复出**完全相同的 24 道**。
两条独立证据互相印证。它也是唯一能用来校验类别模板的样本。

**比对结果（实测，必须按这个写）**：

| | 真实路线（excel + card 双重印证） | 类别模板（`D106NKW1(H).56` 导出） |
|---|---|---|
| 工序数 | 24 | 28 |
| 备料段 | **无**（`原材料检验`/`下料`/`锻造`/`锻造检验`） | **有**（4 道） |
| 热处理 | `调质` | `热处理` |
| 机加段 | 23 道 | 23 道 **逐道一致** |
| 其它段 | 一致 | 一致 |

**机加段 23 道逐道一致**，差异全部集中在前段备料与热处理命名。

正确写法：

> The route of a cage component is largely determined by its product family rather than by
> an individual drawing number. For the one cage drawing that carries both an exact route
> row and an independently recovered working card, the two sources agree on all 24 steps.
> Comparing that route with our category-level template, the 23 machining steps and the
> finishing steps agree operation by operation, while the template additionally inserts a
> four-step preparation section and names the heat-treatment step generically. We
> therefore treat the category route as a **machining-ladder template with an optional
> preparation prefix**, and we record the evidence tier of every case so that a reviewer
> can distinguish an exact route from an inferred one.

**一个必须如实写出的限制**：

> Only one cage drawing in the present dataset admits an exact cross-check, so the
> category template rests on a single observation for the cage category. We state this
> explicitly rather than presenting the template as a validated route.

**关于差异来源的说明（可写进正文，作为"证据驱动"的例证）**：

上游三份保持架路线表对同一图号给出两套路线，而**车间实际使用的那一套
恰好是模板没有采用的那一套**：

| 路线版本 | 道数 | 备料段 | 热处理 |
|---|---:|---|---|
| `20260917` 部署的 `D106NW1(H).56` | 24 | 无 | `调质` |
| `20260918` / `NOVbcj` 部署的 `D106NW1(H).56` | 28 | 有 4 道 | `热处理` |
| **`6006FBC1/P4YA6.66`（车间实际，双源印证）** | **24** | **无** | **`调质`** |

并且 `20260917` 版 `D106NW1(H).56` 的路线与 `6006FBC1` **逐道完全相同**（23 道机加 +
其它段，差异为 0）。模板导出时取的是 28 道版本。

**四个必须写的实现细节**（体现严谨性）：

1. **分页合并**：上游工艺卡的 `..._第1页` / `..._第2页` 是同一道工序的两个页面。
   不合并会把 24 道工序算成 25 道。
2. **图号分隔符归一**：标题栏写 `6006FBC1/P4YA6.66`，文件名写 `6006FBC1-P4YA6.66`。
   查表与查卡都必须用规范化键匹配，否则会漏掉样本。
3. **模板源图号与实际图纸不一致**：模板声明的源图号是 `D106NW1(H).56`，
   而实际测试图纸是 `D106NKW1(H).56`（多一个 `K`）。
   全部署扫描确认 **`D106NKW1(H).56` 在任何路线表中都不存在**，
   所以它只能靠模板覆盖。必须写明，否则"从精确表导出模板"的说法不成立。
4. **版本分歧**：同一图号在不同部署有 24 道 / 28 道两套路线，
   路线证据必须标注取自哪一版，不能混用。

### 3.8.2 Input/target isolation

- `design.json`：**只含白名单输入**，不含真实路线、真实参数、不含分组结果
- `target.json`：事件流 `OP → PARAM → END_OP → EOS`
- 二者的隔离由 `validate_design` / `validate_target` 强制

**必须写**：

> Route and parameter ground truth enter the target only. The design input is filtered
> against an explicit whitelist of observable fields, so no downstream model can
> reconstruct the answer from the input.

### 3.8.3 Splits and leakage control

划分有**三道**独立检查，全部是发布前的前置条件，不是事后观察：

| 检查 | 脚本 | 失败时 |
|---|---|---|
| 族不跨 split | `assign_splits.py` | 抛错 |
| 设计指纹不跨 split | `assign_splits.py` | 抛错 |
| **验证/测试集对 train 词表闭合** | `check_split_closure.py` | **退出码 1** |

第三条是最容易被漏掉的一条：`Vocabulary.build` 只用 train 划分构建词表，
所以一个留出族若独占某些工序或参数，模型**根本发不出**那个 token，
而且那个常数项会污染 `best_validation_loss`，使 best checkpoint 的选择失去意义。

**必须写**：

> Splits are formed at the level of drawing families, and each split is required to be
> closed with respect to the training vocabulary. A family that uniquely supplies an
> operation or a parameter cannot be held out, because the model would have no token to
> emit and the resulting constant would enter the validation loss used for model
> selection. In the present dataset this rules out the outer-ring family, which alone
> supplies all grinding operations, and one cage family that alone supplies the
> heat-treatment operation. The released split is 20/3/2 over 19 families and is closed
> on all three checks.

### 3.8.4 路线证据的已知缺口（论文里必须如实交代）

三个已定位、尚未修复的问题（详见
[划分闭合性与路线证据核查](split-closure-and-route-evidence-20261111.md)）：

1. **模板无条件插入备料段**。24 个保持架中有 19 个的图纸明确写"锻件投料"
   （锻件由外部投料），本不该有 `下料`/`锻造`/`锻造检验`。
   **这其实是设计要求与路线之间的真实不一致**，且可由图纸证据自动判定。
   建议在论文里**作为正面案例**呈现，而不是悄悄把模板改对——
   它正是"设计意图 ↔ 工艺实现"要解决的东西。
2. **`热处理` vs `调质` 命名**。技术要求 24/24 都写明需要调质，
   模板却用泛化的 `热处理`。正确做法是由技术要求文本决定工序名。
3. **`card_exact` 路线残缺**（见 §3.8.1）。这是 `card_exact` 档位强度最低的原因。

**写作建议**：这一节可以写成"证据驱动的路线条件化"这一贡献的**问题陈述**，
把第 1、2 条作为待解决的实例，而不是把模板当成已验证的产物。
这样既诚实，又正好落在本文的主题上。

### 3.8.5 条件化路线模板：三条可自动判定的规则

上述三个问题都能用**图纸证据**自动解决，不需要人工标注。
这是本文 §3.8 从"数据整理"升级为"方法"的关键一步：

| 规则 | 判据（图纸证据） | 模板动作 |
|---|---|---|
| R1 备料段条件化 | 技术要求含"锻件投料" | **删除** `原材料检验`/`下料`/`锻造`/`锻造检验` 4 道 |
| R2 热处理命名 | 技术要求含"调质" | `热处理` → **`调质`** |
| R3 兜孔形态 | 兜孔用 `pocket_radius` / `pocket_diameter` | 区分椭圆/正圆变体 |

R1、R2 的证据来源是 `design.json` 的 `context`（技术要求原文），
R3 的证据来源是 `features` 的语义键。**三者都不需要人工**。

**为什么这值得写进论文**：

> A category route template is not a fixed sequence but a sequence conditioned on
> evidence carried by the drawing. In the cage category the preparation section is
> present only when the drawing does not state that forged stock is supplied, and the
> heat-treatment step is named after the tempering operation the drawing actually
> requires. Conditioning the template on such evidence is what turns a static
> category prior into a design-intent-grounded route, and it removes the systematic
> disagreement between the technical requirements and the generated route.

这一段还顺带把模板的**证据缺口**变成了**方法的动机**：
证据只能支撑 23 道机加段通用，备料与热处理必须**逐图判定**。
这正好说明为什么本文需要"设计要求 ↔ 工艺实现"的显式对齐，而不是纯模板套用。

---

## 10. 3.9 Experimental protocol

**要写什么**：三张表对应三个挑战。

| 表 | 回答的问题 | 指标 | 当前状态 |
|---|---|---|---|
| 表 1 | 是否学到语义关联 | 特征/成员分组/特征—要求对齐命中率 | 可测（弱标） |
| 表 2 | 是否理解工艺链内联系 | 跨工序对象关联、来源寻址准确率、状态一致性 | 部分可测（`sources` 仅套圈有监督） |
| 表 3 | 记忆是否帮助后续规划 | 有/无经验记忆的对比、复用正确性 | **未实现**，标 future work |

**必须写的限制段**（审稿人一定会问）：

> All supervision in the present dataset is system-level, produced by deterministic rule
> execution and by recovering historical process cards; no engineer-reviewed gold is
> available at this scale. We therefore report weak-label metrics explicitly and do not
> claim industrial generalization. The current split is 20/3/2 over 19 drawing families
> with 24 of 25 cases belonging to the cage category, so the reported numbers characterise
> in-domain behaviour rather than cross-domain robustness.

### 3.9.1 指标定义（`training/local_metrics.py`）

局部模型在**已知工序/参数查询**下的条件指标：

| 指标 | 定义 | 统计方式 |
|---|---|---|
| `parameter` | 参数名预测正确 | argmax 精确匹配 |
| `features` / `requirements` / `sources` | 指针选中集合与真值集合相等 | 集合相等（`["NONE"]` 视为空集） |
| `roles` | 阈值化后的角色集合相等 | 集合相等 |
| `update` | 更新算子 argmax 正确 | 精确匹配 |

**必须写清评测范围**（`scope` 字段原文）：

> `known_operation_parameter_queries_and_gold_history_not_route_rollout`

也就是说：这些指标**不是**端到端路线准确率。模型在已知工序与参数条件下回答
"这个参数绑定到哪个对象、从哪个状态继承、扮演什么角色"，
而不是"应该走哪条路线"。这个区分必须写在正文，否则会被误读。

### 3.9.2 分层评测（`training/metrics.py`，GRU 对照基线）

旧 GRU 路线解码器保留了**编辑距离对齐**评测，可作为对照：

- 工序序列用 Levenshtein 对齐（`align_operations`）
- 对齐后逐参数统计 `parameter_recall`
- 跨工序来源跨度分桶统计：`1` / `2-4` / `5-9` / `10+` 跳
  （D207NQ.01 实测最长已核验复制跨度为 10）

**这个跨度分桶表是本文最直接的"跨工序寻址"证据**，建议作为表 2 的主体。

### 3.9.3 弱标评测开关

`evaluate` 默认只评 `gold`（当前为 0，指标返回 `null`）。
`--include-weak` 显式启用弱标评测。**必须写**：

> All reported numbers use the explicit weak-label mode; no metric is reported on
> engineer-reviewed labels, because no such labels exist at this scale.

### 3.9.4 当前已测得的基线（可直接用作论文初始表格）

`runs/local_cage_v2`，epoch 10 的 best checkpoint（验证 teacher-forced loss 0.654），
验证集 3 案例，`--include-weak`：

| 指标 | `C108K2NW1U(H).66` | `D1000905NW1(H).56` | `D304NW1U(H).56` | 合计 |
|---|---:|---:|---:|---:|
| `parameter` | 0.222 | 0.222 | 0.222 | 24/108 = .222 |
| `features` | 0.333 | 1.000 | 0.778 | 19/27 = .704 |
| `requirements` | 0.333 | 1.000 | 1.000 | 14/18 = .778 |
| `update` | 0.972 | 0.972 | 0.972 | 105/108 = .972 |
| `sources` / `roles` | — | — | — | — |

**必须写清 `parameter` 低的原因**（否则会被误读为模型缺陷）：

> The parameter-name head is evaluated under a deliberately ambiguous condition. Within one
> operation of the category route template, several distinct parameter instances share the
> same operation label (e.g. diameter, tolerance, roughness and stock allowance under
> "rough-turn outer diameter"). The model receives only the operation and the parameter
> *class*, and cannot separate two instances of the same class without the candidate
> binding. We therefore read this metric as an upper bound on parameter identity
> resolution, and rely on the update and address metrics to assess association quality.

值得单独指出的两点（都可以直接写进正文）：

1. **三个案例的 `parameter` 数值完全相同**（均 8/36）。这说明模型学到的是模板路线下的
   稳定结构，而不是案例特例；同时也暴露出参数身份**不能只由工序决定**，
   需要引入候选绑定或工序内位置。
2. **同一兜孔的不同符号化方式会失效**。`C108K2NW1U(H).66` 用 `pocket_diameter`
   表达兜孔，其余椭圆保持架用 `pocket_radius`，该案例的 `features`/`requirements`
   掉到 0.333，而另两个接近满分。
   > This identifies a concrete generalization bottleneck: the model degrades when the
   > same geometric semantics is expressed through a different annotation symbol. This is
   > precisely the problem that a semantic, rather than geometric, feature representation
   > is meant to address.

**必须写清 `null` 的含义**：

> A null metric indicates absent supervision, not a perfect or a failed prediction. The
> `sources` and `roles` fields are null on all cage cases because the current dataset
> generator does not annotate them; the absence is masked out of the loss and must not be
> reported as accuracy.

### 3.9.5 消融（对应三个挑战）

| 消融 | 去掉什么 | 检验 |
|---|---|---|
| A1 | 去掉 `sources` 指针头 | 跨工序来源继承能力 |
| A2 | 去掉来源屏蔽（允许看当前/未来工序） | 架构级因果约束的贡献 |
| A3 | 去掉 `role` 头 | "引用 vs 加工"混淆率 |
| A4 | 去掉 `group`（BCE+Dice）损失 | 几何成员分组能力 |
| A5 | 去掉质量权重（全部按 1.0） | 数据工程机制的贡献 |

A2 和 A5 是本文**独有**的消融，因为它们对应本文自己的方法主张（架构级因果约束、
质量加权弱标训练），不是通用 NLP 消融。

### 3.9.6 训练动态（可用于 Convergence 分析小节）

`runs/local_cage_v2`（修正后的闭合划分）：

| epoch | 训练 loss | 验证 teacher-forced loss |
|---:|---:|---:|
| 0 | 2.15 | — |
| 3 | ~1.10 | — |
| 8 | ~0.64 | — |
| 10 | ~0.60 | **0.654** |
| 11 | ~0.46 | — |

外圈案例 `D207NQ.01` 的 loss 稳定高于保持架案例（1.68 vs 0.64 @ epoch 8）。
它也是全批次唯一带跨工序来源监督的案例。可写成：

> Convergence is markedly faster on the cage population than on the outer-ring pilot,
> which is the only case carrying cross-operation source supervision. This suggests that the
> current model relies heavily on family-regular structure, whereas the harder signal —
> long-range state inheritance — is learned more slowly. We treat this as a motivation
> for the explicit addressing mechanism rather than as evidence of its sufficiency.

**训练动态本身说明的一个问题**：第一版划分的验证 loss 里混进了一个
**永远降不下去**的常数项（`调质` 与 9 个参数不在训练词表内），
`best_validation_loss` 因此被"能否猜对那 9 个参数"主导。
这说明**划分闭包不只是数据卫生问题，而是模型选择正确性的前提条件**。

---

## 11. 与 Intro 的呼应表（写完 Method 后回查）

| Intro 声明 | Method 落点 |
|---|---|
| Challenge 1：语义特征表征与意图对齐 | §3.3.3 + §3.7.2（group 损失、features/requirements 指针） |
| Challenge 2：跨工序寻址与状态一致性 | §3.4.1（工序条件化）+ §3.4.4（来源屏蔽）+ §3.5.2（提交兼容性检查） |
| Challenge 3：关联知识的保留与复用 | §3.5（状态库，**已实现**）+ 经验记忆（**未实现**，须标 future work） |
| Contribution 1：问题与关联表征 | §3.1 三任务定义 |
| Contribution 2：学习、寻址与记忆协同 | §3.2–§3.6 |
| Contribution 3：数据组织与实验验证 | §3.8 + §3.9 |

---

## 12. 写作顺序建议

1. **先写 §3.1**（任务定义）。它决定后面所有符号，也决定审稿人读懂"你在解决什么"。
2. **再写 §3.2**（框架图）。图先于文字，画出来能立刻暴露叙事漏洞。
3. **然后 §3.3–3.4**（模型）。这是方法主体，技术密度最高，需要最精确的表述。
4. **§3.5–3.6**（记忆与规划）。重点是**边界**，不是实现细节。
5. **§3.7**（目标）。可与 §3.3–3.4 并行写。
6. **§3.8**（数据）。写完记得回头核对 §3.9 的限制段是否与实际数字一致。
7. **§3.9**（实验）。留空位给实验结果，但**限制段现在就写**，不要留到最后补。

---

## 13. 当前不能写进 Method 的内容（黑名单）

这些在实现里不存在，或只有一个占位。写进 Method 会在审稿时变成实质性问题。

| 不能写 | 原因 | 现状 |
|---|---|---|
| "端到端生成整条工艺路线" | 局部模型无路线解码器；GRU 版已保留但非主线 | `models/model.py` 是 GRU 基线对照 |
| "LLM 自主多轮规划" | Qwen 权重未加载，`backend.py` 是 placeholder | 仅离线响应闭环验证过 |
| "跨案例经验记忆与复用" | 规划经验记忆未实现 | 状态库是 per-plan 的，不跨任务 |
| "工业泛化能力" | 零 gold；25 个数据对；19 族 | 不适用 |
| "端到端 T2 路线准确率" | 本地模型只做条件局部预测 | `evaluate` 只报条件局部指标 |
| "完成物理特征对象合并" | `geometry_handles` 仍空；保持架有 handle 但未归组 | 部分素材已就绪 |
| "保持架全部 28 道工序有参数" | 规则只覆盖 8 道 | 其余显式标 `parameters_unavailable` |

---

## 14. 需要补的数据（阻塞 Method 完整表述）

| 缺口 | 阻塞哪一句 | 获取方式 | 优先级 |
|---|---|---|---|
| 12 维实体向量的逐维定义 | §3.3.1 | `adapt_design` 里补一张定义表 | 高 |
| 保持架 20 个类别路线的逐图号确认 | §3.8.1 的"category route"限定语 | 工程师审 `review/cases.csv` 的 `route_source` 档位 | 高 |
| 物理对象分组（`physical_object_id`） | §3.3.3"特征是对象而非参数角色" | 保持架有真 handle，可半自动归组 | 中 |
| A/B 设计变更对 | §3.1 的 T3、Challenge 3 的复用 | 需要真实案例或受控合成 | 中 |
| 少量人工 gold | §3.9 的指标可信度 | 工程师审 1–2 个数据对 | 中 |
