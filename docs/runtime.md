# ATR 0.2 运行与接入

## 信任边界与迁移

操作者、宿主 Python、配置和控制目录可信；Agent JSON、程序、Manifest checks 和输出不可信。签发接口只存在于宿主 Python/操作者 CLI，不进入固定五接口。控制目录、配置、任务凭证及验收文件须在工具工作区之外。ATR 无法隔离能直接修改控制数据库或调用任意宿主 shell 的同用户 Agent。

0.1 的任意 root_task_id、自拟 checks 和未批准请求不再获得执行权。0.2 使用 TaskContext 由宿主绑定任务；authorize_operation 返回批准请求。AgentAdapter 注入缺省根身份并拒绝替换。CLI 使用 --task-context；serve 缺少凭证时拒绝运行服务。历史运行、费用、对象保留，旧 Review 不自动成为独立验收证据。数据库迁移至 v4，未知版本拒绝打开；迁移前备份控制目录，不可同时运行旧版程序。

## 任务、预算和操作

control.issue_task(scope=ResourceScope(...), budget_usd=..., max_calls=..., ttl_seconds=..., max_creations=...) 生成随机任务 ID 和凭证。控制目录存摘要，宿主保管原凭证。期限不因重启、重试或后代延长，默认1小时，上限24小时。默认总调用128次，费用与创建还受 Settings 上限约束。Review 案例、工作流各级执行、Agent 读取和模型路由消耗同一根调用额度。

authorize_operation(context, request, business_key=..., rule=BehaviorRule(...)) 绑定固定代码或版本、参数、权限、环境及可信规则。同一业务键只能复用同一授权；改变操作需操作者明确签发新业务许可。Agent 自造 operation_id 不被接受。重交返回原运行；失败、效果不确定与崩溃恢复均不自动重放；副作用不能通过 retry_of 再执行。

代理提交前原子预留费用上界，同时检查任务及持久全局上限。已知费用与未结算预留共同占额度；估计账单保守保留上界，可能暂时多占额度。永久预算计数和凭证去重标记独立于日志。重开更高 max_total_cost 不自动加钱；可信宿主用 control.set_total_budget 或 set-total-budget 显式调整并记录事件。无法撤销供应商超额收费。升级前已经删除的旧日志支出无法重建，操作者应据实际支出设置剩余额度。

## 资源授权

全局 Policy 是最大边界，ResourceScope 继续收窄：

- read_paths：精确相对 POSIX 文件，或 docs/ 形式的子树；默认没有工作区读取权。
- write_paths：精确文件映射到 create/update/delete 列表；默认无写权。修改或删除既有文件通常还需读取该文件，以便暂存和冲突检测。
- adapters：允许调用的命名代理，还须满足全局网络/凭证能力。
- tool_actions：工具 ID 映射到 create/review/restore/rollback/archive/purge，分别授予。

例如只为 src/main.py 授予 update，不允许删除它或写其他文件。快照只复制获准读取的文件；归一化路径后再检查读权限，防止父目录穿越。敏感名称、符号链接、Windows junction、控制代码和凭证继续排除。max_workspace_files 默认10000，与 Artifact 的128文件交付上限独立。

## 独立验收与提交

control.approve_program(program, cases=[TrustedCase(inputs=..., rule=...), ...]) 保存宿主案例，绑定完整不可变程序和环境。Manifest checks 只是建议；没有独立案例时 Review/程序执行拒绝。代码变化或批准撤销后不能沿用证据。五接口不能新增、修改可信规则。

每个根操作另有 BehaviorRule，不依赖程序自报成功。checks 比较实际 JSON/文本；expected_path 缺失与合法 null 分开，JSON true 不等于数字1。expected_changes 检查完整文件变更及 UTF-8 内容，删除用 null，空映射表示不能修改任何文件。二进制效果规则尚不支持，不把普通 Artifact 交付当作语义证明。

程序经 stdin 接收 JSON，经 stdout 返回 JSON 或按规则返回文本。validation_status=passed 只表示已声明的结构和可信规则通过，不代表事实、完整业务或无后门；fact_status 不自动变为 verified。覆盖范围由操作者定义。

根写调用持有项目锁，后代共享暂存区。发布前核对所有路径操作、输出、完整文件效果、任务/操作/程序撤销状态、期限和取消。Review、失败、超时及取消不发布。发布检查与撤销通过控制事务串行化；最终仍逐文件写入，宿主崩溃或磁盘失败可能部分提交，不能当作文件系统事务或盲目重放。

失败版本仅在活跃指针仍指向该失败版本时回退，固定历史版本失败不影响健康新版。Review 结束核对并发状态，拒绝覆盖已变化状态；权限不足的 Review 不隔离其他工具。

## Docker 和代理

Linux 容器非 root、只读根、cap-drop、no-new-privileges、network none，设 CPU/内存/PID/时间/输出限制。只挂载过滤工作区和本次代码、交付目录，按本地镜像不可变 ID 执行，无宿主执行回退。容器无宿主凭证和原始网络。暂存目录仍无文件系统级总磁盘配额，需要配额卷提供该边界。

ResearchAdapter 使用精确 HTTPS 主机白名单、公开 IP 验证/连接固定、TLS 和响应上限，拒绝重定向、私网、URL 凭证和非443端口。来源保留 URL、抓取时间、服务器日期和 unknown 事实状态。JSONModelAdapter 的 endpoint/model/key/输出上限来自可信配置，密钥从 api_key_env 读取。输入不能替换配置，代理具体输入还必须匹配操作许可。envelope 或正文异常返回 adapter_contract，保留已取得的 Token、账单标识及未结算预留。

外部写入不能回滚。许可在调用前核对，已提交在途请求可能在撤销或取消后完成。外部 Review 还需 allow_external_review，操作者选择可接受的验证目标。自定义 Adapter/RouteModel 属于可信 Python，须遵守 timeout/cancel 和费用契约；不能承载不可信回调。DNS、故障引擎或不合作的可信回调可能延迟终止。

## 输出、生命周期和退出

输出统一标为 untrusted。JSON 键和值都脱敏，碰撞键加 #2/#3 后缀保留，合法 null 保持 null；分页先脱敏后切片。Agent 对运行、Artifact 的读取与取消均检查根归属。输出指令不能直接生成权限，主 Agent 如何解释内容仍需宿主信任分区策略。

Candidate、FTS5、固定依赖、晋升、归档、休眠和清理保留原有生命周期。0.2 检索只是建议；批准后才执行固定目标，自动观察不更换目标或运行未批准 Review。后代继承根重试/缓存标记、权限、预算和绝对截止时间。

关闭先停止接收，持久取消本实例所有已接收运行，终结排队 Future；活动同步/异步调用退出后才关闭数据库。其他实例的运行不被误取消。崩溃遗留记录需确认原进程停止后 recover RUN_ID，标记效果不确定并释放写锁，不重放原操作。

原始输出默认最多保留30天，事件/详细费用默认180天。未知账单的预留和费用保留至结算，持久预算计数、幂等标记不随日志删除。实际验证见[安全修复与证据](security-hardening.md)。
