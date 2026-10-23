# 一周本地使用量观测

用户明确授权的一次7天真实使用量采集。原本“不运行收益实验”的限制调整为允许本次被动观测；不新增付费模型调用、不重放任务制造对照、不做随机分组，不改业务工具选择。

## 安装与窗口

```powershell
uv run --extra integration python -m atr.usage_probe start
uv run --extra integration atr-native install-clients
```

本机窗口固定为北京时间 **2026-10-23 09:52:26至2026-10-30 09:52:26**，前7天作为历史观察基线。配置在用户数据目录 atr/native/probe.json；重复start、重装或重连不延长期限。新配置需客户端重载，Codex新增/变更hook仍需 /hooks 信任。

UserPromptSubmit在原ATR提示进程内记录是否匹配、提示字符数和逻辑耗时；Stop/SessionEnd/SubagentStop收尾hook增量读客户端JSONL。MCP记录客户端、检索命中、执行尝试、不同run_id、状态、方法耗时及JSON载荷字节数。失败采集不阻断实际任务，没有新的常驻服务、付费路由或记忆observer。

窗口到期后，数值事件不再新增，收尾hook也不再继续扫描。最终report允许只补齐固定时间范围内的旧日志；`stop`关闭本次采集并删除专用收尾hook，保留ATR MCP、原有按需提示和其他hook。

```powershell
uv run --extra integration python -m atr.usage_probe status
uv run --extra integration python -m atr.usage_probe report
uv run --extra integration python -m atr.usage_probe stop
```

报告位于用户数据目录 atr/native/weekly-report.md 与 weekly-report.json；数值库为 usage.sqlite3。数据不提交GitHub、不发送遥测服务器。需要提前退出时执行stop，不必等待一周。

## 口径和数据边界

Codex按total_token_usage增量计算，重复累计值不再相加；初始累计值包含未归属历史前缀时优先使用last_token_usage，并标记覆盖缺口。计数重置后重新分段。Claude按消息ID去重，流式重复片段保留最大已见计数；总输入为input_tokens + cache_read_input_tokens + cache_creation_input_tokens。缓存输入单列，推理输出作为输出子集，避免重复加入总量。[Claude官方计数定义](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#tracking-cache-performance)

仅接收配置允许的 ~/.codex/sessions、archived_sessions 和 ~/.claude/projects 下JSONL，解析后只存模型、来源匿名标识、数值及时间。prompt、对话正文、代码、工具入出参和文件路径不进入数值库。读取有上限，未完成末行留待追加；超大行受限跳过后继续扫描，跳过数量、解析异常和未归属前缀进入覆盖说明。

客户端日志格式不是稳定接口，升级后可能缺失计数；Codex官方也明确了transcript_path格式的这一边界。[Codex hooks](https://learn.chatgpt.com/docs/hooks)、[Claude hooks](https://code.claude.com/docs/en/hooks)

## 可以比较的内容

报告按客户端和模型分列前7天/本周的输入、缓存读写、输出、已见会话与样本数；带ATR调用的会话分组仅为观察分类。ATR调用尝试与不同运行分开，重复业务键不伪装成多次实际复用；提示/检索命中、失败和耗时用于判断是否常用、是否有明显额外负担。

方法耗时和hook逻辑计时不覆盖全部进程启动、记录落库、客户端及模型耗时。字符/字节不是Token，未把它们换算成实测Token或金额。当前未采集供应商实际账单，因此费用节省保持unknown/null；订阅额度、调用输入量和现金支出也不能混为一谈。

前后两周的工作量、模型、缓存比例和任务质量可能变化。没有同任务无ATR对照，所以即使本周Token减少也不证明ATR带来因果收益；没有样本或零计数不证明免费。采集未满7天的报告明确标注未满期。最初本地读取中CC前7天没有可用样本，这是一项覆盖缺口。

## 本次功能证据

新增9项本次观测行为用例通过：固定期限、前后窗口增量、Codex重复计数、Claude消息去重和缓存口径、无正文持久化、末行追加、越界路径拒绝、期满不再记录、重试与不同运行分开、真实配置hook、真实MCP调用指标、超大行后续读取及仅移除探针hook。部分用例包含多个关联断言。静态检查沿用项目Ruff/mypy；未运行历史回归套件、付费模型或伪造一周实验。

继续适用已有A2AB v1.0的08“结构化授权请求与权限不放大”和11“中断任务租约与有界资源回收”（工程解决方案/验收办法）：日志路径限定、数值字段白名单、固定期限、读取边界和fail-open。探针不签发身份或执行程序，01/06/10的已有程序/版本/全链预算控制保持原执行链；其余协议专用项目沿用安全文档中的非适用性评估。
