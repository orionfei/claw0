# LLM Agent Gateway / Agent Runtime

一个以单文件实现为核心的个人项目，用来展示我如何从零设计并实现一个可运行的 LLM Agent Runtime。仓库中的核心入口是 [agent.py](agent.py)：它把 **Agent Loop、Tool Calling、Memory、Gateway Routing、Session Isolation、Heartbeat/Cron、Delivery Queue、Failover、Named-Lane Concurrency** 串成了一个完整的可执行系统。

这个实现是一个 **interview-study gateway / runnable architecture scaffold**：当前默认使用 `MockLLM`，可以离线运行，不依赖真实外部模型服务；但整体代码结构、状态流转、故障处理和模块边界，都是按真实 Agent Gateway / Agent Runtime 的思路组织的，适合作为 GitHub 项目展示、面试讲解样例，以及后续演进成真实服务的基础。

## 为什么做这个项目

很多 “Agent 教程” 只停留在一次 API 调用，或者只展示一个最小聊天循环。但如果一个 Agent 要进入真实环境，还需要解决这些问题：

- 如何让模型进入完整的 `Reasoning -> Tool Use -> Observation` 闭环。
- 如何把工具调用设计成稳定的 `Function Calling + JSON Schema` 框架。
- 如何管理长期记忆、短期会话和上下文溢出。
- 如何做多 Agent 路由、会话隔离和后台任务调度。
- 如何在发送失败、模型失败、上下文过载时继续稳定运行。

`workspace/claw.py` 的目标，就是把这些能力压缩进一个可阅读、可运行、可讲解的单文件 Agent Runtime 中。

## 核心能力概览

- **Agent Loop**: 通过 `run_turn()` 驱动多轮推理，基于 `stop_reason` 判断是否继续工具调用。
- **Tool-Calling Framework**: 通过 `ToolRegistry` 维护工具 schema、handler dispatch 和调用结果回灌。
- **Agent Memory System**: 同时管理 `SessionStore`、`MEMORY.md`、daily memory JSONL、自动 recall 与 `ContextGuard`。
- **Agent Orchestration Runtime**: 通过 `BindingTable`、`build_session_key()`、`CommandQueue` 和 named lanes 实现路由、隔离和调度。
- **Agent Reliability**: 通过 `DeliveryQueue`、`DeliveryRunner`、`ResilienceRunner`、profile rotation 和 fallback model 提升稳定性。
- **Autonomy**: 通过 `HeartbeatRunner` 与 `CronService` 支持主动任务与定时任务。

## 架构图

```text
                         +------------------------------+
Inbound Message -------->|  InterviewGateway.run_turn  |
(cli / telegram / etc.)  +--------------+---------------+
                                        |
                                        v
                         +------------------------------+
                         | Gateway Routing              |
                         | BindingTable.resolve()       |
                         | build_session_key()          |
                         | Session Isolation            |
                         +--------------+---------------+
                                        |
                                        v
                         +------------------------------+
                         | Prompt Assembly              |
                         | PromptBuilder                |
                         | SkillsManager                |
                         | MemoryStore recall_block()   |
                         +--------------+---------------+
                                        |
                                        v
                         +------------------------------+
                         | ResilienceRunner             |
                         | profile rotation             |
                         | overflow compaction          |
                         | fallback model chain         |
                         +--------------+---------------+
                                        |
                                        v
                         +------------------------------+
                         | Agent Loop                   |
                         | stop_reason                  |
                         | tool_use -> tool_result      |
                         | multi-turn iteration         |
                         +--------------+---------------+
                                        |
                    +-------------------+-------------------+
                    |                                       |
                    v                                       v
         +-----------------------+             +-----------------------+
         | ToolRegistry          |             | SessionStore          |
         | JSON Schema           |             | append-only JSONL     |
         | read/list/time        |             | history rebuild       |
         | memory_write/search   |             | ContextGuard          |
         +-----------------------+             +-----------------------+
                                        |
                                        v
                         +------------------------------+
                         | DeliveryQueue                |
                         | write-ahead persistence      |
                         | retry/backoff                |
                         | failed queue recovery        |
                         +--------------+---------------+
                                        |
                                        v
                                  Final Delivery

Background lanes:
  heartbeat lane -> HeartbeatRunner
  cron lane      -> CronService
  main lane      -> user-facing synchronous work
```

## 简历项目点位与代码映射

