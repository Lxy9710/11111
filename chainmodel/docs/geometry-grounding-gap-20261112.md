# 几何落地的缺口：盘点结论与补法

日期：2026-11-12
状态：S0 盘点已完成，结论见 §3

## 1. 问题

我们的 process chain 是**参数链**，不是**几何链**。

`target.json` 的事件只有 `OP / PARAM / END_OP / EOS`。`PARAM` 的 labels 是
`features / requirements / sources / roles / update / value`——
**没有任何字段描述"这一步把图纸改成了什么样"**。

用户的问题因此是准确的：设计图增加一条凹槽，怎么保证每一步工序图的几何修改
正确、约束合法、不干涉不错位？

## 2. 盘点发现（四条硬事实）

盘点脚本：`scripts/audit_geometry_alignment.py`、`scripts/replay_geometry_state.py`

### 事实 1：上游的几何不随工序演进——级联在"值"里，不在"形状"里

对 `C206NW1U(H).56` 的 19 道工序逐一比对上游 working JSON：

| 工序 | 直线/多段线 | 跨度 X | 跨度 Y | 最大半径 |
|---|---|---|---|---|
| 105 粗车第二平面 | 139 | 299.0 | 210.0 | 182.7 |
| 115 粗磨外径 | 133 | 299.0 | 210.0 | 182.7 |
| 125 均车两平面 | 143 | 299.0 | 210.0 | 182.7 |
| 160 终磨外径 | 170 | 299.0 | 210.0 | 182.7 |

**轮廓尺寸完全不变**。工序之间 handle 几乎全换（新增 126–416，删除 97–413），
说明每道工序图是**各自从模板实例化**的，不是从上一道改出来的。

那"级联"体现在哪？体现在 `mogong_patch` 的参数值里：

```
Dc:  粗磨外径 51.6  (evidence: design.Dc+0.3)   ← 留量
    终磨外径 51.3  (evidence: design.Dc+tol)
Bc:  切断    8.1   (evidence: design.Bc+0.6)
    均车两平面 7.5 (evidence: design.Bc+tol)
```

**上游的级联 = 同一个特征在不同工序有不同留量。**
它已经用 `source_type` + `evidence` 记下了"这个数从哪来"——
这正是我们完全缺失的概念。

> 结论：上游**没有**逐步几何状态机。我们不能指望"抄上游"来解决这个问题，
> 必须自己建。但上游的 `source_type`/`evidence` 表达形式值得借鉴。

### 事实 2：我们的实体表征不可逆，无法承载任何几何判定

`prepare.py:adapt_design` 把每个实体压成 12 维向量：

```
[0,1]  归一化中心 (cx/spans[0], cy/spans[1])
[2,3]  归一化范围 (ex/spans[0], ey/spans[1])
[4]    scaled(length)      [5] scaled(radius)
[6,7]  sin(angle), cos(angle)
[8]    scaled(measurement) [9] has_measurement
[10]   is_valid            [11] has_start_end
```

**`minima` 和 `spans` 没有存进 `design.json`**（顶层键只有
`schema_version / case_id / entities / edges / features / requirements / context`）。
所以中心坐标无法还原为图纸单位。

更根本的问题：**直线由两个端点确定，向量只存了中心+范围+长度+角度。**
- `sin/cos` 只给方向轴，无法区分 θ 与 θ+π → 丢失朝向
- 没有端点绝对位置
- 半径只对 CIRCLE 有意义，其余 217 个实体没有半径

长度/半径/角度是**可逆的**（对数变换有逆，实测 `69.8 → 4.2599 → 69.8`），
但几何判定需要的是**坐标**，不是长度。

> 结论：当前实体表征**不能**支撑干涉判定。用户说的"process chain 不能只有
> parameters，还要有几何 entity 的表征"——这一点必须改，而且不是加字段那么简单，
> 是要**另存一份可重建的几何**，让网络用向量、判定用几何。

### 事实 3：级联其实已经在我们的 target 里，只是没人重放

`ring-D207NQ.01`（唯一有语义特征名的案例）的参数链**完整地**携带了级联：

