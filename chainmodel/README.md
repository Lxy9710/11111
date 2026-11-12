# FARM-CAD / ChainModel Hybrid

**迁移服务器或更换 agent：先读 [完整交接备忘](SERVER_HANDOFF.md)。** 其中包含研究目标、实现边界、测试证据、Qwen 下载检查、迁移清单、绝对路径陷阱和接手任务顺序。Qwen 文件已下载并检查，但运行后端仍是占位，未启动推理。

**多图数据进展：** 已盘点39图并整理7份外圈设计输入；当前仅1份系统弱标数据对。
5/1/1是待补标签的预留划分，不是已完成的训练/验证/测试集。逐图缺口和操作说明见
[批数据与人工补齐指南](docs/dataset-batch-20261102.md)。

**T2 主任务：从设计图的结构化输入生成工序路线、参数记录及特征/要求/前序状态指针。**

主线已调整为 **小模型局部预测 + 独立结构化记忆 + 可学习寻址 + LLM 规划 + 受控状态提交**。原 GRU 保留为对照基线，不再作为主架构。

这是研究原型，不是已验证的工艺规划产品。局部模型和持久记忆可以运行，LLM 暂为明确占位：配置指向 `E:\AAhz\20260918\intentionCAD\llm`，不会读取尚未下载完成的 Qwen 权重，不会调用远程服务。混合流程会停在 `awaiting_llm`，不能报告为完成了真实大模型推理。

## 当前主架构与快速入口

```text
设计 JSON → 小模型图编码/成员候选
                  ↓
       精确记录 SQLite ↔ 可学习寻址器
                  ↕
         LLM 结构化查询/工序提案（占位）
                  ↓
       规则检查 → 原子提交 → 新版本状态
```

小模型不包含 GRU/LSTM，也不负责生成整条路线。它根据单次工序/参数查询预测对象、要求、来源、作用和更新候选；训练与原基线共享数据协议、质量权重和恢复机制。

```powershell
Set-Location 'E:\AAhz\20260918\intentionCAD\chainmodel'
$python = 'D:\conda\envs\smartcad38\python.exe'
$env:PYTHONIOENCODING = 'utf-8'

& $python run.py train `
  --manifest 'data\processed\d207nq01\manifest.json' `
  --config 'configs\local-smoke.json' --output 'runs\my_local_smoke' --smoke

& $python run.py hybrid-init `
  --design 'data\processed\d207nq01\design.json' `
  --checkpoint 'runs\my_local_smoke\checkpoints\last' `
  --config 'configs\hybrid.json' --output 'runs\my_hybrid' --smoke

& $python run.py hybrid-pending --session 'runs\my_hybrid'
```

`hybrid-init` 只读取设计输入和局部模型权重，不将训练 target 导入工作记忆。数据库保存完整数值和来源，LLM 请求只包含分页对象、少量成员候选、近期工序和当前查询结果。

架构、占位接口、离线响应示例、事务约束与未实现范围详见 [混合架构说明](docs/hybrid-architecture.md)。下载完成后仍需单独接入模型加载/服务适配器；不是把配置改成非 placeholder 就能启动推理。

本轮验证：54 项测试通过；真实局部模型三步 loss 从 2.2391 降到 1.9456。实际 hybrid 请求已保存到 [混合运行目录](runs/hybrid_d207nq01)，仍处于 awaiting_llm；[验收记录](runs/hybrid_d207nq01/verification.json) 明确区分了离线测试和真实大模型推理。

## 1. 目录

```text
chainmodel\
  run.py                         本地统一 CLI，无需先安装包
  pyproject.toml                 包及依赖声明
  configs\
    hybrid.json                  Qwen 占位、上下文与工具预算
    local.json                   主架构局部模型训练配置
    local-smoke.json             局部模型 CPU 三步验证
    t2.json / smoke.json          原 GRU 基线配置
  src\farmcad\
    data\                        输入/目标协议、原系统适配、人工标签、族划分
    models\                      local.py 局部模型；model.py 原 GRU 基线
    memory\                      store.py 精确记录；addressing.py 可学习寻址
    reasoning\                   规划接口、Qwen 占位、工具执行与上下文预算
    training\                    共享训练/恢复；局部评价与 GRU rollout 分开
    cli.py                       数据、训练、评价及 hybrid-* 入口
    utils.py                     JSON、哈希及数值工具
  data\
    annotations\                 人工标注说明；真实标注由工程师填写
    processed\d207nq01\           已准备的真实试点，输入与目标分文件
  runs\                          版本隔离的局部/混合实验及历史 GRU 实验
  docs\                          数据协议与混合架构说明
  tests\                         独立合成测试及可选真实试点集成测试
