# DB-GPT Memory + Message 系统闭环深度理解

> 本文档系统梳理 DB-GPT 0.8.1 的 memory（记忆）与 message（消息）系统如何**闭环**运作——从用户提问到最终回答，记忆与消息在每个环节如何流转、持久化、回写。并给出面向 excel-workbook-service（Excel Agent）的移植设计方案。
>
> 研究范围：`packages/dbgpt-core/src/dbgpt/agent/core/memory/`、`packages/dbgpt-core/src/dbgpt/core/interface/message.py`、`packages/dbgpt-app/src/dbgpt_app/scene/base_chat.py`、`packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py`。

---

## 1. 双轨闭环架构总览

DB-GPT 的对话系统有**两套平行、但通过 `conv_id` 关联**的存储/记忆体系：

```
                        ┌──────────────────────────────────────────────┐
  用户提问 ────────────▶ │ 轨道 A: StorageConversation (chat_history)   │ ← 面向用户/前端
                        │   chat_history + chat_history_message 表      │   侧边栏历史、消息列表
                        └──────────────────────────────────────────────┘
                                        │ 关联 conv_id（同一会话）
                        ┌──────────────────────────────────────────────┐
  工具调用 / Agent循环 ─▶ │ 轨道 B: AgentMemory + GptsMemory            │ ← 面向 agent
                        │   gpts_messages + gpts_plans 表              │   每步 Thought/Action/Obs
                        └──────────────────────────────────────────────┘
```

- **轨道 A（StorageConversation）**：记录"用户视角"的完整对话——每轮的用户提问、AI 回复、前端渲染内容（view），持久化到 `chat_history` + `chat_history_message` 表。供历史会话加载、前端展示、多轮上下文组装。
- **轨道 B（AgentMemory + GptsMemory）**：记录"agent 视角"的工具调用记忆——每个 ReAct 步骤的 `Thought / Action / Action Input / Observation`，以 `GptsMessage`（含 JSON 化的 `ActionOutput`）持久化到 `gpts_messages` 表。供 ReAct agent 在循环中读取历史工具调用，避免重复操作。

**为什么双轨？** 用户看到的是干净的问题与回答（轨道 A），而 agent 需要的原始工具调用轨迹（轨道 B）通常噪声大、不适合直接展示给用户。两者独立存储，但同一会话共享 `conv_id`，删除会话时一并清理。

---

## 2. 核心类与文件

### 2.1 Memory 抽象体系

**文件**：`packages/dbgpt-core/src/dbgpt/agent/core/memory/base.py`（抽象基类）

```python
class Memory[T]:   # 泛型抽象，T 为 MemoryFragment
    async def write(fragment, now=None, op=ADD) -> Optional[Discarded...]
    async def write_batch(fragments, now=None) -> ...
    async def read(observation, alpha=None, beta=None, gamma=None) -> List[T]
    async def clear() -> List[T]
    def structure_clone(now=None) -> Memory[T]   # 浅克隆，用于重置 agent 记忆
    def initialize(name, llm_client, importance_scorer, insight_extractor, ...)
    async def get_insights(fragments) -> List[Insight...]
    async def score_memory_importance(fragments) -> List[float]
```

实现层次（自下而上）：

| 类 | 文件 | 核心逻辑 |
|---|---|---|
| `SensoryMemory` | `base.py` | 感知缓冲（size=N），溢出按重要性阈值转短时 |
| `ShortTermMemory` | `base.py` | 短时缓冲（默认 size=5），溢出转长时 |
| `EnhancedShortTermMemory` | `short_term.py` | 短时 + embedding 相似度去重合并 |
| `LongTermMemory` | `long_term.py` | 向量存储（ChromaDB），TimeWeightedEmbeddingRetriever |
| `HybridMemory` | `hybrid.py` | 串联 Sensory→ShortTerm→LongTerm 三层管道 |
| `AgentMemory` | `agent_memory.py` | **聚合入口**：内部 `ShortTermMemory` + `GptsMemory` |

### 2.2 AgentMemory 与 GptsMemory（轨道 B 核心）

**文件**：`packages/dbgpt-core/src/dbgpt/agent/core/memory/agent_memory.py`、`gpts/gpts_memory.py`