这一节逐条对应我在简历中对项目的描述，并明确它们在 `workspace/claw.py` 中是如何落地的。

### 1. 基于 LLM Agent Architecture 构建完整 Agent Loop

简历表述：

> 设计并实现基于 LLM Agent Architecture 的智能代理系统，构建完整 Agent Loop（Reasoning -> Tool Use -> Observation），实现多轮推理与自动工具调用。

代码落地：

- `InterviewGateway.run_turn()` 是整个 agent turn 的入口。
- 模型响应返回 `LLMResponse(stop_reason, content)`。
- 当 `stop_reason == "tool_use"` 时，runtime 会遍历所有 `tool_use` block，调用 `ToolRegistry.call()` 执行工具。
- 工具结果被包装成 `tool_result` block，再作为下一轮 user content 回灌给模型，这就是 Observation。
- 当 `stop_reason == "end_turn"` 时，agent 本轮结束，输出最终文本。
- `for _ in range(12)` 给 agent loop 设置了多轮推理上限，防止无限调用工具。

也就是说，这个 runtime 不是“一问一答”的包装器，而是一个真正完成了 `Reasoning -> Tool Use -> Observation` 闭环的 Agent Loop。

### 2. Tool-Calling Framework（Function Calling + JSON Schema）

简历表述：

> 构建 Tool-Calling Framework（Function Calling + JSON Schema），实现 Agent 与文件系统、时间服务及 Memory API 的交互执行能力。

代码落地：

- `ToolRegistry` 维护两张核心表：
  - `schemas`: 暴露给模型的工具定义。
  - `handlers`: `tool_name -> python callable` 的 dispatch table。
- `register()` 接收 `name / description / input_schema / handler`，其中 `input_schema` 就是 JSON Schema。
- `call()` 根据工具名找到 handler，并对参数错误、未知工具、执行异常进行统一包装。
- 内置工具包括：
  - `read_file`
  - `list_directory`
  - `get_current_time`
- runtime 启动时还会通过 `_install_memory_tools()` 动态注册：
  - `memory_write`
  - `memory_search`

因此，整个 Tool-Calling Framework 具备了标准的 “Function Calling + JSON Schema + Handler Dispatch” 形态，模型只需要输出工具名和参数，runtime 就能完成执行。

### 3. Agent Memory System（Session + Retrieval Recall）

简历表述：

> 设计 Agent Memory System（Session + Retrieval Recall），实现长短期记忆管理、历史上下文压缩（Context Guard）与自动记忆检索，提升多轮任务连续性。

代码落地：

- **Session memory / short-term memory**
  - `SessionStore` 负责会话持久化。
  - 每个 session 使用 append-only JSONL 存储消息历史。
  - `load_messages()` 会把 JSONL 重建成模型可直接消费的 `messages` 结构。
- **Long-term memory**
  - `MemoryStore` 同时读取 evergreen memory [workspace/MEMORY.md](workspace/MEMORY.md)。
  - 也把每日写入的记忆保存在 `workspace/.interview_data/memory/daily/*.jsonl`。
- **Retrieval Recall**
  - `MemoryStore.search()` 基于纯 Python TF-IDF + cosine similarity 做检索。
  - `recall_block()` 会把与当前输入最相关的记忆片段拼成 prompt block。
  - `run_turn()` 每次调用模型前，都会执行 `self.memory.recall_block(inbound.text, top_k=3)`，这就是自动 recall。
- **Context Guard**
  - `ContextGuard.estimate_messages_tokens()` 估算上下文体积。
  - `truncate_tool_results()` 先截断超大的 `tool_result`。
  - `compact_history()` 再对较旧历史做 summary-style 压缩，保留最近消息继续运行。

所以这里的 Memory System 不是单独的 KV 存储，而是把 `Session + Retrieval Recall + Context Guard` 合成进 agent runtime 的上下文生命周期中。

### 4. Agent Orchestration Runtime

简历表述：

> 实现 Agent Orchestration Runtime：包括任务调度（Named-Lane Concurrency）、多 Agent 路由（Gateway Routing）与 Session Isolation，支持复杂任务执行流程。

代码落地：

- **Gateway Routing**
  - `BindingTable` 保存路由规则。
  - `resolve()` 按 tier 顺序进行匹配。
  - 当前 demo 中包含默认路由、channel 路由和特定 peer 路由。
