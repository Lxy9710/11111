# 人工标注目录

2026-11-03新增：[数据集构建分工计划与可填写Excel](dataset-build-plan-20261103/README.md)。
后续采用“系统先生成候选与证据，人工审核歧义”的分工；不要求人工从零填写全部路线和语义标签。

将 `data\processed\<case>\annotations.template.json` 复制到本目录，保留原模板，填写真实审核人员和实际确认的字段。

不要直接修改 `design.json` 来放入人工几何 group 或工艺答案。人工标签通过 `prepare --annotations ... --output <新目录>` 合并到 target，数据版本与已有实验隔离。

目前没有实际人工审核完成的 D207NQ.01 标注文件。字段说明和示例见 [数据协议](../../docs/data-contract.md)。
