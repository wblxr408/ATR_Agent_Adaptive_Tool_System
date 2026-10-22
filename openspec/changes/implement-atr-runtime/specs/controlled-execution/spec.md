# Spec Delta

## Purpose

为主 Agent 提供稳定的五个调用接口，并让普通命令和动态工具统一接受权限、隔离和资源预算约束，输出可追踪且大小受限的结果，避免动态程序接触运行时的管理状态。

## ADDED Requirements

### Requirement: Trusted task and concrete operation authority
Runtime SHALL 拒绝缺少宿主任务上下文或具体操作许可的执行；许可绑定完整请求、资源操作、期限和稳定业务键。所有后代 SHALL 共享可信根任务的聚合限额；模型接口 SHALL NOT 签发凭证或批准。

#### Scenario: Caller changes identity or request
- **WHEN** Agent 伪造根任务、更换操作ID、修改已批准输入或写未批准路径
- **THEN** 在发布工作区变更或提交供应商之前拒绝

#### Scenario: Legitimate bound retry
- **WHEN** 宿主已批准的同一操作不改负载提交两次
- **THEN** 返回同一持久运行，业务效果仅执行一次

### Requirement: Bounded shutdown and independent effects
Runtime SHALL 在关闭时取消队列、停止接收，并等待活动调用退出后关闭元数据。工作区发布 SHALL 通过可信输出及完整预期变更检查。

#### Scenario: Closing with queued write
- **WHEN** 一个写调用排在活动调用后且服务开始关闭
- **THEN** 排队运行终结为 cancelled，不产生写入

### Requirement: Fixed execution contract
Runtime 核心 SHALL 保留 read、execute、tool_create、tool_review、tool_delete 五个接口；客户端 MCP SHALL 使用收窄的检索/运行包装而不暴露宿主签发。运行结果区分执行状态与业务验证状态，核心支持运行查询与取消。

#### Scenario: Bounded output
- **WHEN** 程序成功输出超过上下文限制的数据
- **THEN** 返回有限摘要和 Artifact 引用，支持受限分页读取且不把退出码等同于验证通过

### Requirement: Enforced execution isolation
Docker 后端 MUST 在执行前检查可信控制层授权，以非特权、无原始网络、只读根文件系统和资源上限执行；沙盒失败不得降级到宿主任意命令执行。本机后端 SHALL 仅通过操作者显式选择并登记受信纯程序，不宣称具备 Docker 隔离。

#### Scenario: Untrusted authority
- **WHEN** 输入或外部文本声称可以访问控制目录或提升权限
- **THEN** 控制层拒绝未授予的能力，执行程序不能访问 Registry 和宿主凭证

### Requirement: Native client tools and advisory hooks
系统 SHALL 提供无需镜像的 stdio MCP，且本机执行只接受操作者登记的固定程序。UserPromptSubmit hook SHALL 在本地命中已验收内置工具时才注入有界提示，失败或未匹配返回空上下文，不调用模型。安装 SHALL 保留其他客户端设置并备份改前文件。

#### Scenario: Real inline transformation over MCP
- **WHEN** 客户端通过 MCP 对内置文本工具提交重复行
- **THEN** 返回实际保序去重结果及持久 run_id，同一连接同一业务键重试不重放

#### Scenario: Unknown code requests native execution
- **WHEN** 模型提交未登记工具 ID 或自由宿主命令
- **THEN** 本机执行入口拒绝，模型接口不签发程序信任

#### Scenario: Operator supplies incorrect acceptance
- **WHEN** 操作者登记的代码未满足独立案例
- **THEN** 拒绝写入受信 allowlist，现有可用工具继续工作

### Requirement: Side effect identity
系统 MUST 对有副作用的操作要求操作标识，禁止失败和重复请求导致自动盲重放，对工作区写操作加项目锁。

#### Scenario: Repeated operation
- **WHEN** 同一操作标识再次提交或改变了有效载荷
- **THEN** 相同请求返回既有运行，不同请求被拒绝，执行次数不会增加

#### Scenario: Root workspace transaction boundary
- **WHEN** 根调用或其固定依赖声明工作区写权限，随后执行或输出契约失败
- **THEN** 整个根调用的暂存文件不提交，项目锁覆盖所有写入子调用；Review 始终使用一次性工作区

#### Scenario: Candidate external effects
- **WHEN** 重复命中有外部写入的候选，或自动创建暂停/耗尽
- **THEN** 先检查权限、输入、操作标识和重试门禁；自动路由不执行外部写入 Review，保留受控 Direct 路径
