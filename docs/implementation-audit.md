# TODO 实施核对（2026-10-22）

本记录对照 [v1.0 原计划](ATR_Agent_Adaptive_Tool_System_Plan_v1.md)，区分代码、当前行为证据和部署验证。原计划中的22项是设计决策，17项是行为清单；OpenSpec 原有5个大任务的勾选不能代替逐项核对。

用户确认的交付范围为代码、文档及必要功能验证。40项真实任务、成本收益对照、质量盲评和15%节省目标属于范围外实验。

## 22项设计的代码落实

| # | 设计 | 实现位置及核对结论 |
|---|---|---|
| 1 | 四路执行 | `router.py` 返回四种策略及 DEFER；`service.py` 统一 Direct/Tool、观察捕获及候选复用 |
| 2 | 最小基础能力 | `agent_adapter.py` 固定五接口；运行、权限、计数由宿主控制层持有 |
| 3 | 创建时机 | 规则/本地检索优先；歧义最多一次受约束模型决策，预算拒绝记0次 |
| 4 | 接口规范 | `models.py` 严格数据契约，`schemas/` 分发定义，执行与验证状态分开 |
| 5 | 检索去重 | `control/registry.py` 精确/FTS5 检索工具和候选；内容与契约签名去重，返回规范工具ID |
| 6 | 统一调用 | `service.py` 惰性加载版本；`runtime/sandbox.py` 承担 Direct/Script 的 Docker 执行 |
| 7 | 作用域 | `policy.py` 校验项目、环境及授权；检索和 route 入口更新环境 |
| 8 | 组合依赖 | 固定版本依赖、深度/循环/权限闭包、引用保护；Direct 工作流同样检查闭包 |
| 9 | 观察期 | `control/lifecycle.py` 独立的候选 TTL、固定临时观察窗和可注入时钟 |
| 10 | 有效计数 | 唯一运行与业务操作占位；重试、缓存、Review 排除，后代继承标记 |
| 11 | 固化阈值 | 默认5次/2根任务；同任务例外按单个根任务的不同输入计数 |
| 12 | 到期处理 | 活跃检索下线、归档、引用和运行保护、仅清理托管对象 |
| 13 | 长期管理 | 90天闲置休眠；恢复需 Review，新周期重置 |
| 14 | Review | 执行明确期望输出用例、保留证据；研究事实默认 unknown；写入 Review 使用一次性工作区 |
| 15 | 失败修复 | 根任务创建总量、失败版本后续修复 max_repairs 上限；候选创建失败保留 Direct；有副作用不盲重放 |
| 16 | 版本回滚 | 不可变版本与事务活跃指针；接收运行即固定版本，排队和执行期间保护；契约失败隔离及旧版建议 |
| 17 | Research 时效 | `adapters.py` 每次重新获取 HTTPS 来源，保存 URL/时间/更新日期/抓取及事实状态 |
| 18 | 权限隔离 | 非特权 Docker、无原始网络、控制目录不挂载、路径过滤；命名宿主适配器持有凭证 |
| 19 | 副作用资源 | 操作标识、项目锁、根工作区暂存、CPU/内存/PID/超时/并发/输出限制，嵌套代理继承剩余时间 |
| 20 | 全路径成本 | `accounting.py` 九类费用、Token/金额分开、未知账单保留预算和证据、实际结算；金额凭证不抹除已知 Token |
| 21 | 日志观测 | 任务/路由/运行/Review/生命周期事件、脱敏、Artifact 分页、保留策略；重复费用标识绑定负载 |
| 22 | 对照评测 | `evals.py` 观察契约、公平配对校验、分阶段费用/Token/质量报告；仅交付记录与报告代码 |

## 原17项行为清单的证据索引

“初版”指 [2026-10-15 的42个用例记录](implementation.md)，不表示本次重新执行。当前补齐使用 `test_followup_contracts.py` 的23个新行为用例，全部先观察到对应行为失败，再修复至通过。执行器/供应商替身只证明控制逻辑；另有4个当前新增的真实 Docker 用例。

