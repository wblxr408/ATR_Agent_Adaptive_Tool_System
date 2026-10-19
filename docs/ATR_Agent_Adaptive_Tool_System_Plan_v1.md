# ATR：Agent 自演化工具系统实施计划 v1.0

> 设计日期：2026-10-15  
> 适用范围：单用户、以 Coding / Research 为主的 Agent  
> 状态：22/22 项**设计决策已收敛**；实施与效果尚待验证  
> 核心目标：优先以低成本、高可靠性完成当前任务，并把真实高频使用的能力沉淀为可按需调用的 Tool。

## 0. 执行摘要

将 ATR（Adaptive Tool Runtime）建设为现有 Agent 的一层受控运行时，而不是另写一套大型 Agent 框架。Agent 可以直接执行普通命令，复用既有 Tool，执行并保存新生成的独立程序为 Candidate，或者主动创建复杂 Tool。候选程序经过重复需求命中后才注册；临时 Tool 在固定观察期内按有效使用次数晋升；未达标者归档并最终清理。工具可以是脚本、二进制程序、复合工作流、外部模型或 Research Pipeline，不强制追求跨领域泛化性。

**明确不做：**跨 Tool 复杂价值评分、默认加载所有 Tool 定义、每条命令都调用 Router 模型、为所有执行记录注册 Tool、自动改写受保护运行时、无上限递归自修改。

**核心设计决策：**

- 模型侧固定五个 Tool 接口：`read`、`execute`、`tool_create`、`tool_review`、`tool_delete`；Router、Registry、权限、计数和清理在控制层实现。
- `execute` 统一入口，内部区分 Direct Backend 和 Tool Backend。允许执行授权范围内的任意代码，但默认位于受控沙盒。
- Router 由规则与检索优先驱动，低成本模型只处理不确定的执行策略；主 Agent 提议，Router 校验与修正。
- 一次性普通命令不产生 Candidate；只有新生成、实际执行的独立程序/结构化工作流才可能被捕获。项目业务源码修改不自动进入工具池。
- 工具固化按**有效复用频率**，安全性和正确性为准入门槛，而不是与其他工具竞争排名。
- 动态工具只在 Registry 持久存储，由稳定的模型侧接口按需发现、运行，避免反复改变工具定义前缀。

## 1. 成功定义与约束

### 1.1 优先级

1. 当前任务被正确完成；高影响操作必须受控。
2. 在效果不变或提高的前提下，降低端到端模型 Token 和实际费用。
3. 复用有效代码与工作流，减少重复实现、重复搜索和重复调试。
4. 随实际重复任务积累个人专属能力。低频高能力工具允许因经济性而归档，不强制保留。

### 1.2 不应混淆的概念

- Tool **使用频率**：观测需求与复用程度，不等于能力深度。
- Tool **执行成功**：运行结束且输出符合执行契约，不等于整个用户任务正确完成。
- Tool **存储固化**：保留可检索的版本，不等于每轮都加入模型上下文。
- Tool **执行隔离**：沙盒权限边界；**观察期**：生命周期试用阶段。这两者完全不同。
- Token 节省不是费用节省的充分条件：嵌套模型、网络 API、CPU、等待与失败重试也计费或消耗资源。

## 2. 22 项 TODO 的最终决策

