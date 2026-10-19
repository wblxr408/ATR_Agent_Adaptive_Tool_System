# Spec Delta

## Purpose

把真实执行过的独立程序作为可按需查找的候选，通过复用需求、版本验证及有效调用频率逐步固化，同时控制过期工具和依赖关系，使工具库增长有明确边界和可解释的状态变化。

## ADDED Requirements

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

### Requirement: Frequency lifecycle
系统 SHALL 默认以固定14天、5次有效使用且2个根任务晋升；可配置同任务10个不同输入例外；失败、缓存和重试不计入。到期归档30天，持久工具90天闲置休眠，恢复必须复检。

#### Scenario: Reference protection
- **WHEN** 到期子工具被活跃上层工具固定依赖
- **THEN** 不作为默认检索结果，固定依赖仍可执行且托管副本不可物理删除

### Requirement: Control switches
系统 SHALL 支持暂停自动创建、清空候选及关闭动态执行，Direct 路径仍接受同等安全检查。

#### Scenario: Dynamic execution disabled
- **WHEN** 操作者关闭动态执行
- **THEN** 新动态运行被拒绝，正在运行的动态任务收到取消，Direct 仍可使用
