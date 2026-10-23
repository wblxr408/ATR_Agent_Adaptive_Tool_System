# ATR · Agent Adaptive Tool Runtime

ATR 是单用户、本地部署的 Agent 工具运行时，提供受控执行、工具注册与复用、固定版本工作流、生命周期管理和费用记录。

**0.3.0 可直接接入 Codex 和 Claude Code，无需镜像。** MCP 复用已受信的 JSON/CSV/文本工具，hook 只在匹配时提示。未知生成代码仍需独立审批；本机执行限操作者受信程序，隔离执行可选原有 Docker 后端。

```powershell
uv sync --locked --extra integration
uv run --extra integration atr-native install-clients
```

安装保留并备份现有两端配置。重载 MCP/重开会话后可用；Codex 新 hook 在 `/hooks` 审阅信任。[接入、工具输入与扩展](docs/native-clients.md)；[理论成本收益与盈亏平衡](docs/cost-benefit.md)。重复稳定任务理论上可能受益，未测量或承诺节省比例。

0.3.1 增加显式启用的[一周使用量观测](docs/weekly-observation.md)：本地采集真实Token、ATR命中/调用/失败/耗时，与前7天日志比较，到期停止。仅保存数值；缺少账单或任务质量对照时不声称因果节省。

## 能力与边界

| 能力 | 实现 |
|---|---|
| 可信控制层 | 持久任务凭证、固定期限、共享调用/创建/费用限额、持久全局费用上限 |
| 具体授权 | 固定程序或版本、输入、环境和业务键；文件读取与创建/修改/删除分别授权 |
| 独立验收 | 宿主提供程序案例与每操作结果/文件规则，Agent 自拟 checks 不能授予准入 |
| 执行隔离 | 非 root Docker、无网络、只读根、CPU/内存/PID/超时/输出限制 |
| 工具管理 | Candidate、Registry、FTS5、不可变版本、Review、固定依赖与生命周期 |
| 工作流 | 后代共享根权限、预算、截止时间和暂存，完整根调用通过后提交 |
| 费用 | Token、已知/估计/未知金额、调用前预留、真实账单结算及去重 |
| 退出与状态 | 关闭取消活动及排队任务；旧版本失败不会回退无关健康版本 |

宿主程序、配置、签发接口和控制目录必须可信。ATR 不隔离拥有任意宿主 shell 或控制目录写权限的同用户程序。验收只证明满足已配置规则，不能证明任意程序没有后门，也不能保证主 Agent 不受输出中的提示注入影响。执行输出统一标记 untrusted。

## 安装

本机接入需要 Python 3.12–3.14 和 uv。以下隔离执行流程另需 Linux Docker 引擎。

~~~powershell
git clone https://github.com/wblxr408/ATR_Agent_Adaptive_Tool_System.git
cd ATR_Agent_Adaptive_Tool_System
uv sync --python 3.12
docker build -f Dockerfile.sandbox -t atr-sandbox:0.1 .
~~~

正式镜像完整构建此前受 Debian 软件源下载停滞影响，尚无成功记录。当前真实容器验收使用同基础的独立 Python 验证镜像，详见[安全修复与证据](docs/security-hardening.md)。

## 快速开始

以下命令在 PowerShell 7 中运行。工具工作区为 examples/workspace；配置、授权和凭证均在该目录之外。.atr-demo/ 已被 Git 忽略。

~~~powershell
New-Item -ItemType Directory -Force .atr-demo | Out-Null
$atrArgs = @('--workspace', 'examples/workspace', '--config', 'examples/operator.example.json')
uv run atr @atrArgs init

# 仅操作者执行：签发固定期限、64次调用、1美元上限的任务。
uv run atr @atrArgs issue-task examples/host-task.json --output .atr-demo/task.json
$hostArgs = $atrArgs + @('--task-context', '.atr-demo/task.json')

# 仅操作者执行：核对请求和预期结果后，签发固定操作许可。
uv run atr @hostArgs approve-operation examples/direct.json --business-key sum-input-once --acceptance examples/direct.acceptance.json |
  Set-Content -Encoding utf8NoBOM .atr-demo/approved.json

uv run atr @hostArgs execute .atr-demo/approved.json
~~~

结果为 {"sum":7}。重复提交同一批准请求返回同一 run_id。换输入、命令、版本、根任务或自行生成操作 ID 都不会取得新授权。请求模板不能直接执行；先经过宿主审批。签发、审批和凭证文件不能交给模型自主操作。

## 创建并复用工具

Manifest checks 只是建议；以下独立案例从操作者持有文件读取，不从程序自动复制。

~~~powershell
uv run atr @atrArgs approve-program examples/sum.program.json --acceptance examples/sum.acceptance.json
uv run atr @hostArgs create examples/sum.create.json
uv run atr @hostArgs review project.sum --version v1
uv run atr @hostArgs approve-operation examples/tool.execute.json --business-key sum-18 --acceptance examples/tool.acceptance.json |
  Set-Content -Encoding utf8NoBOM .atr-demo/tool-approved.json