| # | 待澄清事项 | 采用的决定 | 第一版优先级 |
|---|---|---|---|
| 1 | Tool 定义与创建边界 | Direct / Reuse / Execute & Observe / Create 四路；默认 B+C 混合 | P0 |
| 2 | 最小基础能力 | 模型侧五接口；Runtime 单独承载执行、安全、Registry | P0 |
| 3 | 创建时机 | 主 Agent 提议，规则 Router 校验；只在歧义时小模型兜底 | P0 |
| 4 | Tool 接口规范 | Manifest + 输入输出 JSON Schema + 标准 ExecutionResult | P0 |
| 5 | 工具检索与去重 | 精确匹配→SQLite FTS5→必要时小模型复核；搜索 Tool 和 Candidate | P0 |
| 6 | 工具调用方式 | 统一 execute，两内部后端，按需加载；不改变固定工具列表 | P0 |
| 7 | 作用域 | global / project / environment；默认 project，环境变更前置检查 | P0 |
| 8 | 组合和依赖 | 有向依赖图、绑定版本、引用保护；分别记录直接/间接调用 | P1 |
| 9 | 临时观察期 | Candidate 保存 14 天；Temporary 自注册起固定观察 14 天 | P1 |
| 10 | 调用计数 | 只计算有效、未重复记账的调用；失败与重试单列 | P1 |
| 11 | 固化阈值 | 默认 14 天内 ≥5 次有效调用且覆盖 ≥2 个独立任务；高频同任务另设可配置例外 | P1 |
| 12 | 到期处理 | 到期从活跃索引下线，短期归档，清理仅限托管副本；有依赖则保护 | P1 |
| 13 | 长期工具管理 | 版本化固化；90 天闲置转休眠，恢复使用前重新验证 | P2 |
| 14 | Review 机制 | 确定性测试优先；Coding/Research 分级验证；非确定性结果保持不确定性 | P0/P2 |
| 15 | 失败、重试、兜底 | 创建/修复尝试有限额；失败时 Direct 或旧版本兜底；有副作用不自动重放 | P0 |
| 16 | 版本与回滚 | 不可变版本、原子切换、固定依赖版本；新版本重新验证 | P2 |
| 17 | Research 时效性 | 工具固化流程，研究事实另存带来源/时间的结果数据，使用时更新 | P2 |
| 18 | 权限与隔离 | 运行时沙盒、最小权限、受保护控制层、网络/凭证代理 | P0 |
| 19 | 副作用与资源约束 | 读写/外部动作分级；CPU/内存/超时/并发/输出量限制 | P0 |
| 20 | Token/成本模型 | 全路径成本日志，基线是已有终端/搜索能力的普通 Agent | P0 |
| 21 | 日志与可观测性 | 结构化事件、任务/工具关联、可回放、密钥脱敏与数据保留策略 | P0 |
| 22 | MVP 验证标准 | 同任务、同模型、同权限基线对照；分别测冷启动、热复用和 Research 质量 | P3 |

> P0/P1/P2/P3 代表实现优先级，不表示技术难度已被验证。上表中的天数与调用阈值属于**实验起始默认值**，不是已经证明最优的参数。

## 3. 总体架构

```text
用户 Coding / Research 请求
          │
          ▼
主 Agent（任务规划，提出执行意图）
          │
          ▼
轻量 Router（本地规则 + Registry 查询 + 可选小模型）
          ├── DIRECT ────────────────────┐
          ├── REUSE ─────────────────────┤
          ├── EXECUTE & OBSERVE ─────────┤
          └── CREATE ─> Tool Creator ────┤
                                        ▼
                                  Unified Execute
                                  /            \
                         Direct Backend    Tool Backend
                                  \            /
                                   受控 Runtime
                                （沙盒/权限/API 代理）
                                        │
                               结果摘要 / Artifact Ref
                                        │
                                        ▼
                                    主 Agent

旁路控制服务（不持续消耗模型 Token）：
  Candidate Store ↔ Registry / FTS5 ↔ Lifecycle Engine
                          │
                 Review / Version / Dependency
                          │
                  Structured Events + Cost
```

**控制平面**包含 Router、Registry、策略、元数据、生命周期和计数器。它位于工具执行沙盒之外，动态生成代码不得直接写入。

**数据平面**只负责受控执行、文件及结果交付。复杂 Tool 可调用其他 Tool、外部模型或 Research 适配器，但每次调用仍遵守运行时权限和预算。

### 3.1 模型侧固定五个工具

| 接口 | 典型操作 | 边界 |
|---|---|---|
| `read` | `workspace_read`、`registry_search`、`run_output` | 文件与工具目录的受控读取，默认返回片段 |
| `execute` | `direct(argv)`、`tool(tool_id, inputs)` | 唯一执行入口，底层后端可不同 |
| `tool_create` | 从 Candidate 注册；新建脚本/工作流；提出新版本 | 创建候选定义，不可绕过沙盒和评估 |
| `tool_review` | 手动触发验证、查看测试证据、审核版本 | 不能授予权限或自我豁免强制安全门禁 |
| `tool_delete` | 请求归档/删除托管 Tool 或版本 | 由生命周期引擎审查引用后执行；不删除用户项目源码 |

