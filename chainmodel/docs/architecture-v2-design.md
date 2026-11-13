# 真正的模型设计：拓扑感知几何级联模型

日期：2026-11-13
状态：**设计稿**。本文定稿前不训练、不跑新实验。

> **本文已被部分取代。** 权威版本是 `architecture-v3-topology.md`（同一天晚些时候）。
> 本文仍然有效的是 **§3 模块 A 编码器**（图注意力、bge 文本编码器、特征 cross-attention）
> 与 **§4 模块 B 级联 Transformer** 的具体结构。
> 本文**已被取代**的是：
> - §1.1 的语料口径 —— "25 个案例 / 12,960 实体 / 81,776 条人造边"的说法，
>   以及"1,122 个视图块 = 1,122 个视图"的推断。实测：块不是视图，
>   `FCFEntity` 块只占全语料几何的 8%，视图须按图面空间聚类恢复。
> - §1.1 结论 3 与 §4.5 的"路线作为条件"—— 这一条 v3 文档独立得出，结论相同，保留。
> - §5.2 中"`delta` 来自 L3 语料实测"的措辞 —— 已由 `geo_state` 的特征级增量取代，
>   数据更细（141 个特征槽、75 步）。
> - §6.2 的正负样本构造 —— "没有设计图的当干扰数据"这条是对的，
>   但"图内视图对"的来源要改成按图面聚类恢复的画。

前置说明：本文取代 `method-section-framework.md` 中 3.3–3.5 的架构描述，
并取代 `annotation-workflow.md` §8 把 GRU run 称作"当前训练基线"的写法。
GRU 自此只作为对照基线存在，不是任何主张的实现载体。

---

## 0. 本文要解决的问题

### 0.1 现有代码的三处"名不副实"

| 名 | 实 | 问题 |
|---|---|---|
| `Topology-Aware` | `prepare.py` 按中心点距离取最近 4 个邻居造边，且绝对坐标被归一化后丢弃（`minima`/`spans` 不入库） | 拓扑是**几何近似造出来的**，不是从图纸拓扑来的；坐标不可逆 ⇒ 无法做几何判定 |
| `DesignEncoder` 输出 `context` | `context_token + h.mean(0) + f.mean(0) + text(context)` | **整张设计图压成一个 128 维向量**。这个向量是所有下游查询的唯一设计入口，信息瓶颈在建模最前面 |
| `ByteTextEncoder` | `Embedding(257,h)` + `Conv1d(h,h,3)` + `tanh` + masked mean | 中文工艺文本被平均池化抹平；"粗磨外径/终检/调质"语义不可分 |

### 0.2 论文标题的两半各自缺什么

标题：*Topology-Aware Geometric Localization and **Cascaded Modification***

- **Localization**（定位）：局部模型已能回答"这个参数指向哪个特征"，但它的设计输入是一个均值向量，没有真正利用几何与拓扑。
- **Cascaded Modification**（级联修改）：**完全落在网络之外**。`target.json` 记录了 `derive/state_copy/design_copy`，但没有任何组件把它重放成几何状态，也没有组件判定修改是否合法。

### 0.3 本文的交付

一个可实现的模型规格 + 可证伪的评测协议。**不包含训练，不包含跑数。**

---

## 1. 约束：实测事实（不是估计）

所有数字来自本仓库 2026-11-13 的实测脚本输出，不引用估计值。