uv run atr @hostArgs execute .atr-demo/tool-approved.json
~~~

结果为 {"sum":18}。工作流文件 workflow.program.json、workflow.create.json、workflow.acceptance.json、workflow.execute.json 和 workflow-operation.acceptance.json 采用同样的批准、创建、Review、批准操作、执行顺序，先注册其依赖 project.sum@v1。

Candidate 捕获与检索继续保留。0.2 的目标由宿主许可固定，检索结果不会自动替换批准请求，也不会自行执行新工具 Review。

## Python 与 Agent 接入

宿主通过 Python 对象绑定凭证，凭证不进入模型参数：

~~~python
from pathlib import Path
from atr.agent_adapter import AgentAdapter
from atr.control.authority import BehaviorRule, ResourceScope
from atr.models import Check, ExecuteRequest, Permissions
from atr.service import Runtime

with Runtime(workspace=Path("examples/workspace"), project_key="repo:demo") as rt:
    task = rt.control.issue_task(scope=ResourceScope(read_paths=["input.json"]))
    request = ExecuteRequest(
        root_task_id=task.root_task_id,
        argv=["python", "-c", "import json; print(json.dumps({'sum':sum(json.load(open('input.json'))['numbers'])}))"],
        permissions=Permissions(file_read=["workspace"]),
    )
    approved = rt.control.authorize_operation(
        task, request, business_key="sum-input",
        rule=BehaviorRule(checks=[Check(expected={"sum": 7})]),
    )
    agent = AgentAdapter(rt, context=task)
    print(agent.call("execute", approved.model_dump(mode="json")))
~~~

固定接口是 read、execute、tool_create、tool_review、tool_delete。模型 Schema 没有任务签发、授权或验收写入接口，也不要求模型填写根任务；宿主适配器注入根身份并拒绝替换。工具的注册、Review、恢复、回滚、归档与删除分别需要任务 tool_actions 授权。

~~~powershell
uv run atr definitions
uv run atr @hostArgs serve
~~~

JSONL 服务每行发送 {"id":1,"name":"execute","arguments":{...已批准请求...}}。后台执行设置 background=true，通过 read 查询运行或 execute 的 cancel action 取消。Agent 只能读取或取消自己任务的运行与 Artifact。

## 路径、验收与费用

ResourceScope.read_paths 支持精确文件或以 / 结尾的目录子树；write_paths 逐个指定文件的 create/update/delete，没有通配写权限。它们与全局 Policy 取交集，敏感路径、控制目录和符号链接继续排除。

BehaviorRule.checks 检查完整输出或 expected_path，支持 JSON/文本。expected_changes 指定完整文件变更及期望 UTF-8 内容，删除用 null；额外、缺失或错误内容拒绝提交。二进制交付使用 Artifact，当前可信文件效果规则只支持 UTF-8 文件。

代理输入也被操作许可固定，包括接收者、URL 或传出内容。主机白名单不能代替操作者对数据与用途的判断。外部服务已经发生的副作用不能由本地验收撤销。

工作流必须在根 BehaviorRule.adapter_calls 中显式列出代理能力、完整输入和次数，前序输出不能自行扩大外传范围。配置模型路由时，还需宿主用 authorize_route/approve-route 绑定具体文本；本地规则检索不发送文本。

费用在调用前原子核对任务和全局上限。未知/估计账单保持预留，日志过期与重启不会补回已耗额度。max_total_cost 默认50美元，重开配置不会自动提高已存上限；操作者可显式用 set-total-budget AMOUNT 调整。cost 报告 Token、金额与预留，record-cost 和 settle-cost 接收真实宿主账单。

## 管理与开发

| 操作者命令 | 用途 |
|---|---|
| issue-task / approve-operation / approve-program | 签发身份、操作和独立验收 |
| revoke-task / revoke-operation / revoke-program | 撤销后续执行和未发布的本地变更 |
| status / tools / runs / search | 状态与检索 |
| cost / metrics / events --export | 费用、指标及显式事件导出 |
| maintenance / disable / switch | 生命周期、暂停和取消 |
| recover RUN_ID | 原进程停止后标记效果不确定、释放写锁，保持原操作不重放 |

~~~powershell
uv run ruff check atr tests scripts
uv run mypy atr
uv run python scripts/export_schemas.py
uv build
~~~

本次行为命令和实际结果见[安全修复与证据](docs/security-hardening.md)。历史42项、23项及4项用例有各自时间和范围，不能当作0.2.0的当前验证。未运行成本收益实验、付费模型调用或真实供应商账单验收。

- [运行、授权与迁移](docs/runtime.md)
- [安全控制、六项修复及证据](docs/security-hardening.md)
- [设计计划](docs/plan.md) 与 [OpenSpec 任务](openspec/changes/implement-atr-runtime/tasks.md)
- [固定五接口 Schema](atr/schemas/interfaces.json)
