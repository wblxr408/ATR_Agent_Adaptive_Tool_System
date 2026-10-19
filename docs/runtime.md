# ATR 运行与接入

## 执行契约

`execute` 接收 JSON 参数数组 `argv`、独立 `program` 或固定 `tool_id/version`。程序通过 stdin 接收 JSON；结构化工具通过 stdout 返回一个 JSON 值。程序在 `/workspace` 工作，可读取 `/tool` 内的托管文件，将交付文件写到 `/out`。文本源代码置于 `files`；二进制程序和资源用 `binary_files` 的 base64 值保存。二进制必须适配所选 Linux 镜像的架构与 ABI。

结果区分 `running/succeeded/failed/timed_out/cancelled` 和 `passed/failed/unknown`。`validation_status=passed` 表示输出满足本版结构契约；Review 的预期输出用例提供额外证据，仍不能代替完整用户任务正确性的判定。没有输出契约时保持 unknown，不进入有效晋升计数。

stdout/stderr 受字节上限约束；摘要默认2000字符，大结果用 Artifact 引用。`read(run_output)` 参数为 `ref/offset/limit`，偏移和上限按字节，最多16000字节。`workspace_read` 和 `tool_source` 同样支持分页；先脱敏后切片，避免用偏移绕过脱敏。超大工作区文件由安全读取预算拒绝。

## 权限和隔离

可信 `Policy` 决定最大权限与资源，Manifest/请求只声明需求。默认可读取经过过滤的工作区，禁止修改工作区和外部写入。网络与凭证由命名适配器授予；程序容器始终没有原始网络或宿主环境凭证。