`read` 同时承担 Registry 检索，减少一个长期暴露的 `tool_search` 接口。后台 Registry API 仍可独立实现。`tool_update` 通过 `tool_create(base_version=...)` 实现，不必增加基础 Tool。

### 3.2 统一执行结果

```json
{
  "run_id": "run_0042",
  "mode": "tool",
  "tool_id": "project.traceback_parser",
  "version": "v3",
  "status": "succeeded",
  "exit_code": 0,
  "summary": "提取到 12 个异常堆栈",
  "artifacts": [{"ref": "artifact:sha256/...", "type": "application/json"}],
  "stdout_excerpt": "...",
  "validation_status": "passed",
  "duration_ms": 1024,
  "truncated": true
}
```

状态至少区分 `running/succeeded/failed/timed_out/cancelled`。`validation_status` 可为 `passed/failed/unknown`，不得把 `exit_code=0` 强行当成业务正确。输出默认只返回摘要、有限截断文本和 Artifact 引用；模型按需读取大结果。

### 3.3 Manifest（建议最小字段）

```yaml
id: project.traceback_parser
version: v3
kind: script               # script | workflow | model_pipeline | adapter
scope: project             # global | project | environment
project_key: repo:<stable-id>
entrypoint: python main.py
input_schema: schemas/input.json
output_schema: schemas/output.json
permissions:
  file_read: [workspace]
  file_write: [artifacts]
  network: []
  secrets: []
runtime:
  python: '3.x'
  timeout_seconds: 30
  memory_mb: 512
dependencies: []            # tool_id + pinned version
side_effect: read_only     # pure | read_only | workspace_write | external_write
checks: [tests/smoke.py]
```

Manifest 应放在控制层，数据字段经 Schema 校验；所有 Tool 源码、依赖锁文件和测试用例不可变并以内容散列存储，避免运行中的文件悄然改变。

## 4. Router 与 B+C 捕获协议

### 4.1 决策次序

```python

def route(intent, registry):
    # 由主 Agent 已有计划产生 compact intent；不要求 Router 重做规划
    match = registry.find_exact_compatible_tool(intent)
    if match:
        return REUSE(match)

    candidate = registry.find_exact_compatible_candidate(intent)
    if candidate:
        return REUSE_OR_REGISTER_CANDIDATE(candidate)

    if is_simple_existing_command(intent):
        return DIRECT

    if intent.explicit_tool_creation and no_equivalent_exists(intent):
        return CREATE

    if intent.is_new_independent_program:
        return EXECUTE_AND_OBSERVE

    # 仅对真正存在歧义的情况调用一次低成本模型
    decision = cheap_model(intent.compact_summary, registry.top_matches(limit=3))
    return decision if decision.is_valid else DEFER_TO_MAIN_AGENT
```

注：示意伪代码省略模糊检索和权限校验。实际实现中，精确检索未命中后还应做本地 FTS5 模糊召回，再根据 Schema/作用域/副作用检查兼容性。小模型输出必须为受约束枚举，不得直接执行模型返回的命令。

### 4.2 Direct 和 Candidate 的边界

**不捕获：**现成 Shell 命令、普通文件读取、已有项目业务代码修改、完整对话、网页内容、研究结论。  
**默认捕获：**Agent 新编写、有独立执行入口、确实执行过且可保存托管副本的程序或结构化工作流。  
**允许主动创建：**用户明确要求、当前任务需要复合能力、计划明确多次使用、Research 工作流无法从单一脚本被动捕获。

执行完成后，轻量规则尝试生成 Candidate：`candidate_id`、意图标签、代码内容 hash、托管副本引用、作用域、输入来源、执行状态、环境指纹。不要为了捕获每个 Candidate 单独请求 LLM。