- **Session Isolation**
  - `build_session_key()` 按 `agent_id / channel / account_id / peer_id / dm_scope` 生成 session key。
  - `dm_scope` 支持不同粒度的隔离策略，例如 `per-peer`、`per-channel-peer` 等。
  - 这让同一个 runtime 在多用户、多渠道、多 agent 情况下仍能保持会话边界。
- **Named-Lane Concurrency**
  - `LaneQueue` 是核心并发原语，每条 lane 都是一个独立 FIFO 队列。
  - `CommandQueue` 管理多个 lane，并按名称派发任务。
  - 当前默认 lane：
    - `main`
    - `heartbeat`
    - `cron`
  - 每条 lane 都支持 `max_concurrency`、`Future` 结果回传和 generation tracking。
- **Background orchestration**
  - `HeartbeatRunner` 在 `heartbeat` lane 中执行主动检查。
  - `CronService` 在 `cron` lane 中调度定时任务。
  - 用户输入通过 `main` lane 运行，不会被后台任务打乱顺序。

这部分组成了一个真正的 Agent Orchestration Runtime，而不是单一请求处理函数。

### 5. Agent Reliability

简历表述：

> 构建 Agent Reliability 机制：包括工具调用重试、模型 Failover、Context Overflow 压缩与 Delivery Queue，提升 Agent 系统在真实环境中的稳定性。

代码落地：

- **Delivery Queue**
  - `DeliveryQueue.enqueue()` 先写磁盘再发送。
  - `_write_atomic()` 使用 `tmp + fsync + os.replace` 实现 write-ahead persistence。
  - `DeliveryRunner` 后台扫描 pending 队列并执行实际投递。
  - 失败时调用 `fail()` 写回新的 `retry_count / next_retry_at / last_error`。
  - 超过 `MAX_RETRIES` 后移动到 `failed/` 目录。
- **retry / backoff**
  - `compute_backoff_ms()` 使用 `[5s, 25s, 120s, 600s]` 的阶梯退避，并加入 jitter。
  - `/retry-failed` 可把 failed 队列重新放回 pending。
- **model failover**
  - `ResilienceRunner` 是重试与 failover 核心。
  - 第一层：`ProfileManager` 在多个 `AuthProfile` 之间轮换。
  - 第二层：如果识别为 `overflow`，优先做上下文压缩，而不是直接失败。
  - 第三层：主模型失败后尝试 `fallback_models`。
- **failure classification**
  - `classify_failure()` 将异常分成 `rate_limit / auth / timeout / billing / overflow / unknown`。
  - 不同失败会触发不同 cooldown 策略。
- **context overflow compaction**
  - 上下文过大时优先截断 tool result。
  - 仍过大时再执行 history compaction。

这一套机制使得 agent runtime 能同时处理发送失败、模型失败、配额问题和上下文膨胀，而不是只在 happy path 下工作。

## 模块拆解

### 1. Tool Registry

核心类型：

- `ToolRegistry`
- `LLMResponse`

职责：

- 注册工具
- 持有 JSON Schema
- 维护工具 handler dispatch
- 执行文件系统、时间、Memory API 工具

当前真实工具接口：

```text
read_file(file_path: str)
list_directory(directory: str = ".")
get_current_time()
memory_write(content: str, category: str = "general")
memory_search(query: str, top_k: int = 5)
```

### 2. Session Persistence 与 Context Guard

核心类型：

- `SessionStore`
- `ContextGuard`

职责：

- 会话到 session id 的映射
- append-only JSONL 持久化
- 历史消息重放
- tool result 截断
- 长历史压缩

### 3. Memory 与 Prompt Intelligence

核心类型：

- `MemoryStore`
- `SkillsManager`
- `PromptBuilder`

职责：

- 读取 evergreen memory 与 daily memory
- 基于 TF-IDF 做 Retrieval Recall
- 扫描 `SKILL.md`
- 组装多层 system prompt

`PromptBuilder.build()` 当前会拼接这些信息层：

- `IDENTITY.md`
- `SOUL.md`
- `TOOLS.md`
- skills block
- `MEMORY.md` + auto recall
- `HEARTBEAT.md`
- `BOOTSTRAP.md`
- `AGENTS.md`
- `USER.md`
- runtime context
- channel hint

### 4. Gateway / Routing / Session Isolation

核心类型：

- `Binding`
- `BindingTable`
- `build_session_key()`

职责：

- 将不同 channel / peer 的请求路由到不同 agent
- 控制 session 隔离边界
- 为多 agent 运行时提供统一入口