容器采用非 root、只读根文件系统、全部丢弃 capabilities、no-new-privileges、network none、CPU/内存/PID/时间/输出限制。运行时查询本地镜像不可变ID，并使用该ID执行，环境变化会拒绝旧版复用。该配置依据 [Docker run](https://docs.docker.com/engine/containers/run/) 与 [Rootless mode](https://docs.docker.com/engine/security/rootless/)；实际隔离及资源限制需要在部署引擎上验证。

控制目录、操作者配置和工具工作区分离。默认不复制 `.env`、凭证目录、私钥、`.git`、依赖缓存及 Agent 指令目录。部署时可通过 `excluded_names/protected_relative_paths` 增加业务敏感路径。运行时包本身若位于工作区，会自动受到保护。宿主符号链接和 Windows junction 不作为挂载内容。

每次执行使用过滤后的工作区快照。获得 `workspace_write` 授权的程序只修改暂存区，成功后校验目标路径和原始输入是否过时，再逐文件提交变更；保存 `workspace_changes.json` 与统一 diff。支持文件新增、修改与删除。多文件提交不是文件系统事务：进程/磁盘在提交中断时可能有部分变更，运行保持失败或不确定，不能盲目重放。默认排除 `.git`，因此沙盒内的 git 仓库操作需要专门准备经过审查的项目副本；系统不会自动挂载宿主 Git 凭证或执行 push。

源码与输出目录使用临时挂载。stdout/stderr 运行中限额，单文件写入由 fsize 限制，Artifact 总量在接收时限制；宿主暂存目录没有文件系统级总磁盘配额。需要更强磁盘隔离时，应将暂存目录放到受配额限制的卷中。Rootless/cgroup/内核配置也属于部署边界。

普通只读命令不产生候选。只有明确标记 `generated_independent=true`、有独立 Manifest 和实际成功执行记录的程序才捕获。业务源码修改默认不捕获。有副作用的调用必须带 operation_id；同一任务、同一标识、相同负载返回原运行，改变负载则拒绝。失败不自动重放；显式 retry_of 也不能绕过副作用门禁。

## Registry、Review 和生命周期

SQLite WAL 事务存储运行与操作身份，FTS5 检索候选及工具；精确匹配优先，返回兼容的前3项摘要。候选和创建共用检索与去重。FTS 使用参数化查询及转义的词项，参见 [SQLite FTS5](https://www.sqlite.org/fts5.html)。版本的源码、数据、Manifest 和契约用例以内容寻址保存，版本不原地修改。

检索和路由可传入期望 output_schema；当前采用与已声明输出契约相等的保守匹配，不推测任意 JSON Schema 的蕴含关系。输入值则按工具输入契约逐项验证；兼容性还包括项目、环境与副作用。

创建后必须 Review。用例包含 `inputs/expected`，可用 `expected_path` 检查研究结果中的稳定字段。Review 只执行已声明用例，不自动加入烟雾或既有回归测试。空用例拒绝准入；文件写入 Review 使用一次性工作区。外部写入 Review 还需独立的 `allow_external_review` 操作者授权。输出契约使用本地 JSON Schema，禁止远程引用；见 [jsonschema validation](https://python-jsonschema.readthedocs.io/en/stable/validate/)。

默认 Candidate 保留14天；Temporary 固定观察14天，至少5次有效使用、2个根任务，或同任务10个不同输入，且 Review 通过后晋升。失败、重试、缓存标签、重复操作和 Review 自身不计入。归档保留30天，持久版本90天无有效使用转休眠。恢复须复检并开启新计数周期。

工作流固定版本依赖形成有向图，创建时检查可用性、权限闭包和循环。父工具必须声明子工具全部权限及副作用。过期子版本退出默认检索，仍可供受保护的依赖链调用；有活跃引用或未结束运行的版本不能物理删除。物理清理仅处理控制目录中的对象，保留一小时发布宽限以保护并发写入。

serve 进程内每分钟执行本地生命周期维护，不消耗模型 Token。批处理 CLI 可执行 `maintenance`，持续运行用 `maintenance --watch`。停止维护进程不等于延长观察期；执行和检索入口也会处理到期状态。控制开关为 `auto_create/dynamic_execute`；`disable` 同时关闭两者并取消正在运行的动态任务。运行取消沿父子链传播，其他客户端通过同一数据库也能取消。

## Research 和模型适配器

[operator.example.json](../examples/operator.example.json) 提供只读 Research 配置。`ResearchAdapter` 只允许明确的 HTTPS 主机，验证解析地址为公开地址并固定连接地址，拒绝重定向、URL 凭证、自定义端口和私网目标。每次执行重新获取资料；freshness_seconds 是需求元数据，当前实现采用每次更新的严格策略。

来源包含 URL、retrieved_at、服务器 Last-Modified（若提供）、正文、抓取状态和 unknown 事实状态。发表日期未知时保持 null。网页内容不会转换成权限、步骤或代码。

模型调用使用可信 `JSONModelAdapter`，配置示例：

```json
{
  "project_key": "repo:demo",
  "policy": {"network_capabilities": ["summary"], "secret_capabilities": ["model_api"]},
  "models": {
    "summary": {
      "endpoint": "https://YOUR_APPROVED_HOST/v1/chat/completions",
      "model": "YOUR_MODEL",
      "api_key_env": "ATR_MODEL_API_KEY",
      "secret_capability": "model_api",
      "max_output_tokens": 1024,
      "max_cost_usd": 0.10
    }
  }
}
```

工具 Manifest 使用 `kind=model_pipeline, adapter=summary` 并声明 network summary、secrets model_api。Endpoint、密钥、模型和输出上限都来自操作者配置，不能由工具输入替换。密钥只在代理内使用，容器不接收。可选费率由操作者提供，未提供则金额保持未知；不能将估计费率视为供应商最终账单。

Python 接入者也可注入受信任的 `Adapter.call` 和 `RouteModel.decide`。适配器必须遵守 timeout/cancel、返回结构化输出及真实费用，并声明 max_cost_usd 和 required_secrets。宿主代理属于可信代码，通用 Python 回调不受容器边界约束。HTTP Socket 超时与整体执行状态共同限制网络调用，但系统 DNS 调用、恶意受信回调或故障引擎仍可能延迟终止；不应把这些回调作为运行不可信代码的方法。

路由默认规则+检索，不依赖任何模型。歧义时至多调用一次已配置的小模型，只接受决策枚举和本地召回项中的 selected；不能返回要执行的新命令。失败回到主 Agent。

## 成本、日志与恢复

`cost` 分别报告 Token、已知货币总额、未知项目和待结算预算，支持全部九类成本。调用付费代理前预留上界；账单缺失或调用结果不明时保留预算，不把未知调用当作免费。上界依赖受信适配器和供应商配置，无法撤销供应商超额收费。

主 Agent/创建/Review 等宿主模型费用需宿主通过 `runtime.ledger.record` 或 `record-cost receipt.json` 提交真实数据；运行时不会推测不可见的账单。receipt_id 可用于重复提交去重。示例凭证中的 null 表示未知，示例不是实际费用证据。compute/storage 价格未知时报告未知；不以 Token 减少冒充货币收益。

操作者可用 `settle-cost RESERVATION_ID receipt.json` 提交实际 USD 凭证，替换同次调用的暂记费用并释放预留；相同凭证重复提交不重复计费。显式配置的费率和 compute/storage 数值标记 estimated，不作为实际账单完整的证明。storage 数值按每次运行保留的交付字节归属估算，不能代表内容去重后的实际磁盘账单。

结构化事件关联 task/run/tool/version，记录路由、选择、执行、Review、晋升、归档与回滚，不默认保存对话或命令正文。`events --export FILE` 显式导出脱敏 JSONL。原始输出默认30天内清理（可调低），事件和费用默认180天；幂等运行身份和生命周期计数保留为管理元数据，研究及托管源码按自身引用和生命周期管理。任务和操作标识应使用不含敏感正文的短 ID。

服务退出会通知本进程运行取消。崩溃可能留下 running 记录和写锁；确认原工作进程已停止后，操作者执行 `recover RUN_ID`，将其标为失败、效果不确定并释放写锁。同一个 operation_id 仍不会重放。不要把进程恢复当作业务回滚。

本项目是单 Agent 的本地工具运行时，没有 A2A 或跨 Agent 委派。已查阅用户知识库的安全方案引用要求；当前没有多 Agent 协议面，未把该专项基线宣传为本项目已验证的安全结论。
