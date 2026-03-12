# Popup Skill

## Purpose

Handle blocking popup/modal/overlay layers before retrying business actions.

## Components

1. `popup_skill_agent.py`
2. `popup_healer.py`

## Exposed Strategy API

1. `should_handle(last_error, dom_snapshot) -> bool`
2. `propose_repair(failed_target, dom_snapshot) -> ReplayRepairDecision | None`

## Runtime Helpers

1. `dismiss_topmost_popup_once(page)`
2. `dismiss_blocking_modal(page)`
3. `try_popup_healer(page, enabled=False)`

## Selection Priority

1. Top-most close control (`X`, close icon, close-like aria-label)
2. Close-like text (`关闭`, `跳过`, `Close`, `Skip`, `Cancel`)
3. Acknowledgement text (`我知道了`, `我已知晓`, `Got it`)

Never prioritize tutorial progression buttons (`下一步`, `Next`) when close options exist.