```
粗磨外径   outer_diameter  72.2  -> 72.06
粗磨外沟   groove_diameter 64.632 -> 64.562   (derive)
           groove_radius   5.72  -> 5.64
细磨外沟   groove_position 8.54  -> 8.5     (design_copy)
终磨外沟   groove_diameter 64.602 -> 64.622
```

因为 `derive / state_copy / design_copy` 这三个算子已经把
"这一步的值从哪继承来"记下来了，所以**从 target 就能重放出每道工序之后
每个特征的当前值**。

`scripts/replay_geometry_state.py` 做了这件事。重放 D207NQ.01 的 31 道工序，
每一步的外径 / 沟底直径 / 曲率 / 壁厚都能算出来，全部 OK：

```
工序                D外径    De沟底径    R曲率    壁厚   判定
均粗磨平面          72.200    64.632    5.72    3.784   OK
粗磨外径            72.060    64.632    5.72    3.714   OK
粗磨外沟            72.060    64.562    5.64    3.749   OK
终磨外径            72.030    64.562    5.64    3.734   OK
终磨外沟            72.010    64.602    5.72    3.704   OK
```

> 结论：**几何状态机不需要新的标注**。它是一个**重放器**，输入是我们已有的 target。
> 这是这个缺口最省力的切入点——今天就能做，而且立刻可验证。

### 事实 4：特征词汇表缺关键量，且特征之间没有关系

25 个案例只有 23 个不同的特征名。**只有 `ring-D207NQ.01` 一个案例有语义特征名**
（含 `groove_diameter` / `groove_position` / `groove_radius` /
`outer_diameter` / `inner_diameter`），其余 24 个保持架案例全是
`Bc / Dc / Mc / Pm / Po / dc / center_diameter / pocket_*` 这类代号。

而且：**`groove_width`（沟宽）根本没有**。判定"R ≤ 沟半宽"需要它。
我们只有 `groove_position`（沟位置 ae）和 `groove_radius`（曲率 R），
但**没有沟的宽度**。

更要命的是：**`features` 是一个平列表，特征之间没有任何关系。**
`edges` 只描述 entity 之间的关系。没有任何地方说
"外沟在外径里面"。

> 结论：干涉判定的前提是**特征拓扑**，而特征拓扑现在完全不存在。
> 这一层必须新建，且是所有后续工作的前提。

## 3. 结论：缺的是三样东西

| 层 | 缺什么 | 现状 | 能否立刻做 |
|---|---|---|---|
| **A** | **特征拓扑图** — 特征之间的从属/包含关系 | 完全没有 | **能**，从现有数据抽 |
| **B** | **几何状态机** — 每道工序后的特征当前值 | 数据已在 target 里，但没人重放 | **能**，已验证可行 |
| **C** | **几何合法性判定** — 干涉、壁厚、悬空、穿字 | 完全没有 | 依赖 A、B |

外加一项**表征改造**：

| **D** | **可重建的实体几何** — 另存一份保真几何，与 12 维向量并存 | 只有不可逆向量 | 能，但数据量增 |

关键洞察：**A 和 B 能立刻做，C 和 D 才是重头。**
而且 C 的第一版检查器**只需要特征值**，不需要真实坐标——
因为特征值本身就是几何量（外径、沟底径、曲率）。
D 可以推迟到需要判定**标注层**几何（箭头贴不贴、字穿不穿线）时再做。

## 4. 补法

### A. 特征拓扑图

在 `design.json` 加一个平铺的关系表：

```json
"feature_relations": [
  {"parent": "outer_diameter", "child": "groove_diameter",
   "type": "machined_in",
   "invariant": "child.diameter < parent.diameter",
   "margin_min": 0.0,
   "evidence": "same_part_section_annotation"}
]
```

`machined_in` 的语义：**child 永远在 parent 内部**，parent 的半径变更自动带动 child。

**怎么自动抽？** 三条互补的规则，按置信度排序：

1. **词汇规则**（高置信）：`groove_*` / `pocket_*` 属于 `outer_diameter`；
   `groove_radius` 属于 `groove_diameter`。
2. **数值规则**（中置信）：`child.nominal < parent.nominal` 且同类。
   例：`inner_diameter`(60.2) < `outer_diameter`(72) ✓
3. **工艺规则**（高置信，来自路线模板）：`车外沟` 工序加工的对象，
   其参数 `De` 指向 `groove_diameter` → 外沟属于外径那个零件体。