```python
class AgentMemory(Memory[AgentMemoryFragment]):
    def __init__(self, memory=None, ..., gpts_memory=None):
        self.memory = memory or ShortTermMemory(buffer_size=5)   # 短时记忆
        self.gpts_memory = gpts_memory or GptsMemory()          # agent 消息记忆
    # write/read/clear 委托给 self.memory
    # plans_memory / message_memory 透传 gpts_memory

class GptsMemory:
    _plans_memory: GptsPlansMemory       # → gpts_plans 表（规划步骤）
    _message_memory: GptsMessageMemory   # → gpts_messages 表（agent 消息）
    messages_cache: defaultdict           # 内存缓存
    channels: defaultdict(Queue)          # SSE 流式推送队列
    def append_message(conv_id, message: GptsMessage):  # 缓存 + DB + SSE
    def get_messages(conv_id):            # 先缓存后 DB
    def get_agent_history_memory(conv_id, agent_role):  # 还原 ActionOutput 列表
```

`AgentMemoryFragment`（记忆单元）两种：
- `AgentMemoryFragment`：仅存 `raw_observation` 字符串
- `StructuredAgentMemoryFragment`：存 JSON `{question, thought, phase, action_intention, action_reason, action, action_input, observation}`

### 2.3 消息类型（双层抽象）

**文件**：`packages/dbgpt-core/src/dbgpt/core/interface/message.py`

**BaseMessage（会话级，给用户/前端）**：

| 子类 | `type` | `pass_to_model` | 用途 |
|---|---|---|---|
| `HumanMessage` | `"human"` | True | 用户输入 |
| `AIMessage` | `"ai"` | True | AI 回复 |
| `ViewMessage` | `"view"` | **False** | 前端渲染，不喂 LLM |
| `SystemMessage` | `"system"` | True | 系统指令 |

核心字段：`content`、`index`（全局自增序号）、`round_index`（所属轮次）、`additional_kwargs`。

**ModelMessage（LLM 级）**：
```python
class ModelMessage(BaseModel):
    role: str            # system | human | ai（View 被过滤）
    content: Union[str, List[MediaContent]]
    round_index: int = 0
    # from_base_messages(List[BaseMessage]) -> List[ModelMessage]  # 自动过滤 View
    # to_common_messages() -> OpenAI 格式 [{"role","content"}]
```

### 2.4 Agent 消息

**文件**：`packages/dbgpt-core/src/dbgpt/agent/core/agent.py`

```python
@dataclass
class AgentMessage:
    content: str
    rounds: int = 0
    action_report: Optional[ActionOutput]   # Thought/Action/Obs 执行结果
    review_info, current_goal, model_name, role, success, ...
```

**文件**：`packages/dbgpt-core/src/dbgpt/agent/core/memory/gpts/base.py`

```python
@dataclass
class GptsMessage:   # 持久化到 gpts_messages 表
    conv_id, sender, receiver, role, content, rounds
    is_success, current_goal, context, review_info
    action_report: str   # JSON 序列化的 ActionOutput
    model_name, resource_info, created_at, updated_at
```

### 2.5 StorageConversation（轨道 A 核心）

**文件**：`packages/dbgpt-core/src/dbgpt/core/interface/message.py`（约 1170 行）

```python
class StorageConversation(OnceConversation, StorageItem):
    def __init__(self, conv_uid, chat_mode, conv_storage, message_storage, ...):
        # 构造时自动 load_from_storage() 恢复历史
    def start_new_round()          # chat_order += 1
    def add_user_message(text)     # append HumanMessage
    def add_ai_message(text)       # append AIMessage
    def add_view_message(text)     # append ViewMessage
    def end_current_round()        # → save_to_storage()
    def save_to_storage()          # 增量保存（只存未存的新消息）
    def load_from_storage()        # 从 DB 恢复 messages 与 chat_order
```

`OnceConversation` 提供内存侧消息管理（`messages` 列表、`get_history_message()`、`get_messages_with_round()`、`get_latest_round()`）。

---

## 3. 完整闭环时序（一次多轮对话）

以 **agent 场景**（`agentic_data_api.py` 的 `_react_agent_stream`）为最完整路径：

