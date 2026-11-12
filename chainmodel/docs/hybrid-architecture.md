# 小模型、独立记忆与 LLM 的混合架构

2026-11-02 迁移交接更新：Qwen 下载已完成，15 个分片的索引、头部与文件长度已核对；尚未加载推理，代码仍为 placeholder。本文的占位行为未改变。最新环境事实、迁移路径问题和真实后端接入任务见 [服务器交接备忘](../SERVER_HANDOFF.md)。

## 1. 实现与边界

本版将存储、表征、寻址和规划分开。LLM 尚未实际接入；本文件描述已实现的接口，不声称 Qwen 已加载或产生工艺推理结果。

| 模块 | 实现 | 不承担什么 |
|---|---|---|
| 局部模型 | `models/local.py`：设计 GNN/文本编码、几何成员、工序条件查询、关系/角色/更新/数值监督头 | 不生成整条路线，没有 GRU/LSTM |
| 精确记忆 | `memory/store.py`：SQLite 设计输入、工序、参数记录、绑定、来源、请求与审核日志 | 不以向量代替原数值，不将提案升级为人工真值 |
| 寻址器 | `memory/addressing.py`：独立可学习多槽特征/要求/来源指针 | 不拥有或修改数据，不自行执行加工 |
| 推理控制 | `reasoning/runtime.py`：分页查询、局部预测、受预算限制的请求、检查与事务提交 | 不接收自由生成的数值作为执行结果 |
| LLM 接口 | `reasoning/backend.py`：PlannerBackend / PlannerReply / QwenPlaceholder | 不读取部分下载文件，不下载依赖，不调用外部接口 |

旧 `models/model.py` 仍是 GRU 基线；不删除原 checkpoint。共享训练工具新增 `architecture=local|gru`；缺少该字段的旧 checkpoint 按 gru 加载，不能把两种权重混用或跨架构续训。

## 2. 记忆、表征与稳定地址

一个数据库只容纳一张设计图的一条方案分支：

- `meta`：原始 design 输入、设计哈希、plan_id、revision、配置、局部 checkpoint 哈希和成员候选。
- `operations`：工序实例及序号。同名工序可以重复，实例 ID 不重复。
- `states`：精确保留输入 JSON 数值的参数记录、单位、缺失字段、作用和来源状态；不会通过向量反解数值。
- `bindings / sources`：对象绑定及前序来源关系。
- `requests`：已发请求、离线响应及工具结果。
- `audit`：局部模型输出、接受/拒绝的决策与关闭记录。

状态地址在 plan_id 内唯一；跨数据库使用时必须同时携带 plan_id。不支持把另一个方案的状态直接作为当前内部来源。外部来源保持 external，待补外部证据接口。

向量由局部模型从记录重新编码，留在模型侧；数据库事实层导入不依赖 PyTorch。向量不是权威数据，也不作为可执行数值。计划执行期间固定 checkpoint 元数据/权重哈希；更换模型要新建计划，避免使用过期表征。

现有 feature_id 仍是设计参数属性候选，不等于完成了物理对象合并。成员结果默认仅向 LLM 提供每个候选排名前三的几何实体，分数未经校准，不是假装完整的人工分组。

## 3. 小模型如何训练和预测

局部模型复用设计编码器，但无循环路线解码器：

```text
设计上下文 + 查询工序 → MLP 查询 → 参数候选
                            + 查询参数
                                 ↓
                 特征/要求/来源指针、角色、更新与数值监督
```

历史记录表示由对象、要求、工序/参数、值、单位和角色构成，不依赖旧 GRU 隐状态，也不读取未来记录。

训练继续使用现有 manifest 和人工标签：

- OP 提供局部任务条件；每条 PARAM 构成一个局部监督问题。
- 不计算路线 action/operation 损失，不声称训练了 LLM 的路线规划能力。
- 参数名称分类头在加入查询参数嵌入之前计算；指针等头以查询参数为条件。
- 参数记录在该步预测之后加入历史；当前工序状态对来源指针屏蔽。
- 原分字段质量权重、缺失标签掩码、几何 BCE/Dice 保留。
- 局部数值头可以训练，但其输出不供状态提交执行。执行只读取事实或未来接入的计算工具。

