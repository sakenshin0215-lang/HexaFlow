# Agent Skills (Project Standard)

## Purpose

`hexaflow/agents/skills/` stores special capabilities used by the core `ReActAgent`.
Core planning/repair stays in `react_agent.py`; domain-specific handling is implemented as pluggable skills.

## Runtime Discovery (No File Scanning)

1. Skill metadata is centralized in `registry.py`.
2. `SpecialAgentRouter.default()` loads skills from `default_skill_agents(...)`.
3. The core agent calls router APIs, not skill files directly.

## Required Skill Contract

Each skill agent should implement:

1. `should_handle(...) -> bool`
2. `propose_repair(...) -> ReplayRepairDecision | None`

Optional runtime helpers are allowed (for direct engine usage), but strategy methods above are mandatory.

## Registry Fields

Every skill in `registry.py` must provide:

1. `skill_id`
2. `name`
3. `description`
4. `tools`
5. `trigger_signals`
6. `factory`

## Current Skills

1. `popup` (`skills/popup/`): overlay/modal blocking handler.