### 5. Delivery / Reliability / Failover

核心类型：

- `QueuedDelivery`
- `DeliveryQueue`
- `DeliveryRunner`
- `FailoverReason`
- `AuthProfile`
- `ProfileManager`
- `ResilienceRunner`

职责：

- 可恢复消息投递
- retry / backoff
- failed queue 管理
- model profile rotation
- fallback model
- context overflow recovery

### 6. Concurrency / Scheduling

核心类型：

- `LaneQueue`
- `CommandQueue`
- `HeartbeatRunner`
- `CronService`

职责：

- named-lane concurrency
- 主流程与后台流程隔离
- heartbeat 主动检查
- cron 定时任务调度

## 如何运行

### 环境要求

- Python 3.11+
- Windows / macOS / Linux 均可，当前仓库默认在本地命令行中运行

### 安装

```bash
git clone <your-repo-url>
cd claw0
pip install -r requirements.txt
```

说明：

- `workspace/claw.py` 当前默认使用 `MockLLM`，所以即使没有真实模型 API，也可以离线运行。
- `requirements.txt` 保留了进一步扩展到真实 gateway / real provider 时会用到的依赖。

### 启动

```bash
python workspace/claw.py
```

启动后你会看到一个本地 REPL。默认会启动：

- main lane
- heartbeat lane
- cron lane
- delivery runner

## 如何与这个 Agent 交互

### 直接输入消息

可以直接输入自然语言，让 `MockLLM` 触发工具调用：

```text
read README.md
list .
time
remember I prefer concise answers
recall concise
```

这些输入会映射到真实工具：

- `read README.md` -> `read_file`
- `list .` -> `list_directory`
- `time` -> `get_current_time`
- `remember ...` -> `memory_write`
- `recall ...` -> `memory_search`

### Slash Commands

运行时还提供了一组用于观察 runtime 状态的命令：

```text
/help
/send <channel> <peer> <txt>
/bindings
/profiles
/simulate-failure <reason>
/lanes
/reset-lanes
/queue
/failed
/retry-failed
/status
/heartbeat
/trigger-heartbeat
/cron
quit
exit
```

推荐重点体验这些命令：

- `/bindings`: 查看 Gateway Routing 规则
- `/profiles`: 查看 profile pool、cooldown 与 failover 状态
- `/simulate-failure rate_limit`: 观察 profile rotation
- `/simulate-failure overflow`: 观察 context overflow recovery
- `/lanes`: 查看 named lanes 的活跃度和排队情况
- `/queue`: 查看 delivery queue
- `/status`: 查看整个 runtime 的系统快照
- `/cron`: 查看定时任务配置是否已加载

## 建议的演示路径

如果这是一个用于面试展示的仓库，建议按下面顺序演示：

### Demo 1: Agent Loop + Tool Calling

```text
read README.md
list .
time
```

展示重点：

- 模型返回 `tool_use`
- runtime 调用工具
- 工具结果作为 `tool_result` 回灌
- agent 在同一 turn 内继续推理并输出最终结果

### Demo 2: Memory API + Retrieval Recall

```text
remember I prefer concise answers
recall concise
```

展示重点：

- `memory_write` 会写入 daily memory JSONL
- `memory_search` 会检索历史记忆
- 后续输入也会触发自动 recall 注入 prompt

### Demo 3: Gateway Routing + Session Isolation

```text
/bindings
/send cli alice hello
/send telegram bob hello
/send discord admin-001 hello
```

展示重点：

- 不同 channel / peer 命中不同 binding tier
- 不同 agent / peer 会形成不同 session key
- runtime 可以模拟多 agent 路由

### Demo 4: Reliability + Failover

```text
/profiles
/simulate-failure rate_limit
time
/simulate-failure auth
time
/simulate-failure overflow
read README.md
```

展示重点：

- `classify_failure()` 如何分类异常
- `ProfileManager` 如何让 profile 进入 cooldown
- `ResilienceRunner` 如何进行 model failover 与 overflow compaction

### Demo 5: Concurrency + Background Runtime

```text
/lanes
/heartbeat
/trigger-heartbeat
/cron
/status
```

展示重点：

- `main / heartbeat / cron` 三条 lane 的职责分离
- 后台任务不会阻塞主用户交互
- runtime 拥有持续运行与后台调度能力

## Workspace 配置文件说明

