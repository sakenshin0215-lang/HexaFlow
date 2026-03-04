# HexaFlow

HexaFlow 是一个以“目标达成”为核心的网页自动化引擎，强调：
1. AI 驱动的动态决策（不是纯脚本硬编码）。
2. 录制-回放-自愈-断点续跑的一体化闭环。
3. 支持 CDP 复用真实 Chrome Profile（扩展、登录态、钱包）。
4. 人工接管可插拔：登录、验证码、钱包解锁等场景可暂停后继续。

---

## 当前能力总览

### 1. 执行模式
1. 动态任务模式（AI 决策下一步）：`run_dynamic_task`
2. 轨迹回放模式（确定性执行）：`run_from_trace`
3. 循环任务模式（按区间循环，成功才计次）：`run_loop_from_trace`
4. 人工示教录制模式（可录 click/type）：`run_manual_record_task`

### 2. 浏览器模式
1. Playwright launch（传统模式）
2. CDP 连接模式（推荐）
   - 复用真实浏览器上下文（Profile/扩展/登录态）
   - 不再额外 new context 破坏钱包与登录态

### 3. 恢复与稳定性
1. 弹窗自愈（Healer + 缓存）
2. 错页修复（reload/back/回跳）
3. 小流程回放修复（可跳过高风险动作）
4. AI 回放修复器（改 selector / 改动作 / 跳步 / 人工接管）
5. 页面守卫（Page Guard）
   - 步骤执行前检查是否在规定页面
   - 不匹配时先执行 `on_mismatch_actions`

### 4. 运行态管理
1. SQLite 状态机（run_id、步骤进度、事件）
2. 挂起/恢复（按 trace 或 run_id）
3. 自动报告（JSON + Markdown）

---

## 快速开始

### 1. 安装依赖
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置环境变量（可选）
```bash
export USE_CDP=1
export CDP_PORT=9222
export CHROME_USER_DATA_DIR=/Users/kenshinnb/pw-profiles/okx-chrome
export CHROME_PROFILE_DIRECTORY=Default
export CDP_START_URL=https://www.okx.com/web3
```

### 3. 运行入口
1. 动态执行（TaskSpec）：`main.py`
2. 示教录制：`main_record.py`
3. 轨迹回放：`main_replay.py`
4. 挂起恢复：`main_resume.py`
5. 循环任务：`main_loop.py`

---

## TaskSpec 关键字段

`memory/workspace/task_specs/*.json` 支持：
1. `goal`、`notes`、`start_url`
2. `manual_review`、`max_steps`
3. `allowed_domains`、`blocked_keywords`
4. `failure_policy`
5. `loop_policy`
   - `enabled`
   - `loop_name`
   - `iterations`
   - `max_iteration_retries`

---

## 录制与循环

### 1. 录制 click + type
示教录制时会自动记录：
1. 点击动作（click）
2. 输入动作（type/change）

### 2. 循环标记
在录制确认输入中：
1. `ls`：录制并标记“循环开始”
2. `le`：录制并标记“循环结束”

标记会写入 trace 的 `loop_marker` / `loop_name`。

### 3. 循环计数规则
1. 只有循环区间“整轮成功”才计 `completed + 1`
2. 任一步失败则本轮重试，不计次
3. 达到 `max_iteration_retries` 才挂起

---

## 页面守卫（防跑偏）

每个步骤支持以下字段：
1. `guard_url_contains`
2. `on_mismatch_actions`
3. `guard_retry_limit`

语义：
1. 执行该步骤前先检查当前 URL 是否符合 guard
2. 若不符合，先执行 `on_mismatch_actions` 回到目标页面
3. 修正后再执行主动作

适用于“误点到其他币、页面状态漂移但 URL 结构相似”的场景。

---

## 人工接管

引擎会在这些场景触发人工接管提示：
1. 登录/重新登录
2. 验证码/人机校验/2FA
3. 钱包解锁/签名确认

处理后回车即可继续主流程。

---

## 目录说明（简版）

```text
hexaflow/
  agents/      # AI 决策、修复器 schema
  browser/     # CDP 启动、人工接管相关组件
  core/        # 引擎、状态机、录制器、报告器、task schema
  tools/       # DOM 解析、弹窗自愈
memory/workspace/
  traces/      # 轨迹
  state/       # SQLite 运行状态
  reports/     # 执行报告
  screenshots/ # 挂起现场截图
```

---

## 说明

1. 本项目目标是网页自动化任务达成，不是盈利策略系统。
2. 真实站点会持续变化，建议结合 AI 修复 + 人工接管。
3. 对高风险动作建议启用 subflow 风险跳过策略，避免重复副作用。
