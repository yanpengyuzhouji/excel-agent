# ModelScope 模型接口说明

ModelScope API-Inference 提供 OpenAI 兼容的 Chat Completions 接口：

```text
POST https://api-inference.modelscope.cn/v1/chat/completions
```

官方文档：[API-Inference](https://www.modelscope.cn/docs/model-service/API-Inference/intro)

## Python 调用

Token 只通过环境变量或密钥管理系统提供，不要写入代码、配置文件或日志。

### 非思考模式

Excel Agent 当前使用此模式，保证模型直接输出 ReAct 的 `Action` 和 `Action Input`：

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://api-inference.modelscope.cn/v1",
    api_key=os.environ["MODELSCOPE_API_KEY"],
)

response = client.chat.completions.create(
    model="Qwen/Qwen3-14B",
    messages=[{"role": "user", "content": "9.9和9.11谁大"}],
    temperature=0.7,
    max_tokens=4096,
    extra_body={"enable_thinking": False},
)

print(response.choices[0].message.content)
```

### 思考模式

思考模式应使用流式响应，并分别读取 `reasoning_content` 和 `content`：

```python
response = client.chat.completions.create(
    model="Qwen/Qwen3-14B",
    messages=[{"role": "user", "content": "9.9和9.11谁大"}],
    stream=True,
    extra_body={
        "enable_thinking": True,
        # "thinking_budget": 4096,
    },
)

for chunk in response:
    if not chunk.choices:
        continue
    delta = chunk.choices[0].delta
    if delta.reasoning_content:
        print(delta.reasoning_content, end="", flush=True)
    elif delta.content:
        print(delta.content, end="", flush=True)
```

## 当前 Excel 服务配置

ModelScope 的参数格式与本地 vLLM 不同：

```yaml
base_url: "https://api-inference.modelscope.cn/v1"
model: "Qwen/Qwen3-14B"
api_key_env: "MODELSCOPE_API_KEY"
extra_body:
  enable_thinking: false
```

Qwen3.5-27B 的 ModelScope 配置使用官方模型 ID，并通过兼容接口关闭思考模式：

```yaml
name: "Qwen3.5-27B-ModelScope"
base_url: "https://api-inference.modelscope.cn/v1"
model: "Qwen/Qwen3.5-27B"
api_key_env: "MODELSCOPE_API_KEY"
env_file: ".env"
extra_body:
  chat_template_kwargs:
    enable_thinking: false
stream: true
max_tokens: 8192
max_retries: 2
analysis_route: large
model_size_b: 27
```

Qwen3.8-27B 的 ModelScope 重测配置使用模型 ID `Qwen/Qwen3.8-27B`，服务内名称为 `Qwen3.8-27B-ModelScope`：

```yaml
name: "Qwen3.8-27B-ModelScope"
base_url: "https://api-inference.modelscope.cn/v1"
model: "Qwen/Qwen3.8-27B"
api_key_env: "MODELSCOPE_API_KEY"
env_file: ".env"
extra_body:
  enable_thinking: false
stream: true
max_tokens: 8192
max_retries: 2
analysis_route: large
model_size_b: 27
```

同一接口还配置了 `Qwen/Qwen3-32B`，使用 `Qwen3-32B-ModelScope` 作为服务内模型名，走 `large` Agent 路由，`max_tokens=8192`，并对初始连接失败启用 2 次重试。

该配置已加入项目根目录 `config.yaml`，前端模型下拉框会通过 `/models` 自动显示。

2026-08-19 使用 `2025年采购目录汇总.xlsx` 实测该模型时，ModelScope 返回 HTTP 400：`Qwen/Qwen3-32B has no provider supported`。因此当前配置已就绪，但 ModelScope API-Inference 尚未为该模型提供可用 provider，不能把这次失败归因于 Excel Agent 的上下文或数据限制。

2026-08-20 使用相同工作簿和问题复测，仍返回相同的 HTTP 400；Excel 服务已正常收到请求，但没有执行 SQL 或生成 HTML 报告。当前不需要调整 Agent 或上下文参数。

不要写成下面这种 vLLM 专用格式：

```yaml
extra_body:
  chat_template_kwargs:
    enable_thinking: false
```

当前进入 `large` 路由的 `Qwen3-30B-A3B-Instruct-2507`、`Qwen3-Coder-30B-A3B-Instruct`
与 DeepSeek 共用（`Qwen3-14B` 和 `Qwen3-VL-8B-Instruct` 默认是 `small` 模板路由，
可显式覆盖为 `large`）：

```text
sql_query → html_interpreter → terminate
```

`Qwen3-30B-A3B-Instruct-2507` 当前配置的输出上限为 `max_tokens=16384`，并启用流式请求；这是输出预算，不等同于上下文窗口。实测该模型流式请求正常，非流式请求可能长时间无响应。

`Qwen3-Coder-30B-A3B-Instruct` 使用相同的 ModelScope 流式适配，适合作为 SQL、代码和 HTML 生成能力的对比模型。当前 `max_tokens=4096` 流式请求已验证正常；`16384` 未在测试窗口内返回，因此暂不启用。

## 实测说明

- `Qwen/Qwen3-VL-8B-Instruct`：ModelScope 接口正常，已生成带 CSS 的 HTML 报告。
- `Qwen/Qwen3-14B` + `enable_thinking=false`：可正常返回普通回答。
- 当前已验证 ModelScope 的 Qwen3-14B 路由：`4096` 正常；`8192` 在 45 秒内超时；`16384` 和 `32768` 返回空 `choices`。注意 `max_tokens` 是输出预算，不等同于模型的原生上下文长度，因此 Excel 服务采用 4096。
- `Qwen/Qwen3-14B` + `enable_thinking=true`：应使用流式请求；测试中流式响应正常返回 `reasoning_content`，非流式请求出现过 `choices: null`。
- Excel Agent 默认关闭思考模式，以保证 ReAct 工具调用格式稳定。
