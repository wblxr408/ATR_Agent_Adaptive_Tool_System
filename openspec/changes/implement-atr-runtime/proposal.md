# Proposal

## Why

仓库只有 ATR v1.0 设计，需要把按需复用工具、受控执行、有效计数及成本观测落实为可安装的单用户 Python Runtime。实现完整的控制链路，同时明确区分功能证据、部署隔离证据及真实经济收益。

## What Changes

- 实现固定五接口、CLI/JSONL Agent 协议、SQLite WAL/FTS5 和内容寻址存储。
- 实现默认 Docker 隔离执行、授权、预算、取消、输出脱敏、Artifact 分页及持久运行记录。
- 实现四路 Router、候选捕获、去重、版本 Review、固定依赖、工作流与 Research/模型代理。
- 实现观察期、晋升、归档、休眠、恢复、引用保护和控制开关。
- 提供公平配对的评测记录、成本报告、Coding/Research 示例和部署说明。

## Capabilities

### New Capabilities

- `controlled-execution`: 固定接口、权限隔离、运行状态及受限结果交付。
- `adaptive-tools`: 路由、候选、Registry、版本、Review、依赖与生命周期。
- `evidence-accounting`: 研究证据、事件、全路径费用和配对评测。

### Modified Capabilities

无。

## Impact

2026-10-23 用户授权7天真实使用量观测：新增可停用数值探针、客户端用量增量去重、按需提示与MCP调用计数、前7天观察基线及期满比较。个人数据仅保存在本地，不上传GitHub；不制造付费模型实验或把前后变化当作因果收益。

2026-10-22 本机接入增量：理论盈亏平衡分析；显式受信 NativeSandbox、内置纯工具、真实 stdio MCP、按命中注入的 hook 和保留配置的双客户端安装器。无镜像路径不提供 OS 隔离，不接受模型自动审批的新代码；Docker 保留为独立的隔离后端。MCP SDK 2.2 为可选 integration 依赖，Python 支持范围明确为3.12–3.14。

2026-10-22 安全增量：修复实现审查 R1—R6。将恶意或被注入的调用 Agent 纳入威胁模型，新增仅供宿主使用的可信任务/操作签发和独立验收；所有执行默认需要宿主绑定上下文与固定操作许可。此为有意收紧的 API 迁移，旧的任意 root_task_id 和自拟 Review 不再授予执行权。保持单用户本地控制层，不新增 A2A 服务，不开展成本收益实验。

新增 Python 3.12 包 atr、JSON Schema、测试、示例、文档及 OCI 镜像定义。依赖 Pydantic、jsonschema、pytest、ruff、mypy。实际沙盒执行依赖本机 Linux Docker 引擎与预先构建的镜像；模型/网络访问仅经宿主配置的适配器。真实 40 项工作任务、模型账单和质量盲评须由实际工作产生，不能以合成数据冒充。
