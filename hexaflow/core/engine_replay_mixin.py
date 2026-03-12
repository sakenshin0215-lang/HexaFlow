import logging
import os
import random
import re

from playwright.async_api import Page

from hexaflow.tools.dom_parser import DomParser
from hexaflow.browser.token_selector import ensure_quote_token
from hexaflow.tools.helpers import build_recent_steps_text


logger = logging.getLogger("HexaEngine")


class EngineReplayMixin:
    @staticmethod
    def _extract_selector_from_step_description(description: str) -> str:
        raw = (description or "").strip()
        if not raw:
            return ""
        # 支持 ```json ...``` 包裹
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
        m = re.search(r'"selector"\s*:\s*"([^"]+)"', raw)
        if m:
            return (m.group(1) or "").strip()
        return ""

    @staticmethod
    def _normalize_class_tokens(raw_classes: str) -> set[str]:
        tokens = set()
        for t in (raw_classes or "").split():
            s = (t or "").strip()
            if not s:
                continue
            # 过滤明显动态 hash 类，保留结构语义类
            if "__" in s:
                continue
            if len(s) >= 14 and any(ch.isdigit() for ch in s):
                continue
            tokens.add(s.lower())
        return tokens

    def _candidate_similarity_score(
        self,
        candidate_text: str,
        candidate_classes: str,
        target_text_hint: str,
        fingerprint_classes: str,
    ) -> float:
        score = 0.0

        # 文本相似度
        c_text = (candidate_text or "").strip().lower()
        t_text = (target_text_hint or "").strip().lower()
        if t_text:
            if c_text == t_text:
                score += 2.0
            elif t_text in c_text:
                score += 1.2
            elif c_text and (c_text in t_text):
                score += 0.8

        # classes 相似度（Jaccard）
        fp_tokens = self._normalize_class_tokens(fingerprint_classes)
        cd_tokens = self._normalize_class_tokens(candidate_classes)
        if fp_tokens and cd_tokens:
            inter = len(fp_tokens & cd_tokens)
            union = len(fp_tokens | cd_tokens)
            if union > 0:
                score += 2.5 * (inter / union)
            # 额外加权：关键语义类命中
            key_hits = sum(1 for k in ("tabs-pane", "segmented", "active", "button") if k in cd_tokens and k in fp_tokens)
            score += 0.25 * key_hits
        elif fp_tokens and not cd_tokens:
            score -= 0.2

        return score

    @staticmethod
    def _class_pattern_to_regex(pattern: str):
        import re

        p = pattern or ""
        escaped = re.escape(p).replace(r"\*\*\*", ".*?")
        return re.compile(escaped, re.IGNORECASE)

    async def _wait_class_pattern(
        self,
        page,
        selector: str,
        class_pattern: str,
        timeout_ms: int = 3000,
        expect_match: bool = True,
    ):
        if not selector or not class_pattern:
            return
        pattern = self._class_pattern_to_regex(class_pattern)
        rounds = max(1, int(timeout_ms / 200))
        last_class = ""
        for _ in range(rounds):
            try:
                loc = page.locator(selector).first
                if not await loc.is_visible(timeout=200):
                    await page.wait_for_timeout(200)
                    continue
                cls = (await loc.get_attribute("class") or "").strip()
                last_class = cls
                matched = bool(pattern.search(cls))
                if expect_match and matched:
                    return
                if (not expect_match) and (not matched):
                    return
            except Exception:
                pass
            await page.wait_for_timeout(200)
        mode = "match" if expect_match else "not-match"
        raise Exception(
            f"PostCheck class {mode} timeout: selector={selector}, pattern={class_pattern}, last_class={last_class}"
        )

    async def _get_visible_locator_best_match(
        self,
        page,
        selector: str,
        timeout_ms: int,
        text_hint: str = "",
        fp_classes: str = "",
    ):
        for _ in range(max(1, int(timeout_ms / 250))):
            base_loc = page.locator(selector)
            count = await base_loc.count()
            best = None  # (score, locator)
            for i in range(count):
                loc = base_loc.nth(i)
                if not await loc.is_visible():
                    continue
                cand_text = ""
                cand_classes = ""
                try:
                    cand_text = (await loc.inner_text() or "").strip()
                except Exception:
                    cand_text = ""
                try:
                    cand_classes = (await loc.get_attribute("class") or "").strip()
                except Exception:
                    cand_classes = ""
                score = self._candidate_similarity_score(
                    candidate_text=cand_text,
                    candidate_classes=cand_classes,
                    target_text_hint=text_hint,
                    fingerprint_classes=fp_classes,
                )
                if best is None or score > best[0]:
                    best = (score, loc)
            if best is not None:
                return best[1]
            await page.wait_for_timeout(250)
        raise Exception(f"Timeout: 没有找到可见的元素 -> {selector}")

    async def _execute_deterministic_step(self, page, step, fast_mode: bool = False):
        logger.info(f"⏳ 步骤 [{step.step_id}]: {step.description}")
        pre = step.pre_check
        act = step.action

        if act.action_type == "navigate":
            logger.info(f"   ⚙️ 动作: navigate -> {act.target}")
            await page.goto(act.target)
            if pre.expected_dom_selector and pre.expected_dom_selector != "body":
                logger.info(f"   🔍 导航后校验 DOM: {pre.expected_dom_selector}")
                await page.wait_for_selector(pre.expected_dom_selector, timeout=pre.timeout_ms)
            return

        await self._ensure_step_page_guard(page, step)

        raw_target = act.target or self._extract_selector_from_step_description(step.description)
        actual_target = self._apply_trace_wildcard_selector(raw_target)
        target_locator = None

        pre_selector = self._apply_trace_wildcard_selector(pre.expected_dom_selector)
        target_text_hint = self._extract_text_hint_from_selector(actual_target or pre_selector or "")
        fp_classes = ""
        try:
            fp = getattr(act, "fingerprint", None)
            fp_classes = (fp.classes or "") if fp else ""
        except Exception:
            fp_classes = ""
        if pre_selector and pre_selector != "body":
            try:
                target_locator = await self._get_visible_locator_best_match(
                    page,
                    pre_selector,
                    pre.timeout_ms,
                    text_hint=target_text_hint,
                    fp_classes=fp_classes,
                )
            except Exception:
                logger.warning(
                    f"⚠️ 原始定位器失效或被遮挡: {pre_selector}，启动特征模糊搜索..."
                )
                fp = getattr(act, "fingerprint", None)
                if fp:
                    fuzzy_selector = await DomParser.fuzzy_find_by_fingerprint(page, fp.model_dump())
                    if fuzzy_selector:
                        try:
                            logger.info(f"🔍 模糊搜索生成的候选定位器: {fuzzy_selector}")
                            target_locator = await self._get_visible_locator_best_match(
                                page,
                                fuzzy_selector,
                                3000,
                                text_hint=target_text_hint,
                                fp_classes=fp_classes,
                            )
                            actual_target = fuzzy_selector
                            logger.info("✅ 模糊匹配自愈成功！")
                        except Exception:
                            logger.warning("❌ 模糊搜索也未能找到可见元素...")
                            logger.warning("↪️ 进入降级模式：跳过 pre_check 强匹配，继续尝试 action target 的鲁棒点击。")
                else:
                    logger.warning("↪️ 缺少 fingerprint，进入降级模式：跳过 pre_check 强匹配，继续尝试 action target 的鲁棒点击。")

        if not target_locator:
            target_locator = page.locator(actual_target).first

        logger.info(f"   ⚙️ 动作: {act.action_type} -> {actual_target}")

        if not fast_mode:
            humanoid_delay = random.uniform(1.5, 3.0) * 1000
            await page.wait_for_timeout(humanoid_delay)

        if act.action_type == "click":
            await self._robust_click(page, selector=actual_target, preferred_locator=target_locator)
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass
        elif act.action_type == "type":
            locator, _ = await self._resolve_input_locator(page, actual_target)
            await self._robust_input_text(page, locator, act.input_value or "", press_enter=False)
        elif act.action_type == "click_type_enter":
            locator, _ = await self._resolve_input_locator(page, actual_target)
            await self._robust_input_text(page, locator, act.input_value or "", press_enter=True)
        elif act.action_type == "press_enter":
            if actual_target:
                await target_locator.click(timeout=5000)
                await target_locator.press("Enter", timeout=5000)
            else:
                await page.keyboard.press("Enter")
        elif act.action_type == "refresh":
            await page.reload(wait_until="domcontentloaded", timeout=15000)
        elif act.action_type == "call_tool":
            tool_name = (act.target or "").strip() or "summarize_page"
            await self._execute_override_action(
                page=page,
                action_type="call_tool",
                target=tool_name,
                input_value=(act.input_value or step.description or ""),
            )
        elif act.action_type == "summarize":
            # Backward compatibility for old traces.
            await self._execute_override_action(
                page=page,
                action_type="call_tool",
                target="summarize_page",
                input_value=(act.input_value or step.description or ""),
            )
        elif act.action_type == "wait_for_timeout":
            await page.wait_for_timeout(1000)
        elif act.action_type == "ensure_quote_token":
            symbol = (act.input_value or "USDT").strip().upper()
            selector = act.target or ".dex-select-value-box button"
            await ensure_quote_token(
                page=page,
                target_symbol=symbol,
                button_selector=selector,
            )

        # Optional post-action assertion for stateful controls (tabs/active classes/etc.)
        post = getattr(step, "post_check", None)
        if post:
            timeout_ms = int(getattr(post, "timeout_ms", 3000) or 3000)
            must_sel = self._apply_trace_wildcard_selector(getattr(post, "expected_dom_selector", None))
            absent_sel = self._apply_trace_wildcard_selector(getattr(post, "absent_dom_selector", None))
            class_sel = self._apply_trace_wildcard_selector(
                getattr(post, "class_target_selector", None)
            ) or actual_target
            expected_class_pattern = getattr(post, "expected_class_pattern", None)
            absent_class_pattern = getattr(post, "absent_class_pattern", None)

            if must_sel:
                await page.locator(must_sel).first.wait_for(state="visible", timeout=timeout_ms)
            if absent_sel:
                try:
                    await page.locator(absent_sel).first.wait_for(state="hidden", timeout=timeout_ms)
                except Exception:
                    # fallback: if selector doesn't exist, that's also acceptable for absent condition
                    cnt = await page.locator(absent_sel).count()
                    if cnt > 0:
                        raise
            if expected_class_pattern:
                await self._wait_class_pattern(
                    page=page,
                    selector=class_sel,
                    class_pattern=expected_class_pattern,
                    timeout_ms=timeout_ms,
                    expect_match=True,
                )
            if absent_class_pattern:
                await self._wait_class_pattern(
                    page=page,
                    selector=class_sel,
                    class_pattern=absent_class_pattern,
                    timeout_ms=timeout_ms,
                    expect_match=False,
                )

    async def run_from_trace(
        self,
        trace_path: str,
        context=None,
        viewport: dict = None,
        user_agent: str = None,
        resume: bool = True,
        suspend_on_failure: bool = True,
        run_id: str = None,
        subflow_window: int = 2,
        subflow_skip_risky: bool = True,
        subflow_risky_keywords: list[str] = None,
        human_handoff_on_auth: bool = True,
        replay_repair_agent=None,
        repair_context_window: int = 5,
        disable_fallback_recovery: bool = False,
    ):
        from datetime import datetime

        from hexaflow.agents.schemas import WorkflowBlueprint

        self.disable_fallback_recovery_runtime = disable_fallback_recovery

        if not os.path.exists(trace_path):
            raise FileNotFoundError(f"找不到轨迹文件: {trace_path}")

        logger.info(f"📂 正在加载黄金轨迹: {trace_path}")
        with open(trace_path, "r", encoding="utf-8") as f:
            trace_data = f.read()

        blueprint = WorkflowBlueprint.model_validate_json(trace_data)
        logger.info(f"▶️ 开始回放任务: {blueprint.task_name} (共 {len(blueprint.steps)} 步)")
        self._runtime_tool_agent = replay_repair_agent
        self._runtime_tool_goal = f"Replay task: {blueprint.task_name}"
        self._runtime_tool_history = ""
        if run_id:
            run_state = self.state_machine.resume_by_run_id(run_id)
            if run_state.trace_path != trace_path:
                raise ValueError(
                    f"run_id={run_id} 绑定的trace与当前输入不一致: {run_state.trace_path} != {trace_path}"
                )
        else:
            run_state = self.state_machine.start_or_resume(
                trace_path=trace_path,
                task_name=blueprint.task_name,
                total_steps=len(blueprint.steps),
                resume=resume,
            )
        start_index = run_state.current_step_index
        logger.info(
            f"🧭 RunID={run_state.run_id} 状态={run_state.status}，将从步骤索引 {start_index} 开始继续执行"
        )
        heal_log = self._create_heal_log(
            task_name=blueprint.task_name,
            run_id=run_state.run_id,
            trace_path=trace_path,
        )
        self._init_element_monitor(
            run_key=run_state.run_id,
            task_name=blueprint.task_name,
            mode="replay",
        )

        if not context:
            vp = viewport or {"width": 1280, "height": 800}
            context_options = {"viewport": vp}

            if user_agent:
                context_options["user_agent"] = user_agent

            context = await self._resolve_context(context=context, context_options=context_options)

        page = await context.new_page()
        if start_index < len(blueprint.steps):
            first_pending_step = blueprint.steps[start_index]
            if page.url == "about:blank" and first_pending_step.action.action_type != "navigate":
                bootstrap_url = self._infer_bootstrap_url(blueprint, start_index)
                if bootstrap_url:
                    logger.info(f"🧭 回放预热: 当前是 about:blank，自动导航到 {bootstrap_url}")
                    await page.goto(bootstrap_url, wait_until="domcontentloaded")
        await self._human_handoff_if_needed(page, checkpoint="回放启动检查", enabled=human_handoff_on_auth)

        for step_index, step in enumerate(blueprint.steps[start_index:], start=start_index):
            self.state_machine.mark_step_started(run_state.run_id, step_index, step.step_id)
            max_attempts = 2 if disable_fallback_recovery else 4
            for attempt in range(max_attempts):
                try:
                    dom_snapshot = await DomParser.get_interactive_elements(page)
                    self._update_element_monitor(
                        run_key=run_state.run_id,
                        step_label=f"{step.step_id}@attempt_{attempt + 1}",
                        dom_snapshot=dom_snapshot,
                        current_url=page.url,
                    )
                    await self._update_live_element_overlay(
                        page=page,
                        run_key=run_state.run_id,
                        step_label=f"{step.step_id}@attempt_{attempt + 1}",
                        dom_snapshot=dom_snapshot,
                        current_url=page.url,
                    )
                    await self._execute_deterministic_step(page, step)
                    self.state_machine.mark_step_success(run_state.run_id, step_index, step.step_id)
                    break
                except Exception as e:
                    error_msg = str(e).lower()
                    handled = await self._human_handoff_if_needed(
                        page,
                        checkpoint=f"回放步骤失败人工检查(step_id={step.step_id})",
                        enabled=human_handoff_on_auth,
                    )
                    if handled and attempt < max_attempts - 1:
                        self.state_machine.add_event(
                            run_state.run_id,
                            event="recovery_human_handoff",
                            detail=f"step={step.step_id} resumed_after_human=True",
                            step_index=step_index,
                            step_id=step.step_id,
                        )
                        await page.wait_for_timeout(800)
                        continue

                    if getattr(step, "is_optional", False) and (
                        "timeout" in error_msg or "not visible" in error_msg
                    ):
                        logger.info(f"⏭️ [可选步骤] 元素未出现，安全跳过: {step.step_id}")
                        self.state_machine.mark_step_skipped(
                            run_state.run_id,
                            step_index,
                            step.step_id,
                            "optional step skipped due to timeout or invisibility",
                        )
                        break

                    if disable_fallback_recovery and replay_repair_agent and attempt < max_attempts:
                        logger.info("🧠 [回放阶段] AI_ONLY 模式：任意异常直接进入 AI 修复...")
                        recent_steps = build_recent_steps_text(
                            blueprint=blueprint,
                            current_index=step_index,
                            window=repair_context_window,
                        )
                        heal_result = await self._run_ai_heal_agent(
                            page=page,
                            ai_heal_agent=replay_repair_agent,
                            task_name=blueprint.task_name,
                            recent_steps=recent_steps,
                            failed_step_desc=step.description,
                            failed_action_type=step.action.action_type,
                            failed_target=step.action.target,
                            failed_input_value=step.action.input_value,
                            last_error=str(e),
                            step_id=step.step_id,
                            step_index=step_index,
                            run_id=run_state.run_id,
                            validate_coro=lambda: self._execute_deterministic_step(
                                page, step, fast_mode=True
                            ),
                            human_handoff_on_auth=human_handoff_on_auth,
                            max_attempts=3,
                            heal_log=heal_log,
                            expected_url_contains=getattr(step, "guard_url_contains", None)
                            or step.pre_check.expected_url_contains
                            or "",
                        )
                        if heal_result.get("resolved"):
                            outcome = heal_result.get("outcome")
                            if outcome == "skipped":
                                reason = heal_result.get("detail") or "AI 建议跳过该步骤"
                                self.state_machine.mark_step_skipped(
                                    run_state.run_id, step_index, step.step_id, reason
                                )
                            else:
                                self.state_machine.mark_step_success(
                                    run_state.run_id, step_index, step.step_id
                                )
                            break
                        self._append_heal_record(
                            heal_log,
                            phase="ai_summary",
                            step_id=step.step_id,
                            step_index=step_index,
                            result=heal_result.get("outcome", "failed"),
                            detail=heal_result.get("detail", ""),
                        )
                        continue

                    if (
                        "timeout" in error_msg
                        or "intercepted" in error_msg
                        or "not visible" in error_msg
                        or "dom matching failed" in error_msg
                    ):
                        logger.warning(f"🛑 步骤 [{step.step_id}] 受阻。原因: 元素不可操作或超时。")

                        if attempt < max_attempts - 1:
                            ai_attempt_index = 0 if disable_fallback_recovery else 2
                            if (not disable_fallback_recovery) and attempt == 0:
                                logger.info("↩️ [回放阶段] 尝试同URL上下文修复(reload/back)...")
                                healed = await self._attempt_wrong_page_recovery(page, step)
                                self._append_heal_record(
                                    heal_log,
                                    phase="fallback_wrong_page",
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    result="ok" if healed else "failed",
                                    detail=f"attempt={attempt + 1}",
                                )
                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_wrong_page",
                                    detail=f"step={step.step_id} healed={healed}",
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )
                                await page.wait_for_timeout(1200)
                                continue

                            if (not disable_fallback_recovery) and attempt == 1:
                                logger.info("🔁 [回放阶段] 尝试小流程回放修复(前2步)...")
                                fixed = await self._replay_recent_subflow(
                                    page=page,
                                    blueprint=blueprint,
                                    step_index=step_index,
                                    window=subflow_window,
                                    skip_risky=subflow_skip_risky,
                                    risky_keywords=subflow_risky_keywords,
                                )
                                self._append_heal_record(
                                    heal_log,
                                    phase="fallback_subflow",
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    result="ok" if fixed else "failed",
                                    detail=(
                                        f"attempt={attempt + 1} window={subflow_window} "
                                        f"skip_risky={subflow_skip_risky}"
                                    ),
                                )
                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_subflow",
                                    detail=f"step={step.step_id} fixed={fixed} url={page.url}",
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )
                                await page.wait_for_timeout(1200)
                                continue

                            if attempt == ai_attempt_index and replay_repair_agent:
                                logger.info("🧠 [回放阶段] 启动 AI 修复 Agent（最多3次）...")
                                recent_steps = build_recent_steps_text(
                                    blueprint=blueprint,
                                    current_index=step_index,
                                    window=repair_context_window,
                                )
                                heal_result = await self._run_ai_heal_agent(
                                    page=page,
                                    ai_heal_agent=replay_repair_agent,
                                    task_name=blueprint.task_name,
                                    recent_steps=recent_steps,
                                    failed_step_desc=step.description,
                                    failed_action_type=step.action.action_type,
                                    failed_target=step.action.target,
                                    failed_input_value=step.action.input_value,
                                    last_error=str(e),
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    run_id=run_state.run_id,
                                    validate_coro=lambda: self._execute_deterministic_step(
                                        page, step, fast_mode=True
                                    ),
                                    human_handoff_on_auth=human_handoff_on_auth,
                                    max_attempts=3,
                                    heal_log=heal_log,
                                    expected_url_contains=getattr(step, "guard_url_contains", None)
                                    or step.pre_check.expected_url_contains
                                    or "",
                                )
                                if heal_result.get("resolved"):
                                    outcome = heal_result.get("outcome")
                                    if outcome == "skipped":
                                        reason = heal_result.get("detail") or "AI 建议跳过该步骤"
                                        logger.warning(f"⏭️ [AI修复] 跳过步骤: {step.step_id} | {reason}")
                                        self.state_machine.mark_step_skipped(
                                            run_state.run_id, step_index, step.step_id, reason
                                        )
                                    else:
                                        logger.info(f"✅ [AI修复] 已恢复步骤: {step.step_id}")
                                        self.state_machine.mark_step_success(
                                            run_state.run_id, step_index, step.step_id
                                        )
                                    break

                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_ai_failed",
                                    detail=(
                                        f"step={step.step_id} outcome={heal_result.get('outcome')} "
                                        f"detail={heal_result.get('detail')}"
                                    ),
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )
                                self._append_heal_record(
                                    heal_log,
                                    phase="ai_summary",
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    result=heal_result.get("outcome", "failed"),
                                    detail=heal_result.get("detail", ""),
                                )
                                continue

                            if attempt == ai_attempt_index and not replay_repair_agent:
                                logger.warning("⚠️ 未配置 AI 修复 Agent，无法继续自动修复。")
                                self._append_heal_record(
                                    heal_log,
                                    phase="ai_summary",
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    result="no_agent",
                                    detail="replay_repair_agent is None",
                                )
                                continue
                        else:
                            logger.error("❌ 已达到最大重试次数，主线动作依然失败。")
                            screenshot_path = await self._capture_suspend_snapshot(
                                page, run_state.run_id, step.step_id
                            )
                            detail = str(e)
                            if screenshot_path:
                                detail = f"{detail}\n[screenshot]: {screenshot_path}"
                            if suspend_on_failure:
                                self.state_machine.mark_run_suspended(
                                    run_state.run_id, step_index, step.step_id, detail
                                )
                                heal_log["status"] = "suspended"
                            else:
                                self.state_machine.mark_run_failed(
                                    run_state.run_id, step_index, step.step_id, detail
                                )
                                heal_log["status"] = "failed"
                            heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
                            self._finalize_element_monitor(run_state.run_id)
                            self._save_heal_log(heal_log)
                            self._save_run_report(run_state.run_id)
                            self._runtime_tool_agent = None
                            self._runtime_tool_goal = ""
                            self._runtime_tool_history = ""
                            raise
                    else:
                        screenshot_path = await self._capture_suspend_snapshot(
                            page, run_state.run_id, step.step_id
                        )
                        detail = str(e)
                        if screenshot_path:
                            detail = f"{detail}\n[screenshot]: {screenshot_path}"
                        if suspend_on_failure:
                            self.state_machine.mark_run_suspended(
                                run_state.run_id, step_index, step.step_id, detail
                            )
                            heal_log["status"] = "suspended"
                        else:
                            self.state_machine.mark_run_failed(
                                run_state.run_id, step_index, step.step_id, detail
                            )
                            heal_log["status"] = "failed"
                        heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
                        self._finalize_element_monitor(run_state.run_id)
                        self._save_heal_log(heal_log)
                        self._save_run_report(run_state.run_id)
                        self._runtime_tool_agent = None
                        self._runtime_tool_goal = ""
                        self._runtime_tool_history = ""
                        raise

        self.state_machine.mark_run_completed(run_state.run_id)
        heal_log["status"] = "completed"
        heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
        heal_paths = self._save_heal_log(heal_log)
        element_monitor_paths = self._finalize_element_monitor(run_state.run_id)
        report_paths = self._save_run_report(run_state.run_id)
        logger.info("🎉 轨迹回放圆满完成！")
        if report_paths is not None:
            report_paths["heal_log"] = heal_paths
            report_paths["element_monitor"] = element_monitor_paths
        self._runtime_tool_agent = None
        self._runtime_tool_goal = ""
        self._runtime_tool_history = ""
        self.disable_fallback_recovery_runtime = False
        return report_paths