| 项 | 实测值 | 来源 |
|---|---|---|
| 原始图纸 | 19 设计 + 75 工序 = **94 张** | `设计图/geo/` |
| 实体总数 | **80,059**（均 851/张）；其中 33,506 是**无几何的代理实体**（`HIDE_*`），实际可用 **46,553** | 全量统计 |
| 视图块 | **1,122** 个块，但 `FCFEntity<N>` 只有 **98** 个，装 **3,661** 实体（占几何 **8%**） | 全量统计 |
| ⚠️ 视图 ≠ 块 | 块内实体与顶层实体几何重合率 **0.0%**；块不是顶层的拷贝。视图须按图面空间聚类恢复（2798Z → 10 张画，9 张有实质内容） | 第二轮实测 |
| 尺寸标注 | 1,078 个 DIMENSION；含中文的 1,039 个文本实体 | 全量 |
| 实体类型 | LINE 29105 / HIDE_Line 22655 / HIDE_Arc 7098 / ARC 6161 / SPLINE 4070 / MTEXT 3478 / DIMENSION 1078 / INSERTBLOCK 806 / LEADER 381 | 全量 |
| 保真几何字段 | `start,end,center,radius,start_angle,end_angle,position,measurement,text,layer,group,normal` | 单文件检视 |
| 视图块命名 | `FCFEntity<N>`（几何视图）、`ASSORTED_DRAFT<N>`、`LABLE3D_<N>` | 同上 |
| 图层 | 6 个：`1`(6930) `0`(338) `dim`(225) `120`(200) `Defpoints`(136) `118`(63) | 块内统计；顶层另有 `group`（`A2_PATTERN`/`ANNOTATION`/`default`） |
| 处理后案例 | 25 案例，12,960 实体，317 特征，199 要求，**81,776 边**（3,271/案例） | `combined-batch-v1` |
| 有工序链的零件 | **6** 个（2798Z-01-03 / 3309-02-02 / 3309-07-02 / 3309-09-01 / 4362.52-01-03 / C3359-08-01） | FAXIN 目录 |
| 无工序链的设计图 | **13** 张 | 同上 |
| 路线模板 | `stage_order=[M,R,F,S,R2,P,X]`；`feature_keys={GROOVE,OD,ID,PLANE}`；4 个类别档案 | `route_templates_v1.json` |
| 现有模型规模 | `LocalPredictor` 500,173 参数；`FARMModel`(GRU) 672,780 参数 | `dataset_report.json` |
| GPU | RTX 4060，8 GB 显存 | `SERVER_HANDOFF.md` §7.3 |
| 可用训练环境 | `xueyao`: torch 2.4.1+cu124, CUDA True, transformers 4.55.3, sklearn 1.7.1 | 实测 |
| 当前训练环境 | `smartcad38`: torch 1.13.1+**cpu**, transformers 4.30.2 | 实测 |
| 本地中文编码器 | `E:\AAhz\bge-large-zh-v1.5`（BERT-large-zh，1.3 GB，`pytorch_model.bin` 完整） | 目录实测 |
| 本地 LLM | Qwen3.6-27B，15 shard，权重 51.75 GiB —— **装不进本机 8 GB** | `SERVER_HANDOFF.md` |

### 1.1 从事实推出的三条硬结论

**结论 1：预训练语料不是 25 个案例，是 80,059 个实体 + 1,122 个视图。**
深度模型的容量必须匹配这个语料量，而不是匹配 25 个带标签案例。
25 个案例只适合**有监督微调**（头），不适合训练主干。

**结论 2：图内存在免费的、量级足够大的正样本对。**
一张图纸有多个视图块（`FCFEntity<N>`），它们是**同一零件的不同投影**。
因此"同一张图的不同视图之间"就有天然的正样本对，不需要跨图对应。
1,122 个块 ⇒ 数百个视图对，且**零标注成本**。
跨图纸的实体级正样本则由已有的 `correspondence.py` 提供（残余位移中位 0.000–0.067 mm vs 1.9–98 mm，二分干净）。

> ⚠️ **第二轮修正**：上面的"视图"其实是**块**。实测 `FCFEntity` 块只装了全语料
> 几何的 8%，且与顶层实体零重合。视图必须按**图面空间聚类**恢复。
> 这一步已实现（`faithful.split_by_picture`），2798Z-01-03 得到 10 张画
> （9 张有实质内容，装下 1,597/1,600 个有形状的实体）。
> 正样本对的来源随之改为"同一张图的不同画"。

**结论 3：路线不是必须预测的目标，它是有结构的条件。**
`route_templates_v1.json` 给出 `stage_order` 与 `feature_keys`。且本地 LLM（Qwen 27B）**本机装不下**，指望 LLM 在本机跑路线生成不现实。
因此路线应当**作为条件输入**（给定路线 O，预测每步的绑定与几何状态），
这与 `docs/2-process-chain-model-plan.md` 已定义的 **T1（关联生成，辅助诊断）** 任务一致。
自由生成整条路线（旧的 T2）**降级为对照实验**，用 GRU 顶替即可，不值得为它做深度模型。

---

## 2. 模型总览

### 2.1 命名与边界

模型名：**TAM-CAD**（Topology-Aware Modification with Cascaded Design state）。

```
                        ┌──────────────────────────────────────────┐
   设计图 D (保真几何)   │  A. Geometry Encoder  (图 Transformer)   │
   ────────────────────►│     实体 token + 关系偏置 + 几何位置编码  │
                        └───────────────┬──────────────────────────┘
                                        │  design tokens (N_e × d)
                        ┌───────────────▼──────────────────────────┐
   路线 O (条件)        │  B. Cascade Transformer (工序级因果)      │
   ────────────────────►│     工序 token ← 因果自注意力 ← 几何状态   │
                        │     每个活跃特征一个 state token           │
                        └───────────────┬──────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
      ┌───────▼────────┐      ┌────────▼────────┐      ┌─────────▼────────┐
      │ C1 绑定头       │      │ C2 几何操作头   │      │ C3 合法性内核     │
      │ 多槽类型化指针   │      │ 操作类型+改量   │      │ 确定性重放+检查   │
      │ roles/update   │      │ 特征组归属      │      │ (不可学习)        │
      └────────────────┘      └─────────────────┘      └──────────────────┘
```

### 2.2 与论文四个主张的对应