**关键边界：**Candidate 的存在不代表可以无检查地再次执行；再次命中时需确认输入参数化、依赖可用、权限不扩大。重复任务出现后才付出注册与封装成本。

### 4.3 检索与去重

1. `tool_id` / 别名 / 完整作用域与输入 Schema 精确匹配。
2. SQLite FTS5 按名称、能力描述、标签、输入输出字段检索 Tool 和 Candidate；返回前 3 个小型摘要。
3. 兼容性过滤：作用域、依赖环境、输入输出契约、副作用类型。
4. 仅在多个候选难分或不匹配时调用低成本模型；不在第一版使用向量数据库。
5. 记录 `retrieved → selected → executed → validated`，区分漏检索与工具低价值。

**反重复约束：**创建前始终走同一套 Tool + Candidate 检索，C 路径不得绕过 B 路径。避免工具重命名后分散调用计数。

## 5. 生命周期状态机与晋升机制

```text
Agent 编写独立执行单元 → CANDIDATE（候选；未注册 Tool）
          │ 需求重复出现、接口检查通过
          ▼
      TEMPORARY（可调用；观察与计数） ──符合阈值+Review通过──> PERSISTENT
          │                                                   │
       到期未达标                                         90天无有效使用
          ▼                                                   ▼
       ARCHIVED                                           DORMANT
          │                                                   │
       保留期结束                                        再次使用则复检
          ▼                                                   ▼
       DELETED                                      PERSISTENT 或 DISABLED

ARCHIVED 再次命中 → 复检通过后重新进入 TEMPORARY（计数重置）
安全校验失败：任意适用状态 → QUARANTINED / DISABLED（不可执行）
```

**注意：**`DIRECT` 是执行路径，不是长期状态；`CANDIDATE` 是代码执行产物，不等于 Tool。所谓隔离观察期为 `TEMPORARY`，新代码执行前的安全沙盒检查另行进行。

### 5.1 建议初始默认值（全部配置化）

| 参数 | 初值 | 原因 |
|---|---:|---|
| Candidate 保留期 | 14 天 | 给真实重复需求一次命中机会，避免长期积压 |
| Temporary 观察窗口 | 注册后固定 14 天 | 简单、可比较，不因调用持续无限续期 |
| 自动晋升阈值 | ≥5 次有效执行 | 以使用频率为主，不评价难度 |
| 跨任务门槛 | ≥2 个独立根任务 | 避免同一次失败循环触发固化 |
| 高频同任务例外 | ≥10 次不同输入的有效执行 | 避免误删在同一大任务内真实高频的 Tool |
| Temporary 到期 | 先 ARCHIVED 30 天 | 托管代码重建可能比存储贵 |
| PERSISTENT 闲置 | 90 天转 DORMANT | 减少活跃索引噪声，保留重启能力 |
| 单次自动修复次数 | 最多 2 次 | 防止消耗失控 |

晋升还需强制满足权限审查通过、基本契约测试通过、无已知阻断性错误。**不能只凭退出码晋升。** 对很难验证内容正确性的 Research Tool，仅凭自动成功计数不宣称内容可靠；自动晋升只表示其执行流程满足准入条件，不代表研究结论正确。

### 5.2 有效调用的精确定义

一个 Tool 版本的可计数调用必须：

1. 真实运行，产生唯一 `run_id`，符合输入契约；
2. 返回状态成功，最低限度的运行/输出契约检查通过；
3. 不属于对同一个操作的失败重试、重复记账或被输出缓存直接命中的事件；
4. 归属于可追踪的 `root_task_id`；同一大任务多次处理不同输入仍可计数。

维护 `total_calls`、`eligible_uses`、`failed_calls`、`direct_uses`、`indirect_uses`、`distinct_root_tasks`，不强制合成为质量评分。Tool A 经 Tool B 调用时，分别记录各自实际执行事件，但不把同一个工具执行事件计两次；删除前检查引用关系。

### 5.3 过期和依赖保护