当前候选指针之间仍主要通过共享表征学习联系，提交层另做对象/要求/来源兼容性检查；尚未实现复杂的联合结构能量模型。来源检索先按指定对象缩小候选；允许 feature_id=null 在全历史中寻址。超过候选预算报错，不静默遗漏历史。

`evaluate` 加载 local checkpoint 时报告条件局部指标：已知工序/参数查询、标注历史下的字段匹配，不是端到端 T2 路线准确率。默认只评 gold，无可评标签返回 null。`--include-weak` 是显式弱标签检查。

## 4. LLM 只接收决策所需信息

初始请求不包含全部实体图或完整参数历史，而包含：

- 设计 ID/hash、plan_id、revision、request_id；
- 分页设计候选和少量模型成员候选；
- 分页工序/参数词表；
- 最近三道已提交工序及总数；
- 当前工具结果及小模型来源；
- 工具协议、占位状态和预算说明。

可继续调用：

| 动作 | 用途 |
|---|---|
| list | 分页读取 features / requirements / operations / parameters |
| read | 按类型与 ID 读取 feature / requirement / entity / state |
| read_design_context | 按偏移读取技术要求等文字，每片最多 1000 字符，明确未读完位置 |
| query_states | 查询一个设计候选的历史记录，保留分页游标 |
| predict_local | 指定工序、参数及候选对象范围，获得局部预测 |
| commit_operation | 提交一道工序及其参数关联，不接受 value 字段 |
| finish | 关闭提案，仍需专家审核 |

数值和证据不经过 LLM 自由改写后再入库。局部预测的完整候选概率留在 audit 中，给 LLM 的结果只含选中 ID、每槽前两名和候选数量。它们是模型分数，不是经过校准的置信度。

寻址器的 NONE/UNKNOWN/EXTERNAL 是特殊选择，不是实际记录 ID。提交时不能将 UNKNOWN 或 EXTERNAL 写进 source_state_ids；来源不确定时应保持空来源和 unknown/external 更新类型。没有可靠对象绑定时，不能提交该对象的计划变更。

上下文限制对实际序列化请求的 Unicode 字符数计量，**不是 token 数**。超限明确报错；不从尺寸、公差、要求中静默删字段。将来接入真实 tokenizer 后才可做 token 预算验证。

## 5. Qwen 占位与离线响应

[hybrid.json](../configs/hybrid.json) 将用户提供的模型标签设为 `qwen3.6-27B`，路径为 `E:\AAhz\20260918\intentionCAD\llm`。

当前不会检查或打开目录中的权重，因此不会干扰下载。返回：

```json
{
  "status": "awaiting_llm",
  "backend": {
    "backend": "placeholder",
    "weights_loaded": false,
    "inference_ready": false
  },
  "engineering_verified": false
}
```

为了在模型不可用时验证工具闭环，提供 `hybrid-step` 接收明确的离线 JSON 响应。这是人工/测试响应入口，日志标为 offline_response；不冒充 Qwen 输出。

从当前 pending 请求复制四个版本字段，填写一个动作。例如以下值中的 PLAN_ID 必须替换为实际值；不要把示例当真实响应直接提交：

```json
{
  "schema_version": "farmcad-hybrid-v1",
  "plan_id": "PLAN_ID",
  "request_id": 0,
  "revision": 0,
  "action": "list",
  "payload": {"collection": "requirements", "offset": 0}
}
```

```powershell
& $python run.py hybrid-step --session 'runs\my_hybrid' `
  --decision 'runs\my_hybrid\decision-0.json' --output 'runs\my_hybrid\response-0.json'