| 论文主张 | 架构实现 | 可证伪评测 |
|---|---|---|
| Topology-Aware Localization | A 的**关系偏置注意力**（不是 message passing）+ 视图级对照 | 消融 A2：去掉关系偏置 |
| Cascaded Modification | B 的**几何状态 token** 被 C2 就地更新 | L3 链覆盖 100%（已有）+ 状态重放一致率 |
| 几何合法性 | C3 确定性内核 | 注入错误检出率 K>0 |
| 跨工序来源寻址 | B 的因果掩码 + C1 的 `sources` 指针 | 跨度分桶表（已有） |

---

## 3. 模块 A：Geometry Encoder

### 3.1 输入：保真几何双轨（`design.json` v2）

现有 `numeric` 12 维**不可逆**（`minima`/`spans` 未入库），必须并存一份可重建几何。

```jsonc
{
  "id": "2F3",
  "type": "LINE",
  "text": "",                      // 保留：给文本编码器
  "numeric": [ ...12 维... ],      // 保留：给网络，架构不变
  "geometry": {                    // 新增：给判定器，可逆
    "kind": "line",                // line|arc|circle|spline|polyline|point|text|dim|block
    "start": [468.99, 5.0],
    "end":   [468.99, 74.0],
    "center": null,
    "radius": null,
    "start_angle": null, "end_angle": null,
    "bbox": [468.99, 5.0, 468.99, 74.0],
    "length": 69.0
  },
  "layer": "119",
  "group": ["A2_PATTERN"],
  "block": "FCFEntity1",           // 新增：视图归属（拓扑的关键）
  "view": 1
}
```

顶层新增：

```jsonc
{
  "frame": { "min": [x,y], "span": [sx,sy], "unit": "mm", "scale": 4.0 },
  "views": [ {"name":"FCFEntity1","bbox":[...],"entity_ids":[...]} ],
  "feature_relations": [ ... ]     // 见 §5.3
}
```

`frame` 让 12 维 `numeric` **可逆**：`x = min[0] + numeric[0]*span[0]`。
这样：网络输入不变（旧 checkpoint 仍可加载），几何判定成为可能。

### 3.2 实体 token

对每个实体：

$$
h_e^{(0)} = W_{num}\,\mathrm{numeric}_e + E_{\mathrm{type}}[\tau_e] + E_{\mathrm{layer}}[\lambda_e] + E_{\mathrm{group}}[\gamma_e] + \mathrm{PE}_{\mathrm{geo}}(p_e) + \mathrm{TE}(\mathrm{text}_e)
$$

- `PE_geo`：二维 Fourier 位置编码，作用在**保真归一化坐标** `p_e` 上（用 `frame` 归一化，不是猜的 `spans`）。
  频率取 8 个尺度：`[2^k]`，k=0..7。
- `TE`：真中文编码器（§3.4）。

**新增输入维度**：layer / group / block 三项是**视图与分组拓扑的直接证据**，
现有代码完全没用（`prepare.py` 只保留了 `text` 与 `numeric`）。

### 3.3 关系偏置的图注意力（Graphormer 式）

**这是 "Topology-Aware" 的实处，也是与现有 `RelationLayer` 的本质区别。**

现有做法（2018 年级 GCN）：

```python
messages = self.message(h[source]) + self.relations(relation)
aggregate.index_add_(0, target, messages)      # 度均值
update = self.self_layer(h) + aggregate / degree
```

问题：关系类型只是**加在消息上**，且聚合是**度均值**（无权重、无多头、无几何感知）。

新做法：

$$
\text{Attention}_{ij} = \frac{(W_Q h_i)(W_K h_j)^\top}{\sqrt{d}} \;+\; \underbrace{b_{\rho(i,j)}}^{\text{关系偏置}} \;+\; \underbrace{\phi_{\text{geom}}(p_i,p_j)}_{\text{几何距离偏置}}
$$

- $b_{\rho(i,j)}$：**可学习**的边类型偏置向量（按头独立）。
  边类型集合从现有 3 类扩到 **7 类**：
  `same_view` / `cross_view` / `endpoint_shared` / `annotation_of`（尺寸→几何）/
  `block_ref`（INSERT→块内实体） / `proximity`（同视图邻近） / `none`。
- $\phi_{\text{geom}}(p_i,p_j) = -\|\hat p_i - \hat p_j\|_2^2 \cdot w$：几何邻近偏置，
  $\hat p$ 为归一化坐标。**这条让注意力天然偏向邻近实体**，替代原来的"最近 4 邻居"硬边。
  邻居不再需要**硬编码上限**（现有代码 `[:4]`），注意力自己学。
- 多头（8 头，d=384）。

堆叠 **6 层**，每层：MHSA → FFN(4×, GELU) → LayerNorm → dropout 0.1。

**前向开销估算**：单图 851 实体 × 6 层 × 8 头 → O(N²d) = 851²×384 ≈ 2.8e8 MAC/层，全图 6 层 ≈ 1.7e9。
8 GB 显存下 batch=8 可行（注意力矩阵 851²×8头×4字节 ≈ 23 MB/图）。

