# Spec Delta

## Purpose

为执行、管理、研究和模型调用保留可解释的结构化证据，分别计量 Token、货币、时间和质量，以公平的配对任务对照评估工具复用收益，保留不足数据和不确定结论的真实边界。

## ADDED Requirements

### Requirement: Research provenance
系统 SHALL 通过控制层配置的只读网络适配器获取研究数据，保存来源、检索日期和新鲜度，区分抓取、结构验证及事实核验；模型调用受能力和预算约束。

#### Scenario: Repeated research
- **WHEN** 再次运行相同研究流程
- **THEN** 按新鲜度策略重新获取来源，结果保留引用，事实状态默认为未核验

### Requirement: Auditable accounting
系统 SHALL 记录任务、路由、创建、Review、直接及嵌套运行和生命周期事件，脱敏日志及输出；费用覆盖全部类别，未知计费项不得默认为免费。

#### Scenario: Partial billing
- **WHEN** 某次模型调用没有供应商账单数据
- **THEN** 报告标记费用不完整，保留已知 Token 与费用，不宣称实际净收益

### Requirement: Fair paired evaluation
评测 SHALL 对齐任务、模型、权限、预算和冷热阶段，以质量为前提报告成本、Token、时延、检索和失败指标，不得伪造真实工作样本。

#### Scenario: Unmatched observations
- **WHEN** Baseline 与 ATR 的模型或任务预算不同
- **THEN** 拒绝将这些记录作为公平配对收益证据