到期时把 Tool 从活跃召回索引移出，再归档托管源码、Manifest 和测试，不继续将其默认交给模型。若被仍活跃的上层 Tool 依赖，保持依赖版本可用或打包固定依赖，不得直接删除。物理删除只针对控制层管理的副本，不触碰用户仓库。归档恢复需重新验证，恢复后的观察计数重新开始；旧成功记录供诊断参考，但不能充当新版本的准入证据。

## 6. 可靠性、安全与异常处理

### 6.1 Review 分级

| 类别 | 自动检查 | 需要加强验证的情况 |
|---|---|---|
| 确定性脚本 | JSON Schema、烟雾测试、退出状态、输出断言 | 文件修改与外部副作用 |
| Coding 工具 | 单元测试、类型/静态检查、构建、Git diff | 大规模代码重写、自动推送 |
| Research 工具 | 来源存在性、时间戳、引用可回溯、结果结构 | 关键事实判断、证据冲突、结论质量 |
| 模型/规划工具 | 参数约束、资源预算、结构化输出、可执行性检查 | 非确定性、嵌套模型和高影响决策 |

Review 在创建和版本更新时运行最低门禁；运行中的任务只做必要验证，避免每次工具调用都引入额外 Reviewer 模型。LLM Reviewer 仅用于非确定性的模糊判断，不能成为安全权限的授权者。

### 6.2 执行权限

- **控制层隔离：**Registry、策略文件、凭证仓库和生命周期数据库不直接映射到动态代码容器。
- **最小权限：**Direct 可以执行任意被授权的代码，不代表能写系统目录或访问凭证；Tool 模式不能因固化自动扩权。
- **沙盒默认：**使用非特权容器/隔离执行环境，限制文件挂载、CPU、内存、进程数、网络及运行时间。Rootless Docker 可作为 Linux 参考实现，但并非完整安全边界，部署环境需验证其资源限制是否真实生效。
- **网络与凭证：**默认为无原始网络访问；Research 经只读数据/网络适配器获取外部资料。密钥由控制层按能力授予，不能作为环境变量无差别注入所有进程。
- **操作分级：**只读与限定工作区修改可按预授权自动执行；仓库外删除、远程推送、敏感文件读取、外部写入和高额付费调用需要显式授权。
- **不可信内容：**网页、Git 仓库 README、论文文本只能作为数据；其中包含的指令不得改变工具权限、执行路径或系统策略。

### 6.3 失败与版本策略

- 创建失败：最多进行有限自动修复，失败退回 Direct 或说明阻塞，不强制注册失败工具。
- Tool 运行失败：返回标准错误与可引用日志，优先使用同一已验证版本的安全重试路径；存在副作用时无幂等保证不得自动重跑。
- 更新：保留旧版，新版在隔离沙盒中测试，通过后原子切换；新版本有效调用计数独立记录。出现回归立即 rollback。
- 并发：对同一项目写操作加锁；长任务用 `run_id` 查询状态；超时可终止子进程树。
- 输出：大文件保存 Artifact，模型只读摘要或指定片段；日志与输出统一脱敏。

## 7. Coding 与 Research 的特别规则

### 7.1 Coding

- 普通 `rg/git status/cat/pytest` 走 Direct，不创建 Tool。
- Agent 为具体项目新写的独立分析脚本，执行成功后保存 Candidate 托管副本。
- 仓库业务源码、依赖锁文件的修改是任务产物，不因此注册 Tool。
- Tool 必须声明 repo / language / environment 作用域；切换项目后不能仅凭名称匹配直接复用。
- 工具产生代码修改时保留 Git diff 和测试记录；不得绕过用户设定的版本控制授权。

### 7.2 Research

- 允许多阶段检索、抽取、去重、模型分析和引用检查组成复合 Tool。
- 固化的是**研究过程**（如论文发现与证据整理），不是某一次“最新研究结论”。
- 研究数据单独保存来源 URL、检索时间、发表/更新日期、必要引文和时效策略；重新使用时按新鲜度要求获取最新证据。
- 工具输出需要明确区分“抓取成功”“结构校验通过”“事实经过验证”；不能用前两者代替第三者。
- 外部资料中的提示注入内容按不可信文本处理，不给工具管理系统下命令的权力。

## 8. 总成本、缓存与可观测性

