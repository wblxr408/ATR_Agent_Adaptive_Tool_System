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

新增 Python 3.12 包 atr、JSON Schema、测试、示例、文档及 OCI 镜像定义。依赖 Pydantic、jsonschema、pytest、ruff、mypy。实际沙盒执行依赖本机 Linux Docker 引擎与预先构建的镜像；模型/网络访问仅经宿主配置的适配器。真实 40 项工作任务、模型账单和质量盲评须由实际工作产生，不能以合成数据冒充。