& $python run.py hybrid-pending --session 'runs\my_hybrid'
```

读取动作也会消耗一次 request_id，但不会修改状态 revision。提交工序同时推进 revision。陈旧版本、不同 plan_id 和已消费请求均被拒绝。拒绝后保留原待处理请求，允许纠正。

下载完成后仍需实现本地加载或服务适配器、消息格式、结构化输出解析及实际 token 计量，再验证推理环境；当前没有 Transformers/vLLM 依赖，也未假定该模型的具体 API 或所需显存。

## 6. 状态提交与工程边界

一条工序含若干参数记录。提交前检查：

- 工序/参数在局部模型词表中；
- 对象与要求存在，要求属于绑定对象；
- 来源是当前计划中的前序工序记录；
- 同工序不得对同一对象的同一属性提交相互覆盖的多条变更；
- design_copy 只有一个设计候选且无状态来源；
- state_copy 只有一个来源，且对象绑定集合和参数名称相同；
- 禁止传入任意 value 字段绕过执行器。

跨参数别名复制当前保守拒绝，需后续增加已审核的语义映射，不能仅靠 LLM 认定两个符号相等。

全部参数校验、记录写入、版本推进、响应消费和下一请求创建在一个 SQLite 事务内完成。拒绝或新请求预算溢出时回滚，另记拒绝日志。数据库为事实来源；若事务提交后导出 JSON 失败，可以用 hybrid-pending 恢复，不要重放旧响应。

记录区分：

- planned_change：计划加工改变；更新对象当前计划状态索引。
- planned_observation：计划检测记录；不覆盖当前物理属性计划状态。
- reference：引用记录；同样不覆盖。

所有历史仍保留。lookup/derive/unknown/external 保留 unknown 值和待执行状态，未接工艺规则执行器。设计复制只是保留原设计候选值，不代表该值适用于当前加工阶段。

finish 只表示提案关闭，返回全部尚未工程核验的要求；不因链接了要求或数值落入区间就宣布要求满足。达到 max_steps 时返回 max_steps_pending_review，不伪造 EOS 或工艺成功。

## 7. 暂未实现的研究扩展

- 实际 Qwen 推理、自动多轮 LLM 规划及工具调用训练；
- 跨案例检索库、向量 ANN 数据库、自动跨设计物理身份对齐；
- 分支搜索、A/B 变更影响重规划；
- 规范查表、留量公式、工序前置条件完整执行器；
- 开放工序词表、预训练文本编码、模型置信度校准；
- 独立真实数据上的端到端 T2 评价。

目前提供的是可训练局部模型与可核验的工具/记忆骨架。现有单案例不能证明工业泛化，系统弱标签不能自动升级为 gold。

## 8. 本轮实际验证

- 全项目 54 项 unittest 通过，无失败、错误或跳过；包含原 GRU 基线的行为回归。
- 合成监督小样本验证：局部模型能学会特征、要求、来源和更新标签，不含路线生成指标。
- SQLite 重开后数值和来源不变；重复工序与长距离引用不覆盖历史；检测记录不改变当前计划属性。
- 提交失败、上下文超限均验证了事务回滚；陈旧/重复请求被拒绝并留下日志。
- 局部训练中断恢复与连续 CPU 训练权重一致。
- 真实 D207NQ.01 局部模型三次更新：loss 2.239096 → 1.945626，仅为 smoke。
- 真实 hybrid 初始化仅从 design 建库，未导入 target 中的工序/参数状态。
- 初始请求为 4956 个序列化字符，离线局部预测后的请求为 7782 个字符，均小于配置的 16000 字符预算。
- 真实案例的离线检查只做局部查询，没有提交工序；数据库有 2 个请求，仍停在 awaiting_llm。
- 真实 gold 评价的字段指标均为 null，因为尚无可用人工 gold；没有输出虚假的完美结果。

产物在 `runs\local_smoke_d207nq01` 和 `runs\hybrid_d207nq01`。后者的 verification.json 保存了测试结果、数据/源码一致性和请求预算检查。

原 `runs\smoke_d207nq01` 属于之前 GRU 版本，原有验收记录是当时的快照，不代表现在源码哈希仍与旧训练完全相同。旧权重已通过本轮兼容性测试，不覆盖旧实验。