```

数据和实验输出被 `.gitignore` 排除，避免把工业图纸或二进制权重意外纳入代码仓库。测试的临时目录自动清理。`prepare/train/predict/evaluate` 均拒绝覆盖已有输出；断点续训也写入新的 run 目录。

## 2. 保留的 GRU 基线

下面第 2、5、6 节介绍原 GRU 路线生成基线。新主架构使用上面的 local/hybrid 命令；局部模型的 `evaluate` 是已知工序查询与标注历史条件下的评价，不是自由路线生成。

事件流：

```text
OP(工序名称)
  PARAM(参数名称, 对象指针集合, 要求指针集合, 前序状态指针集合,
        作用角色集合, 更新类型, 数值候选)
  PARAM(...)
END_OP
OP(...)
...
EOS
```

- 设计编码：边感知实体图网络、离线 UTF-8 字节卷积文本编码、数值和单位编码。
- 稳定设计记忆：参数角色候选及其原图标注、尺寸要求；不会读取工序路线、目标参数或复制来源。
- 特征成员头：对设计候选预测实体成员，支持人工几何分组监督；人工成员仅进 target，不进输入。
- 动态状态记忆：保留生成/训练历史中的参数记录，不覆盖旧状态。
- T2 解码器：GRU 自回归预测工序、参数和结束事件；推理不接收真实路线。
- 三组类型化指针：对象、要求、前序状态，各自支持最多 `pointer_slots` 个对象和 NONE/UNKNOWN/EXTERNAL。
- 因果掩码：来源只能引用更早工序的状态，不能访问当前工序或真实未来状态。
- 作用角色和来源/更新类型分头预测，避免把“复制参数”当作“加工特征”。
- 数值：已选定的唯一设计/前序来源可确定性复制；lookup/derive 仅输出未核验数值候选，真实 `value` 保持 unknown，等待规范引擎执行或人工确认。

这不是旧检索模型换分类头，但也不是论文所有设想的最终实现。当前边界：

1. 特征数量和属性候选仍来自设计图参数提取，不是开放集合的物理特征发现；同一沟道的沟径/沟位/半径尚为不同属性锚点。
2. 使用小型本地字节文本编码器，没有下载预训练 LLM/VLM，也没有加载旧 GNN 权重。
3. 工序/参数名称使用训练集词表，不能自动生成词表外的新工序。验证/测试 OOV 会报告，不能通过读取测试集扩词表。
4. 当前训练是 teacher forcing；评价是真实自由 rollout。尚未实现 scheduled sampling、强化学习或跨设计 A/B 影响学习。
5. 本版不执行规范查表/留量公式，不绘制 CAD，不预测刀具可达性等缺少监督的功能意图。
6. END_OP/EOS、长度上限和工程正确性分开报告。达到上限的结果标为未完成，不强行补 EOS 冒充成功。

## 3. 环境

已验证：Windows、Python 3.8.20、PyTorch 1.13.1 CPU、NumPy 1.23.5。不需要额外安装即可在当前环境运行：

```powershell
Set-Location 'E:\AAhz\20260918\intentionCAD\chainmodel'
$python = 'D:\conda\envs\smartcad38\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
& $python run.py --help
```

新环境按 [pyproject.toml](pyproject.toml) 安装依赖；若需要命令行入口，可在本目录运行 `python -m pip install -e .`，再使用 `farmcad`。本地 `run.py` 不需要 editable 安装。

建议在编辑器中单独打开 `chainmodel` 文件夹，并选择上述解释器；项目的 Pyright 配置声明了 `src` 导入路径。直接打开外层 `E:\AAhz` 大工作区时，编辑器可能不采用嵌套项目配置，测试文件会出现 `reportMissingImports`；这不等于 unittest 无法运行，也不应通过删除检查或修改共享工作区来掩盖。

checkpoint 使用受限的 `weights_only=True` 加载并检查 SHA256。为兼容 PyTorch 1.13 受限加载器对浮点 pickle 的限制，优化器标量/结构放在 JSON，张量留在权重文件；不回退到不受限的 pickle 加载。

## 4. 数据准备与人工监督

本次已准备 [试点 manifest](data/processed/d207nq01/manifest.json)。以下示例使用新目录重放：

```powershell
& $python run.py prepare `
  --pilot '..\pilot\D207NQ.01' `
  --family-id D207NQ `
  --output 'data\processed\d207nq01_replay'
```

输出：

- `design.json`：可独立用于推理的设计数据；无真实路线、目标参数、人工 group 或来源字段。
- `target.json`：工序/参数事件、字段级监督质量、可选人工特征成员。
- `annotations.template.json`：空人工标注模板。
- `manifest.json`：显式 case/family/split。
- `provenance.json`：源图、关联图及输入/目标哈希。

真实样本包含 428 个实体、8 个设计参数候选、32 道工序、104 条参数记录，共 169 个事件。13 道未覆盖参数的工序，其 END_OP 损失被屏蔽；不能把“缺参数”监督为“没有参数”。

人工标注流程见 [数据协议](docs/data-contract.md)。将模板复制到 `data\annotations`，填写 reviewer 和需要确认/纠正的字段，然后：