### 3.4 文本编码器：换掉 `ByteTextEncoder`

现有实现把 1,039 个中文工艺文本全部压成均值池化向量。

替换方案（分级，可退化）：

| 档 | 编码器 | 参数量（可训练） | 备注 |
|---|---|---|---|
| **T-1（推荐）** | `bge-large-zh-v1.5` 本地加载，**冻结主干**，加 2 层可训练 adapter + 投影 | ~2M | 权重本地已有；离线；中文语义强 |
| T-2 | 同上，微调最后 2 层 | ~7M | 需要更多显存与时间 |
| T-3 | 保持 `ByteTextEncoder` | 0 | 退化档，仅用于消融 |

**约束**：不得联网下载。`E:\AAhz\bge-large-zh-v1.5` 必须先校验完整性（`pytorch_model.bin` 1.3 GB 已在，
但 `model.safetensors` 只有 135 字节 —— **不能用 safetensors 路径**，必须走 `pytorch_model.bin`）。
实现时需把路径做成配置项，禁止硬编码。

### 3.5 特征 token（Feature tokens）

特征（设计参数，如 `outer_diameter`）不是图节点，而是**查询对象**。

现有做法是"锚定实体求均值"（`h[indices].mean(0)`），这会丢掉特征跨多个实体的结构。
新做法：

$$
f_k^{(0)} = W_{f}\,\mathrm{value\_vec}(k) + E_{\mathrm{unit}}[u_k] + \mathrm{TE}(\mathrm{text}_k)
$$
$$
f_k^{(l+1)} = f_k^{(l)} + \mathrm{MHA}\big(f_k^{(l)};\ \mathcal{E}_{\mathrm{anchor}(k)}\big)
$$

即：**特征对它的锚定实体做 cross-attention**（不是均值），得到 `membership_logits`（特征↔实体成员打分），
监督沿用现有的 BCE + Dice。

**同时保留"图上"的实体**：`f_k` 也参与实体注意力（作为一类特殊 token），
这样"特征在图上的位置"是被建模的，而不是外挂的。

### 3.6 输出

```
DesignMemory:
  entities   (N_e, d)      # 实体 token
  features   (N_f, d)      # 特征 token
  requirements (N_r, d)
  membership_logits (N_f, N_e)
  tokens     (N_e + N_f + N_r, d)   # 统一的 design token 池，供模块 B cross-attend
```

**关键改动**：不再输出单一 `context` 向量。B 模块直接对 `tokens` 做 cross-attention。
`context` 保留仅为兼容，但不再作为唯一设计入口。

---

## 4. 模块 B：Cascade Transformer（级联）

### 4.1 为什么需要它

论文标题的 *Cascaded Modification* 要求"每一步建立在前一步的结果上"。
现有实现里，"前一步的结果"只以**指针标签**的形式存在（`sources` 指向前序 state id），
**模型看不到前序工序的几何状态**，因此无法学到"改了外径，沟径要跟着动"这类级联语义。

### 4.2 状态 token：把几何状态做成架构对象

对每道工序 $t$、每个**活跃特征** $j$，维护一个状态 token：

$$
s_{t,j} \in \mathbb{R}^{d}
$$

它编码"到工序 $t$ 为止，特征 $j$ 的当前几何状态"。

初始化（$t=0$，状态 = 名义设计）：

$$
s_{0,j} = f_j^{(L)} + \mathrm{PE}_{\mathrm{stage}}(0) + \mathrm{PE}_{\mathrm{value}}(\mathrm{nominal}_j)
$$

工序 $t$ 的更新：

$$
\hat{s}_{t,j} = s_{t-1,j} + \mathrm{FFN}\big(\mathrm{MLP}(\Delta_{t,j})\big)
$$

$\Delta_{t,j}$ 由几何操作头给出（§5.2）。**这一步就是"级联"的字面实现**：
工序 $t$ 的状态来自工序 $t-1$ 的状态加上本步的增量，不是重新从设计图查一次。

### 4.3 工序级因果自注意力

工序 token $o_t$：

$$
o_t = E_{\mathrm{op}}[\mathrm{op}_t] + E_{\mathrm{stage}}[\mathrm{stage}_t] + \mathrm{PE}_{\mathrm{pos}}(t)
$$

$$
O = \mathrm{Transformer}_{\mathrm{causal}}\big(\big[O \,\|\, S_{1:M}\big]\big)
$$

- 工序 token 之间**因果掩码自注意力**：工序 $t$ 只能看 $t' \le t$。
- 工序 token 与**状态 token** 拼接后一起过注意力 ⇒ 工序能直接"看见"前序工序的几何状态。

### 4.4 与设计图的双向接口

每个工序 token 对 design tokens 做 cross-attention：

$$
o_t \leftarrow o_t + \mathrm{MHA}\big(o_t;\ \mathcal{T}_{design}\big)
$$