| # | 行为 | 代码及证据 |
|---|---|---|
| 1 | 普通 rg/git status/pytest 不捕获 | 初版 `test_proxies.py::test_direct_commands_and_business_edits_are_not_candidates`；实际命令可用性依赖镜像及过滤后的工作区 |
| 2 | 成功独立程序保存托管候选 | 初版 `test_acceptance.py::test_independent_program_is_captured_and_searchable`；Docker 用例另列 |
| 3 | 业务代码不误捕获 | 同#1；只有显式 `generated_independent` 且实际成功的 Program 才捕获 |
| 4 | Tool/Candidate 检索和创建去重 | 初版 creation/dedup 与自动注册用例；当前创建暂停/耗尽仍可受控 Direct |
| 5 | 环境/输入不兼容拒绝 | 初版输入拒绝、环境与输出契约用例；当前 `test_route_refreshes_environment_before_selecting_reuse` |
| 6 | 一次模型兜底及失败 DEFER | 初版 router ambiguity；当前 `test_budget_rejection_does_not_report_a_routing_model_call` |
| 7 | 控制层/策略/凭证边界 | 当前真实 Docker 用例拒绝读取/修改宿主控制状态、读取过滤凭证、获取宿主秘密环境变量和原始外网连接 |
| 8 | 不可信网页/README 无授权能力 | 当前真实 Docker 将恶意 README 作为数据读取且无法扩权；当前 Direct 依赖闭包与外部候选门禁 |
| 9 | 受限摘要/Artifact 分页 | 当前真实 Docker 大结果返回截断摘要及256字节片段；JSON 凭证脱敏保留合法结构和 null |
| 10 | 失败/重试/重复不晋升 | 初版操作去重、重试缓存计数；当前 workflow attempt markers 及单根任务例外 |
| 11 | 晋升和过期归档 | 初版可控时钟状态用例；当前同任务输入归属用例修复跨任务误晋升 |
| 12 | 复合依赖保护 | 初版间接调用、归档依赖和删除保护；当前排队运行保护及 Direct 权限闭包 |
| 13 | 保留旧版及原子回滚 | 初版升级失败、运行契约失败回退；当前排队期间切换版本不改变已接受的版本 |
| 14 | 外部写入授权和不盲重试 | 初版权限和操作幂等；当前外部候选不自动 Review、无操作ID不执行、写锁和根暂存 |
| 15 | Research 更新证据和引用 | 初版 research refresh 用例；HTTPS 每次重新获取，实际供应商接入需部署验证 |
| 16 | 成本链路和脱敏 | 初版成本/事件/分页脱敏；当前预算调用数、未结算保留、费用身份冲突、结算 Token 保真 |
| 17 | kill switch 和 Direct | 初版动态取消及 Direct 可用；当前关闭 auto_create 后重复候选仍能受控 Direct |

## 当前验证记录

- `uv run pytest tests/test_followup_contracts.py -q`：23 passed。
- `uv run ruff check atr tests/test_followup_contracts.py tests/test_workspace_docker.py scripts`：通过。
- `uv run mypy atr`：19个源文件通过。
- `openspec.cmd validate implement-atr-runtime --strict`：通过。
- `uv run python scripts/export_schemas.py`：生成7份数据契约及固定五接口，Schema 校验通过。
- `uv build`：sdist/wheel 构建通过。
- `ATR_TEST_DOCKER=1 ATR_TEST_IMAGE=atr-python-validation:0.1 uv run pytest tests/test_workspace_docker.py -q`：4 passed，实际 Docker 执行。

真实容器用例覆盖：输出契约失败丢弃改动、成功操作只提交一次；工作流后续失败丢弃全部子调用改动；超时丢弃改动；控制层/凭证/只读工作区/网络边界及受限结果交付。最后一项先暴露 JSON 的 null 被文本脱敏破坏，改用 JSON 结构脱敏后通过。

验证镜像从同一 `python:3.12-slim` 基础镜像构建，创建 `/tool /workspace /out`，设置非 root 用户；执行时使用正式 DockerSandbox 的完整隔离参数。该镜像单独标记为 `atr-python-validation:0.1`，不包含 git/ripgrep/pytest。正式 `Dockerfile.sandbox` 两次构建均在 Debian Packages 索引下载阶段长期无进展，已终止本次构建；增加 HTTPS 和连接超时/重试限制仍未取得完整镜像构建结果。此结果不证明正式镜像或其中的普通命令已经可用。原有 `tests/test_docker.py` 四项未重跑。

本次未运行成本收益实验和付费模型请求。未自动重跑初版整套历史测试；历史证据和本次新增用例分别报告。

## 部署边界

根调用在执行及契约失败时丢弃暂存改动，成功后逐文件提交。磁盘故障或进程在提交阶段中断仍可能产生部分文件变更；该阶段不是文件系统多文件事务。外部服务已经发生的写入也不能由本地暂存回滚。

`.git` 默认过滤，宿主凭证不复制；需要 Git 仓库元数据的命令应使用经过审查的专用项目副本。项目自己的第三方依赖应安装到操作者选定的镜像。宿主目录总磁盘配额、DNS、故障引擎和任意受信 Python 回调的强制终止仍属于 [运行说明](runtime.md) 的部署边界。