### 8.1 端到端费用

定义每任务总成本：

`C_total = C_main_LLM + C_router_LLM + C_creator_LLM + C_review_LLM + C_nested_LLM + C_external_API + C_compute + C_storage + C_fail_retry`

另记 `latency`、`task_success`、`tool_lookup_time`、`registry_growth`、`manual_intervention`。输入/输出 Token 分别计数，区分缓存命中 Token 和实际计费 Token，尽量使用供应商返回的计费数据。报告 Token 和货币成本两个视图，避免把费用与 Token 混为一谈。

**基线必须公平：**对比的是已有终端、文件、网络搜索能力的普通 Agent，而不是只能纯文本手工处理的模型。

### 8.2 控制开销预算

- 普通 Direct 命令：0 次额外路由模型调用。
- 规则无法决策的一个路由事件：最多 1 次低成本模型调用；仍不明确返回主 Agent。
- Candidate 自动捕获：0 次额外模型调用；摘要优先来自主 Agent 现有执行意图。
- 临时使用计数、到期、晋升：0 次额外模型调用；只在质量门槛遇到歧义时要求 Review。
- Tool 创建或修复：有可配置的单任务预算和重试上限。
- 动态 Tool 定义不常驻上下文；模型只使用固定接口和少量候选简述，完整定义按需加载。

### 8.3 核心事件与存储保留

事件：`task_started`、`route_decided`、`candidate_saved`、`tool_created`、`review_completed`、`execution_started`、`execution_finished`、`tool_promoted`、`tool_archived`、`tool_rollback`、`permission_denied`。每个事件包含 `task_id`、`run_id`、时间、版本、状态、Token 与费用摘要，不默认保存敏感正文。

起始策略：原始执行日志最多保留 30 天（可以由用户调低）；非敏感聚合指标保留 180 天；研究数据、用户文件和托管工具源码遵循各自独立的保留策略。提供一键暂停自动 Tool 创建、清空候选和关闭动态工具执行的 kill switch。

## 9. 技术选型（单用户 MVP）

| 模块 | 第一版建议 | 暂时不需要 |
|---|---|---|
| 服务实现 | Python 3.12 + 类型检查 + Pydantic/JSON Schema | 重型 Agent 编排框架 |
| 元数据 | SQLite（WAL 模式） | PostgreSQL 分布式集群 |
| 检索 | SQLite FTS5 + 标签/精确匹配 | 向量数据库、复杂 reranker |
| Tool 代码与数据 | 文件仓库 + SHA-256 内容寻址 | 分布式对象存储 |
| 执行 | Unix 参数数组、受控 Python、OCI 沙盒 | Host 上无隔离的任意执行 |
| 调度 | 本地事件驱动生命周期 worker | 消息队列/多租户任务平台 |
| 工具 Schema | JSON Schema | 每个 Tool 自定义协议 |
| 监控 | SQLite events + JSONL + 可选 OpenTelemetry | 大型数据平台 |
| 用户界面 | CLI 命令 + 少量状态/成本报告 | 全功能 Web Dashboard |

如果将来需要兼容 MCP，优先做一个适配层把现有 Tool Manifest 映射为 MCP 工具，而不要让 MCP 协议反过来决定内部生命周期与权限结构。

建议目录：

```text
atr/
  agent_adapter/        # 主 Agent 与固定 Tool 入口
  router/               # 规则、兼容性判断、低成本模型兜底
  control/              # Registry、Candidate、Review、Lifecycle、Policy
  runtime/              # Dispatcher、Direct/Tool Backend、Sandbox
  adapters/             # shell/python/workflow/model/research
  schemas/              # Tool Manifest、input/output、ExecutionResult
  storage/              # SQLite + 托管 artifacts（不直接挂进执行沙盒）
  tests/                # 状态机、隔离、回滚、路由、端到端
  evals/                # Coding/Research 基线与重复任务实验
  cli/                  # atr status / tools / runs / cost / disable
```

## 10. 分阶段开发计划与验收门槛

### 阶段 0 — 基线与规格冻结（优先启动）