这一步替代了现有的 `context` 均值瓶颈。**设计图不再被压成一个向量**。

### 4.5 路线作为条件

路线 $O$ 是**给定输入**，不是预测目标（§1.1 结论 3）。它提供：

- `op_t`：工序名（如"粗磨外沟"）
- `stage_t`：阶段码（`stage_order` 里的 M/R/F/S/R2/P/X）
- `feature_keys`：本工序作用的特征类别（GROOVE/OD/ID/PLANE）—— **来自路线模板，是真结构**

**这一点很关键**：`feature_keys` 让"这一步改的是外径类还是沟槽类"**成为显式条件**，
而不是让模型从工序名的字符串里猜。这直接支持论文的"操作—特征组"主张。

---

## 5. 模块 C：三个输出头

### 5.1 C1 绑定头（替代 `LocalPredictor` 的对应部分）

沿用现有多槽类型化指针（这是我们已有的、本身有技术含量的部分，**不改**）：

- 三个指针头：`features` / `requirements` / `sources`，各 4 槽
- 特殊列 `NONE` / `UNKNOWN` / `EXTERNAL` 可学习
- `roles` 4 类多标签 sigmoid
- `update` 6 类

**改动只有一处**：query 的构造。

现有：
```python
hidden = self.query(torch.cat([design.context, op, state.squeeze(0)]))
```

新：
```python
q = self.query(op + stage + emitted_state)          # 不含 design.context
hidden = q + cross_attention(q, design_tokens)        # design 通过注意力进入
```

即：**设计信息通过 cross-attention 进入，而不是通过一个均值向量**。

### 5.2 C2 几何操作头（新增，对应论文的 Modification）

三个子头：

| 头 | 类别 | 弱监督来源（**无需新标注**） |
|---|---|---|
| `geometry_op` | `turn / grind / cut_groove / chamfer / drill / bore / heat_treat / clean / none` | 从现有 `operation` × `parameter` 组合自动生成：`粗磨外径`+`Dc` ⇒ `grind` |
| `feature_group` | `GROOVE / OD / ID / PLANE / none` | **直接来自 `route_templates_v1.json` 的 `feature_keys`** |
| `delta` | 标量（相对改量）+ 符号 | 来自 L3 语料实测：`touched[].delta_mm`（工序图 vs 名义设计图） |

**`delta` 的监督是免费的**：`build_l3_dataset.py` 已经从 75 张工序图算出了
"每道工序对每个名义特征声明了什么值"，共 6 个零件 13 道工序、160 个尺寸标注。
这就是"几何必须被携带而非被推断"的训练信号。

**`delta` 头是级联更新的驱动量**（§4.2 的 $\Delta_{t,j}$）。

### 5.3 C3 合法性内核（确定性，不可学习）

按 `geometry-grounding-gap-20261112.md` §4-C 设计：

| 检查器 | 判据 | 依赖 |
|---|---|---|
| `no_feature_interference` | `machined_in` 关系上 child < parent | 特征拓扑 + 状态重放 |
| `min_wall_thickness` | 壁厚 ≥ 工艺下限 | 同上 |
| `monotone_progress` | 精加工的值不应比粗加工"差" | 状态序列 |
| `final_meets_design` | 终检值落在设计要求区间 | 状态 + requirements |

**特征拓扑从哪来**（§1.1 说现在完全没有）：
1. **词汇规则**（高置信）：`groove_*`/`pocket_*` ⊂ `outer_diameter`；`groove_radius` ⊂ `groove_diameter`
2. **数值规则**（中置信）：`child.nominal < parent.nominal`
3. **工艺规则**（高置信，来自路线模板 + 现有 `sources`/`roles`）：
   "粗磨外沟"工序的参数指向 `groove_diameter` ⇒ 外沟属于外径那个零件体

**明确记录缺口**：`groove_width` 在特征表中不存在（沟宽是导出量），
判定"R ≤ 沟半宽"需要它。这作为**显式 gap 状态**记录，不假装有。

---

## 6. 训练方案

### 6.1 阶段划分（与数据量匹配）

```
阶段 0  语料构建（无梯度）
        ├─ 保真几何抽取：94 张图 → 保真 entity 记录
        ├─ 视图划分：1,122 blocks → view 归属（复用 topology.split_views）
        └─ 实体对构造：图内视图对 + 跨图对应（复用 correspondence.correspond）

阶段 1  自监督预训练（80,059 实体 / 1,122 视图 / 94 图）
        ├─ 对比学习：同零件不同视图 = 正；不同零件 = 负
        ├─ 掩码几何预测：遮住 entity 的角度/半径/长度/坐标 → 回归
        ├─ 边类型预测：给定实体对，预测其 7 类关系
        └─ 对比目标可用性：bge 文本做弱 pair 监督（工序文本 ↔ 名义文本）

阶段 2  有监督微调（25 案例，冻结或低 lr 微调主干）
        ├─ C1 绑定头（features/requirements/sources/roles/update）
        ├─ C2 几何操作头（geometry_op / feature_group / delta）
        └─ 成员关系头（现有 BCE+Dice，保留）

阶段 3  级联与合法性（端到端，无/弱标签）
        ├─ 状态重放一致性
        └─ 合法性判定（注入错误 → 检出）
```

