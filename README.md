# HexaFlow

HexaFlow 是一个面向复杂网页流程的 AI 自动化框架，核心目标是：

1. 在真实浏览器环境里稳定执行网页任务
2. 支持 AI 动态决策、人工示教录制、确定性回放、循环执行
3. 内置失败自愈（AI 修复）与可扩展 Skill 机制

本项目当前以 `README.md` 作为唯一文档入口。`docs/` 目录不再作为主维护文档。

## 核心能力

1. `main_ai.py`：AI 动态任务执行（按 TaskSpec 自动推进）
2. `main_record.py`：人工示教录制（生成 trace）
3. `main_replay.py`：按 trace 回放（可叠加 AI 修复）
4. `main_loop.py`：按 trace 循环执行（成功才计次）
5. CDP 复用本地 Chrome Profile（扩展、钱包、登录态都可复用）
6. 核心 Agent + Skills 架构（`ReActAgent` + skill registry）
7. Tool 调用机制（`call_tool`），支持将“总结类动作”标准化并可回放

## 架构概览

### 1) Core

1. `hexaflow/core/engine.py`：总控引擎装配
2. `engine_dynamic_mixin.py`：动态任务执行
3. `engine_replay_mixin.py`：回放执行
4. `engine_loop_mixin.py`：循环执行
5. `engine_record_mixin.py`：示教录制
6. `core/special/*`：AI 修复、恢复链路、元素监控等

### 2) Agent

1. 核心 Agent：`hexaflow/agents/react_agent.py`
2. LLM 适配：`hexaflow/agents/llm_gateway.py`
3. Skill 路由：`hexaflow/agents/special_agents.py`
4. Skill 注册表：`hexaflow/agents/skills/registry.py`

### 3) Browser / Tools

1. `hexaflow/browser/`：CDP、token_selector、sidecar 等浏览器相关能力
2. `hexaflow/tools/`：通用辅助工具（DOM 解析、helpers、tool runtime）

### 4) Workspace

1. `workspace/traces/`：录制轨迹
2. `workspace/task_specs/`：任务 DSL
3. `workspace/screenshots/`：截图缓存
4. `workspace/reports/`：报告输出（可关闭）
5. `workspace/state/`：运行时状态数据库

## 快速开始

## 环境要求

1. Python 3.12+
2. 已安装 Playwright 依赖浏览器
3. 本地或远程 OpenAI-Compatible 模型服务（如 Ollama）
4. 若使用 CDP：本机已安装 Chrome

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 运行入口

```bash
python3 main_ai.py
python3 main_record.py
python3 main_replay.py
python3 main_loop.py
```

## CDP 使用（推荐）

HexaFlow 默认支持 CDP 连接本地 Chrome，复用真实 profile。

在各 `main_*.py` 中配置：

1. `use_cdp = True`
2. `cdp_user_data_dir = "/path/to/your/chrome-user-data"`
3. `cdp_profile = "Default"`

引擎会连接 `http://127.0.0.1:9222`（可通过 `CDPConfig` 或环境变量修改）。

## 四个主入口如何用

### 1) `main_ai.py`（动态任务）

适合“给目标，AI 自己走流程”。

最小配置：

1. `task_spec_path`
2. `ai_provider`（模型地址与模型名）
3. `enabled_skill_ids`
4. `save_reports`

### 2) `main_record.py`（示教录制）

适合“人工点一遍，沉淀可回放 trace”。

录制会生成：

1. `workspace/traces/ManualTask_*.json`

### 3) `main_replay.py`（确定性回放）

适合“严格按 trace 跑”。

可配：

1. `enable_ai_repair`
2. `ai_repair_only`

### 4) `main_loop.py`（循环任务）

适合“某段流程重复执行 N 次，失败重试直到成功”。

循环范围由 trace 内 `loop_marker=start/end` 定义。

## main 里常用开关说明

下面这些都在 `main_*.py` 顶部配置块里改，平时最常用：

1. `use_cdp`：是否使用 CDP 连接本地浏览器
2. `headless`：仅非 CDP launch 时有效
3. `enabled_skill_ids`：启用哪些 skill
   - `['popup']`：开启弹窗 skill
   - `[]`：关闭所有 skill
4. `save_reports`：是否写入 `workspace/reports`
   - 默认建议 `False`
5. `enable_ai_repair`：是否启用 AI 修复
6. `ai_repair_only`：是否禁用传统回退，只走 AI 修复
7. `ai_decision_use_vision`：动态任务决策时是否带截图
8. `repair_context_window`：修复时传入最近步骤窗口

## AI Provider 配置

每个 main 里都有：

```python
ai_provider = {
  "provider": "openai_compatible",
  "api_key": "ollama",
  "base_url": "http://localhost:11434/v1",
  "model_name": "qwen3-vl:8b-instruct",
}
```

可切换为任意 OpenAI-Compatible 代理商/网关，只要接口兼容。

## TaskSpec 配置详解

TaskSpec 定义在 `workspace/task_specs/*.json`，核心字段如下：

1. `task_name`：任务名
2. `goal`：任务目标（最关键）
3. `notes`：约束说明
4. `start_url`：起始 URL
5. `manual_review`：是否人工验收每一步
6. `max_steps`：最大步数
7. `allowed_domains`：导航白名单
8. `blocked_keywords`：动作黑名单关键字
9. `completion_logic`：`any` / `all`
10. `completion_checks`：硬完成判定（可选）
11. `allow_repeat_summarize`：是否允许同页重复总结（默认 false）
12. `failure_policy`
    - `mode`: `continue` / `stop`
    - `max_consecutive_failures`
13. `loop_policy`
    - `enabled`
    - `loop_name`
    - `iterations`
    - `max_iteration_retries`

### completion_checks 什么时候需要

1. 可不写：让 AI 自主判断 `done`
2. 建议写：高价值任务、容易误判完成的任务

示例：

```json
"completion_checks": [
  {
    "check_type": "action_target_contains",
    "action_type": "click",
    "value": "买入"
  }
],
"completion_logic": "any"
```

## 动作类型（Action Types）

核心动作见 `hexaflow/agents/schemas.py`：

1. `navigate`
2. `click`
3. `type`
4. `click_type_enter`
5. `press_enter`
6. `refresh`
7. `call_tool`
8. `done`

历史兼容：`summarize` 仍可读，但建议统一用 `call_tool`。

## 报告与产物

当 `save_reports=True` 时会产出：

1. run report（JSON/MD）
2. heal log（JSON/MD）
3. ai action log（JSON/MD）
4. element monitor（JSON/MD）

当 `save_reports=False`（默认）时不写这些文件。

## 最小实战流程（推荐）

1. 用 `main_record.py` 示教一次流程
2. 在 trace 中标记 loop start/end（可选）
3. 用 `main_replay.py` 验证稳定性
4. 用 `main_loop.py` 批量跑
5. 需要开放探索时使用 `main_ai.py`

---

如需新增 skill/tool，建议先在 `agents/skills/registry.py` 或 `tools/tool_runtime.py` 增量注册，再接到 `ReActAgent` 输出规范中。