**交付：**统一 Tool Manifest、ExecutionResult、事件 Schema；选取有代表性的 Coding/Research 真实任务并记录普通 Agent 的总成本和质量。  
**验收：**能用相同输入重复运行基线任务；具有可追踪的 Token、时间、成功与失败标签；不存储机密明文。

### 阶段 1 — 固定 Tool 入口 + 受控 Runtime

**交付：**`read/execute`、统一 Dispatcher、Direct/Tool 双 Backend、沙盒、只读/写入授权、输出 Artifact 引用、必要的 `tool_create/review/delete` 空壳与门禁。  
**验收：**Direct 可以完成真实任务；动态代码不可直接修改 Registry/策略；越权读写被阻止；stdout 上限和超时生效。

### 阶段 2 — Router + Candidate（先验证 B+C）

**交付：**四路 Router；固定规则；一次最多一次低成本模型路由；独立执行单元候选捕获；SQLite FTS5 检索。  
**验收：**普通命令不会生成候选；新脚本能够自动进入 Candidate；遇到相似需求可命中候选；主动 Tool 创建请求会先去重。

### 阶段 3 — Temporary、自动晋升与归档

**交付：**注册/版本号、成功调用事件、观察期计时、计数防重、依赖保护、固化/归档/清理定时任务。  
**验收：**5 次有效调用可以按策略晋升；失败重试不能凑次数；到期工具离开活跃索引；依赖仍被引用时不发生破坏性删除。

### 阶段 4 — 复杂 Tool、Review 与回滚

**交付：**工作流/嵌套模型工具、Review 分级、Research 证据/时效元数据、环境 fingerprint、固定版本依赖、升级回滚。  
**验收：**至少一个复合 Coding Tool 和一个 Research Tool 可稳定执行；新版本回归失败能恢复旧版；Research 输出具有可回溯来源。

### 阶段 5 — 冷热任务对照评测和调参

**交付：**基线 vs ATR 的可复现实验、成本仪表、检索命中率、创建率、Router 兜底比例、错误率，随后调整 TTL/晋升阈值。  
**验收：**不能仅凭 Tool 数量增长宣称有效；必须在保持任务质量的前提下观察到端到端净收益；未达标则关闭自动创建或降低候选捕获强度。

### 优先开发顺序

`控制接口与隔离 → Direct 执行与成本日志 → Candidate 捕获与检索 → Temporary 计数与晋升 → 工作流和 Research → 评测优化`

原因：第一版首先证明**在不牺牲完成质量的情况下，B+C 能否稳定减少重复实现**，然后再增加复杂能力工具。不要一开始就开发自动技能组合、向量索引或复杂的 Tool 价值评分。

## 11. 验证实验设计

### 11.1 任务集

准备至少约 40 个来自本人真实工作的任务记录（例如 Coding 20、Research 20；数量是初始样本建议，不是统计显著性的保证）。将其中部分重复在不同时间、不同参数和小幅环境变化下运行。设置两条基线：

- **Baseline：**相同主模型、相同终端/文件/网络权限、相同系统提示预算，但关闭动态 Candidate/注册/复用功能。
- **ATR：**开启 Router + Candidate + Tool 生命周期；同一任务预算和执行环境。

分别比较首次任务（冷启动）、重复任务（热复用）、高频工作流和高复杂度 Research。避免把热复用成本与基线冷启动成本混淆。评测任务尽量匹配难度并交叉执行，记录外部数据发生变化的情况。

### 11.2 测量指标

| 维度 | 指标 |
|---|---|
| 质量（硬条件） | Coding 单测/构建成功、任务需求满足；Research 引用准确、事实支持程度（必要时人工盲评） |
| Token | 主 Agent + Router + Creator + Reviewer + 嵌套模型累计输入/输出 Token |
| 费用 | 真实模型/API 账单估计、CPU 与存储消耗 |
| 时延 | 每任务总时间、首次创建开销、复用执行时间、p50/p95 |
| 复用 | Tool 命中率、Candidate 复用率、重复创建率、真实有效调用数 |
| 路由 | 规则直接决策率、低成本模型介入率、错误选择率 |
| 安全 | 越权执行次数、误删、重复副作用、提示注入诱导成功率 |

