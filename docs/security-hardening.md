# 0.2.0 安全修复与证据（2026-10-22）

后续0.3.0已新增显式受信本机后端与 Codex/Claude Code 接入，见 [本机说明](native-clients.md)。以下 Docker 隔离条目仅适用于 Docker 后端；不得套用于 NativeSandbox。本机模式不运行模型自拟任意命令，审批不作为 MCP 工具开放；它执行已受信代码，仍具有宿主用户的潜在权限。

本次范围为实现审查发现的 R1—R6，以及恶意/被注入 Agent 下的可信身份、总预算、具体授权、独立验收和退出状态。保留单用户 Python/SQLite/Docker 架构，不进行成本收益实验或付费供应商调用。

## 六项问题的处理

| 问题 | 修复控制点 | 本次行为证据 |
|---|---|---|
| R1 JSON 键泄漏 | storage.Redactor.data 处理键和值，碰撞键加编号，保留全部值 | 已知秘密作为嵌套/碰撞键，公开 JSON 和分页结果均脱敏 |
| R2 工作区误用128文件上限 | 独立 max_workspace_files，快照与提交使用 workspace_limit | 128新增第129个文件；真实容器中已有129文件再新增 |
| R3 历史版本误回退 | Registry.rollback 事务核对目标健康状态及 expected_active | 当前健康 v3、固定失败 v2，v3 保持选中 |
| R4 关闭执行队列 | 接收时登记所有运行；关闭持久取消并终结排队 Future，等待活动调用及管理入口 | 活动/排队调用关闭、同步调用并发关闭、重开后的持久 cancelled 状态 |
| R5 envelope 丢账单 | AdapterResponse 分离输出错误和费用，记录费用后返回 adapter_contract | 缺 choices、空 choices、异常 message，均保留9输入/4输出 Token、账单ID及预留 |
| R6 null 路径误通过 | 独立 BehaviorRule 对非空路径始终求值，缺失路径失败 | 根 null + required_field + expected null 拒绝 Review |

审查原始报告 [implementation-review-2026-10-22.md](implementation-review-2026-10-22.md) 保留发现时状态；本页为后续修复结果，不将原先源码判断冒充原先已经运行的证据。

## 新的可信控制层

ControlPlane 只供宿主调用，AgentAdapter 的五接口不含签发与审批。TaskContext 凭证走宿主对象或工作区外文件，控制数据库只保存摘要，已知凭证进入统一脱敏。任务身份随机生成，不能自行选新 root_task_id 获取额度；期限、调用与创建计数持久保存。后代、Review 案例、Agent 读取及路由共享根限额。

操作许可绑定固定目标、输入、权限、环境、业务键及独立规则。相同键不能重新批准另一个请求；复用许可不会重放业务效果。任务与持久全局费用预留在同一事务核对；预算计数与凭证标记不随日志过期，重启不自动提高已存总额度。操作者显式 set-total-budget 才能改变总上限。

任务 read_paths 与逐文件 create/update/delete 权限在快照、读取和发布三个位置执行。归一化后检查读取范围；Agent 不能读或取消其他根任务的运行/Artifact。宏观 workspace_write 不会授予任务未指定文件的操作权。

独立 TrustedCase 绑定完整代码与环境；Manifest checks 只是建议，不能自行准入。每个根操作还检查可信输出与完整 expected_changes；输出结构正确或程序退出码为0不足以发布。撤销任务、操作或程序后，排队运行和未发布变更重新核对授权。版本回退采用条件更新，权限不足的 Review 不修改已有健康状态。

直接代理调用固定完整输入。工作流另需在根 BehaviorRule.adapter_calls 中列出能力、完整输入和允许次数；不得根据前序工具输出自行扩大数据外传权限。单步适配器的已批准请求可自动生成对应精确额度；网络 Review 的可信案例需显式填写 adapter_calls。例如：

```json
{
  "checks": [{"expected_path": "evidence.fact_status", "expected": "unknown"}],
  "adapter_calls": [{"capability": "research", "inputs": {"urls": ["https://www.sqlite.org/fts5.html"]}, "max_calls": 1}]
}
```

