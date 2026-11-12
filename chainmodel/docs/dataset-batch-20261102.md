# 多图数据集与人工补齐指南（2026-11-02）

## 1. 先看结论

本轮已经处理全部 **39 张 DWG**，但不能把它们说成 39 份可训练的设计—工序数据对。

- 14 张原本没有 JSON 的根目录图纸完成独立 AutoCAD 副本提取；D207 沿用已验证提取。
- 24 张已有 JSON 的子目录图纸全部属于保持架/半保持架。本轮未重新提取这 24 张。
- 分类：外圈 7、内圈 7、保持架/半保持架 24、内外圈混合工艺参考 1。
- 外圈设计输入全部发布：3,063 实体、49 个属性候选、43 个尺寸要求候选。
- 当前只有 D207NQ.01 具备可复用的系统弱标目标：32 工序、104 参数记录、169 事件。
- 人工 gold 为 0。正式验证/测试目标为 0。仍只能进行 smoke 训练，不能报告泛化性能。

数据主目录：[outer-batch-v2](../data/processed/outer-batch-v2)。
总览：[dataset_summary.json](../data/processed/outer-batch-v2/dataset_summary.json)。

### 为什么不能自动补成七条路线

本轮只使用 `20260917/smartCAD/CAD_Process_Hub`，没有混入其他日期的规则。
当前默认外圈工序表精确覆盖这些输入中的 D207NQ.01。616 图号的大表也没有其余目标外圈的精确对应路线。
相似图号、通用棒料/锻件模板、保持架路线都不能直接作为新图纸的正确答案。

`3D1000095X3.01` 在“工序选择（测试流程完整版）”中有 **30 行精确图号候选路线**，已单独导出供审核，
但该表不等于专家认可的正式路线；其设计精度也没有被当前解析器明确识别。
本轮没有使用原系统的默认 D 等级，也没有拿 D207 路线给其他图纸生成伪目标。

## 2. 已同意的暂定划分

用户同意先冻结下列采集/补标归属，物理家族仍需工程师复核。
数字系列只是一种保守分组启发式，不是经确认的工程族标签。

| 编号 | 图纸中识别的图号 | 实体 | 属性候选 / 要求 | 预留集合 | 主要缺口 |
|---|---|---:|---:|---|---|
| sample-01 | 3D1000095X3.01 | 368 | 7 / 6 | train | 确认精度；审核备用表的30行路线 |
| sample-03 | C110K1QT2U(H).01 | 413 | 8 / 8 | train | 提供精确图号路线 |
| sample-04 | D207NQ.01 | 428 | 8 / 8 | train | 已有弱标；仍需对象分组、参数来源及角色审核 |
| sample-05 | D208N4Q.01 | 428 | 8 / 6 | train | 提供精确图号路线；确认缺失的公差要求 |
| sample-34 | D209NW1U1(H).01 | 489 | 2 / 1 | validation | 提供路线；属性提取覆盖很低，需重点补齐 |
| sample-36 | D210NW1U(H).01 | 379 | 8 / 6 | test | 提供路线；审核缺失要求与全部监督 |
| sample-38 | E207N4QU.01 JP | 558 | 8 / 8 | train | 文件名没有 JP，图内有 JP；先确认版本/身份，再补路线 |

**预留设计数量是 5 / 1 / 1；可加载弱标数据对数量是 1 / 0 / 0。这两个数字不能混用。**

D207 已经用于开发和训练，因此必须留在 train。E207 与其暂按 207 系列归同组。
以后加入同族内圈、工序图、改版图或增强样本时，必须沿用同组，不按实体或工序随机切分。
冻结 test 后，不用其正确标签选择模型、调阈值或修改工艺推理策略；工程标注修正需独立记录。

### 去重和身份异常

- `207.02.dwg` 与 `E207N4QU.02 JP.dwg` 的图内图号相同，规范化实体几何/文字输入指纹相同；
  记为待确认重复组，不把两者当独立样本跨集合使用。DWG 二进制不同不代表独立设计。
- 混合图 `D208N4Q车加工内外圈+规则.dwg` 虽解析出外圈标题，但不是单一静态外圈输入，已单独排除。
- 保持架还有文件名与图内图号不一致的情况，详见逐图审核表；未擅自删除 `3`、`JP` 或工程后缀。
- 当前指纹检查不是通用 CAD 几何等价证明；变体、尺寸改版和物理家族仍需人工确认。

## 3. 数据结构与入口