**建议的第一轮目标（待测，不是保证）：**

- 高频重复任务中位数计费 Token 降低 ≥15%；
- 当前任务成功率不明显下降，尤其不能产生未经授权的高影响操作；
- 对主要一次性任务，Router 和管理层附加成本保持较低；
- 创建数量、Registry 大小不呈现无法解释的失控增长；
- 检索失败、误复用、重复创建可通过日志定位并修复。

如果质量下滑或净成本上升，优先回退到 Direct + 轻量 Candidate 捕获，而不是为维持“自进化”叙事继续增加复杂度。

## 12. 测试清单（必须覆盖）

- [ ] 直接 `rg/git status/pytest` 不产生 Tool 候选。
- [ ] 新编写、成功执行的独立 Python 程序会保存托管 Candidate。
- [ ] 项目业务源码文件的修改不会被误认为工具。
- [ ] 已有 Tool/Candidate 可检索；创建前会去重。
- [ ] 不兼容环境或输入 Schema 的工具不得被直接复用。
- [ ] Router 歧义时最多触发一次低成本模型，失败退回主 Agent。
- [ ] Dynamic Tool 源码无法读写控制层 DB、策略和敏感凭证。
- [ ] 恶意网页文本/README 无法诱导跳过权限检查。
- [ ] stdout/大文件不完整塞入 LLM 上下文，Artifact 可按需读取。
- [ ] 失败、重试和重复记账不会触发晋升。
- [ ] 临时工具达到阈值会固化；到期未达标会归档。
- [ ] 复合工具依赖受保护，不会因子工具删除而失效。
- [ ] 工具升级保留旧版且可原子回滚。
- [ ] 外部写入操作经授权；有副作用的调用不会自动盲重试。
- [ ] Research 工具重新执行时能更新时效性证据并保存引用。
- [ ] 日志包含总成本与执行链路，敏感信息得到脱敏。
- [ ] kill switch 可立即停用动态创建和执行，而 Direct 安全路径可继续运行。

## 13. 重要剩余风险（不是阻塞设计的 TODO）

1. **未来重复性不可预测：**观察期阈值可能漏掉间歇性需求，这是成本优先的可接受取舍；保留短期归档缓冲。
2. **检索错误仍会出现：**即使用 FTS5，也无法保证语义等价识别。以重复创建率和人工抽查反馈改进，不在第一版追求完美检索。
3. **Research 正确性难以自动证明：**工具可以自动化流程，但事实评价仍需要可信证据和必要人工核验。
4. **复杂能力工具可能更贵：**内部模型调用可以增强任务能力，但未必节省 Token 或金钱；按任务质量和净费用分别考量。
5. **沙盒不是万能安全保证：**需要评估宿主系统、网络代理、挂载目录及容器逃逸风险，不能仅靠提示词或代码静态扫描。
6. **工具库增长不能证明能力增长：**只有成功解决任务的范围扩大或成本下降，才是有意义的结果。

## 14. 参考设计依据

- OpenAI Tool Search（工具定义按需加载与缓存）：https://developers.openai.com/api/docs/guides/tools-tool-search
- Anthropic Tool Search / Prompt Caching：https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool ；https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching
- Anthropic Programmatic Tool Calling（复杂调用工作流的 Token 收益与适用条件）：https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling
- SQLite FTS5：https://www.sqlite.org/fts5.html
- Docker Rootless 安全模型与限制：https://docs.docker.com/engine/security/rootless/
- OWASP Agentic Tool Execution 安全实践：https://cornucopia.owasp.org/cards/AAI8
- OpenTelemetry GenAI Metrics：https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-metrics/
- OpenAI Agent Workflow Evals：https://developers.openai.com/api/docs/guides/agent-evals

---

**最终结论：**先把“低成本、按需创建、可验证复用”的运行时做好，再考虑自动组合和增强复杂能力。工具创建不是目的，能以更少的重复推理和更低的总成本可靠完成当前 Coding/Research 任务，才是 ATR 的验收目标。
