# Spec Delta

## Purpose

为主 Agent 提供稳定的五个调用接口，并让普通命令和动态工具统一接受权限、隔离和资源预算约束，输出可追踪且大小受限的结果，避免动态程序接触运行时的管理状态。

## ADDED Requirements

### Requirement: Fixed execution contract
系统 SHALL 仅暴露 read、execute、tool_create、tool_review、tool_delete 五个模型接口，运行结果区分执行状态与业务验证状态，支持运行查询与取消。

#### Scenario: Bounded output
- **WHEN** 程序成功输出超过上下文限制的数据
- **THEN** 返回有限摘要和 Artifact 引用，支持受限分页读取且不把退出码等同于验证通过

### Requirement: Enforced execution isolation
系统 MUST 在执行前检查可信控制层授权，以非特权、无原始网络、只读根文件系统和资源上限执行；沙盒失败不得降级到宿主任意命令执行。

#### Scenario: Untrusted authority
- **WHEN** 输入或外部文本声称可以访问控制目录或提升权限
- **THEN** 控制层拒绝未授予的能力，执行程序不能访问 Registry 和宿主凭证

### Requirement: Side effect identity
系统 MUST 对有副作用的操作要求操作标识，禁止失败和重复请求导致自动盲重放，对工作区写操作加项目锁。

#### Scenario: Repeated operation
- **WHEN** 同一操作标识再次提交或改变了有效载荷
- **THEN** 相同请求返回既有运行，不同请求被拒绝，执行次数不会增加