```
STEP 1  用户提问
  gpt_memory = GptsMemory(plans=DefaultGptsPlansMemory(),
                          messages=MetaDbGptsMessageMemory())   # DB 持久化
  agent_memory = AgentMemory(gpts_memory=gpt_memory)
  storage_conv = StorageConversation(conv_uid, "chat_react_agent",
                                     conv_storage, message_storage)
  storage_conv.start_new_round()
  storage_conv.add_user_message(user_input)

STEP 2  组装 LLM 请求（每次调用重载历史）
  _build_model_request():
    ├─ history = current_message.get_history_message()   # 从 DB 重载全部 BaseMessage
    ├─ 窗口裁剪 BufferedConversationMapperOperator(keep_start_rounds, keep_end_rounds)
    ├─ ModelMessage.from_base_messages(history)          # 过滤 ViewMessage
    └─ → [system, ...history..., user] → LLM

STEP 3  agent ReAct 循环（每轮 generate_reply）
  ├─ 3a read_memories(question)
  │     → ShortTermMemory.read() → "最远近况" 注入 system prompt
  ├─ 3b thinking(messages) → LLM 生成 Thought/Action/Action Input
  ├─ 3c review() → 校验
  ├─ 3d act() → ActionOutput{thought, action, action_input, observation}
  ├─ 3e verify() → 通过/重试
  ├─ 3f write_memories(question, thought, action, observation)   ← 记忆写入点
  │     → AgentMemoryFragment("Question:..\nThought:..\nAction:..\nObservation:..")
  │     → ShortTermMemory.write()
  │     → _task_progress 更新（独立于 buffer，注入每个 system prompt）
  └─ 每步 send/receive → gpts_memory.append_message(GptsMessage含action_report)
        → 持久化 gpts_messages 表 + 推送 SSE queue

STEP 4  回答回写（双轨同时）
  轨道A: storage_conv.add_ai_message(回答)
         storage_conv.add_view_message(前端渲染 JSON: steps/plan/final)
         storage_conv.end_current_round() → save_to_storage() → chat_history 表
  轨道B: 最终 GptsMessage 持久化到 gpts_messages 表
```

**多轮闭环的本质**：STEP 2 每次提问都**重新从 DB 加载全部历史 → 窗口裁剪 → 喂 LLM**；STEP 4 把新的问答**写回 DB**。因此下一次提问天然"记得"之前所有轮次（被窗口裁剪保留的部分），无需额外状态同步。

---

## 4. 持久化表结构

### 4.1 chat_history（会话头）— 轨道 A

**文件**：`packages/dbgpt-core/src/dbgpt/storage/chat_history/chat_history_db.py`

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| conv_uid | String(255) UNIQUE | 会话 ID |
| chat_mode | String(255) | 场景模式 |
| summary | Text | 用户输入前 250 字 |
| user_name / sys_code / app_code | String | 归属 |
| messages | Text | 旧版兼容（JSON 整段） |
| message_ids | Text | 新版：逗号分隔消息 ID `"message___conv123___0,..."` |
| gmt_created / gmt_modified | DateTime | 时间戳 |

### 4.2 chat_history_message（消息体）— 轨道 A

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| conv_uid | String(255) | 索引 |
| index | Integer | 消息全局序号 |
| round_index | Integer | 轮次 |
| message_detail | Text | JSON：`{"type":"human","data":{...},"index":0,"round_index":1}` |
| gmt_created / gmt_modified | DateTime | 时间戳 |

### 4.3 gpts_messages（agent 消息）— 轨道 B

**文件**：`packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_messages_db.py`

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| conv_id | String(255) 索引 | 关联会话 |
| sender / receiver | String(255) | agent 名 |
| role | String(255) | 消息角色 |
| content | Text | 消息体 |
| rounds | Integer | 轮次 |
| is_success | Boolean | 是否成功 |
| action_report | Text | **JSON 的 ActionOutput（Thought/Action/Obs）** |
| current_goal / context / review_info / resource_info | Text | 上下文 |
| model_name | String(255) | 模型 |
| created_at / updated_at | DateTime | 时间戳 |

索引：`(conv_id, rounds, sender)`。

### 4.4 gpts_plans（规划步骤）— 轨道 B

**文件**：`packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_plans_db.py`

| 列 | 说明 |
|---|---|
| conv_id + sub_task_num | 联合唯一 |
| sub_task_title / content | 任务内容 |
| sub_task_agent | 执行 agent |
| rely | 依赖 "1,2,3" |
| state | todo/running/waiting/retrying/failed/complete |
| result | 执行结果 |

---

## 5. 窗口与记忆策略

### 5.1 窗口裁剪（keep_start_rounds / keep_end_rounds）

**文件**：`packages/dbgpt-serve/src/dbgpt_serve/core/config.py`（配置）、`packages/dbgpt-core/src/dbgpt/core/interface/operators/message_operator.py`（实现）

```python
def _filter_round_messages(messages_by_round):
    total = len(messages_by_round)
    if start and end:
        if start + end > total: return messages_by_round   # 避免重叠 → 全保留
        return messages_by_round[:start] + messages_by_round[-end:]
    elif start: return messages_by_round[:start]
    elif end:   return messages_by_round[-end:]
    else:       return []          # 都不设 → 不喂历史
```

另有 `TokenBufferedConversationMapperOperator`：消息 token 超限时从头逐轮 evict。