```text
outer-batch-v2/
  manifest.ready.json            现有模型可加载；仅 D207 train
  design_pool_manifest.json      7个输入的索引；不是训练manifest
  reserved_splits/
    train.json                   5个设计预留
    validation.json              D209设计预留，target=null
    test.json                    D210设计预留，target=null
  design_pool/sample-XX/
    design.json                  严格白名单模型输入
    source.json                  原DWG/JSON路径、SHA、提取与路线审计
    feature_candidates.json      属性候选、原始标注实体和要求证据
    annotations.template.json    待人工填写，不代表已有标签
    unapproved_route_candidates/ 仅有备用精确路线时存在；不作目标
  cases/sample-04/
    design.json / target.json    当前唯一系统弱标数据对
    source/                      原参数结果、关联图及证据
    provenance.json              上游代码、规则资产、输入输出SHA
    review_tasks.csv             D207原关联图审核任务
    feature_chains.csv           按属性整理的工序参数链
  audit/                         39图盘点和路线表覆盖
  review/
    cases.csv                    39行身份、路线、精度与族审核
    features.csv                 49行属性候选与完整几何成员审核
    requirements.csv             43行尺寸要求审核
    routes.csv                   D207的32工序 + 其他6图待填占位行
    parameters.csv               D207的104参数记录逐字段审核
```

没有路线的样本不会生成空的或复制来的 target。其输入可以用于设计编码和后续人工标注，
但当前局部训练器不能直接拿无 target 的输入池做监督训练。

`design.json` 中：

- `entities`：CAD 实体 ID、类型、12维几何数值、原文字；
- `edges`：自环、邻近和共端点边；
- `features`：参数属性候选，不是已确认物理特征；锚点不等于完整几何成员；
- `requirements`：由有证据尺寸产生的绝对区间候选，不代表功能意图已被确认；
- `context`：图中可见材料、产品类型、技术要求、精度；
- 不含路线、未来参数状态或人工分组答案。

`target.json` 中是 OP → PARAM → END_OP → EOS；PARAM 含对象、要求、前序来源、角色、更新方式和值的字段级质量。
详见 [数据协议](data-contract.md)。数值相对偏差、绝对要求区间和未知单位必须区分。

## 4. 人工补齐：按顺序做

**2026-11-03分工修订：** 后续构建按[新计划与填写表](../data/annotations/dataset-build-plan-20261103/README.md)执行。
由系统先生成路线、对象、制造意图及来源候选并采集证据，人工主要补少量上下文及审核歧义。
以下仍描述当前v2的缺口，不表示必须全部由人工从零标注，也不表示完整生产链路已经补接完成。

### 第一轮：先解锁数据对

先填写 [cases.csv](../data/processed/outer-batch-v2/review/cases.csv) 的审核人员、决策及确认信息。

1. **确认图号/版本/族**：重点处理 E207 的 JP、内部图前缀和重复的 E207 内圈。
2. **确认材料和精度**：3D1000095 的精度需要图纸/规范依据，不能从默认值推断。
3. **审核系统生成的完整有序路线候选**：先用 D208 训练侧案例验证完整链路，再扩展其他图。
   已有工序卡可提供作参考，但不是要求人工从零编写全部路线；D209、D210保持独立审核。
   每个工序记录顺序、工序实例标识、名称、工序号、来源表/卡及适用材料/毛坯/工艺阶段。
   同名工序重复出现时不能合并，必须区分实例。
4. **注明参数覆盖**：没有计算结果不等于该工序没有参数。未审核时 `parameters_complete` 不能标真。

路线可以由工程师提供现有工序卡，或在系统中为精确图号建立/批准对应路线后重新生成参数。
系统负责参数计算与证据追踪；路线适用性仍要有人确认。

`routes.csv` 中空行是新增路线的填写起点，**一行不是默认的一道工序**；请按实际路线扩展多行并填写完整顺序。
3D1000095 的候选路线见 [候选目录](../data/processed/outer-batch-v2/design_pool/sample-01/unapproved_route_candidates)。

### 第二轮：补稳定对象与设计要求

在 [features.csv](../data/processed/outer-batch-v2/review/features.csv) 中：

- 确认该候选的物理含义；将同一沟道的沟径、沟位、沟曲率等关联到同一个 `physical_object_id`。
- 填写完整几何实体 handle 集合；标注锚点主要是尺寸文字/标注，不能直接当完整几何组。
- 不完整集合要明确说明未完成，不能作为完整成员监督导入，否则其余实体会被当负例。
- D209 当前只识别出两个属性候选，必须对照图纸补查，不能认为其只有两个真实特征。

