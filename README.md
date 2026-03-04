# HexaFlow

HexaFlow is a goal-driven web automation engine focused on:
1. Automatic browser operations from task goals.
2. Record and replay of stable execution traces.
3. Error healing and checkpoint resume.
4. Human takeover only when needed.

---

## What Was Added In Recent Iterations

This README summarizes the major upgrades implemented in the latest steps.

### 1) Runtime State Machine + Checkpoint Resume

Added persistent run-state management via SQLite:
- `hexaflow/memory/local_db.py`
- `hexaflow/core/state_machine.py`

Integrated into replay:
- `hexaflow/core/engine.py` `run_from_trace(...)`

Key behavior:
- Every replay run is tracked with `run_id`.
- Step progress (`current_step_index`) is saved after each successful step.
- On failure, run is marked `suspended` (or `failed`), and next replay can resume.
- Resume can be:
  1. Automatic by trace path.
  2. Exact by `run_id`.

### 2) Dynamic Task Unattended Mode

Upgraded dynamic run mode:
- `hexaflow/core/engine.py` `run_dynamic_task(...)`

New options:
- `manual_review=True/False`
- `manual_review=False` enables unattended execution (no `y/n/done` prompt).

Also improved:
- Auto-save trace and browser storage state on completion/exit.

### 3) Task DSL (Goal + Notes + Policies)

Added structured task schema:
- `hexaflow/core/task_schema.py`

New entry:
- `run_task_from_spec(...)` in `hexaflow/core/engine.py`

Example spec:
- `memory/workspace/task_specs/okx_web3_demo.json`

Supported fields:
- `goal`, `notes`, `start_url`
- `manual_review`, `max_steps`
- `allowed_domains` (navigation domain whitelist)
- `blocked_keywords` (action keyword blacklist)
- `failure_policy.mode` and `failure_policy.max_consecutive_failures`

### 4) Suspend/Resume Operational Flow

Added suspend-run inspection + recovery workflow:
- `main_resume.py`

State-machine additions:
- List runs by status.
- List recent run events.
- Resume by exact `run_id`.

Replay additions:
- `run_from_trace(..., run_id=...)` for deterministic recovery target.
- Suspend snapshot screenshots are captured and linked in error detail.

### 5) Auto Execution Reports (JSON + Markdown)

Added report generation:
- `hexaflow/core/run_reporter.py`

Integrated into replay:
- Reports generated automatically when run completes, suspends, or fails.

Report includes:
- Final status.
- Success rate (`completed_steps / total_steps`).
- Failure points and screenshot paths.
- Resume count.
- Event timeline.

Output directory:
- `memory/workspace/reports/`

### 6) Replay Bootstrap URL Fix (about:blank issue)

Fixed replay startup for traces missing an initial `navigate` step:
- If first pending step is not `navigate` and page is `about:blank`,
  engine infers a bootstrap URL from:
  1. Current step `pre_check.expected_url_contains`
  2. Nearby/first `navigate` step in trace
- Then auto `goto(...)` before executing the pending step.

Also updated dynamic recording:
- When `start_url != about:blank`, initial navigation is recorded into trace.

### 7) Multi-Layer Auto-Recovery for Replay Errors

Upgraded replay retry chain (per step):
1. `Healer` popup recovery.
2. Wrong-page recovery (`reload` / inferred `goto` / `go_back`).
3. Recent subflow replay (replay previous N steps).
4. Suspend if still failing.

Recovery events are persisted in run events:
- `recovery_healer`
- `recovery_wrong_page`
- `recovery_subflow`

### 8) Safe Subflow Replay (Risk Guard)

Added risky-step filtering to avoid repeated side-effect clicks during subflow repair:
- Default skip for risky click semantics (e.g. confirm/swap/approve/sign/buy/sell/pay/connect wallet).

Replay parameters:
- `subflow_window` (default `2`)
- `subflow_skip_risky` (default `True`)
- `subflow_risky_keywords` (customizable)

---

## Current Main Entrypoints

- Dynamic task from DSL:
  - `main.py`
- Manual teaching/recording:
  - `main_record.py`
- Trace replay:
  - `main_replay.py`
- Suspended run resume:
  - `main_resume.py`
- Concurrent replay demo:
  - `main_concurrent.py`

---

## Typical Workflow

1. Create task spec JSON in `memory/workspace/task_specs/`.
2. Run `main.py` for dynamic execution and trace generation.
3. Replay stable trace with `main_replay.py`.
4. If suspended, recover with `main_resume.py`.
5. Review reports in `memory/workspace/reports/`.

---

## Notes and Boundaries

1. This project is automation-oriented (not profit strategy trading).
2. DOM-driven replay is fast and deterministic, but site changes can still break selectors.
3. Healer and recovery reduce failures but cannot guarantee 100% unattended completion.
4. For high-risk flows, tune risky keywords and keep manual takeover available.

