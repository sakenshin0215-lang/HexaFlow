import logging


logger = logging.getLogger("HexaEngine")


class EngineRecoveryMixin:
    """
    Shared recovery orchestration for runtime execution.
    Keep mode-specific mixins lean by centralizing blocked-action recovery chain.
    """

    async def _route_dynamic_blocked_recovery(
        self,
        *,
        page,
        attempt: int,
        max_attempts: int,
        disable_fallback_recovery: bool,
        ai_heal_agent,
        human_handoff_on_auth: bool,
        repair_context_window: int,
        action_history: list[str],
        task_name: str,
        next_action,
        stable_target: str,
        failed_input_value: str,
        error,
        step_count: int,
        allowed_domains: list[str],
        chosen_click_ratio,
        current_url: str,
        ai_action_records: list[dict],
    ) -> dict:
        """
        Returns:
        - {"outcome": "none"}: no recovery applied, caller continues normal error handling.
        - {"outcome": "continue"}: recovery applied, caller should continue next attempt loop.
        - {"outcome": "resolved", "current_action_log": "..."}: recovery resolved action.
        """
        if attempt >= max_attempts - 1:
            return {"outcome": "none"}

        if attempt == 0:
            healed_by_popup = await self._try_popup_healer(page)
            if healed_by_popup:
                await page.wait_for_timeout(600)
                return {"outcome": "continue"}

        if attempt == 0 and (not disable_fallback_recovery):
            logger.info("↩️ [动态阶段] 尝试回退修复(reload/back)...")
            await self._attempt_generic_recovery(page)
            await page.wait_for_timeout(1200)
            return {"outcome": "continue"}

        should_ai_heal = (attempt == 0 and disable_fallback_recovery) or (
            attempt == 1 and ai_heal_agent
        )
        if not should_ai_heal:
            return {"outcome": "none"}
        if not ai_heal_agent:
            return {"outcome": "continue"}

        logger.info("🧠 [动态阶段] 启动 AI 修复 Agent（最多3次）...")
        recent_steps = "\n".join(action_history[-repair_context_window:]) or "No previous steps."
        heal_result = await self._run_ai_heal_agent(
            page=page,
            ai_heal_agent=ai_heal_agent,
            task_name=task_name,
            recent_steps=recent_steps,
            failed_step_desc=next_action.thought or "dynamic action failed",
            failed_action_type=next_action.action_type,
            failed_target=stable_target or next_action.target,
            failed_input_value=failed_input_value,
            last_error=str(error),
            step_id=f"dynamic_step_{step_count}",
            validate_coro=lambda: self._execute_dynamic_action(
                page=page,
                next_action=next_action,
                allowed_domains=allowed_domains,
                fast_validate=True,
                click_ratio=chosen_click_ratio,
            ),
            human_handoff_on_auth=human_handoff_on_auth,
            max_attempts=3,
            expected_url_contains=current_url,
        )
        ai_action_records.append(
            {
                "step": step_count,
                "action_type": next_action.action_type,
                "target": stable_target or next_action.target,
                "input_value": next_action.input_value,
                "thought": next_action.thought,
                "success": bool(heal_result.get("resolved")),
                "note": f"ai_heal={heal_result.get('outcome')}: {heal_result.get('detail')}",
            }
        )
        if heal_result.get("resolved"):
            return {
                "outcome": "resolved",
                "current_action_log": (
                    f"AI healed {next_action.action_type} on "
                    f"{stable_target or next_action.target}"
                ),
            }
        return {"outcome": "continue"}