### 6.2 阶段 1 的正负样本构造（这是把 94 张图变成训练集的关键）

**正样本对**（零标注成本）：

| 来源 | 数量级 | 说明 |
|---|---|---|
| 图内视图对 | 1,122 blocks ⇒ 数百对 | 同一张图的不同 `FCFEntity<N>` 是同一零件的不同投影 |
| 跨工序同零件对 | 75 工序图 ÷ 6 零件 ⇒ 每零件 ~12 张 | 同一零件的不同工序图，共享零件轮廓 |
| 工序图 ↔ 名义设计图 | 6 零件 × ~12 = **75 对** | 用 `correspondence.py` 给出实体级对应 |

**负样本对**（用户明确要求的"没有设计图的当干扰数据"）：

| 来源 | 数量级 | 说明 |
|---|---|---|
| 13 张无工序链的设计图 × 75 工序图 | 975 | 不同零件 ⇒ 不该匹配 |
| 6 零件互配 | 6×5×~12 = 360 | 不同零件 ⇒ 不该匹配 |
| 同零件**错工序**对 | 75×11 ≈ 825 | 同零件但工序不同 ⇒ 轮廓相似但**状态不同**（难负例） |

**类别不平衡**：正 75 vs 负 2,160 ⇒ 必须**难负例挖掘** + 温度系数 $\tau$ 调参 + 按零件均衡采样。
否则对比学习会退化成"分清不同零件"，学不到视图对应。

**实体级监督放大**：图级 75 对 ⇒ 实体级正样本对可达数千
（每对图纸对应 5–20 个实体对，由 `correspondence.py` 给出 handle 级对应）。
这才是能在 6 个零件上训出可用表征的原因。

### 6.3 训练超参与资源

| 项 | 值 | 依据 |
|---|---|---|
| 环境 | `D:\conda\envs\xueyao`（torch 2.4.1+cu124, CUDA） | `smartcad38` 是 CPU 版，训不动 |
| 精度 | AMP (bf16) | RTX 4060 支持 |
| d（hidden） | 384 | |
| 层数 | A:6 / B:4 | |
| 头数 | 8 | |
| 头数×d | 384 | |
| batch | 8 图（≈6,800 实体/批） | 8 GB 显存上限 |
| 可训练参数（估算） | **≈ 32M**（不含冻结的 bge 324M） | 见 §7 参数量表 |
| 阶段 1 epoch | 由语料定（94 图 ⇒ epoch 定义为"遍历全部视图对"） | |
| 阶段 2 lr | 主干 1e-5 / 头 3e-4 | 冻结主干时头可用较大 lr |

### 6.4 评测协议（三层，口径必须写清）

**T-A 条件局部定位**（沿用现有 `evaluate_local`，口径不变）
- `scope = known_operation_parameter_queries_and_gold_history_not_route_rollout`
- teacher_forced 是**上界**；self_predicted 是部署口径
- **只报 gold**；弱标必须 `--include-weak` 显式开启

**T-B 几何落地 L3**（已有可证伪目标，保持不变）
- 端点：`oracle` / `rule`（参数链唯一能做���）/ `model`（推断，**保留为负结果**）/ `chain`（携带）
- 已得结论：rule 验证集 R=58.2%，chain R=100%，覆盖率 100%
- **负结果必须保留**：它证明"改动量不可推断，只能携带"

**T-C 级联与合法性**（新增，是论文的新主张）
1. **状态重放一致率**：把设计图加一条凹槽 → 全链重放 → 与真实工序图对比
2. **注入错误检出率 K**：在真实链上注入 N 个已知非法修改（壁厚过薄 / 特征倒挂 / 精加工比粗加工更差 / 终检超差），看内核检出多少
3. **误报率**：在**未注入**的真实链上，内核是否报警
4. **判据可分性**：合法性 vs 错位 vs 干涉 三个判据能否互相区分（`topology.py` 已给出三个判据的可用性实测）

**K > 0 是关键**：证明这套判定能抓到人也会犯的错，而不只是复现已知的对的东西。

### 6.5 消融表（论文 Table 3 的设计）

| 编号 | 消融 | 检验的主张 |
|---|---|---|
| A1 | 去掉关系偏置 $b_{\rho}$ | "Topology-Aware" 的贡献 |
| A2 | 去掉几何距离偏置 $\phi_{geom}$ | 几何感知的贡献 |
| A3 | 视图内自注意力 → 图内全局注意力 | 视图划分的必要性 |
| A4 | `ByteTextEncoder` ↔ bge | 文本编码的贡献 |
| A5 | 去均值 context ↔ cross-attention | "设计图不该被压成向量"的贡献 |
| B1 | 去因果掩码 | 架构级因果约束的贡献 |
| B2 | 去状态 token（每步独立查设计图） | "Cascaded" 的贡献 |
| C1 | 路由模板 `feature_keys` ↔ 从工序名猜 | 显式结构条件的贡献 |
| P1 | 阶段 1 自监督 ↔ 从头训 | 语料规模的贡献 |
| P2 | `sources` 屏蔽开/关 | 跨工序寻址的因果性 |