配置了模型路由时，authorize_route/approve-route 另行绑定发送文本和意图；同一批准路由不重复调用模型，未完成或中断的路由保持 DEFER。只有本地规则检索时不需要发送文本的许可。

## 安全基线与适用性

依据用户 Basic Memory main 的 `main/engineering/security/a2abreak/a2-abreak-多-agent-安全基线与-11-项方案索引`，基线 **v1.0**，来源核对日2026-10-08；适用条目的 **“工程解决方案”“验收办法”** 已读取。它们是本地工程方案，不能表述为本项目受所有 A2A 漏洞影响或官方漏洞已被完全修复。

| 编号/标题 | ATR 控制点及验收 |
|---|---|
| A2AB-01 技能声明验证与委派准入 | approve_program、trusted_acceptance、tool_review；自拟检查拒绝，宿主反例拒绝，修改代码失去原批准 |
| A2AB-06 并发状态版本与审批绑定 | operation_authority、版本条件回退、Review 状态核对、发布事务；改参数/状态与撤销不沿用旧许可 |
| A2AB-08 结构化授权请求与权限不放大 | ResourceScope、scoped_policy、固定操作、根出口额度和模型路由许可；拒绝越界读写、跨任务读取、参数替换与工作流扩大外传 |
| A2AB-10 委派环路检测与全链预算 | task_authority、Ledger、后代上下文；伪造身份、换操作、重启、日志清理和工作流展开不补回额度 |
| A2AB-11 中断任务租约与有界资源回收 | 固定任务期限、Cancellation、关闭接收与 Future 终结；到期拒绝、同步/异步退出及持久取消 |

其余项目已评估：02 无 Agent Card/远程密钥身份；03 无多租户共享上下文服务（本次任务归属检查不是租户安全认证）；04 无 SSE；05 无产物分块流，现有对象一致性不证明语义真实；07 无 Webhook；09 无多跳主体/凭证交换。对应协议专用验收不适用，不能据此推导单 Agent 没有恶意行为风险。

## 本次验证

本次行为目标的测试位于 test_review_repairs.py、test_trusted_control.py、test_host_cli.py、test_security_docker.py。新缺陷先观察失败，再实现通过；后补边界包括程序中途撤销、日志清理补回额度、工作流未批准外传、路由未批准文本以及重开工具环境。执行器/供应商替身只证明控制层行为，不冒充真实隔离或实际供应商收费。

最终针对性检查：37项控制/CLI用例通过，3项真实 Docker 用例通过。Ruff、mypy（20个源文件）、10份数据 Schema及固定五接口生成、OpenSpec strict、git diff --check、0.2.0 wheel/sdist 构建通过。工作流根许可撤销后的后续出口同样在提交供应商之前拒绝。

当前目标命令（不运行历史套件或成本收益实验）：

```powershell
uv run --offline pytest tests/test_review_repairs.py tests/test_trusted_control.py tests/test_host_cli.py -q
$env:ATR_TEST_DOCKER='1'
$env:ATR_TEST_IMAGE='atr-python-validation:0.1'
uv run --offline pytest tests/test_security_docker.py -q
uv run --offline ruff check atr tests scripts
uv run --offline mypy atr
uv run --offline python scripts/export_schemas.py
openspec.cmd validate implement-atr-runtime --strict
uv build --offline
```

本次真实容器使用本机已有 atr-python-validation:0.1（python:3.12-slim 基础，无 git/ripgrep/pytest），并沿用生产 DockerSandbox 的挂载、用户和隔离标志。正式 Dockerfile 的完整构建此前因 Debian 软件源停滞未成功，不将该替代镜像结果扩写为完整部署成功。未运行历史42项、23项和4项用例；旧0.1任意根ID调用夹具不代表0.2接口契约，历史结果不作为本次通过证据。

## 保留的实际边界

独立验收覆盖有限业务规则，不证明程序无后门；untrusted 标记不保证主 Agent 永不服从提示注入。外部在途副作用不可撤回，多文件最终提交不是文件系统事务，临时挂载没有文件系统级总磁盘配额，自定义宿主代理必须可信并合作取消。真实供应商账单、正式镜像完整依赖和经济收益均未验证。