在 [requirements.csv](../data/processed/outer-batch-v2/review/requirements.csv) 中：

- 确認绝对上下限、单位、对应属性与物理对象，以及是否确为成品设计要求。
- 缺失偏差不补零；暂未识别的公差、粗糙度、基准及技术要求另列补充清单。
- 成品要求不能直接套到粗加工中间状态。

### 第三轮：标工序—参数—来源关联

在 [parameters.csv](../data/processed/outer-batch-v2/review/parameters.csv) 中审核：

1. `features` / `requirements`：作用于哪个对象和哪条要求；
2. `sources`：准确的前序 PARAM 事件 ID，不能只写“前面某个磨工序”；
3. `roles`：加工目标、引用复制、检测目标还是基准使用；
4. `update`：设计引用、前序复制、查表、计算或外部来源；
5. `value`：名义值、上下偏差、直接显示值、单位和证据；
6. 查表/公式的版本、定位信息及实际执行证据。

复制/检测不应被解释为重新加工；参数记录也不自动等于真实工件物理状态。

### 哪些内容当前可以导入

- 已有 target 的完整几何成员和既有 OP/PARAM 字段纠正，可整理为 `annotations.template.json` 的
  `feature_members` / `event_overrides`，由 `apply_annotations` 校验并合入新版本 target。
- D207 原试点可以使用现有 `prepare --annotations` 命令；其 provenance 中的旧 Windows 绝对路径
  在服务器不能直接使用，需在本地重建，或显式做路径重定位并保留原始证据。
- 新图**先补路线并生成事件**，再填写事件级 `event_overrides`；不能给不存在的事件写指针。
- 上述 CSV 是人工交接/审阅表，**当前没有通用 CSV 自动导入器**。
- `physical_object_id` 的合并、缺失候选/要求的新增、路线表审批导入需要后续数据构建/协议扩展；
  不能把这些新字段直接塞进现有 design 白名单。
- 只提升实际审核字段的质量，不能把整个文件一键改为 gold。

保持架与内圈本轮只完成盘点。它们需要对应的参数/状态适配、路线与标签，不是改产品名称就能复用外圈训练协议。

## 5. 验证、复现与服务器使用

本轮新增脚本位于研究根目录的 [scripts](../../scripts)：

- [prepare_batch_sources.py](../../scripts/prepare_batch_sources.py)：副本提取、原图SHA核验、独立CAD进程退出等待；
- [build_outer_dataset.py](../../scripts/build_outer_dataset.py)：库存、精确路线覆盖、系统参数与弱标生成；
- [assemble_outer_dataset.py](../../scripts/assemble_outer_dataset.py)：输入池、预留划分、人工审核包；
- [verify_batch_dataset.py](../../scripts/verify_batch_dataset.py)：数据契约、实际计数、来源SHA和模型输入兼容性检查；
- [test_dataset_batch.py](../../scripts/test_dataset_batch.py)：策略与真实批数据回归。

旧 v1 尝试目录保留缺路线失败日志，不作为当前发布数据。重建请写入新目录；
失败/半成品目录不应直接覆盖。当前提取器仅适用于有 AutoCAD 的 Windows，服务器消费已经导出的数据。

本地通过 45 项脚本测试（原37项 + 新8项）与54项模型测试。D207 的 design/target 与旧试点结构完全相同。
首次运行模型测试缺少 src 路径；设置 `PYTHONPATH=...\chainmodel\src` 后全部通过，未安装/升级依赖。

在服务器已激活 chain 环境、进入 chainmodel 后：

```bash
python run.py validate-data --manifest data/processed/outer-batch-v2/manifest.ready.json --smoke
python ../scripts/verify_batch_dataset.py \
  --dataset data/processed/outer-batch-v2 \
  --checkpoint runs/local_smoke_d207nq01/checkpoints/last \
  --device cuda --output runs/batch_v2_server_verification.json
```

第二条只检验各图输入能否经过现有局部模型，不衡量预测正确率，不生成正确工序路线。
模型输出是研究候选，不应用于实际加工放行。

原始 source/provenance 内的 Windows 绝对路径故意保留，避免篡改历史；模型清单路径均相对数据集目录。
服务器验证不加 `--check-sources`；本地可用该选项检查39份原图/JSON和15份提取清单。

**下一阶段达标条件：** 补齐额外路线和关键字段监督，确认族与重复关系，拥有真正独立的
validation/test 目标，才发布正式训练清单。即使7图全部补齐，也只是小规模试验集，不足以支持广泛泛化结论。