第 3 条最有价值：**我们的 `sources`/`roles` 已经记录了参数与特征的绑定**，
"粗磨外沟"工序的 `De` 指向 `groove_diameter`，而"粗磨外径"的 `D` 指向
`outer_diameter`——同一个零件体上的这两个特征就是父子关系。

### B. 几何状态机

```python
def replay(target) -> list[State]:
    """每道工序之后，每个特征的当前值。"""
```

规则：
- `derive` / `design_copy` / `external` → 该特征当前值 = 本工序写入的值
- `state_copy` → 沿 `sources` 指向的状态继承（**不能直接用标签值**，
  标签值是系统给的候选，继承才是工程语义）
- 未被写入的特征 → 保持上一状态的值（**这就是"子特征跟着父特征走"的机制**：
  改了外径但没改沟径，沟径自动跟着变）

最后一条是关键：**不需要显式写"子特征跟随"，值守恒自然实现。**
这也是为什么 B 能立刻做。

### C. 几何合法性判定器

第一版只做**特征层**的检查（不需要坐标）：

| 检查器 | 判什么 | 依赖 |
|---|---|---|
| `no_feature_interference` | 任何 `machined_in` 关系上，child < parent | A + B |
| `min_wall_thickness` | 壁厚 ≥ 工艺下限（留量表） | A + B |
| `monotone_progress` | 精加工工序的值不应比粗加工更"差"（如外径单调减小） | B |
| `final_meets_design` | 终检工序的值必须落在设计要求区间内 | B + requirements |

第二版才做**标注层**检查（需要 D）：
`annotation_attachment`（箭头贴住被标注几何）、`no_text_collision`（不穿字）。

### D. 实体几何保真表征

`design.json` 的 entity 改成**双轨**：

```json
{"id": "21A47", "type": "LINE",
 "geometry": {"start": [1044.87, -13806.81], "end": [975.04, -13806.81]},
 "layer": "0", "group": ["A1"],
 "numeric": [ ...12 维, 给网络用... ],
 "text": "", "bbox": [975.0, -13806.8, 1044.9, -13806.8]}
```

- `numeric` 保给网络，架构不变，**现有 checkpoint 不用重训**
- `geometry` 保给判定器
- `layer` / `group` 保给标注层检查（上游 README 里 16 条教训，
  大多是图层/组的问题）

代价：`design.json` 体积从 428 实体的小文件涨到含坐标的大小——可接受，
因为 `chainmodel/data/processed/` 已经在 `.gitignore` 里。

## 5. 模型怎么参与

用户说"process chain 不能只有 parameters，还要有几何 entity 的表征"。
落实到模型上，是**两个新头**，都不是必须重训现有架构：

### 头 1：`geometry_op` — 这一步是什么几何操作

```
grind_outer_diameter / cut_groove / turn_plane / chamfer / drill / none
```

**弱监督可得，不用新标注**：我们已有 `operation` → `parameter` 的对应。
`粗磨外径` + `Dc` 几乎必然是 `grind_outer_diameter`。
`车外沟` + `De` 必然是 `cut_groove`。这是**从现有数据直接生成的标签**。

### 头 2：`feature_topology` — 这一步改的是哪个特征组

不是单个 feature，而是**特征组**（一组互相从属的特征）。
`粗磨外径` 改的是 `{outer_diameter, inner_diameter, planar_dimension}` 这一组，
而不是单个值。

这个头可以用 **A 层的拓扑图**当标签：拓扑图一建好，
"哪些特征属于同一个被加工对象"就有标签了。

### 分工（和现有架构一致）

| 谁 | 做什么 | 为什么 |
|---|---|---|
| 模型 | 预测几何操作类型 + 影响的特征组 | 模式识别，模型擅长 |
| 确定性内核 | 执行修改、重放状态、跑检查器 | 几何计算必须可复现、可证明 |
| 人 | 确认模型对操作类型/特征组的判断 | 成本低，只需分类确认 |

落地位置很明确：`MemoryStore.commit_operation` 现在已经有一堆拒绝规则了
（`Conflicting writes to the same object attribute in one operation`、
`Source must precede this operation`）。**几何约束检查就是往这个位置加**，
架构上已经预留了。

## 6. 实施顺序