`workspace/` 目录中的 markdown / json 文件是这个 agent 的“外部可配置运行时上下文”，不是装饰性文档。

### Bootstrap / prompt files

- [workspace/IDENTITY.md](workspace/IDENTITY.md)
  - 定义 agent 的身份基线。
- [workspace/SOUL.md](workspace/SOUL.md)
  - 定义 agent 的人格、语气和行为风格。
- [workspace/TOOLS.md](workspace/TOOLS.md)
  - 定义工具使用规范。
- [workspace/USER.md](workspace/USER.md)
  - 存放用户侧偏好或上下文说明。
- [workspace/AGENTS.md](workspace/AGENTS.md)
  - 描述多 agent 运行方式和隔离边界。
- [workspace/BOOTSTRAP.md](workspace/BOOTSTRAP.md)
  - 提供额外系统初始化说明。
- [workspace/HEARTBEAT.md](workspace/HEARTBEAT.md)
  - 定义 heartbeat 定时检查的目标与输出规则。
- [workspace/MEMORY.md](workspace/MEMORY.md)
  - 提供 evergreen memory，作为长期记忆基底。

### Scheduled task config

- [workspace/CRON.json](workspace/CRON.json)
  - 定义定时任务列表，由 `CronService` 加载。

### Skills

- [workspace/skills/example-skill/SKILL.md](workspace/skills/example-skill/SKILL.md)
  - 示例 skill 文件，`SkillsManager` 会自动扫描并注入 prompt。

## 持久化数据目录

运行 `python workspace/claw.py` 后，数据会写入 `workspace/.interview_data/`。

```text
workspace/.interview_data/
  sessions/             会话 JSONL 与 session map
  delivery-queue/       pending delivery items
  delivery-queue/failed/ 投递失败的消息
  state/                预留状态目录
  memory/daily/         daily memory JSONL
  cron/cron-runs.jsonl  cron 执行记录
```

这些目录分别对应：

- **sessions**: `SessionStore` 的短期会话历史
- **delivery queue**: `DeliveryQueue` 的待投递消息
- **failed deliveries**: 达到最大重试后移动的失败消息
- **state**: 运行时预留状态空间
- **daily memory**: `memory_write` 的落盘位置
- **cron run log**: `CronService` 的执行日志

## 这个项目当前真实实现了什么，没实现什么

### 已实现

- 完整的 Agent Loop
- Function Calling + JSON Schema 风格的工具系统
- Session + Retrieval Recall + Context Guard
- Gateway Routing + Session Isolation
- Named-Lane Concurrency
- Heartbeat / Cron 背景任务
- Delivery Queue + retry/backoff
- failure classification + profile rotation + fallback model

### 当前未实现或故意简化

- 没有接真实外部 LLM API，默认使用 `MockLLM`
- 没有完整 HTTP / WebSocket 网关服务层
- 没有真实多进程 / 多机部署
- 没有生产级权限系统、监控系统和外部存储
- 当前 memory retrieval 使用纯 Python TF-IDF，而不是向量数据库

因此，README 中把它描述为 **single-file agent runtime / interview-study gateway** 是准确的；把它描述成“已上线的生产集群系统”则不准确。

## 项目亮点总结

如果用一句话概括这个项目：

> 这是一个用单文件 Python 运行时完整演示 LLM Agent Gateway 核心机制的个人项目，重点覆盖 Agent Loop、Tool-Calling Framework、Memory System、Gateway Routing、Session Isolation、Named-Lane Concurrency、Failover、Context Overflow 压缩与 Delivery Queue。

如果用工程角度概括：

- 它展示了一个 agent 从输入、路由、记忆、提示词组装、模型调用、工具执行、消息投递到后台调度的完整生命周期。
- 它把简历里提到的每个高级概念都落实到了具体代码结构，而不是停留在概念描述。
- 它适合作为后续扩展真实模型 provider、真实消息渠道、真实工具集和真实多 agent 服务的基础骨架。

## 后续可扩展方向

- 将 `MockLLM` 替换成真实 provider SDK
- 为 `ToolRegistry` 增加写文件、编辑文件、shell 等受控工具
- 为 `MemoryStore` 接入 embedding / vector DB
- 为 `BindingTable` 增加可热更新配置
- 为 `DeliveryRunner` 增加真实 Telegram / Discord / Slack 适配器
- 为 named lanes 增加 metrics、tracing 和限流控制

## License

MIT