```powershell
& $python run.py prepare `
  --pilot '..\pilot\D207NQ.01' --family-id D207NQ `
  --annotations 'data\annotations\d207nq01.reviewed.json' `
  --output 'data\processed\d207nq01_reviewed'
```

只将人工编辑的字段升级为 gold，不把整个案例自动变为 gold。不会自动给你填写真实审核人或伪造几何成员。

## 5. 快速验证训练链路

```powershell
& $python run.py validate-data --manifest 'data\processed\d207nq01\manifest.json' --smoke
& $python run.py train `
  --manifest 'data\processed\d207nq01\manifest.json' `
  --config 'configs\smoke.json' `
  --output 'runs\my_smoke' --smoke
```

`--smoke` 仅允许在缺少独立验证族的情况下验证程序，不能生成泛化结论。正式训练不加该标志，要求 manifest 中至少有相互隔离的 train 和 validation 族。

默认权重：gold=1，silver=0.5，system=0.2，candidate=0，unknown=0。当前自动几何/属性对应通常为 candidate，因此对象指针和几何成员的强监督需要人工补齐。不要仅为降低训练损失而把候选全部标成 gold。

训练输出有：`metrics.jsonl`、`training.log`、`dataset_report.json`、解析配置、验证结果、`checkpoints\last`；存在独立验证集时按验证损失另存 `checkpoints\best`。

真实试点三步烟雾训练已运行：loss 从约 3.1749 降到 2.8431。该权重自由生成触发 256 事件上限，明确标为未完成；它不是可用的工艺规划模型。另有独立合成单元测试验证模型能过拟合完整两工序路线、正确 EOS 和来源指针。

## 6. T2 推理：不提供真实工序路线

使用已经导出的独立 `design.json`：

```powershell
& $python run.py predict `
  --checkpoint 'runs\my_smoke\checkpoints\last' `
  --design 'data\processed\d207nq01\design.json' `
  --output 'runs\my_smoke\prediction.json'
```

也可以只从新的原始设计图提取 JSON 构造输入，不调用工序 Excel 或磨工引擎：

```powershell
& $python run.py prepare-design `
  --drawing-json '..\pilot\D207NQ.01\source\D207NQ.01.json' `
  --project-root 'E:\AAhz\20260917\smartCAD\CAD_Process_Hub' `
  --output 'runs\my_smoke\design_only.json'
```

原始 DWG 仍先使用已有系统提取；`prepare-design` 当前只适配可识别图号的外圈设计图，复用工作区中的原项目解析器。推理模型本身不依赖原项目、manifest 或 target 文件。

输出包含：工序/参数事件、对象/要求/来源 ID、作用角色、更新类型、确定性复制值、未执行的数值候选、预测几何成员、终止原因与文本截断诊断。`engineering_verified` 始终为 false，不能直接驱动机床或工艺卡生产。

## 7. 评价与断点续训

正式评价默认只使用 gold：

```powershell
& $python run.py evaluate `
  --checkpoint 'runs\experiment\checkpoints\best' `
  --manifest 'data\processed\multi_case\manifest.json' `
  --split test --output 'runs\experiment\test_metrics.json'
```

只有检查弱监督管线时才显式指定 `--include-weak`。训练集评价同时要求 `--split train --smoke`。没有可用 gold 的指标为 null，不返回 100%；参数标注非穷尽时不报告虚假的参数 precision。

评价使用自由生成后的工序编辑对齐，报告路线编辑距离、已知参数召回、字段集合匹配和来源跨度分桶；数值候选不算已经执行的加工参数。结果质量受参考标签质量影响。

续训写入新目录，`epochs/max-updates` 是累计预算：

```powershell
& $python run.py train `
  --manifest 'data\processed\d207nq01\manifest.json' `
  --config 'configs\smoke.json' `
  --resume 'runs\my_smoke\checkpoints\last' `
  --output 'runs\my_smoke_resumed' `
  --epochs 6 --max-updates 6 --smoke
```

恢复模型、优化器、随机状态和数据游标；拒绝数据、词表、架构及监督设置不一致的续训。已测试 CPU 中断恢复与连续训练权重一致。

## 8. 测试与研究规划

```powershell
& $python -m unittest discover -s tests -t . -v
```

合成测试不依赖工业数据；真实试点集成测试在相应本地数据/权重不存在时显式跳过。覆盖输入泄漏、族划分、缺失标签掩码、人工标注、未来/同工序来源屏蔽、梯度、完整路线学习、checkpoint 哈希、恢复一致性和无 target 的 CLI 推理。

完整研究规划见 [关联生成方案](../docs/2-process-chain-model-plan.md)。后续优先补充人工分组、要求绑定、工序作用和来源实例标签，建立独立案例划分，再扩展规范执行接口、预训练语义编码和变化一致性训练。不要把本次 CPU smoke 权重作为论文性能结果。
