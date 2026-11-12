# T2 输入、监督与人工标注协议

版本：`farmcad-t2-v1`。具体校验实现见 [schema.py](../src/farmcad/data/schema.py)。

该设计/目标协议继续用于 GRU 基线和新局部模型训练。主线混合推理另有 `farmcad-hybrid-v1` 请求协议及 `farmcad-memory-v1` 持久记忆协议，见 [混合架构说明](hybrid-architecture.md)。混合推理不把本文件中的 target 导入运行数据库。

## 输入与目标分离

`design.json` 仅允许以下顶层字段：

| 字段 | 内容 |
|---|---|
| schema_version / case_id | 格式及回溯标识；case_id 不参与神经编码 |
| entities | ID、类型、12 维几何数值、原始文字 |
| edges | `[source_index, target_index, relation]`；0 自环、1 邻近、2 共端点 |
| features | 设计参数候选 ID、文字、值与单位、原始标注实体锚点 |
| requirements | 有证据的设计尺寸区间候选、对应候选和标注索引 |
| context | 设计图可见材料、产品名称、技术要求和精度文字 |

禁止工序路线、待预测参数、复制来源、真实未来状态、人工几何 group 和加工 Flag。候选原始标注锚点来自设计解析，不是人工几何分组。`validate_design` 拒绝未知顶层字段和候选中的额外答案字段。

原图参数候选和尺寸要求是先验提取结果；当前模型在这些候选上学习关联和几何成员，不声称已经实现开放世界的特征/功能意图发现。

## 目标事件

`target.json` 含 reviewer、feature_members 和以下事件流：

1. OP：工序名称、参数覆盖是否完整、事件监督质量。
2. PARAM：参数名及字段级标签。
3. END_OP：结束当前工序。参数覆盖未知时质量为 unknown。
4. EOS：路线结束。

每条 PARAM 的 labels 必须有：

| 标签 | 值 |
|---|---|
| features | 候选 ID 列表，或 null |
| requirements | 要求 ID 列表，或 null |
| sources | 更早工序的 PARAM 事件 ID 列表，或 null |
| roles | machining_target / reference_copy / inspection_target / datum_use 的集合，或 null |
| update | unknown / design_copy / state_copy / external / lookup / derive |
| value | nominal/lower/upper/direct/unit，或 null |

标签形状为 `{"value": ..., "quality": "gold|silver|system|candidate|unknown"}`。工序和参数 ID 是回溯键，不作为语义嵌入。

**缺失与不存在不同：**

- `null`：未标注，屏蔽相应监督。
- `[]` 或 `["NONE"]`：确认无对象/来源，只有有依据时才这样标。
- `["UNKNOWN"]`：明确要求模型表达不确定。
- `["EXTERNAL"]`：来源在当前可寻址状态集合外。
- 特殊指针不能与具体对象混在一个集合里。

source 指针不得指向当前工序、未来工序或不存在的状态。单个集合超过配置的 `pointer_slots` 时训练报错，不静默截断。

参数 `value` 中的 lower/upper 是相对偏差，不是绝对区间端点；设计 `requirements` 的 lower/upper 则是已计算的绝对区间端点。缺失不补 0。单位仅 mm/um/unknown；原系统中未审核的直接显示值保留为 unknown，不因字段名带 `_mm` 就宣称毫米。

## 人工标注

从已生成的 `annotations.template.json` 复制一份。下例仅示意格式，不是实际 D207NQ.01 专家标注：

```json
{
  "schema_version": "farmcad-t2-v1",
  "case_id": "example",
  "reviewer": "实际审核人员标识",
  "feature_members": {
    "diameter": ["L1", "L2"]
  },
  "event_overrides": {
    "state-2": {
      "features": ["diameter"],
      "sources": ["state-1"],
      "roles": ["reference_copy"],
      "update": "state_copy"
    }
  }
}
```

规则：

- 输入的 case、候选、实体、事件 ID 必须已存在。
- reviewer 不能为空；只将实际填写的字段提升为 gold。
- feature_members 是完整成员集合，训练 BCE/Dice 时其它实体为负例；不要把未标全的部分集合当完整 gold。
- OP 可覆盖 operation 或 parameters_complete；确认完整/不完整同步调整 END_OP 监督。
- 人工标注仅改变 target；不会将 gold 几何成员复制进 design。
- 本版模板适用于校正既有事件。新增/删除工序、补充未生成参数时，需在新数据版本中按完整 target 协议编辑事件流，运行 validate-data；没有提供可视化标注器。
- 程序能核验引用和格式，不能代替专家判断“是否真的加工了某个特征”。

## 多案例 manifest

每个零件/图纸族指定一个 split；同族所有工序图、A/B 改版和增强样本必须同 split。`family_id` 要人工确定，不从文件名自动猜测。

```json
{
  "schema_version": "farmcad-t2-v1",
  "cases": [
    {"case_id":"A","family_id":"family-A","split":"train","design":"A_design.json","target":"A_target.json"},
    {"case_id":"B","family_id":"family-B","split":"validation","design":"B_design.json","target":"B_target.json"},
    {"case_id":"C","family_id":"family-C","split":"test","design":"C_design.json","target":"C_target.json"}
  ]
}
```

文件路径相对 manifest 所在目录。校验重复 case、跨 split 同族和除 case_id 外完全相同的输入；该检查不能自动识别所有几何等价副本，族标注仍然必要。

词表仅由 train 构建。正式训练需要独立 validation；`--smoke` 是显式例外，不自动复制一个样本伪造 validation/test。新增人工标注后数据 fingerprint 改变，旧 checkpoint 不允许以相同实验续训，需要启动新实验。

## 已实现与后续扩展边界

- 可以训练参数候选的几何成员、多对象/多要求指针、多来源指针、作用角色和数值候选。
- 稳定物理身份的合并、开放特征发现、基准角色完整绑定仍需补数据和扩展 schema。
- 参数状态是计划记录，不等于实际工件状态。
- 模型采用监督 teacher forcing，但自由预测只能访问设计信息与自身生成历史。
- 任意 source_paths、工序规则 copy_from 和计算得到的目标值均不能作为当前待预测事件的输入。
- lookup/derive 的候选数值不进入已执行值；未来应通过受控规范执行器填充并附带出处。
- 尚未完成校准；sigmoid/softmax 仅为模型分数，不是工程可靠性保证。