---

## 7. 参数量与实现规模

| 模块 | 结构 | 参数量（估算） |
|---|---|---|
| A. 实体 token 投影 | `W_num(12→384)` + type/layer/group emb + PE | ~0.2M |
| A. 几何位置编码 | 8 尺度 × 2 轴 × 2（sin/cos） | ~0.01M |
| A. 文本编码器 T-1 | bge 冻结 + 2 层 adapter + 投影 | ~2M（bge 324M 冻结） |
| A. 图注意力 6 层 | 8 头 + FFN(1536) + 边偏置(7×8×384) | ~10.8M |
| A. 特征 cross-attn + 成员头 | query/key/value + Dice/BCE | ~1.5M |
| B. 工序/状态 token | op/stage emb + Δ 投影 + FFN | ~1.2M |
| B. 因果 Transformer 4 层 | 8 头 + FFN | ~7.1M |
| B. design cross-attn | 4 处 | ~1.8M |
| C1. 绑定头 | 3 指针 × (2×384×384 + 4×384×384 + specials) | ~2.6M |
| C2. 几何操作头 | op(9) + group(5) + delta(1) + 符号(1) | ~0.1M |
| C. 角色/更新/数值头 | 4 + 6 + 4 + 4 + 2 | ~0.05M |
| **合计（可训练）** | | **≈ 27–32M** |

对比现有：`LocalPredictor` 500,173 参数。**新模型约 60×**，且主干由 80,059 实体的自监督语料训练。

### 7.1 实现工作量分解

| 阶段 | 新增文件 | 估计行数 | 依赖 |
|---|---|---|---|
| P0 保真几何抽取 | `farmcad/data/fidelity.py` | ~250 | AutoCAD 抽取 JSON |
| P1 新编码器 | `farmcad/models/graph_encoder.py` | ~350 | torch |
| P2 状态/级联 | `farmcad/models/cascade.py` | ~300 | torch |
| P3 新头 | `farmcad/models/heads.py` | ~250 | torch |
| P4 特征拓扑 | `farmcad/data/feature_topology.py` | ~200 | 规则 |
| P5 合法性内核 | `farmcad/reasoning/legality.py` | ~250 | 纯确定性 |
| P6 预训练 | `farmcad/training/pretrain.py` + `scripts/pretrain.py` | ~400 | torch, bge |
| P7 语料构建 | `scripts/build_geometry_corpus.py` | ~300 | |
| P8 配置/CLI | `configs/*.json`, `cli.py` | ~120 | |

**总计 ≈ 2,400 行**，分 8 个可独立验收的增量。每个增量都有单测。

---

## 8. GRU 的处置（一次性定死）

### 8.1 定位

`FARMModel`（`models/model.py`）降级为**对照基线**，仅用于：
- 论文里"T2 自由生成整条路线"的基线数字（替换掉"我们比 GRU 强"的空泛说法）
- 复用其 `training/metrics.py` 的编辑距离对齐评测

### 8.2 代码约束

1. `trainer.build_model`：架构名从 `"gru"` 改为 `"gru_baseline"`，**默认值改为 `"local"`**（不是 `gru`）。
2. `trainer.load_checkpoint`：`metadata.get("architecture", "gru_baseline")`，
   旧 checkpoint 仍可加载（向后兼容），但**新训练不得默认产出 gru**。
3. `cli.py`：新增 `--baseline` 开关。只有显式给出才能跑 `gru_baseline`；
   否则 `train`/`predict` 对 GRU checkpoint 直接报错：
   `"GRU route baseline is deprecated; use --baseline to run it explicitly."`
4. `models/model.py`：文件头加显式注释
   `"""Deprecated route-generation baseline. NOT the paper's method. ..."""`
5. **不删除**旧 checkpoint 与旧 run 目录（它们是论文基线数字的来源）。

### 8.3 文档约束

- `annotation-workflow.md` §8 标题：`当前训练基线` → `历史 GRU 对照基线（已弃用为主线）`，
  并指向新 run。
- `README.md` / `hybrid-architecture.md` / `SERVER_HANDOFF.md`：
  补一句「GRU 不属于任何主线，仅为对照；主线模型为 TAM-CAD」。
- `method-section-framework.md` §3.3–3.5 标注「已被 architecture-v2-design 取代」。

---

## 9. 实现顺序与验收（每个增量都要能证伪）

