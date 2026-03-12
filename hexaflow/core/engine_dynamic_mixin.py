import asyncio
import hashlib
import logging
import os

from playwright.async_api import BrowserContext

from hexaflow.core.task_schema import TaskSpec
from hexaflow.core.trace_recorder import TraceRecorder
from hexaflow.tools.dom_parser import DomParser
from hexaflow.tools.helpers import (
    build_goal_with_notes,
    contains_blocked_keyword,
    is_domain_allowed,
)
from hexaflow.tools.tool_runtime import execute_tool


logger = logging.getLogger("HexaEngine")


class EngineDynamicMixin:
    @staticmethod
    def _is_summary_tool_action(next_action) -> bool:
        at = (getattr(next_action, "action_type", "") or "").strip().lower()
        tg = (getattr(next_action, "target", "") or "").strip().lower()
        if at == "summarize":
            return True
        if at == "call_tool" and tg in ("summarize_page", "summarize", ""):
            return True
        return False

    @staticmethod
    def _normalize_action_target_for_memory(target: str) -> str:
        t = (target or "").strip()
        if not t:
            return ""
        if len(t) > 160:
            t = t[:160]
        return t.lower()

    @staticmethod
    def _compute_state_sig_from_snapshot(url: str, dom_snapshot: str, normalizer) -> str:
        normalized = normalizer(dom_snapshot or "")
        return hashlib.sha1(
            f"{url or ''}\n{normalized}".encode("utf-8", errors="ignore")
        ).hexdigest()

    @staticmethod
    def _detect_state_cycle(recent_states: list[str]) -> bool:
        if len(recent_states) < 4:
            return False
        s = recent_states
        # ABAB 循环
        if s[-1] == s[-3] and s[-2] == s[-4] and s[-1] != s[-2]:
            return True
        # 同一状态连续打转
        if len(s) >= 3 and s[-1] == s[-2] == s[-3]:
            return True
        return False

    @staticmethod
    def _format_blocked_action_keys(blocked_keys: set[tuple], limit: int = 8) -> list[str]:
        items = []
        for _state_sig, action_type, target_norm in list(blocked_keys)[-limit:]:
            items.append(f"{action_type}:{target_norm}")
        return items

    @staticmethod
    def _normalize_url_wo_query(url: str) -> str:
        if not url:
            return ""
        u = str(url)
        return u.split("?", 1)[0].split("#", 1)[0]

    @staticmethod
    def _thought_indicates_invalid_target(thought: str) -> bool:
        t = (thought or "").lower()
        negative = [
            "不符合", "不满足", "不是", "不对", "排除", "不在", "不属于", "不匹配", "无效",
            "not match", "doesn't match", "does not match", "not on", "exclude", "invalid",
        ]
        continue_search = [
            "继续", "继续查找", "继续寻找", "下一个", "向下", "返回", "回到",
            "continue", "keep searching", "next", "go back", "return",
        ]
        return any(k in t for k in negative) and any(k in t for k in continue_search)

    @staticmethod
    def _is_leaving_detail_action(action_type: str, target: str, current_url: str, post_url: str) -> bool:
        if "/token/" not in (current_url or "").lower():
            return False
        if "/token/" not in (post_url or "").lower():
            return True
        at = (action_type or "").lower().strip()
        tg = (target or "").lower()
        if at == "navigate":
            return "/token/" not in tg
        if at == "click":
            return any(k in tg for k in ["boost", "行情", "market", "返回", "back", "查看详情"])
        return False

    async def _collect_click_candidates(self, page, selector: str, max_n: int = 8) -> list[dict]:
        out = []
        if not selector:
            return out
        try:
            loc = page.locator(selector)
            count = min(await loc.count(), max_n)
        except Exception:
            return out
        for i in range(count):
            cand = loc.nth(i)
            try:
                if not await cand.is_visible():
                    continue
                box = await cand.bounding_box()
                if not box:
                    continue
                text = ""
                aria = ""
                tag = ""
                try:
                    text = (await cand.inner_text() or "").strip()
                except Exception:
                    text = ""
                try:
                    aria = (await cand.get_attribute("aria-label") or "").strip()
                except Exception:
                    aria = ""
                try:
                    tag = await cand.evaluate("(el) => (el.tagName || '').toLowerCase()")
                except Exception:
                    tag = ""
                vp = page.viewport_size or {}
                width = int(vp.get("width", 0) or 0)
                height = int(vp.get("height", 0) or 0)
                cx = float(box["x"]) + float(box["width"]) / 2.0
                cy = float(box["y"]) + float(box["height"]) / 2.0
                x_ratio = (cx / width) if width > 0 else None
                y_ratio = (cy / height) if height > 0 else None
                out.append(
                    {
                        "index": i,
                        "tag": tag,
                        "text": text[:80],
                        "aria_label": aria[:80],
                        "x": round(cx, 2),
                        "y": round(cy, 2),
                        "x_ratio": round(x_ratio, 6) if x_ratio is not None else None,
                        "y_ratio": round(y_ratio, 6) if y_ratio is not None else None,
                        "width": round(float(box["width"]), 2),
                        "height": round(float(box["height"]), 2),
                    }
                )
            except Exception:
                continue
        return out

    async def _resolve_click_coordinate_with_ai(
        self,
        page,
        selector: str,
        agent,
        goal: str,
        history_str: str,
        screenshot_path: str = "",
    ):
        if not selector or not agent or not hasattr(agent, "decide_click_coordinate"):
            return None
        enabled = os.getenv("ENABLE_AI_COORD_DISAMBIG", "1") == "1"
        if not enabled:
            return None
        candidates = await self._collect_click_candidates(page, selector, max_n=8)
        if len(candidates) <= 1:
            return None
        try:
            decision = await agent.decide_click_coordinate(
                goal=goal,
                history=history_str,
                current_url=page.url,
                selector=selector,
                candidates=candidates,
                screenshot_path=screenshot_path or "",
            )
        except Exception:
            return None

        if bool(getattr(decision, "use_coordinate", False)):
            x = getattr(decision, "x_ratio", None)
            y = getattr(decision, "y_ratio", None)
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                x = max(0.0, min(1.0, float(x)))
                y = max(0.0, min(1.0, float(y)))
                logger.info(
                    "🧭 [CoordDisambig] 使用AI坐标点击: selector=%s x=%.4f y=%.4f conf=%.2f",
                    selector,
                    x,
                    y,
                    float(getattr(decision, "confidence", 0.0) or 0.0),
                )
                return (x, y)

        idx = getattr(decision, "candidate_index", None)
        if isinstance(idx, int):
            cand = next((c for c in candidates if int(c.get("index", -1)) == idx), None)
            if cand and cand.get("x_ratio") is not None and cand.get("y_ratio") is not None:
                x = max(0.0, min(1.0, float(cand["x_ratio"])))
                y = max(0.0, min(1.0, float(cand["y_ratio"])))
                logger.info(
                    "🧭 [CoordDisambig] 使用候选索引点击: selector=%s idx=%d x=%.4f y=%.4f",
                    selector,
                    idx,
                    x,
                    y,
                )
                return (x, y)
        return None

    async def _evaluate_completion(
        self,
        page,
        completion_checks: list[dict],
        completion_logic: str = "any",
        last_action: dict | None = None,
    ):
        if not completion_checks:
            return False, "no_completion_checks"
        results = []
        details = []
        for item in completion_checks:
            ctype = (item.get("check_type") or "").strip()
            value = str(item.get("value") or "").strip()
            if not ctype or not value:
                continue
            ok = False
            try:
                if ctype == "url_contains":
                    ok = value in (page.url or "")
                elif ctype == "text_visible":
                    ok = await page.get_by_text(value, exact=False).first.is_visible(timeout=500)
                elif ctype == "selector_visible":
                    ok = await page.locator(value).first.is_visible(timeout=500)
                elif ctype == "action_target_contains":
                    if last_action and last_action.get("success"):
                        expected_action = (item.get("action_type") or "").strip().lower()
                        actual_action = str(last_action.get("action_type") or "").strip().lower()
                        if expected_action and expected_action != actual_action:
                            ok = False
                        else:
                            target = str(last_action.get("target") or "")
                            thought = str(last_action.get("thought") or "")
                            target_hint = self._extract_text_hint_from_selector(target)
                            haystack = f"{target} {target_hint} {thought}".lower()
                            ok = value.lower() in haystack
            except Exception:
                ok = False
            results.append(ok)
            details.append(f"{ctype}({value})={'ok' if ok else 'miss'}")
        if not results:
            return False, "no_valid_checks"
        if completion_logic == "all":
            return all(results), "; ".join(details)
        return any(results), "; ".join(details)

    def _build_dynamic_memory_lines(
        self,
        blocked_action_keys: set[tuple],
        blocked_target_norms: set[str],
        invalid_targets: dict[str, dict],
        state_cycle_detected: bool,
        summarized_state_count: int = 0,
    ) -> list[str]:
        lines = []
        blocked_brief = self._format_blocked_action_keys(blocked_action_keys, limit=8)
        if blocked_brief:
            lines.append(
                "[CONTEXT_GUARD] Blocked repeated-failure actions (never retry): "
                + " | ".join(blocked_brief)
            )
        if blocked_target_norms:
            lines.append(
                "[CONTEXT_GUARD] Globally blocked repeated targets (never retry): "
                + " | ".join(list(blocked_target_norms)[-10:])
            )
        if invalid_targets:
            lines.append("[注意] 已尝试但无效的目标：")
            for item in list(invalid_targets.values())[-8:]:
                lines.append(
                    f"- 元素 \"{item.get('target_display')}\"：{item.get('reason')}，请勿再次尝试。"
                )
        if state_cycle_detected:
            lines.append(
                "[CONTEXT_GUARD] State-cycle detected (ABAB/AAAA). "
                "Choose a fundamentally different action path; do NOT repeat previous entry buttons."
            )
        if summarized_state_count > 0:
            lines.append(
                f"[CONTEXT_GUARD] Summary already generated for {summarized_state_count} page state(s). "
                "Do NOT summarize same state again; continue next step."
            )
        return lines

    async def _execute_summarize_action(
        self,
        *,
        agent,
        goal: str,
        history_str: str,
        current_url: str,
        dom_snapshot: str,
        step_screenshot: str,
        next_action,
        step_count: int,
        ai_action_records: list[dict],
        pre_state_signature: str,
        page,
    ):
        tool_instruction = (
            (next_action.input_value or "").strip()
            or (next_action.thought or "").strip()
            or (goal or "").strip()
        )
        if not (next_action.input_value or "").strip():
            # 固化工具调用指令，确保后续 trace/replay 可复现相同总结语义。
            next_action.input_value = tool_instruction

        tool_name = (
            (next_action.target or "").strip()
            if (next_action.action_type or "").strip().lower() == "call_tool"
            else "summarize_page"
        )
        if not tool_name:
            tool_name = "summarize_page"
        summary_text = await execute_tool(
            tool_name=tool_name,
            agent=agent,
            goal=goal,
            history=history_str,
            current_url=current_url,
            dom_snapshot=dom_snapshot,
            screenshot_path=step_screenshot,
            instruction=tool_instruction,
        )
        logger.info("📝 [Summary]\n%s", summary_text[:1200])

        stable_target = tool_name
        current_action_log = f"Executed tool {tool_name}"
        ai_action_records.append(
            {
                "step": step_count,
                "action_type": "call_tool",
                "target": stable_target,
                "input_value": next_action.input_value,
                "thought": next_action.thought,
                "success": True,
                "summary": summary_text,
                "note": "call_tool",
            }
        )
        return (
            stable_target,
            current_action_log,
            True,
            {
                "pre_url": current_url,
                "post_url": page.url,
                "changed": True,
                "pre_state_signature": pre_state_signature,
            },
        )

    @staticmethod
    def _record_dynamic_step_if_needed(
        recorder,
        *,
        current_url: str,
        next_action,
        stable_target: str,
        is_optional: bool = False,
        fingerprint_dict: dict | None = None,
    ):
        if next_action.action_type == "summarize":
            return
        recorder.record_step(
            current_url,
            next_action.action_type,
            stable_target,
            next_action.input_value,
            next_action.thought,
            is_optional=is_optional,
            fingerprint_dict=fingerprint_dict,
        )

    async def _execute_dynamic_action(
        self,
        page,
        next_action,
        allowed_domains: list[str],
        fast_validate: bool = False,
        click_ratio: tuple[float, float] | None = None,
    ):
        pre_action_sig = ""
        pre_checkbox_state = None
        pre_page_count = 0
        pre_inside_dialog = False

        if next_action.action_type in ("click", "press_enter"):
            pre_action_sig = await self._compute_page_state_signature(page)
            try:
                pre_page_count = len(page.context.pages)
            except Exception:
                pre_page_count = 0

        raw_target = self._canonicalize_runtime_target(next_action.target or "")
        if raw_target and "hexa-id" in raw_target:
            stable = await DomParser.get_stable_selector(page, raw_target)
        else:
            stable = raw_target

        try:
            if next_action.action_type == "click" and stable:
                pre_inside_dialog = await self._is_target_text_inside_dialog(page, stable)
        except Exception:
            pre_inside_dialog = False

        if next_action.action_type == "navigate":
            if not is_domain_allowed(stable, allowed_domains):
                raise Exception(f"Navigation blocked by allowed_domains policy: {stable}")
            await page.goto(stable)
        elif next_action.action_type == "refresh":
            await page.reload(wait_until="domcontentloaded", timeout=15000)
        elif next_action.action_type == "click":
            if not stable:
                raise Exception("click 动作缺少 target 选择器")
            try:
                loc = page.locator(stable).first
                pre_checkbox_state = await loc.evaluate(
                    """(el) => {
                        const toState = (n) => {
                            if (!n) return null;
                            const aria = n.getAttribute && n.getAttribute('aria-checked');
                            if (aria !== null && aria !== undefined && aria !== '') return String(aria);
                            if (typeof n.checked === 'boolean') return String(!!n.checked);
                            return null;
                        };
                        let n = null;
                        if (el.matches && el.matches('[role="checkbox"],input[type="checkbox"]')) {
                            n = el;
                        } else {
                            n = el.closest('[role="checkbox"],label') ||
                                (el.querySelector && el.querySelector('[role="checkbox"],input[type="checkbox"]')) ||
                                null;
                        }
                        if (n && n.matches && n.matches('label')) {
                            const inner = n.querySelector('[role="checkbox"],input[type="checkbox"]');
                            if (inner) n = inner;
                        }
                        return toState(n);
                    }"""
                )
            except Exception:
                pre_checkbox_state = None
            if click_ratio is not None:
                x_ratio, y_ratio = click_ratio
                await self._execute_override_action(
                    page=page,
                    action_type="click_relative",
                    target=None,
                    input_value=f"{x_ratio:.6f},{y_ratio:.6f}",
                )
            else:
                await self._robust_click(page, selector=stable)
        elif next_action.action_type == "type":
            if not stable:
                raise Exception("type 动作缺少 target 选择器")
            locator, stable = await self._resolve_input_locator(page, stable)
            await self._robust_input_text(
                page, locator, next_action.input_value or "", press_enter=False
            )
        elif next_action.action_type == "click_type_enter":
            if not stable:
                raise Exception("click_type_enter 动作缺少 target 选择器")
            locator = page.locator(stable).first
            await locator.hover(timeout=5000)
            await locator.click(timeout=5000)
            is_editable = False
            try:
                is_editable = bool(
                    await locator.evaluate(
                        """(el) => {
                            const tag = (el.tagName || '').toLowerCase();
                            const role = (el.getAttribute('role') || '').toLowerCase();
                            return tag === 'input' || tag === 'textarea' || tag === 'select' ||
                                   el.isContentEditable || role === 'textbox' || role === 'searchbox';
                        }"""
                    )
                )
            except Exception:
                is_editable = False
            if is_editable:
                await self._robust_input_text(
                    page, locator, next_action.input_value or "", press_enter=True
                )
            else:
                input_loc, stable = await self._resolve_input_locator(page, stable)
                await self._robust_input_text(
                    page, input_loc, next_action.input_value or "", press_enter=True
                )
        elif next_action.action_type == "press_enter":
            if stable:
                locator = page.locator(stable).first
                await locator.click(timeout=5000)
                await locator.press("Enter", timeout=5000)
            else:
                await page.keyboard.press("Enter")
        elif next_action.action_type == "summarize":
            # Non-operational action, keep page untouched.
            return stable or "__summary__"
        elif next_action.action_type == "call_tool":
            # Tool execution is orchestrated in outer loop where full context is available.
            return stable or "__call_tool__"
        else:
            raise Exception(f"Unsupported dynamic action: {next_action.action_type}")

        if not fast_validate:
            await page.wait_for_timeout(1500)
        else:
            await page.wait_for_timeout(250)

        if pre_action_sig and next_action.action_type in ("click", "press_enter"):
            if next_action.action_type == "click" and pre_checkbox_state is not None and stable:
                try:
                    loc = page.locator(stable).first
                    post_checkbox_state = await loc.evaluate(
                        """(el) => {
                            const toState = (n) => {
                                if (!n) return null;
                                const aria = n.getAttribute && n.getAttribute('aria-checked');
                                if (aria !== null && aria !== undefined && aria !== '') return String(aria);
                                if (typeof n.checked === 'boolean') return String(!!n.checked);
                                return null;
                            };
                            let n = null;
                            if (el.matches && el.matches('[role="checkbox"],input[type="checkbox"]')) {
                                n = el;
                            } else {
                                n = el.closest('[role="checkbox"],label') ||
                                    (el.querySelector && el.querySelector('[role="checkbox"],input[type="checkbox"]')) ||
                                    null;
                            }
                            if (n && n.matches && n.matches('label')) {
                                const inner = n.querySelector('[role="checkbox"],input[type="checkbox"]');
                                if (inner) n = inner;
                            }
                            return toState(n);
                        }"""
                    )
                    if (
                        post_checkbox_state is not None
                        and post_checkbox_state != pre_checkbox_state
                    ):
                        return stable
                except Exception:
                    pass
            post_action_sig = await self._compute_page_state_signature(page)
            if pre_action_sig == post_action_sig:
                try:
                    post_page_count = len(page.context.pages)
                    if post_page_count > pre_page_count:
                        return stable
                except Exception:
                    pass

                if next_action.action_type == "click" and stable:
                    try:
                        if pre_inside_dialog:
                            still_in_dialog = await self._is_target_text_inside_dialog(page, stable)
                            if not still_in_dialog:
                                return stable
                        loc = page.locator(stable).first
                        if not await loc.is_visible(timeout=300):
                            return stable
                    except Exception:
                        pass
                raise Exception(
                    f"No observable state change after {next_action.action_type} on {stable}"
                )
        return stable

    async def run_task_from_spec(
        self,
        spec_input,
        agent,
        context: BrowserContext = None,
        replay_repair_agent=None,
        disable_fallback_recovery: bool = False,
        ai_decision_use_vision: bool = True,
    ):
        if isinstance(spec_input, TaskSpec):
            spec = spec_input
        elif isinstance(spec_input, str):
            spec = TaskSpec.from_json_file(spec_input)
        elif isinstance(spec_input, dict):
            spec = TaskSpec.model_validate(spec_input)
        else:
            raise ValueError("Unsupported task spec input type")

        logger.info(f"📘 载入任务DSL: {spec.task_name}")
        return await self.run_dynamic_task(
            goal=build_goal_with_notes(spec),
            agent=agent,
            context=context,
            max_steps=spec.max_steps,
            manual_review=spec.manual_review,
            start_url=spec.start_url,
            allowed_domains=spec.allowed_domains,
            blocked_keywords=spec.blocked_keywords,
            max_consecutive_failures=spec.failure_policy.max_consecutive_failures,
            failure_mode=spec.failure_policy.mode,
            replay_repair_agent=replay_repair_agent,
            disable_fallback_recovery=disable_fallback_recovery,
            ai_decision_use_vision=ai_decision_use_vision,
            completion_checks=[c.model_dump() for c in spec.completion_checks],
            completion_logic=spec.completion_logic,
            allow_repeat_summarize=spec.allow_repeat_summarize,
        )

    async def run_dynamic_task(
        self,
        goal: str,
        agent,
        context: BrowserContext = None,
        max_steps: int = 15,
        manual_review: bool = True,
        start_url: str = "about:blank",
        allowed_domains: list[str] = None,
        blocked_keywords: list[str] = None,
        max_consecutive_failures: int = 999999,
        failure_mode: str = "continue",
        human_handoff_on_auth: bool = True,
        replay_repair_agent=None,
        repair_context_window: int = 5,
        disable_fallback_recovery: bool = False,
        ai_decision_use_vision: bool = True,
        completion_checks: list[dict] = None,
        completion_logic: str = "any",
        allow_repeat_summarize: bool = False,
    ):
        from datetime import datetime

        logger.info(f"▶️ 开始执行动态自适应任务: {goal} (manual_review={manual_review})")
        self.disable_fallback_recovery_runtime = disable_fallback_recovery
        try:
            self._cleanup_screenshot_cache("workspace/screenshots")
        except Exception:
            pass

        allowed_domains = allowed_domains or []
        blocked_keywords = blocked_keywords or []
        completion_checks = completion_checks or []
        block_threshold = max(1, int(os.getenv("CONTEXT_GUARD_BLOCK_THRESHOLD", "2")))
        repeat_target_threshold = max(
            2, int(os.getenv("CONTEXT_GUARD_REPEAT_TARGET_THRESHOLD", "3"))
        )

        task_name = "Task_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        self._init_element_monitor(run_key=task_name, task_name=task_name, mode="dynamic")
        recorder = TraceRecorder(task_name=task_name)
        ai_heal_agent = replay_repair_agent or (
            agent if hasattr(agent, "repair_failed_replay_step") else None
        )

        if not context:
            context_options = {"viewport": {"width": 1280, "height": 800}}
            context = await self._resolve_context(context=context, context_options=context_options)

        page = await context.new_page()
        await page.goto(start_url)
        await self._human_handoff_if_needed(
            page, checkpoint="动态任务启动检查", enabled=human_handoff_on_auth
        )
        if start_url != "about:blank":
            recorder.record_step(
                current_url=start_url,
                action_type="navigate",
                target=start_url,
                description=f"访问初始网页: {start_url}",
            )

        action_history = []
        ai_action_records = []
        step_count = 0
        consecutive_failures = 0
        stagnant_action_counts = {}
        failed_action_counts = {}
        blocked_action_keys = set()
        blocked_target_norms = set()
        recent_click_targets = []
        invalid_targets: dict[str, dict] = {}
        last_detail_entry = {"target_norm": "", "target_display": "", "detail_url": ""}
        pending_invalid_reason = ""
        recent_state_sigs = []
        loop_pressure = 0
        summarized_state_sigs = set()
        trace_saved = False
        trace_path = None

        while step_count < max_steps:
            step_count += 1
            logger.info(f"\n" + "=" * 40 + f"\n--- 第 {step_count} 步 ---")

            current_url = page.url
            await page.wait_for_timeout(1000)
            await self._wait_for_page_settle(page)
            dom_snapshot = await DomParser.get_interactive_elements(page)
            self._update_element_monitor(
                run_key=task_name,
                step_label=f"step_{step_count}",
                dom_snapshot=dom_snapshot,
                current_url=current_url,
            )
            await self._update_live_element_overlay(
                page=page,
                run_key=task_name,
                step_label=f"step_{step_count}",
                dom_snapshot=dom_snapshot,
                current_url=current_url,
            )
            step_screenshot = ""
            if ai_decision_use_vision:
                step_screenshot = await self._capture_suspend_snapshot(
                    page, task_name, f"ai_step_{step_count}"
                )

            current_state_sig = self._compute_state_sig_from_snapshot(
                current_url, dom_snapshot, self._normalize_snapshot_for_stability
            )
            recent_state_sigs.append(current_state_sig)
            if len(recent_state_sigs) > 12:
                recent_state_sigs = recent_state_sigs[-12:]
            state_cycle_detected = self._detect_state_cycle(recent_state_sigs)
            if state_cycle_detected:
                loop_pressure += 1
            else:
                loop_pressure = max(0, loop_pressure - 1)

            memory_lines = self._build_dynamic_memory_lines(
                blocked_action_keys=blocked_action_keys,
                blocked_target_norms=blocked_target_norms,
                invalid_targets=invalid_targets,
                state_cycle_detected=state_cycle_detected,
                summarized_state_count=len(summarized_state_sigs),
            )
            history_str = "\n".join(action_history + memory_lines)
            try:
                next_action = await agent.decide_next_action(
                    goal, history_str, current_url, dom_snapshot, screenshot_path=step_screenshot
                )
            except TypeError:
                next_action = await agent.decide_next_action(goal, history_str, current_url, dom_snapshot)

            stable_target = next_action.target
            current_action_log = f"Skipped unknown action: {next_action.action_type}"
            action_success = False
            no_progress_detected = False
            progress_context = None

            if next_action.action_type == "done":
                logger.info("🎉 AI 认为任务已完成！")
                trace_path = recorder.save_to_disk()
                trace_saved = True
                break

            action_raw_text = f"{next_action.action_type} {next_action.target or ''} {next_action.thought or ''}"
            if contains_blocked_keyword(action_raw_text, blocked_keywords):
                current_action_log = f"BLOCKED by keyword policy: {next_action.action_type} on {next_action.target}"
                logger.warning(f"🛡️ 动作已拦截: {current_action_log}")
                action_success = False
            else:
                logger.info(f"⚡ 自动执行: [{next_action.action_type}] 目标: {next_action.target}")
                pre_state_signature = await self._compute_page_state_signature(page)
                if (
                    self._is_summary_tool_action(next_action)
                    and not allow_repeat_summarize
                    and current_state_sig in summarized_state_sigs
                ):
                    logger.warning("⚠️ 重复 summarize（同一页面状态）已跳过，要求 AI 继续下一步。")
                    stable_target = "__summary_skip__"
                    current_action_log = "Skipped duplicate summarize on same page state"
                    ai_action_records.append(
                        {
                            "step": step_count,
                            "action_type": next_action.action_type,
                            "target": stable_target,
                            "input_value": next_action.input_value,
                            "thought": next_action.thought,
                            "success": True,
                            "note": "skipped_duplicate_summarize_same_state",
                        }
                    )
                    action_success = True
                    progress_context = {
                        "pre_url": current_url,
                        "post_url": page.url,
                        "changed": False,
                    }
                if self._is_summary_tool_action(next_action):
                    if not (action_success and stable_target == "__summary_skip__"):
                        (
                            stable_target,
                            current_action_log,
                            action_success,
                            progress_context,
                        ) = await self._execute_summarize_action(
                            agent=agent,
                            goal=goal,
                            history_str=history_str,
                            current_url=current_url,
                            dom_snapshot=dom_snapshot,
                            step_screenshot=step_screenshot,
                            next_action=next_action,
                            step_count=step_count,
                            ai_action_records=ai_action_records,
                            pre_state_signature=pre_state_signature,
                            page=page,
                        )
                        if action_success:
                            summarized_state_sigs.add(current_state_sig)
                    action_key = None
                    planned_target = next_action.target or ""
                    global_target_key = ""
                    no_progress_detected = False
                elif (next_action.action_type or "").strip().lower() == "call_tool":
                    (
                        stable_target,
                        current_action_log,
                        action_success,
                        progress_context,
                    ) = await self._execute_summarize_action(
                        agent=agent,
                        goal=goal,
                        history_str=history_str,
                        current_url=current_url,
                        dom_snapshot=dom_snapshot,
                        step_screenshot=step_screenshot,
                        next_action=next_action,
                        step_count=step_count,
                        ai_action_records=ai_action_records,
                        pre_state_signature=pre_state_signature,
                        page=page,
                    )
                    if action_success and not allow_repeat_summarize:
                        summarized_state_sigs.add(current_state_sig)
                    action_key = None
                    planned_target = next_action.target or ""
                    global_target_key = ""
                    no_progress_detected = False
                else:
                    planned_target = self._canonicalize_runtime_target(next_action.target or "")
                    if planned_target and "hexa-id" in planned_target:
                        try:
                            planned_target = await DomParser.get_stable_selector(page, planned_target)
                        except Exception:
                            pass
                    chosen_click_ratio = None
                    if next_action.action_type == "click" and planned_target:
                        chosen_click_ratio = await self._resolve_click_coordinate_with_ai(
                            page=page,
                            selector=planned_target,
                            agent=agent,
                            goal=goal,
                            history_str=history_str,
                            screenshot_path=step_screenshot,
                        )

                    action_key = (
                        pre_state_signature,
                        (next_action.action_type or "").strip().lower(),
                        self._normalize_action_target_for_memory(planned_target or next_action.target or ""),
                    )
                    global_target_key = self._normalize_action_target_for_memory(
                        planned_target or next_action.target or ""
                    )
                    if (
                        next_action.action_type == "click"
                        and global_target_key
                        and global_target_key in invalid_targets
                    ):
                        reason = invalid_targets.get(global_target_key, {}).get("reason", "该目标已判定无效")
                        current_action_log = f"BLOCKED_INVALID_TARGET {global_target_key}"
                        logger.warning(f"🧠 [ContextGuard] 拦截已判定无效目标: {current_action_log} | {reason}")
                        action_success = False
                        no_progress_detected = True
                        ai_action_records.append(
                            {
                                "step": step_count,
                                "action_type": next_action.action_type,
                                "target": planned_target or next_action.target,
                                "input_value": next_action.input_value,
                                "thought": next_action.thought,
                                "success": False,
                                "note": f"blocked_invalid_target:{reason}",
                            }
                        )
                        action_history.append(
                            current_action_log
                            + f" -> [FAILED] - Invalid target memory: {reason}"
                        )
                        consecutive_failures += 1
                        continue
                    if action_key in blocked_action_keys:
                        current_action_log = (
                            f"BLOCKED_REPEATED_FAILURE {action_key[1]} -> {action_key[2]}"
                        )
                        logger.warning(f"🧠 [ContextGuard] 拦截重复失败动作: {current_action_log}")
                        action_success = False
                        no_progress_detected = True
                        ai_action_records.append(
                            {
                                "step": step_count,
                                "action_type": next_action.action_type,
                                "target": planned_target or next_action.target,
                                "input_value": next_action.input_value,
                                "thought": next_action.thought,
                                "success": False,
                                "note": "blocked_repeated_failure",
                            }
                        )
                        action_history.append(
                            current_action_log
                            + " -> [FAILED] - This action already failed repeatedly in the same state. Choose another path."
                        )
                        consecutive_failures += 1
                        continue
                    if (
                        next_action.action_type == "click"
                        and global_target_key
                        and global_target_key in blocked_target_norms
                    ):
                        current_action_log = f"BLOCKED_GLOBAL_REPEATED_TARGET {global_target_key}"
                        logger.warning(f"🧠 [ContextGuard] 拦截全局重复目标: {current_action_log}")
                        action_success = False
                        no_progress_detected = True
                        ai_action_records.append(
                            {
                                "step": step_count,
                                "action_type": next_action.action_type,
                                "target": planned_target or next_action.target,
                                "input_value": next_action.input_value,
                                "thought": next_action.thought,
                                "success": False,
                                "note": "blocked_global_repeated_target",
                            }
                        )
                        action_history.append(
                            current_action_log
                            + " -> [FAILED] - This target repeated too many times without completion. Pick another target."
                        )
                        consecutive_failures += 1
                        continue

                    max_attempts = 3 if disable_fallback_recovery else 4
                    for attempt in range(max_attempts):
                        try:
                            stable_target = await self._execute_dynamic_action(
                                page=page,
                                next_action=next_action,
                                allowed_domains=allowed_domains,
                                fast_validate=False,
                                click_ratio=chosen_click_ratio,
                            )
                            current_action_log = f"Executed {next_action.action_type} on {stable_target}"
                            ai_action_records.append(
                                {
                                    "step": step_count,
                                    "action_type": next_action.action_type,
                                    "target": stable_target,
                                    "input_value": next_action.input_value,
                                    "thought": next_action.thought,
                                    "success": True,
                                    "note": "executed",
                                }
                            )
                            action_success = True
                            break
                        except Exception as e:
                            error_msg = str(e).lower()
                            handled = await self._human_handoff_if_needed(
                                page,
                                checkpoint=f"动态步骤失败前人工检查(step={step_count})",
                                enabled=human_handoff_on_auth,
                            )
                            if handled:
                                await page.wait_for_timeout(800)
                                continue

                            blocked_like = (
                                "timeout" in error_msg
                                or "intercepted" in error_msg
                                or "not visible" in error_msg
                                or "no observable state change" in error_msg
                                or "no_progress" in error_msg
                                or "robust click failed" in error_msg
                            )
                            if not blocked_like and next_action.action_type in ("click", "click_type_enter"):
                                blocked_like = True
                            if blocked_like:
                                logger.warning("🛑 动作受阻 (timeout/intercepted/not visible/no_progress)。")
                                recovery_result = await self._route_dynamic_blocked_recovery(
                                    page=page,
                                    attempt=attempt,
                                    max_attempts=max_attempts,
                                    disable_fallback_recovery=disable_fallback_recovery,
                                    ai_heal_agent=ai_heal_agent,
                                    human_handoff_on_auth=human_handoff_on_auth,
                                    repair_context_window=repair_context_window,
                                    action_history=action_history,
                                    task_name=task_name,
                                    next_action=next_action,
                                    stable_target=stable_target or next_action.target,
                                    failed_input_value=next_action.input_value,
                                    error=e,
                                    step_count=step_count,
                                    allowed_domains=allowed_domains,
                                    chosen_click_ratio=chosen_click_ratio,
                                    current_url=current_url,
                                    ai_action_records=ai_action_records,
                                )
                                if recovery_result.get("outcome") == "continue":
                                    continue
                                if recovery_result.get("outcome") == "resolved":
                                    current_action_log = recovery_result.get(
                                        "current_action_log",
                                        f"AI healed {next_action.action_type}",
                                    )
                                    action_success = True
                                    break

                            logger.error(f"❌ 动作执行失败: {e}")
                            failed_action_counts[action_key] = failed_action_counts.get(action_key, 0) + 1
                            if failed_action_counts[action_key] >= block_threshold:
                                blocked_action_keys.add(action_key)
                            ai_action_records.append(
                                {
                                    "step": step_count,
                                    "action_type": next_action.action_type,
                                    "target": stable_target or next_action.target,
                                    "input_value": next_action.input_value,
                                    "thought": next_action.thought,
                                    "success": False,
                                    "note": str(e),
                                }
                            )
                            current_action_log = f"FAILED to execute {next_action.action_type} on {stable_target}"
                            break

            if action_success and (not self._is_summary_tool_action(next_action)):
                post_url = page.url
                post_state_signature = await self._compute_page_state_signature(page)
                progress_context = {
                    "pre_url": current_url,
                    "post_url": post_url,
                    "changed": pre_state_signature != post_state_signature,
                }
                stagnation_key = f"{next_action.action_type}|{stable_target}|{current_url}"
                if pre_state_signature == post_state_signature:
                    stagnant_action_counts[stagnation_key] = stagnant_action_counts.get(stagnation_key, 0) + 1
                else:
                    stagnant_action_counts.pop(stagnation_key, None)
                # 同状态同动作成功后，清空失败计数，允许后续再次使用该策略
                failed_action_counts.pop(action_key, None)
                if action_key in blocked_action_keys:
                    blocked_action_keys.discard(action_key)
                if (
                    next_action.action_type == "click"
                    and global_target_key
                    and global_target_key in blocked_target_norms
                ):
                    blocked_target_norms.discard(global_target_key)

                if next_action.action_type == "click":
                    target_norm = self._normalize_action_target_for_memory(
                        stable_target or planned_target or next_action.target or ""
                    )
                    if target_norm:
                        recent_click_targets.append(target_norm)
                        if len(recent_click_targets) > 30:
                            recent_click_targets = recent_click_targets[-30:]
                        window = recent_click_targets[-12:]
                        repeat_count = sum(1 for x in window if x == target_norm)
                        if repeat_count >= repeat_target_threshold:
                            blocked_target_norms.add(target_norm)
                            logger.warning(
                                "🧠 [ContextGuard] 目标近期重复过多，加入全局拦截: %s (repeat=%d/%d)",
                                target_norm,
                                repeat_count,
                                repeat_target_threshold,
                            )
                            action_history.append(
                                f"[CONTEXT_GUARD] blocked repeated target: {target_norm} repeat={repeat_count}"
                            )

                # 记录“进入详情页”的入口目标，用于后续不符合条件时写入无效目标记忆
                if (
                    next_action.action_type == "click"
                    and "/token/" in (post_url or "").lower()
                    and "/token/" not in (current_url or "").lower()
                ):
                    tn = self._normalize_action_target_for_memory(
                        stable_target or planned_target or next_action.target or ""
                    )
                    if tn:
                        last_detail_entry = {
                            "target_norm": tn,
                            "target_display": stable_target or planned_target or next_action.target or tn,
                            "detail_url": self._normalize_url_wo_query(post_url),
                        }

                # 若在详情页思考已明确“不符合并返回”，且本步确实离开详情页，则记为无效目标
                if (
                    pending_invalid_reason
                    and last_detail_entry.get("target_norm")
                    and self._is_leaving_detail_action(
                        next_action.action_type,
                        stable_target or planned_target or next_action.target or "",
                        current_url,
                        post_url,
                    )
                ):
                    tn = last_detail_entry["target_norm"]
                    invalid_targets[tn] = {
                        "target_display": last_detail_entry.get("target_display") or tn,
                        "reason": pending_invalid_reason,
                    }
                    blocked_target_norms.add(tn)
                    logger.info(
                        "🧠 [ContextGuard] 已登记无效目标: %s | reason=%s",
                        invalid_targets[tn]["target_display"],
                        pending_invalid_reason,
                    )
                    pending_invalid_reason = ""

                if stagnant_action_counts.get(stagnation_key, 0) >= 1:
                    no_progress_detected = True
                    action_success = False
                    failed_action_counts[action_key] = failed_action_counts.get(action_key, 0) + 1
                    if failed_action_counts[action_key] >= block_threshold:
                        blocked_action_keys.add(action_key)
                    current_action_log = (
                        f"NO_PROGRESS after {next_action.action_type} on {stable_target} "
                        f"(state unchanged, current_url={current_url})"
                    )
                    logger.warning(f"⚠️ 无状态变化，判定为未推进: {current_action_log}")
                    ai_action_records.append(
                        {
                            "step": step_count,
                            "action_type": next_action.action_type,
                            "target": stable_target,
                            "input_value": next_action.input_value,
                            "thought": next_action.thought,
                            "success": False,
                            "note": "no_progress_same_state",
                        }
                    )

            if action_success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            # 在详情页里，若 thought 明确“当前对象不符合条件，准备继续找/返回”，先缓存原因
            if (
                "/token/" in (current_url or "").lower()
                and self._thought_indicates_invalid_target(next_action.thought or "")
                and last_detail_entry.get("detail_url") == self._normalize_url_wo_query(current_url)
            ):
                pending_invalid_reason = (next_action.thought or "").strip()[:220]

            last_action_ctx = {
                "action_type": next_action.action_type,
                "target": stable_target or next_action.target,
                "thought": next_action.thought,
                "success": action_success,
            }
            completed, completion_detail = await self._evaluate_completion(
                page=page,
                completion_checks=completion_checks,
                completion_logic=completion_logic,
                last_action=last_action_ctx,
            )
            if completed:
                logger.info(f"✅ 命中完成条件，任务结束: {completion_detail}")
                trace_path = recorder.save_to_disk()
                trace_saved = True
                break

            if not manual_review:
                if action_success:
                    fp_dict = await DomParser.get_element_fingerprint(page, stable_target)
                    result_url = progress_context["post_url"] if progress_context else page.url
                    if self._is_summary_tool_action(next_action):
                        action_history.append(
                            current_action_log
                            + f" -> [SUMMARY_DONE] - Summary completed on this page state. result_url={result_url}. "
                              "Do NOT summarize the same state again. Proceed to next actionable step or done."
                        )
                    else:
                        action_history.append(
                            current_action_log
                            + f" -> [SUCCESS] - Action completed. result_url={result_url}. DO NOT repeat this target. Move to the next step."
                        )
                    self._record_dynamic_step_if_needed(
                        recorder,
                        current_url=current_url,
                        next_action=next_action,
                        stable_target=stable_target,
                        is_optional=False,
                        fingerprint_dict=fp_dict,
                    )
                else:
                    failure_suffix = "Try alternative target or sequence."
                    if no_progress_detected:
                        failure_suffix = (
                            "This action caused no visible page change. DO NOT repeat this target. "
                            "Choose a more specific row/button/modal close action instead."
                        )
                    action_history.append(current_action_log + f" -> [FAILED] - {failure_suffix}")
                    if consecutive_failures >= max_consecutive_failures:
                        logger.error(
                            f"🛑 连续失败达到阈值 ({consecutive_failures}/{max_consecutive_failures})，failure_mode={failure_mode}"
                        )
                        if failure_mode == "stop":
                            break
                continue

            loop = asyncio.get_running_loop()
            prompt_msg = "\n👉 操作对吗？(y: 对 / o: 对，但设为【可选跳过】 / n: 错 / done: 结束): "
            user_input = await loop.run_in_executor(None, input, prompt_msg)
            user_input = user_input.strip().lower()

            if user_input in ["done", "d", "quit"]:
                logger.info("🛑 任务结束，正在保存轨迹...")
                if action_success:
                    fp_dict = await DomParser.get_element_fingerprint(page, stable_target)
                    self._record_dynamic_step_if_needed(
                        recorder,
                        current_url=current_url,
                        next_action=next_action,
                        stable_target=stable_target,
                        is_optional=False,
                        fingerprint_dict=fp_dict,
                    )
                trace_path = recorder.save_to_disk()
                trace_saved = True
                break

            if user_input == "o":
                logger.info("✅ 验收通过，并标记为【可选步骤 (Optional)】。")
                if action_success:
                    action_history.append(
                        current_action_log
                        + " -> [SUCCESS (OPTIONAL)] - Action completed. DO NOT repeat this target. Move to the next step."
                    )
                    self._record_dynamic_step_if_needed(
                        recorder,
                        current_url=current_url,
                        next_action=next_action,
                        stable_target=stable_target,
                        is_optional=True,
                    )
                continue

            if user_input == "n":
                logger.warning("🚫 动作标记为错误，不录制。")
                action_history.append(
                    current_action_log + " -> [USER MARKED AS INCORRECT] - Do not repeat this."
                )
                continue

            if user_input != "y" and user_input != "":
                action_history.append(current_action_log + f" -> [USER HINT: {user_input}]")
                continue

            logger.info("✅ 验收通过，已录制到暂存区。")
            if action_success:
                action_history.append(
                    current_action_log + " -> [SUCCESS] - Action completed. DO NOT repeat this target. Move to the next step."
                )
                self._record_dynamic_step_if_needed(
                    recorder,
                    current_url=current_url,
                    next_action=next_action,
                    stable_target=stable_target,
                    is_optional=False,
                )
            else:
                if consecutive_failures >= max_consecutive_failures and failure_mode == "stop":
                    logger.error(
                        f"🛑 连续失败达到阈值 ({consecutive_failures}/{max_consecutive_failures})，手动模式下停止任务。"
                    )
                    break

        if not trace_saved:
            logger.info("💾 达到步数上限或流程自然结束，自动保存当前轨迹。")
            trace_path = recorder.save_to_disk()

        await page.close()
        ai_log_paths = self._save_ai_action_log(task_name=task_name, actions=ai_action_records)
        element_monitor_paths = self._finalize_element_monitor(task_name)
        self.disable_fallback_recovery_runtime = False
        return {
            "task_name": task_name,
            "trace_path": trace_path,
            "ai_action_log": ai_log_paths,
            "element_monitor": element_monitor_paths,
            "total_actions": len(ai_action_records),
        }
