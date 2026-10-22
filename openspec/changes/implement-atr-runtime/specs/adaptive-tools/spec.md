# Spec Delta

## Purpose

把真实执行过的独立程序作为可按需查找的候选，通过复用需求、版本验证及有效调用频率逐步固化，同时控制过期工具和依赖关系，使工具库增长有明确边界和可解释的状态变化。

## ADDED Requirements

### Requirement: Independent behavioral admission
Tool Review SHALL 使用绑定不可变程序和环境的宿主验收案例。模型自拟检查 SHALL NOT 单独授予准入；固定历史版本失败 SHALL NOT 替换无关的活跃版本。

#### Scenario: Self-authored checks conceal a backdoor
- **WHEN** 程序能通过自拟检查但没有可信验收
- **THEN** Review 拒绝准入并记录缺少可信证据

### Requirement: Retrieval before creation
系统 SHALL 对 Tool 和 Candidate 先精确再模糊检索，过滤作用域、环境、输入和副作用；普通命令不捕获，歧义事件最多一次模型路由。

#### Scenario: Duplicate program
- **WHEN** 新独立程序已执行且随后出现兼容需求
- **THEN** 可命中候选并复用，创建接口返回已有等价定义而非重复注册

### Requirement: Reviewed immutable versions
系统 MUST 保存不可变版本及固定版本依赖，新版本通过实际契约用例才能切换，失败保留旧版，依赖不得循环或扩大权限。

#### Scenario: Failed upgrade
- **WHEN** 新版本输出不符合契约
- **THEN** Review 失败，旧版本仍为活跃版本，允许原子回滚至已验证版本

#### Scenario: Queued version selection
- **WHEN** 已接收的调用正在排队，而活跃指针被切换或旧版被请求删除
- **THEN** 运行保留接收时固定的版本，未完成运行保护该版不被删除

### Requirement: Frequency lifecycle
系统 SHALL 默认以固定14天、5次有效使用且2个根任务晋升；可配置同任务10个不同输入例外；失败、缓存和重试不计入。到期归档30天，持久工具90天闲置休眠，恢复必须复检。

#### Scenario: Reference protection
- **WHEN** 到期子工具被活跃上层工具固定依赖
- **THEN** 不作为默认检索结果，固定依赖仍可执行且托管副本不可物理删除

#### Scenario: Workflow retry lineage
- **WHEN** 工作流带有 retry_of 或 cache_hit 标记
- **THEN** 所有后代保留该属性并不计入有效晋升调用，适配器使用父调用剩余时间

#### Scenario: Same task exception
- **WHEN** 多个根任务的不同输入合计达到同任务例外阈值，但任何单个任务都未达到
- **THEN** 不通过该例外晋升；只有单个根任务达到阈值时才能适用

### Requirement: Bounded repair attempts
系统 MUST 对失败版本后续修复执行根任务级 max_repairs 上限；耗尽后返回明确失败且不注册额外版本。

#### Scenario: Repair budget exhausted
- **WHEN** 同一根任务已用完失败版本的修复额度
- **THEN** 后续修复创建被拒绝，已有版本和 Direct 路径保持可供主 Agent 选择

### Requirement: Control switches
系统 SHALL 支持暂停自动创建、清空候选及关闭动态执行，Direct 路径仍接受同等安全检查。

#### Scenario: Dynamic execution disabled
- **WHEN** 操作者关闭动态执行
- **THEN** 新动态运行被拒绝，正在运行的动态任务收到取消，Direct 仍可使用
