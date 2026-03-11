# HexaFlow

HexaFlow 是一个面向复杂网页流程的 AI 自动化引擎，支持：
1. 动态 AI 执行（按目标推进，不是纯脚本）
2. 人工示教录制 + 轨迹回放
3. 循环任务（成功才计次）
4. CDP 复用真实 Chrome Profile（登录态、扩展、钱包）

## 入口脚本

1. `main_ai.py`：AI 全流程执行（TaskSpec）
2. `main_record.py`：人工示教录制
3. `main_replay.py`：轨迹回放
4. `main_loop.py`：循环任务执行

## 统一配置（推荐）

默认读取：`workspace/config/runtime.json`

可选覆盖：
- `RUNTIME_CONFIG_PATH=/path/to/your_runtime.json`

### 配置结构（重点）

```json
{
  "agent_profile": "ollama",
  "agent_ollama": {
    "api_key": "ollama",
    "base_url": "http://localhost:11434/v1",
    "model_name": "qwen3-vl:8b-instruct"
  },
  "agent_openai": {
    "api_key": "",
    "base_url": "https://api.openai.com/v1",
    "model_name": "gpt-4.1-mini"
  }
}
```

运行时会把选中的 profile 注入为统一的 `agent`（`main_*` 无需区分）。

## 本地模型 / OpenAI 切换

### 1) 用配置文件切换

修改 `runtime.json`：
- `"agent_profile": "ollama"` 或 `"openai"`

### 2) 用环境变量切换

```bash
export AGENT_PROFILE=ollama
# 或
export AGENT_PROFILE=openai
```

### 3) 按 profile 覆盖参数

Ollama：
```bash
export AGENT_OLLAMA_API_KEY=ollama
export AGENT_OLLAMA_BASE_URL=http://localhost:11434/v1
export AGENT_OLLAMA_MODEL=qwen3-vl:8b-instruct
```

OpenAI：
```bash
export AGENT_OPENAI_API_KEY=sk-xxx
export AGENT_OPENAI_BASE_URL=https://api.openai.com/v1
export AGENT_OPENAI_MODEL=gpt-4.1-mini
```

兼容旧变量（作用于当前 profile）：
```bash
export AI_API_KEY=...
export AI_BASE_URL=...
export AI_MODEL=...
```

## 安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

运行：
```bash
python3 main_ai.py
python3 main_record.py
python3 main_replay.py
python3 main_loop.py
```

## 浏览器/CDP

`runtime.json` 的 `browser` 段控制：
1. `use_cdp`：是否走 CDP
2. `user_data_dir`：Chrome 用户数据目录
3. `profile_directory`：如 `Default`
4. `cdp_start_url`：CDP 启动后的起始页

## 工作目录

项目数据统一在 `workspace/`：
1. `workspace/traces`：录制轨迹
2. `workspace/task_specs`：任务定义
3. `workspace/reports`：执行报告
4. `workspace/screenshots`：运行截图
5. `workspace/state`：运行状态数据库

## 常见问题

1. 报错 `model is required`
   - 检查选中的 profile 是否有 `model_name`
   - 避免把 `AI_MODEL` / `AGENT_*_MODEL` 设为空字符串

2. 为什么 main 要求的参数变少了
   - 现在统一从 `runtime.json` 读取，main 只负责启动流程