| # | 增量 | 验收标准（不是"跑通"，是"有结论"） |
|---|---|---|
| 1 | 保真几何抽取 | 抽出的坐标经 `frame` 反解后与原 JSON 一致（误差 < 1e-6）；`numeric` 可逆 |
| 2 | 视图划分 | 1,122 blocks 全部归入 view；`split_views` 的 FCFEntity 比例实测 |
| 3 | 特征拓扑抽取 | 抽出的父子关系与人工判断一致（人工核对 ≥ 20 条）；记录无法判定的比例 |
| 4 | 图注意力编码器 | 在**负样本对**上对比学习 AUC ≥ 0.9（区分不同零件）→ 再看**视图对** AUC（这个才是难点，预期低） |
| 5 | bge 替换 | 工艺文本近邻检索：`粗磨外径` 的最近邻应为 `粗磨外沟`/`终磨外径` 而非 `热处理` |
| 6 | 状态重放 | 重放 D207NQ.01 每道工序后的特征值，与 L3 实测值逐条一致 |
| 7 | 合法性内核 | 在 6 零件 13 工序上，**注入 4 类错误各 ≥ 5 个**，检出率 ≥ 80%；未注入时误报 ≤ 10% |
| 8 | 阶段 1 预训练 | 对比学习在**验证零件**（留出 1 个零件）上 AUC 显著高于随机 |
| 9 | 阶段 2 微调 | C1 各字段指标 **不低于** 现有 `LocalPredictor`（50 万参数基线）；否则回退 |
| 10 | 端到端 | 设计图加凹槽 → 全链重放 → 合法性判定，端到端跑通并给出 K |

**关键约束**：第 9 步是**不可退让的验收线**。新模型 60 倍参数，如果在 gold 指标上打不过
50 万参数的现有模型，说明设计有问题，必须回头改而不是继续训。

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 8 GB 显存装不下 6 层 d=384 图注意力 | 阶段 1 跑不动 | 梯度检查点（activation checkpointing）；序列长度截断到 512 实体/图；d 降到 256 |
| 对比学习在**视图对**上不 work（只学会分零件） | 阶段 1 无效 | 难负例（同零件错工序对）；按实体级监督放大；先看视图对 AUC 再决定是否继续 |
| bge 在 CAD 文本上分布外 | 收益不达预期 | 保留 `ByteTextEncoder` 作消融对照；先用近邻检索做快速验证再投入训练 |
| 13 张无链设计图做负样本太容易 | 对比学习退化 | 必须混同零件错工序难负例；报告两类负样本分开的 AUC |
| 25 案例有监督微调过拟合 | C1 指标上不去 | 主干冻结 + 强 dropout + 早停；验收线用现有模型 |
| `feature_keys` 只覆盖 4 类特征 | 特征组头学不全 | 显式记 `none` 类；route_templates 已知 `coverage_pct` 48–93%，要如实写进论文 |
| 特征拓扑自动抽取错误率高 | 合法性内核不可信 | 三条规则分层给置信度；人工核对 20 条；低置信关系不参与判定 |

---

## 11. 与已有工作的关系（避免重复劳动）

| 已有成果 | 在本设计中的角色 | 不重复的部分 |
|---|---|---|
| `localization.py` 声明值定位 | **直接用于阶段 1 的跨图实体对应** | 不重写 |
| `correspondence.py` 跨图纸对应 | **直接用于阶段 1 的实体级正样本** | 不重写 |
| `topology.py` 视图划分 + 三判据 | **直接用于阶段 0 视图归属 + 阶段 3 合法性** | 不重写 |
| `geometric_chain.py` 几何链 | **C2 `delta` 头的监督来源** | 不重写 |
| `l3_model.py` 推断模型 | **保留为负结果**（证明必须携带） | 不删不改 |
| `models/pointer.py` 多槽指针 | **C1 直接沿用** | 不重写 |
| `memory/store.py` 事务与检查 | **C3 的提交层落点** | 不重写 |
| `bge-large-zh-v1.5` | 文本编码器 | 已有权重 |
| `FARMModel` (GRU) | **对照基线** | 不再是主线 |

---

## 12. 需要拍板的事项

1. **路线归属**：确认"路线作为条件输入（任务 T1）"为主线，自由生成（T2）降级为 GRU 对照。
   → 这决定模块 B 是否需要自回归解码器，工作量差 ~800 行。
2. **文本编码器档位**：T-1（bge 冻结 + adapter，推荐）/ T-2（微调）/ T-3（保持现状）。
3. **实现顺序**：按 §9 的 1→10 顺序（先数据后模型），还是先做模块 A 的编码器（§9 第 4 步）
   以便尽早看到对比学习是否 work？
4. **GRU 处置**：§8 的 5 条代码约束是否照做（含 `cli.py` 默认拒绝 GRU）。
5. **`feature_keys` 依赖**：是否接受把 `route_templates_v1.json` 的 `feature_keys` 作为
   C2 特征组头的**条件输入**（有外部结构依赖，但可证伪、可消融）？
