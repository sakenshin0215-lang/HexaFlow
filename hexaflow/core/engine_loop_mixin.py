import os
import logging

from playwright.async_api import Page

from hexaflow.core.task_schema import TaskSpec
from hexaflow.tools.dom_parser import DomParser
from hexaflow.tools.helpers import build_recent_steps_text


logger = logging.getLogger("HexaEngine")


class EngineLoopMixin:
    @staticmethod
    def _find_loop_range(steps, loop_name: str):
        start_idx = None
        end_idx = None
        for i, step in enumerate(steps):
            if getattr(step, "loop_marker", None) == "start" and getattr(step, "loop_name", None) == loop_name:
                start_idx = i
                break
        if start_idx is None:
            return None, None
        for j in range(start_idx, len(steps)):
            step = steps[j]
            if getattr(step, "loop_marker", None) == "end" and getattr(step, "loop_name", None) == loop_name:
                end_idx = j
                break
        return start_idx, end_idx

    async def _execute_step_for_loop(
        self,
        page: Page,
        step,
        human_handoff_on_auth: bool,
        replay_repair_agent=None,
        task_name: str = "",
        recent_steps_text: str = "",
        max_attempts: int = 5,
        heal_log: dict = None,
        disable_fallback_recovery: bool = False,
        monitor_run_key: str = "",
    ) -> bool:
        effective_max_attempts = 2 if disable_fallback_recovery else max_attempts
        for attempt in range(effective_max_attempts):
            try:
                if monitor_run_key:
                    dom_snapshot = await DomParser.get_interactive_elements(page)
                    self._update_element_monitor(
                        run_key=monitor_run_key,
                        step_label=f"{step.step_id}@attempt_{attempt + 1}",
                        dom_snapshot=dom_snapshot,
                        current_url=page.url,
                    )
                    await self._update_live_element_overlay(
                        page=page,
                        run_key=monitor_run_key,
                        step_label=f"{step.step_id}@attempt_{attempt + 1}",
                        dom_snapshot=dom_snapshot,
                        current_url=page.url,
                    )
                await self._execute_deterministic_step(page, step)
                return True
            except Exception as e:
                error_msg = str(e).lower()
                if getattr(step, "is_optional", False) and (
                    "timeout" in error_msg
                    or "intercepted" in error_msg
                    or "not visible" in error_msg
                    or "dom matching failed" in error_msg
                ):
                    logger.info(f"⏭️ [循环可选步骤] 执行失败，安全跳过: {step.step_id}")
                    return True

                handled = await self._human_handoff_if_needed(
                    page,
                    checkpoint=f"循环步骤失败人工检查(step_id={step.step_id})",
                    enabled=human_handoff_on_auth,
                )
                if handled and attempt < max_attempts - 1:
                    await page.wait_for_timeout(800)
                    continue

                if disable_fallback_recovery and replay_repair_agent and attempt < effective_max_attempts:
                    heal_result = await self._run_ai_heal_agent(
                        page=page,
                        ai_heal_agent=replay_repair_agent,
                        task_name=task_name or "loop_task",
                        recent_steps=recent_steps_text or "No previous steps.",
                        failed_step_desc=step.description,
                        failed_action_type=step.action.action_type,
                        failed_target=step.action.target,
                        failed_input_value=step.action.input_value,
                        last_error=str(e),
                        step_id=step.step_id,
                        validate_coro=lambda: self._execute_deterministic_step(page, step, fast_mode=True),
                        human_handoff_on_auth=human_handoff_on_auth,
                        max_attempts=3,
                        heal_log=heal_log,
                        expected_url_contains=getattr(step, "guard_url_contains", None) or step.pre_check.expected_url_contains or "",
                    )
                    if heal_result.get("resolved"):
                        return True
                    self._append_heal_record(
                        heal_log,
                        phase="loop_ai_summary",
                        step_id=step.step_id,
                        result=heal_result.get("outcome", "failed"),
                        detail=heal_result.get("detail", ""),
                    )
                    continue

                if "timeout" in error_msg or "intercepted" in error_msg or "not visible" in error_msg or "dom matching failed" in error_msg:
                    if attempt == 0 and (not disable_fallback_recovery):
                        fixed = await self._attempt_wrong_page_recovery(page, step)
                        self._append_heal_record(
                            heal_log,
                            phase="loop_fallback_wrong_page",
                            step_id=step.step_id,
                            result="ok" if fixed else "failed",
                            detail=f"attempt={attempt + 1}",
                        )
                        await page.wait_for_timeout(1000)
                        continue
                    ai_attempt_index = 0 if disable_fallback_recovery else 1
                    if attempt == ai_attempt_index:
                        if replay_repair_agent:
                            heal_result = await self._run_ai_heal_agent(
                                page=page,
                                ai_heal_agent=replay_repair_agent,
                                task_name=task_name or "loop_task",
                                recent_steps=recent_steps_text or "No previous steps.",
                                failed_step_desc=step.description,
                                failed_action_type=step.action.action_type,
                                failed_target=step.action.target,
                                failed_input_value=step.action.input_value,
                                last_error=str(e),
                                step_id=step.step_id,
                                validate_coro=lambda: self._execute_deterministic_step(page, step, fast_mode=True),
                                human_handoff_on_auth=human_handoff_on_auth,
                                max_attempts=3,
                                heal_log=heal_log,
                                expected_url_contains=getattr(step, "guard_url_contains", None) or step.pre_check.expected_url_contains or "",
                            )
                            if heal_result.get("resolved"):
                                return True
                            self._append_heal_record(
                                heal_log,
                                phase="loop_ai_summary",
                                step_id=step.step_id,
                                result=heal_result.get("outcome", "failed"),
                                detail=heal_result.get("detail", ""),
                            )
                            await page.wait_for_timeout(600)
                            continue
                        if not disable_fallback_recovery:
                            fixed = await self._attempt_generic_recovery(page)
                            self._append_heal_record(
                                heal_log,
                                phase="loop_fallback_generic",
                                step_id=step.step_id,
                                result="ok" if fixed else "failed",
                                detail=f"attempt={attempt + 1}",
                            )
                            await page.wait_for_timeout(900)
                            continue
                if attempt >= effective_max_attempts - 1:
                    return False
        return False

    async def run_loop_from_trace(
        self,
        trace_path: str,
        loop_iterations: int,
        loop_name: str = "main_loop",
        max_iteration_retries: int = 30,
        context=None,
        viewport: dict = None,
        user_agent: str = None,
        human_handoff_on_auth: bool = True,
        replay_repair_agent=None,
        repair_context_window: int = 5,
        disable_fallback_recovery: bool = False,
    ):
        """
        循环任务执行器：
        - 依据 trace 中的 loop_marker(start/end) 定义循环区间
        - 循环次数由外部参数指定
        - 仅当“整轮循环步骤全部成功”才计为完成一次
        - 失败则整轮重试，直到成功或超过 max_iteration_retries
        """
        from hexaflow.agents.schemas import WorkflowBlueprint
        self.disable_fallback_recovery_runtime = disable_fallback_recovery

        if loop_iterations < 1:
            raise ValueError("loop_iterations 必须 >= 1")

        if not os.path.exists(trace_path):
            raise FileNotFoundError(f"找不到轨迹文件: {trace_path}")

        logger.info(f"📂 正在加载循环轨迹: {trace_path}")
        with open(trace_path, "r", encoding="utf-8") as f:
            trace_data = f.read()
        blueprint = WorkflowBlueprint.model_validate_json(trace_data)

        loop_start, loop_end = self._find_loop_range(blueprint.steps, loop_name=loop_name)
        if loop_start is None or loop_end is None or loop_start > loop_end:
            raise ValueError(
                f"未在 trace 中找到有效循环标记: loop_name={loop_name} (start/end)"
            )

        run_state = self.state_machine.start_or_resume(
            trace_path=trace_path,
            task_name=f"{blueprint.task_name}::loop({loop_name})",
            total_steps=len(blueprint.steps),
            resume=False,
        )
        heal_log = self._create_heal_log(
            task_name=f"{blueprint.task_name}::loop({loop_name})",
            run_id=run_state.run_id,
            trace_path=trace_path,
        )
        logger.info(
            f"🔁 循环任务启动: run_id={run_state.run_id} loop={loop_name} "
            f"range=[{loop_start}, {loop_end}] iterations={loop_iterations}"
        )
        self._runtime_tool_agent = replay_repair_agent
        self._runtime_tool_goal = f"Loop task: {blueprint.task_name}::{loop_name}"
        self._runtime_tool_history = ""
        self._init_element_monitor(
            run_key=run_state.run_id,
            task_name=f"{blueprint.task_name}::loop({loop_name})",
            mode="loop",
        )

        if not context:
            vp = viewport or {"width": 1280, "height": 800}
            context_options = {"viewport": vp}
            if user_agent:
                context_options["user_agent"] = user_agent
            context = await self._resolve_context(context=context, context_options=context_options)

        page = await context.new_page()
        first_bootstrap = self._infer_bootstrap_url(blueprint, 0)
        if first_bootstrap:
            await page.goto(first_bootstrap, wait_until="domcontentloaded")
        await self._human_handoff_if_needed(page, checkpoint="循环任务启动检查", enabled=human_handoff_on_auth)

        completed_loops = 0

        try:
            # A) 循环前步骤，只执行一次
            pre_steps = blueprint.steps[:loop_start]
            for idx, step in enumerate(pre_steps):
                recent = build_recent_steps_text(blueprint, idx, repair_context_window)
                ok = await self._execute_step_for_loop(
                    page=page,
                    step=step,
                    human_handoff_on_auth=human_handoff_on_auth,
                    replay_repair_agent=replay_repair_agent,
                    task_name=blueprint.task_name,
                    recent_steps_text=recent,
                    heal_log=heal_log,
                    disable_fallback_recovery=disable_fallback_recovery,
                    monitor_run_key=run_state.run_id,
                )
                if not ok:
                    raise Exception(f"循环前置步骤失败: {step.step_id}")

            # B) 循环区间
            loop_steps = blueprint.steps[loop_start: loop_end + 1]
            for i in range(1, loop_iterations + 1):
                iteration_retry = 0
                while True:
                    iteration_retry += 1
                    logger.info(
                        f"🔁 开始第 {i}/{loop_iterations} 次循环尝试 (retry={iteration_retry}/{max_iteration_retries})"
                    )
                    iter_ok = True
                    for offset, step in enumerate(loop_steps):
                        step_index = loop_start + offset
                        recent = build_recent_steps_text(
                            blueprint, step_index, repair_context_window
                        )
                        ok = await self._execute_step_for_loop(
                            page=page,
                            step=step,
                            human_handoff_on_auth=human_handoff_on_auth,
                            replay_repair_agent=replay_repair_agent,
                            task_name=blueprint.task_name,
                            recent_steps_text=recent,
                            heal_log=heal_log,
                            disable_fallback_recovery=disable_fallback_recovery,
                            monitor_run_key=run_state.run_id,
                        )
                        if not ok:
                            iter_ok = False
                            break

                    if iter_ok:
                        completed_loops += 1
                        remaining = loop_iterations - completed_loops
                        logger.info(
                            f"✅ 循环完成: {completed_loops}/{loop_iterations} (remaining={remaining})"
                        )
                        self.state_machine.add_event(
                            run_state.run_id,
                            event="loop_iteration_completed",
                            detail=f"loop={loop_name} completed={completed_loops} remaining={remaining}",
                        )
                        break

                    if iteration_retry >= max_iteration_retries:
                        raise Exception(
                            f"循环第 {i} 轮重试超过上限({max_iteration_retries})，仍未成功"
                        )

                    self.state_machine.add_event(
                        run_state.run_id,
                        event="loop_iteration_retry",
                        detail=f"loop={loop_name} iteration={i} retry={iteration_retry}",
                    )
                    self._append_heal_record(
                        heal_log,
                        phase="loop_iteration_retry",
                        step_id=f"loop:{loop_name}",
                        result="retry",
                        detail=f"iteration={i} retry={iteration_retry}",
                    )
                    logger.warning(f"⚠️ 第 {i} 次循环未成功，本轮重试。")

            # C) 循环后步骤，只执行一次
            post_steps = blueprint.steps[loop_end + 1:]
            for idx2, step in enumerate(post_steps, start=loop_end + 1):
                recent = build_recent_steps_text(blueprint, idx2, repair_context_window)
                ok = await self._execute_step_for_loop(
                    page=page,
                    step=step,
                    human_handoff_on_auth=human_handoff_on_auth,
                    replay_repair_agent=replay_repair_agent,
                    task_name=blueprint.task_name,
                    recent_steps_text=recent,
                    heal_log=heal_log,
                    disable_fallback_recovery=disable_fallback_recovery,
                    monitor_run_key=run_state.run_id,
                )
                if not ok:
                    raise Exception(f"循环后置步骤失败: {step.step_id}")

            self.state_machine.mark_run_completed(run_state.run_id)
            from datetime import datetime
            heal_log["status"] = "completed"
            heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
            heal_paths = self._save_heal_log(heal_log)
            element_monitor_paths = self._finalize_element_monitor(run_state.run_id)
            report_paths = self._save_run_report(run_state.run_id)
            logger.info("🎉 循环任务执行完成")
            self._runtime_tool_agent = None
            self._runtime_tool_goal = ""
            self._runtime_tool_history = ""
            self.disable_fallback_recovery_runtime = False
            return {
                "completed_loops": completed_loops,
                "target_loops": loop_iterations,
                "remaining_loops": max(0, loop_iterations - completed_loops),
                "report_paths": report_paths,
                "heal_log_paths": heal_paths,
                "element_monitor": element_monitor_paths,
            }
        except Exception as e:
            detail = str(e)
            screenshot_path = await self._capture_suspend_snapshot(page, run_state.run_id, "loop_task")
            if screenshot_path:
                detail = f"{detail}\n[screenshot]: {screenshot_path}"
            self.state_machine.mark_run_suspended(run_state.run_id, loop_start, "loop_task", detail)
            from datetime import datetime
            heal_log["status"] = "suspended"
            heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
            self._finalize_element_monitor(run_state.run_id)
            self._save_heal_log(heal_log)
            self._save_run_report(run_state.run_id)
            self._runtime_tool_agent = None
            self._runtime_tool_goal = ""
            self._runtime_tool_history = ""
            self.disable_fallback_recovery_runtime = False
            raise

    async def run_loop_task_from_spec(
        self,
        trace_path: str,
        spec_input,
        context=None,
        replay_repair_agent=None,
        disable_fallback_recovery: bool = False,
    ):
        if isinstance(spec_input, TaskSpec):
            spec = spec_input
        elif isinstance(spec_input, str):
            spec = TaskSpec.from_json_file(spec_input)
        elif isinstance(spec_input, dict):
            spec = TaskSpec.model_validate(spec_input)
        else:
            raise ValueError("Unsupported task spec input type")

        if not spec.loop_policy.enabled:
            raise ValueError("TaskSpec.loop_policy.enabled 为 false，无法运行循环任务。")

        return await self.run_loop_from_trace(
            trace_path=trace_path,
            loop_iterations=spec.loop_policy.iterations,
            loop_name=spec.loop_policy.loop_name,
            max_iteration_retries=spec.loop_policy.max_iteration_retries,
            context=context,
            human_handoff_on_auth=True,
            replay_repair_agent=replay_repair_agent,
            disable_fallback_recovery=disable_fallback_recovery,
        )