### 5.2 记忆写入触发（write_memories）

**文件**：`packages/dbgpt-core/src/dbgpt/agent/core/role.py`（约 276 行）

每个 act/verify 步骤之后调用：
1. 渲染模板 `"Question: {{question}}\nThought: {{thought}}\nAction: {{action}}\nObservation: {{observation}}"`
2. 创建 `AgentMemoryFragment`（有 phase/action_intention 时用 `StructuredAgentMemoryFragment`）
3. 更新 `_task_progress`（任务进度，**独立于 buffer**，即使 buffer 淘汰了旧 fragment 仍在 system prompt 中）
4. `self.memory.write(fragment)` → ShortTermMemory

### 5.3 ShortTermMemory 淘汰策略

- 默认 `buffer_size=5`：超出时丢弃「重要性 + enhance_count」最低的 fragment
- `EnhancedShortTermMemory`：写时算 embedding 相似度，>0.7 则 `enhance_cnt++`，≥3 次合并相同记忆并提取 insights 转长时

---

## 6. 移植方案（面向 excel-workbook-service / Excel Agent）

> 原始现状：`app/agent/react_loop.py` 的 `messages` 只是 `List[Dict]`（无类型、无持久化、无 round）；`/ask` 端点不记录问题/回答；前端刷新即丢。以下方案已在当前服务中落地为 SQLite 会话、工具轨迹和报告视图的简化闭环。

### ① 会话历史持久化（高价值）

**新增** `app/conversation.py`：
```python
class ExcelWorkbookConversation:
    """StorageConversation 简化版（DB-GPT 轨道 A）。"""
    conv_id: str
    workbook_id: str
    messages: List[dict]        # {"type","role","content","index","round_index"}
    chat_order: int
    start_new_round() / add_user_message() / add_ai_message()
    add_tool_message()          # 工具调用记录（Thought/Action/Obs）
    end_current_round()
    save() / load()             # JSON 文件或 SQLite 单表
```

- **改动文件**：新增 `app/conversation.py` + `app/models/conversation.py`（SQLite 单表 `workbook_conversation` + `workbook_message`）
- **存储**：建议 SQLite 单表（已有 `sqlalchemy` 依赖），存 `data/workbook_imports/{wid}/conversations/{conv_id}.json` 或元数据库两张表
- **接口**：
  - `GET /api/v1/workbooks/{wid}/conversations` — 历史会话列表
  - `GET /api/v1/workbooks/{wid}/conversations/{conv_id}` — 单会话消息
  - `DELETE /api/v1/workbooks/{wid}/conversations/{conv_id}` — 删除会话
- **验证**：问答两次 → GET 列表 2 条 → GET 单会话含 2 轮消息 → 重启后仍在

### ② 多轮对话上下文（高价值）

- **改动**：`app/api/endpoints.py` 的 `/ask` 加可选 `conv_id` 字段；`app/agent/react_loop.py` 的 `run_react` 接收 `history_messages`
- **流程**：/ask 带 conv_id → 加载历史 → `messages = [system] + history(窗口裁剪) + [user]` → ReAct 循环 → 结束后 `add_ai_message(full_answer)` + `end_current_round()`
- **窗口**：默认 `keep_end_rounds=10`（保留最近 10 轮），可配
- **验证**：第一问"表有几张"→ 第二问"最大那张呢"（不带表名，AI 应记得）

### ③ 工具调用记忆（对应 DB-GPT write_memories）

- **改动**：`app/agent/react_loop.py` — 每轮 act 后把 `Thought/Action/Action Input/Observation` 模板化为一条 `tool` 消息，`add_tool_message()` 追加
- **作用**：既持久化工具调用轨迹（排查用），又让后续轮次 LLM 可见已执行的步骤（防重复）
- **验证**：日志/历史中能看到每步 `Action: sql_query / html_interpreter` 及结果

### ④ 后端日志落盘

- **改动**：`app/main.py` — `logging.basicConfig` 换成 `RotatingFileHandler`（写 `logs/excel-agent.log`，滚动备份）+ 保留控制台
- **改动**：`app/api/endpoints.py` `/ask` — `logger.info` 记录 `conv_id`、`workbook_id`、`question`
- **改动**：`app/agent/react_loop.py` — 记录每轮 `action`、最终 `full_answer`（截断）、`report_url`
- **验证**：启动后 `logs/excel-agent.log` 持续写入；包含一次完整问答的问题与回答

### 接口设计汇总

```
POST /api/v1/workbooks/{wid}/ask          { question, conv_id?, max_rows }
GET  /api/v1/workbooks/{wid}/conversations
GET  /api/v1/workbooks/{wid}/conversations/{conv_id}
DELETE /api/v1/workbooks/{wid}/conversations/{conv_id}
```

