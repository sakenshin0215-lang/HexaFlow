import os
import json
import logging

from playwright.async_api import Page
from hexaflow.tools.dom_parser import DomParser


logger = logging.getLogger("HexaEngine")


class EngineAIHealMixin:
    async def _run_ai_heal_agent(
        self,
        page: Page,
        ai_heal_agent,
        task_name: str,
        recent_steps: str,
        failed_step_desc: str,
        failed_action_type: str,
        failed_target: str,
        last_error: str,
        failed_input_value: str = None,
        step_id: str = "",
        step_index: int = -1,
        run_id: str = "",
        validate_coro=None,
        human_handoff_on_auth: bool = True,
        max_attempts: int = 3,
        heal_log: dict = None,
        expected_url_contains: str = "",
    ):
        """
        AI 修复链路：
        1) 传入最近步骤 + 当前DOM + 截图 + 上一次尝试历史
        2) 执行 AI 提议动作后验证是否恢复
        3) 连续失败 max_attempts 次后，触发人工接管
        """
        if not ai_heal_agent or not hasattr(ai_heal_agent, "repair_failed_replay_step"):
            return {"resolved": False, "outcome": "no_agent", "detail": "missing ai heal agent"}

        failed_target = self._canonicalize_runtime_target(failed_target or "")
        avoid_modal_dismiss = False
        try:
            if failed_action_type in ("click", "type", "click_type_enter"):
                avoid_modal_dismiss = await self._is_target_text_inside_dialog(page, failed_target)
        except Exception:
            avoid_modal_dismiss = False

        attempts_log = []
        for attempt in range(1, max_attempts + 1):
            try:
                if failed_action_type == "click" and await self._is_click_intent_satisfied(page, failed_step_desc, failed_target):
                    self._append_heal_record(
                        heal_log,
                        phase="ai_validate",
                        step_id=step_id,
                        step_index=step_index,
                        result="ok",
                        detail=f"attempt={attempt}/{max_attempts} intent_already_satisfied target={failed_target}",
                    )
                    logger.info(f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 检测到步骤意图已满足，结束修复")
                    return {
                        "resolved": True,
                        "outcome": "applied",
                        "detail": "intent_already_satisfied",
                    }
            except Exception:
                pass

            # 优先尝试关闭遮挡弹窗（X/关闭/跳过），避免进入“下一步”教程链
            try:
                dismissed = False
                if not avoid_modal_dismiss:
                    dismissed = await self._dismiss_blocking_modal(page)
                if dismissed:
                    self._append_heal_record(
                        heal_log,
                        phase="modal_dismiss",
                        step_id=step_id,
                        step_index=step_index,
                        result="ok",
                        detail=f"attempt={attempt}/{max_attempts}",
                    )
                    if validate_coro:
                        try:
                            await validate_coro()
                            logger.info(f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 关闭弹窗后验证成功")
                            return {
                                "resolved": True,
                                "outcome": "applied",
                                "detail": "modal_dismiss_then_validated",
                            }
                        except Exception:
                            pass
            except Exception:
                pass

            # 修复期间保持页面上下文稳定，防止误点后漂移到错误页面
            if expected_url_contains and expected_url_contains not in (page.url or ""):
                inferred = self._normalize_url_candidate(expected_url_contains)
                if inferred:
                    try:
                        await page.goto(inferred, wait_until="domcontentloaded", timeout=12000)
                        await page.wait_for_timeout(600)
                    except Exception:
                        pass

            screenshot_paths = await self._capture_heal_visual_pack(
                page, run_id or "aiheal", step_id or "unknown"
            )
            screenshot_path = screenshot_paths[0] if screenshot_paths else ""
            await self._wait_for_page_settle(page)
            dom_snapshot = await DomParser.get_interactive_elements(page)
            attempts_text = "\n".join(
                [f"{idx + 1}. {item}" for idx, item in enumerate(attempts_log)]
            ) or "None."
            composed_error = (
                f"{last_error}\n"
                f"[ai_heal_attempt]={attempt}/{max_attempts}\n"
                f"[screenshots]={screenshot_paths if screenshot_paths else 'N/A'}\n"
                f"[previous_attempts]\n{attempts_text}"
            )

            try:
                decision = await ai_heal_agent.repair_failed_replay_step(
                    task_name=task_name,
                    recent_steps=recent_steps,
                    current_url=page.url,
                    dom_snapshot=dom_snapshot,
                    failed_step_desc=failed_step_desc,
                    failed_action_type=failed_action_type,
                    failed_target=failed_target,
                    last_error=composed_error,
                    screenshot_path=screenshot_path or "",
                    screenshot_paths=screenshot_paths,
                )
            except TypeError:
                # 兼容旧版 agent 签名（不支持 screenshot_path）
                decision = await ai_heal_agent.repair_failed_replay_step(
                    task_name=task_name,
                    recent_steps=recent_steps,
                    current_url=page.url,
                    dom_snapshot=dom_snapshot,
                    failed_step_desc=failed_step_desc,
                    failed_action_type=failed_action_type,
                    failed_target=failed_target,
                    last_error=composed_error,
                )

            if run_id:
                self.state_machine.add_event(
                    run_id,
                    event="ai_heal_decision",
                    detail=(
                        f"step={step_id} attempt={attempt}/{max_attempts} "
                        f"strategy={decision.strategy} confidence={decision.confidence:.2f} "
                        f"thought={decision.thought}"
                    ),
                    step_index=step_index,
                    step_id=step_id,
                )
            self._append_heal_record(
                heal_log,
                phase="ai_decision",
                step_id=step_id,
                step_index=step_index,
                result=decision.strategy,
                detail=(
                    f"attempt={attempt}/{max_attempts} confidence={decision.confidence:.2f} "
                    f"target={decision.target or ''} action={decision.action_type or ''} "
                    f"screenshots={screenshot_paths if screenshot_paths else []}"
                ),
            )

            if decision.strategy == "skip_step":
                reason = decision.skip_reason or "AI heal: step skipped"
                self._append_heal_record(
                    heal_log,
                    phase="ai_apply",
                    step_id=step_id,
                    step_index=step_index,
                    result="skipped",
                    detail=reason,
                )
                return {"resolved": True, "outcome": "skipped", "detail": reason}

            if decision.strategy == "human_handoff":
                await self._human_handoff_if_needed(
                    page,
                    checkpoint=f"AI修复建议人工接管(step_id={step_id or 'dynamic'})",
                    enabled=human_handoff_on_auth,
                )
                self._append_heal_record(
                    heal_log,
                    phase="ai_apply",
                    step_id=step_id,
                    step_index=step_index,
                    result="human_handoff",
                    detail="manual intervention requested",
                )
                if validate_coro:
                    try:
                        await validate_coro()
                        logger.info(f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 人工接管后验证成功")
                        return {
                            "resolved": True,
                            "outcome": "human_handoff",
                            "detail": "human intervention validated",
                        }
                    except Exception as handoff_err:
                        logger.warning(
                            f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 人工接管后验证失败: {handoff_err}"
                        )
                        attempts_log.append(
                            f"strategy=human_handoff validated_failed error={handoff_err}"
                        )
                        continue
                attempts_log.append("strategy=human_handoff but no validate_coro")
                continue

            if decision.strategy in ("retry_with_new_selector", "replace_action"):
                if decision.strategy == "retry_with_new_selector":
                    action_type = failed_action_type
                    target = self._canonicalize_runtime_target(decision.target or "")
                    input_value = failed_input_value
                    if not target:
                        self._append_heal_record(
                            heal_log,
                            phase="ai_apply",
                            step_id=step_id,
                            step_index=step_index,
                            result="failed",
                            detail="retry_with_new_selector returned empty target",
                        )
                        attempts_log.append("retry_with_new_selector returned empty target")
                        continue
                else:
                    action_type = decision.action_type or failed_action_type
                    target = self._canonicalize_runtime_target(decision.target or "")
                    input_value = decision.input_value
                used_new_target = bool(target and target != failed_target)

                # AI修复点击动作统一坐标化执行（不直接按文本/selector点击）
                if action_type in ("click", "click_type_enter"):
                    ratio = None
                    # 优先用 AI 给的 target 解析坐标，失败则回退原失败 target
                    if target:
                        ratio = await self._selector_to_click_ratio(page, target)
                    if (not ratio) and failed_target:
                        ratio = await self._selector_to_click_ratio(page, failed_target)
                    if ratio:
                        x_ratio, y_ratio = ratio
                        if action_type == "click":
                            action_type = "click_relative"
                            input_value = f"{x_ratio:.6f},{y_ratio:.6f}"
                            target = None
                        else:
                            # click_type_enter -> 坐标点击后键盘输入回车
                            action_type = "click_relative_type_enter"
                            target = f"{x_ratio:.6f},{y_ratio:.6f}"
                    else:
                        # 坐标不可用时回退 selector 执行，避免“坐标解析失败”卡死
                        fallback_selector = target or failed_target
                        if fallback_selector:
                            self._append_heal_record(
                                heal_log,
                                phase="ai_apply",
                                step_id=step_id,
                                step_index=step_index,
                                result="fallback_selector",
                                detail=f"coordinate_resolve_failed, fallback_selector={fallback_selector}",
                            )
                            logger.warning(
                                f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 坐标解析失败，回退 selector 执行: {fallback_selector}"
                            )
                            target = fallback_selector
                        else:
                            self._append_heal_record(
                                heal_log,
                                phase="ai_apply",
                                step_id=step_id,
                                step_index=step_index,
                                result="failed",
                                detail=f"coordinate_resolve_failed target={target} fallback_target={failed_target}",
                            )
                            attempts_log.append(
                                f"coordinate_resolve_failed target={target} fallback_target={failed_target}"
                            )
                            logger.warning(
                                f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 坐标解析失败，进入下一轮"
                            )
                            continue
                pre_attempt_sig = ""
                try:
                    pre_attempt_sig = await self._compute_page_state_signature(page)
                except Exception:
                    pre_attempt_sig = ""
                exec_err = None
                try:
                    await self._execute_override_action(
                        page=page,
                        action_type=action_type,
                        target=target,
                        input_value=input_value,
                    )
                except Exception as e:
                    exec_err = e
                    self._append_heal_record(
                        heal_log,
                        phase="ai_apply",
                        step_id=step_id,
                        step_index=step_index,
                        result="failed",
                        detail=f"action={action_type} target={target} exec_error={exec_err}",
                    )
                    logger.warning(
                        f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 动作执行失败: {exec_err}"
                    )

                # 每次修复动作后都立即验证一次“原步骤是否已恢复”
                # 注意：若 AI 已切换到新 target，强行用旧 target 验证会误判失败。
                # 这种情况下优先认定本次修复已应用成功，交给主流程继续。
                if used_new_target and decision.strategy == "retry_with_new_selector":
                    self._append_heal_record(
                        heal_log,
                        phase="ai_validate",
                        step_id=step_id,
                        step_index=step_index,
                        result="ok",
                        detail=(
                            f"attempt={attempt}/{max_attempts} "
                            f"skip_old_target_validation old={failed_target} new={target}"
                        ),
                    )
                    logger.info(
                        f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 已应用新selector，跳过旧目标强验证"
                    )
                    return {
                        "resolved": True,
                        "outcome": "applied",
                        "detail": f"action={action_type} target={target}",
                    }

                if exec_err is None:
                    try:
                        post_action_sig = await self._compute_page_state_signature(page)
                        if post_action_sig != pre_attempt_sig:
                            self._append_heal_record(
                                heal_log,
                                phase="ai_validate",
                                step_id=step_id,
                                step_index=step_index,
                                result="ok",
                                detail=f"attempt={attempt}/{max_attempts} state_signature_changed",
                            )
                            logger.info(
                                f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 检测到状态签名变化，视为修复成功"
                            )
                            return {
                                "resolved": True,
                                "outcome": "applied",
                                "detail": "state_signature_changed",
                            }
                    except Exception:
                        pass

                if validate_coro:
                    try:
                        await validate_coro()
                        self._append_heal_record(
                            heal_log,
                            phase="ai_validate",
                            step_id=step_id,
                            step_index=step_index,
                            result="ok",
                            detail=f"attempt={attempt}/{max_attempts}",
                        )
                        logger.info(f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 验证成功")
                        return {
                            "resolved": True,
                            "outcome": "applied",
                            "detail": f"action={action_type} target={target}",
                        }
                    except Exception as validate_err:
                        try:
                            if failed_action_type == "click" and await self._is_click_intent_satisfied(page, failed_step_desc, failed_target):
                                self._append_heal_record(
                                    heal_log,
                                    phase="ai_validate",
                                    step_id=step_id,
                                    step_index=step_index,
                                    result="ok",
                                    detail=f"attempt={attempt}/{max_attempts} intent_satisfied_after_validate_error target={failed_target}",
                                )
                                logger.info(
                                    f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 虽验证报错但步骤意图已满足，结束修复"
                                )
                                return {
                                    "resolved": True,
                                    "outcome": "applied",
                                    "detail": "intent_satisfied_after_validate_error",
                                }
                        except Exception:
                            pass
                        self._append_heal_record(
                            heal_log,
                            phase="ai_validate",
                            step_id=step_id,
                            step_index=step_index,
                            result="failed",
                            detail=f"attempt={attempt}/{max_attempts} error={validate_err}",
                        )
                        logger.warning(
                            f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 验证失败: {validate_err}"
                        )
                        attempts_log.append(
                            f"strategy={decision.strategy} action={action_type} target={target} "
                            f"exec_error={exec_err} validate_error={validate_err}"
                        )
                        continue

                # 无 validate 时，以动作执行结果判定
                if exec_err is None:
                    self._append_heal_record(
                        heal_log,
                        phase="ai_apply",
                        step_id=step_id,
                        step_index=step_index,
                        result="applied",
                        detail=f"action={action_type} target={target}",
                    )
                    return {
                        "resolved": True,
                        "outcome": "applied",
                        "detail": f"action={action_type} target={target}",
                    }
                attempts_log.append(
                    f"strategy={decision.strategy} action={action_type} target={target} exec_error={exec_err}"
                )
                continue

            attempts_log.append(
                f"strategy={decision.strategy} no executable fix returned"
            )
            self._append_heal_record(
                heal_log,
                phase="ai_apply",
                step_id=step_id,
                step_index=step_index,
                result="no_fix",
                detail=f"strategy={decision.strategy}",
            )

        # 三次仍失败，要求人工介入
        await self._human_handoff_if_needed(
            page,
            checkpoint=f"AI修复连续失败{max_attempts}次(step_id={step_id or 'dynamic'})",
            enabled=True,
        )
        return {
            "resolved": False,
            "outcome": "failed_after_retries",
            "detail": "\n".join(attempts_log),
        }

    async def _replay_recent_subflow(
        self,
        page: Page,
        blueprint,
        step_index: int,
        window: int = 2,
        skip_risky: bool = True,
        risky_keywords: list[str] = None,
    ) -> bool:
        """
        回放前 N 步小流程，尝试把页面状态带回正确上下文。
        """
        if risky_keywords is None:
            risky_keywords = [
                "确认",
                "提交",
                "兑换",
                "swap",
                "approve",
                "授权",
                "sign",
                "签名",
                "购买",
                "buy",
                "sell",
                "下单",
                "pay",
                "付款",
                "connect wallet",
                "连接钱包",
            ]
        start = max(0, step_index - window)
        if start >= step_index:
            return False

        for idx in range(start, step_index):
            replay_step = blueprint.steps[idx]
            if skip_risky and self._is_risky_step_for_replay(replay_step, risky_keywords):
                continue
            try:
                await self._execute_deterministic_step(page, replay_step)
            except Exception:
                return False
        return True

    def _save_run_report(self, run_id: str):
        if not getattr(self, "save_reports", False):
            return None
        try:
            report_paths = self.run_reporter.save_report(run_id)
            logger.info(
                f"🧾 执行报告已生成: json={report_paths['json_path']} md={report_paths['md_path']}"
            )
            return report_paths
        except Exception as e:
            logger.error(f"❌ 报告生成失败(run_id={run_id}): {e}")
            return None

    @staticmethod
    def _create_heal_log(task_name: str, run_id: str, trace_path: str):
        from datetime import datetime
        return {
            "task_name": task_name,
            "run_id": run_id,
            "trace_path": trace_path,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "ended_at": "",
            "status": "running",
            "records": [],
        }

    @staticmethod
    def _append_heal_record(heal_log: dict, **kwargs):
        from datetime import datetime
        if heal_log is None:
            return
        row = {"ts": datetime.now().isoformat(timespec="seconds")}
        row.update(kwargs)
        heal_log["records"].append(row)

    def _save_heal_log(self, heal_log: dict):
        if not getattr(self, "save_reports", False):
            return None
        if not heal_log:
            return None
        from datetime import datetime
        os.makedirs("workspace/reports", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = heal_log.get("run_id", "unknown")
        base = f"heal_{run_id}_{ts}"
        json_path = os.path.join("workspace/reports", f"{base}.json")
        md_path = os.path.join("workspace/reports", f"{base}.md")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(heal_log, f, ensure_ascii=False, indent=2)

        lines = [
            f"# 修复清单 - {heal_log.get('task_name', '')}",
            "",
            f"- RunID: `{heal_log.get('run_id', '')}`",
            f"- Trace: `{heal_log.get('trace_path', '')}`",
            f"- Status: `{heal_log.get('status', '')}`",
            f"- Started: `{heal_log.get('started_at', '')}`",
            f"- Ended: `{heal_log.get('ended_at', '')}`",
            "",
            "## 记录",
            "",
        ]
        records = heal_log.get("records", [])
        if not records:
            lines.append("- 无修复记录")
        else:
            for i, rec in enumerate(records, start=1):
                lines.append(
                    f"{i}. [{rec.get('ts')}] phase={rec.get('phase', '')} "
                    f"step={rec.get('step_id', '')} result={rec.get('result', '')} "
                    f"detail={rec.get('detail', '')}"
                )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"🩹 修复清单已生成: json={json_path} md={md_path}")
        return {"json_path": json_path, "md_path": md_path}

    def _save_ai_action_log(self, task_name: str, actions: list[dict]):
        if not getattr(self, "save_reports", False):
            return None
        from datetime import datetime
        os.makedirs("workspace/reports", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_task = (task_name or "ai_task").replace("/", "_").replace(" ", "_")
        base = f"ai_actions_{safe_task}_{ts}"
        json_path = os.path.join("workspace/reports", f"{base}.json")
        md_path = os.path.join("workspace/reports", f"{base}.md")

        payload = {
            "task_name": task_name,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total_actions": len(actions),
            "actions": actions,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        lines = [f"# AI 操作日志 - {task_name}", "", f"- total_actions: {len(actions)}", "", "## Actions", ""]
        if not actions:
            lines.append("- 无动作")
        else:
            for i, a in enumerate(actions, start=1):
                lines.append(
                    f"{i}. step={a.get('step')} action={a.get('action_type')} target={a.get('target')} "
                    f"success={a.get('success')} note={a.get('note', '')}"
                )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"🤖 AI操作日志已生成: json={json_path} md={md_path}")
        return {"json_path": json_path, "md_path": md_path}
