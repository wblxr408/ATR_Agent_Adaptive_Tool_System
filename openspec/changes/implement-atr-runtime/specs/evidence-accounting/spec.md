# Spec Delta

## Purpose

为执行、管理、研究和模型调用保留可解释的结构化证据，分别计量 Token、货币、时间和质量，以公平的配对任务对照评估工具复用收益，保留不足数据和不确定结论的真实边界。

## ADDED Requirements

### Requirement: Bounded real weekly observation
系统 SHALL 在用户显式启用后，固定7天采集窗口及此前7天观察基线；客户端累计计数与重复消息 SHALL 去重，日志正文 SHALL NOT 持久化到探针库。期满 SHALL 停止新增探针事件，报告区分缺失、实测数值和未知费用，不把观察总量变化归因为ATR净收益。

#### Scenario: Duplicated or partially appended usage
- **WHEN** Codex 重复累计用量、Claude 重复同一消息、或日志末行尚未写完
- **THEN** 不重复计入，末行补全后仍能采集；大行受限跳过但继续收集后续用量，并记录覆盖缺口

#### Scenario: End of the week
- **WHEN** 固定窗口结束或操作者停用
- **THEN** 不再新增探针事件；期满报告保留成本/质量对照未知，清理仅属于探针的收尾hook并保留ATR功能

### Requirement: Trusted aggregate budget and failed response evidence
费用预留 SHALL 原子执行持久全局和宿主签发的任务预算。供应商已知用量及账单身份 SHALL 在响应异常时保留。JSON Artifact 的键和值 SHALL 脱敏，碰撞键不得静默覆盖。

#### Scenario: Rotated identity cannot renew funds
- **WHEN** 调用者伪造任务ID，或已签发任务耗尽聚合额度
- **THEN** 不提交供应商请求，未知费用预留继续占用额度

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

#### Scenario: Pending billing retention
- **WHEN** 普通聚合日志达到保留期，但调用尚未取得实际账单
- **THEN** 保留该调用的费用及 Token 和预留供结算使用，报告仍显示未完成计费

#### Scenario: Budget rejection before routing model
- **WHEN** 路由预算拒绝调用或供应商凭证重复提交
- **THEN** 未调用模型时记录零次调用，相同凭证只产生一次计费，不同内容复用同一凭证标识时拒绝

#### Scenario: Structured output redaction
- **WHEN** JSON 输出包含凭证值或 null 敏感字段
- **THEN** 按结构脱敏并保留合法 JSON 与 null，后续工作流能够解析公开结果

### Requirement: Fair paired evaluation
评测 SHALL 对齐任务、模型、权限、预算和冷热阶段，以质量为前提报告成本、Token、时延、检索和失败指标，不得伪造真实工作样本。

#### Scenario: Unmatched observations
- **WHEN** Baseline 与 ATR 的模型或任务预算不同
- **THEN** 拒绝将这些记录作为公平配对收益证据