SSE 事件新增（向后兼容）：`done` 事件增加 `conv_id`；历史会话相关事件可选。

---

## 7. 可简化清单（移植时不必移植）

| DB-GPT 能力 | 是否需要 | 理由 |
|---|---|---|
| Sensory/LongTerm/HybridMemory | ❌ | 单会话无需分层长期记忆 |
| embedding 增强 / 重要性评分 / LLM insights | ❌ | 增加延迟，非必需 |
| GptsMemory 多 agent 路由 | ❌ | 单 agent（Excel 问答） |
| AWEL DAG 编排 | ❌ | 用简单函数替代 |
| SQLAlchemy 双 storage（conv + message） | ✅（简化） | 两张 SQLite 表承载会话元数据和消息，未引入 DB-GPT 的多层 storage |
| ViewMessage 多模态 | ❌ | 纯文本足够 |
| ContextManager 多层压缩（L1-L4） | ❌ | 窗口裁剪足够 |

---

## 8. 当前落地状态

本轮已完成 DB-GPT 双轨记忆的 Excel 简化实现：

- **轨道 A（StorageConversation）**：`workbook_conversation` 保存会话元数据，`workbook_message` 保存 `user / ai / tool / view / budget` 消息；每次请求先加载历史，再写入当前轮，刷新或服务重启后可恢复。
- **轨道 B（AgentMemory/GptsMemory 的最小等价物）**：ReAct 的 SQL、HTML 工具步骤以结构化 `tool.meta.detail` 持久化；只保留可审计的 Action/Observation，过滤 Thought，不把私有推理暴露给前端。
- **报告 ViewMessage**：`html_interpreter` 完成后只保存 `report_id/title/url`，不重复保存 HTML；前端按 `report_id/url` 合并 tool 与 view，刷新后仍显示同一份报告。报告接口直接返回已落盘的原始（一次安全清洗后的）HTML，不再套第二层展示壳。
- **前端恢复**：工作簿和会话 ID 写入 localStorage；页面刷新后自动选中上次工作簿、加载会话列表和消息；报告只呈现 HTML iframe，PDF 按钮固定在右下角并调用浏览器“另存为 PDF”。

当前仍有意不做 DB-GPT 的向量长期记忆、重要性评分、多层压缩和多 Agent 共享记忆；Excel 场景先采用最近 10 轮窗口，避免把报告 HTML 重新喂给模型。

## 9. 后续迭代（可选）

- 增加显式的“删除会话”按钮，并在前端调用已有 DELETE 接口。
- 若需要跨会话知识召回，再单独引入向量长期记忆；不要把它与当前短期会话消息混用。
- **会话删除联动**：删除 workbook 时级联删除其 conversations

---

## 附录：关键参考文件路径

| 模块 | 路径 |
|---|---|
| Memory 抽象 + Sensory/ShortTerm | `packages/dbgpt-core/src/dbgpt/agent/core/memory/base.py` |
| AgentMemory | `packages/dbgpt-core/src/dbgpt/agent/core/memory/agent_memory.py` |
| GptsMemory / GptsMessage | `packages/dbgpt-core/src/dbgpt/agent/core/memory/gpts/{gpts_memory,base}.py` |
| 增强短时记忆 | `packages/dbgpt-core/src/dbgpt/agent/core/memory/short_term.py` |
| 长时/混合记忆 | `packages/dbgpt-core/src/dbgpt/agent/core/memory/{long_term,hybrid}.py` |
| 消息类（BaseMessage/ModelMessage/StorageConversation） | `packages/dbgpt-core/src/dbgpt/core/interface/message.py` |
| chat_history 表 | `packages/dbgpt-core/src/dbgpt/storage/chat_history/chat_history_db.py` |
| 存储适配器 | `packages/dbgpt-core/src/dbgpt/storage/chat_history/storage_adapter.py` |
| gpts_messages 表 | `packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_messages_db.py` |
| gpts_plans 表 | `packages/dbgpt-serve/src/dbgpt_serve/agent/db/gpts_plans_db.py` |
| BaseChat 闭环 | `packages/dbgpt-app/src/dbgpt_app/scene/base_chat.py` |
| agent 场景闭环 | `packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/agentic_data_api.py` |
| 窗口裁剪实现 | `packages/dbgpt-core/src/dbgpt/core/interface/operators/message_operator.py` |
| 记忆窗口配置 | `packages/dbgpt-serve/src/dbgpt_serve/core/config.py` |
