# Codex / Claude Code 本机接入

0.3.0 提供不依赖 Docker 镜像的真实 stdio MCP 与 UserPromptSubmit hook。Python 3.12–3.14，安装 integration extra；本机当前验证环境为 Windows / Python 3.12，其他系统尚未实际验证。

```powershell
uv sync --locked --extra integration
uv run --extra integration atr-native install-clients
```

安装时会注册并独立验收内置工具，然后修改用户配置。已有其他 MCP、hook 和偏好保留；修改前的原文件保存在同目录 `文件名.atr-backup-时间`，其中可能包含原配置凭证，不应提交或分享。重复安装不会重复追加 ATR 入口。Python 使用安装时解释器绝对路径，控制目录默认 `%LOCALAPPDATA%/atr/native`（非 Windows 为 `~/.local/share/atr/native`）。不要移动/删除该 Python 环境；变更路径后重新安装。

修改文件：Codex 的 `~/.codex/config.toml`、`~/.codex/hooks.json`，Claude Code 的 `~/.claude.json`、`~/.claude/settings.json`。两端需重载 MCP 或重开会话。Codex 新增或改变的非托管 hook 必须通过 `/hooks` 审阅信任；安装器不伪造信任记录，也不启用全局信任绕过。[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Codex hooks](https://learn.chatgpt.com/docs/hooks)、[Claude MCP](https://code.claude.com/docs/en/mcp)、[Claude hooks](https://code.claude.com/docs/en/hooks)

## 实际使用

用自然语言提出数据处理请求即可；命中提示后 Agent 可调用 `atr_search(query)` 获取输入约定，再调用 `atr_run(tool_id, inputs, business_key)`。business_key 对同一预期操作保持固定，重试返回原 run_id；相同键更改目标或输入会拒绝。模型不提供根任务身份、凭证、宿主命令或审批。

| 工具 | 操作 | 示例输入 |
|---|---|---|
| native.json | select / format / keys | `{"operation":"select","value":{"rows":[3,7]},"path":["rows",1]}` |
| native.csv | parse / write | `{"operation":"parse","text":"name,note\n甲,你好\n"}` |
| native.text | unique_lines / sort_lines / count | `{"operation":"unique_lines","text":"b\na\nb\n"}` |

结果是 `{"tool":"native.…","value":…}`，MCP 外层带 status、run_id 和 output_trust。CSV parse 保留字符串，拒绝重复表头和行宽不一致；write 通过 fields 与 rows 控制列顺序和转义。JSON path 用字符串键与非负整数下标数组，空数组选择整个值；不存在的路径返回失败。内联输入上限128 KiB，输出仍受运行时限制，大内容可能抵消成本收益。

## 操作者扩展

MCP 不开放信任审批。操作者审核完整源码后，用包含 program、cases、call_rule 的 JSON 文件登记：

```powershell
uv run --extra integration atr-native register examples/native-sum.operator.json
```

登记意味着信任代码以当前用户权限运行。限定 global、pure、固定 `python /tool/main.py` 入口、标准库，不授予工作区、网络、秘密或适配器权限；这些声明不能把恶意本机代码变成安全代码。不同输入的业务语义应由操作者设计独立案例和适用的 call_rule；示例固定 sum=10 只是展示拒绝机制，不代表任意求和都可通过。程序变化需要增加版本；撤销/改验收必须由操作者处理，不能从模型文本中批准。

每个服务进程有宿主签发、8小时固定期限、512次调用上限的任务上下文；两个客户端共享目录中的持久成本总额，native 客户端不配置模型/外网能力，费用额度为0。连接重启产生新的宿主任务，不把它宣称为客户端整个长任务的跨会话调用总预算；它不允许模型通过 MCP 自行签发或续期。幂等键作用域也是该宿主连接，断线重启不会假装跨连接复用运行。

内置工具首次使用真实独立例子验收；每次调用仍验证 schema 与工具标识。这不是对任意输入都重新计算业务正确性的证明。自定义工具的 call_rule 由操作者提供，并在运行时检查。

## 安全与理论边界

见 [成本收益](cost-benefit.md) 与 [安全修复](security-hardening.md)。继续引用 A2AB v1.0“工程解决方案/验收办法”：A2AB-01 技能声明验证与委派准入（明确允许 bundle 与独立案例）；A2AB-06 并发状态版本与审批绑定（固定版本和输入）；A2AB-08 结构化授权请求与权限不放大（纯输入、无自由 argv）；A2AB-10 委派环路检测与全链预算（可信身份、调用额度、零费用能力）；A2AB-11 中断任务租约与有界资源回收（期限、超时、取消、Windows Job/进程组清理）。02/03/04/05/07/09 不具备其 Agent Card、多租户、SSE、分块流、Webhook、多跳凭据条件，沿用全11项评估。它们是本地工程方案，不能宣称无镜像后仍有 Docker 隔离保证。

## 撤下接入

在各客户端 MCP 配置中删除 atr 项，在 UserPromptSubmit 中只删除 command 包含 `-m atr.hook --home` 的 handler。保留其他 handler；需完整回退时使用安装器保存的对应备份。停止客户端 ATR 服务后才处理控制目录；无需停止 Docker 或其他 MCP 服务。

## 本次交付证据

2026-10-22：新增12项本机接入消费者用例通过，覆盖意图检索、JSON/CSV/文本变换、业务键幂等、未知工具和非法输入拒绝、hook 匹配与空结果、配置保留和幂等安装、真实 stdio MCP 输出、自定义独立验收、未受信 bundle 拒绝、超时/取消、验收失败保留原工具、双客户端首次并发准入、到期关闭。首次检索用例观察到空结果失败；错误验收、并发准入和租约关闭同样先观察失败再修复。

已实际安装到本机两端用户配置；以这些配置中的真实 command/args 从项目外临时目录分别发起 MCP 调用，两端均正确解析中文且带引号逗号的 CSV。另直接运行配置中的两条 hook 命令，均返回对应已验收工具提示。这证明配置命令可工作，不代表运行中的客户端已热加载，也不代替 Codex 的 /hooks 信任。

本次只执行 test_native_integration.py 的请求行为目标，不运行历史回归套件、Docker 或收益实验；没有付费模型调用。Ruff、mypy（26个源文件）、OpenSpec strict、git diff --check 及0.3.0 wheel/sdist构建通过。集成依赖已用 uv.lock 固定，SDK2.2安装在本项目解释器内；没有借用 Basic Memory 的环境运行 ATR。