| 阶段 | 做什么 | 验证方式 | 需要新标注？ |
|---|---|---|---|
| **S1** | 特征拓扑图 schema + 自动抽取 | 抽出的父子关系与人工判断一致 | 否 |
| **S2** | 几何状态机（只重放） | 重放值与 D207NQ.01 的路线语义一致 | 否 |
| **S3** | 特征层检查器（4 个） | 在真实案例上跑通；**故意注入一个非法值看能否抓住** | 否 |
| **S4** | 模型 `geometry_op` 头 + 弱监督 | 准确率 > 基线 | 否（弱标签自生成） |
| **S5** | 实体几何保真表征（D） | 坐标可还原；标注层检查器可跑 | 否 |
| **S6** | 端到端：设计图加凹槽 → 全链重放 → 判定 | 改造前后对比 | 可能 |

**S1–S4 全部不需要新标注**，这是这个方案最大的优势：
它建立在已有的 25 个案例和已有的 target 上。

## 7. 论文角度

论文标题的 "Topology-Aware Geometric Localization and **Cascaded Modification**"
—— 后半句现在完全落空。

- **Cascaded**：修改沿工艺链累积，每步建立在前面所有步的结果上。
  我们的 `state_copy`/`derive` 已经在记录这件事，但**没人把它重放成几何状态**。
- **Modification**：每一步真的改了几何，而且改得合法。
  现在只有参数，没有几何。

补上之后可以有一个**可证伪的主张**：

> 给定设计图和工艺链，系统重放出每道工序之后的零件几何状态，
> 并自动判定修改的合法性。在 N 个案例 M 道工序上，系统重放的几何状态
> 与工程实践一致，并在 K 个案例上捕获了人工修改引入的非法几何。

**K > 0 是关键**——证明这套判定能抓到人也会犯的错，而不只是复现已知的对的东西。
这正好是上游 16 条"不可违反的边界"的反面：上游是人肉补丁，
我们把它变成机器判定。

## 8. 与上游的关系

上游 `mogong/README.md` 的 16 条边界是**经验教训**，每条都对应一次人工返工
（箭头要顶住尺寸界线、竖线不能穿字、MTEXT 要居中、降级 DIMENSION 要补 SOLID 顶点……）。

这些是**标注层**的约束（第 2 版的 `annotation_attachment` /
`no_text_collision`），不是特征层。特征层的约束（干涉、壁厚）上游**根本没做**——
因为上游不做几何重放，它只改文字。

所以我们的增量在两处：
1. 特征层合法性判定（上游空白）
2. 标注层约束的**形式化**（上游是人肉补丁，我们是可执行检查器）

不要复用上游的补丁逻辑——那是在 AutoCAD COM 对象上做的，
依赖具体模板、具体句柄，不可迁移。我们要**抽取约束语义**，
在自己的内核里重新实现判定。

上游值得借鉴的两点：
- `source_type` + `evidence`（这个数从哪来，是留量还是图纸值）
- `conditional_sdc` 的条件化规则形式 + `expected_present`/`actual_present` 一致性断言

## 9. 需要你拍板的

1. **从哪个案例开始做验证？** 我建议 `ring-D207NQ.01`——
   它是**唯一**有语义特征名（含 groove_*）的案例，拓扑关系现成。
   但它只有 1 个。保持架那 24 个案例是代号式特征名，
   做拓扑抽取时**需要先做代号→语义的映射**（`Dc`→`outer_diameter`、
   `pocket_diameter`→`groove_diameter`？）。这个映射要不要现在做？
2. **`groove_width` 缺失怎么办？** 上游有 `Se`（沟宽）参数但我们的特征表没有。
   是从上游补抽，还是先用 `groove_radius` + `groove_position` 近似？
   我倾向先不补，把"缺少判定所需量"作为一个**显式的 gap 状态**记录下来——
   这本身就是个诚实的实验结论。
3. **代号→语义映射的权威来源？** 保持架的代号（`Dc`/`Bc`/`Mc`/`Pm`/`Po`/`dc`/
   `pocket_*`）在保持架规则引擎里有定义吗？
   如果有，S1 的自动抽取可以直接用它，`25 个案例都能用`；
   如果没有，S1 只能先在 1 个案例上验证。
