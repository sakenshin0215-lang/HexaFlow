import os
import asyncio
import logging
import base64
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Page, BrowserContext
from hexaflow.browser.cdp_runtime import CDPConfig, ensure_cdp_browser
from hexaflow.core.trace_recorder import TraceRecorder
from hexaflow.core.state_machine import StateMachine
from hexaflow.core.run_reporter import RunReporter
from hexaflow.core.task_schema import TaskSpec
from hexaflow.tools.dom_parser import DomParser
from hexaflow.agents.react_agent import NextAction
from hexaflow.tools.popup_healer import PopupHealer

import random
import json

# ==========================================
# 0. 配置日志
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] Engine: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("HexaEngine")

class HexaEngine:
    def __init__(
        self,
        headless: bool = False,
        state_path: str = "memory/workspace/auth_state.json",
        runtime_db_path: str = "memory/workspace/state/runtime.db",
    ):
        """
        初始化执行引擎
        :param headless: 是否无头模式运行。调试时建议设为 False 观看浏览器动作
        """
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.state_path = state_path
        self.state_machine = StateMachine(db_path=runtime_db_path)
        self.run_reporter = RunReporter(self.state_machine)
        self.use_cdp = False
        self.cdp_config = None
        
    async def start(self, use_cdp: bool = False, cdp_config: CDPConfig = None, cdp_start_url: str = "about:blank"):
        """启动浏览器环境"""
        logger.info("🚀 正在启动 Playwright 引擎...")
        self.playwright = await async_playwright().start()
        self.use_cdp = use_cdp

        if use_cdp:
            self.cdp_config = cdp_config or CDPConfig()
            # ensure_cdp_browser 是同步函数，用线程池避免阻塞事件循环
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, ensure_cdp_browser, self.cdp_config, cdp_start_url
            )
            self.browser = await self.playwright.chromium.connect_over_cdp(
                self.cdp_config.endpoint
            )
            logger.info(
                f"✅ 已通过CDP连接浏览器: {self.cdp_config.endpoint} | "
                f"user_data_dir={self.cdp_config.user_data_dir} | "
                f"profile={self.cdp_config.profile_directory}"
            )
            return

        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        logger.info("✅ 浏览器启动成功 (Playwright launch)")

    async def stop(self):
        """关闭浏览器环境"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("🛑 引擎已关闭")

    async def _detect_human_intervention_reason(self, page: Page) -> str:
        url = (page.url or "").lower()
        if any(k in url for k in ["login", "signin", "auth", "verify", "captcha", "challenge"]):
            return f"URL 命中登录/验证路径: {page.url}"

        text_checks = [
            "登录", "重新登录", "验证码", "验证", "人机验证", "二次验证", "双重验证",
            "Sign in", "Log in", "Verify", "CAPTCHA", "2FA", "Authentication",
            "解锁钱包", "Unlock", "Wallet password", "请输入密码"
        ]
        for t in text_checks:
            try:
                if await page.get_by_text(t, exact=False).first.is_visible(timeout=800):
                    return f"页面检测到人工验证文案: {t}"
            except Exception:
                continue
        return ""

    async def _prompt_human_choice(
        self,
        prompt: str,
        timeout_seconds: int = 600,
        default: str = "n",
        allowed: list[str] = None,
    ) -> str:
        """
        等待人工输入（可超时）。超时返回 default。
        说明：使用线程池 input，不阻塞事件循环。
        """
        allowed = allowed or ["y", "n"]
        loop = asyncio.get_running_loop()

        def _blocking_input():
            return input(prompt)

        try:
            raw = await asyncio.wait_for(
                loop.run_in_executor(None, _blocking_input),
                timeout=max(1, int(timeout_seconds)),
            )
            value = (raw or "").strip().lower()
            if value in allowed:
                return value
            return default
        except Exception:
            return default

    async def _human_handoff_if_needed(self, page: Page, checkpoint: str = "", enabled: bool = True) -> bool:
        if not enabled:
            return False
        reason = await self._detect_human_intervention_reason(page)
        if not reason:
            return False

        line = "=" * 72
        logger.warning("\n" + line)
        logger.warning("[人工接管] 检测到需要人工处理")
        if checkpoint:
            logger.warning(f"检查点: {checkpoint}")
        logger.warning(f"原因: {reason}")
        logger.warning(f"当前 URL: {page.url}")
        logger.warning("请在浏览器里完成：重新登录/验证码/钱包解锁/签名确认。")
        logger.warning("完成后回终端按回车继续。")
        logger.warning(line + "\n")

        # 标准化：允许人工选择继续/终止，并支持超时
        choice = await self._prompt_human_choice(
            prompt="[等待人工] 完成后按 y 继续 / n 终止 (默认 y): ",
            timeout_seconds=1800,
            default="y",
            allowed=["y", "n"],
        )
        if choice == "n":
            raise Exception(f"HumanHandoffAborted: {reason}")
        return True

    @staticmethod
    def _match_keyword(text: str, keywords: list[str]) -> str:
        lower_text = (text or "").lower()
        for k in (keywords or []):
            kk = (k or "").strip().lower()
            if kk and kk in lower_text:
                return k
        return ""

    async def _check_success_criteria(self, page: Page, criteria_cfg) -> tuple[bool, str]:
        """
        根据 TaskSpec.success_criteria 判定任务是否完成。
        返回 (is_done, detail_reason)
        """
        if not criteria_cfg or not getattr(criteria_cfg, "criteria", None):
            return False, ""

        mode = getattr(criteria_cfg, "mode", "any")
        max_wait_ms = int(getattr(criteria_cfg, "max_wait_ms", 0) or 0)
        end_ts = asyncio.get_running_loop().time() + max(0.0, max_wait_ms / 1000.0)

        async def eval_once() -> list[tuple[bool, str]]:
            results = []
            for c in criteria_cfg.criteria:
                ctype = getattr(c, "type", "")
                value = getattr(c, "value", "") or ""
                ci = bool(getattr(c, "case_insensitive", True))
                if not value:
                    results.append((False, f"{ctype}:<empty>"))
                    continue

                if ctype == "url_contains":
                    hay = page.url or ""
                    ok = (value.lower() in hay.lower()) if ci else (value in hay)
                    results.append((ok, f"url_contains({value})"))
                    continue

                if ctype == "text_present":
                    try:
                        loc = page.get_by_text(value, exact=False).first
                        ok = await loc.is_visible(timeout=800)
                        results.append((bool(ok), f"text_present({value})"))
                    except Exception:
                        results.append((False, f"text_present({value})"))
                    continue

                if ctype == "dom_selector_present":
                    try:
                        loc = page.locator(value).first
                        ok = await loc.is_visible(timeout=800)
                        results.append((bool(ok), f"dom_selector_present({value})"))
                    except Exception:
                        results.append((False, f"dom_selector_present({value})"))
                    continue

                results.append((False, f"unknown({ctype})"))
            return results

        # 允许小幅等待（如页面动画/跳转尚未完成）
        last_results = []
        while True:
            last_results = await eval_once()
            oks = [ok for ok, _ in last_results]
            if mode == "all":
                done = all(oks) if oks else False
            else:
                done = any(oks) if oks else False
            if done:
                matched = [name for ok, name in last_results if ok]
                return True, f"success_criteria_matched: {', '.join(matched)}"

            if max_wait_ms <= 0:
                break
            if asyncio.get_running_loop().time() >= end_ts:
                break
            await page.wait_for_timeout(250)

        return False, ""

    async def _save_browser_state(self, context: BrowserContext):
        if self.state_path:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            await context.storage_state(path=self.state_path)
            logger.info(f"💾 浏览器状态(Cookie/缓存)已永久保存至: {self.state_path}")

    async def _resolve_context(self, context: BrowserContext = None, context_options: dict = None):
        """
        统一获取可用 context：
        - 传入 context 时优先使用
        - CDP 模式默认复用 browser.contexts[0]（保留真实 profile、扩展、登录态）
        - 非 CDP 模式创建新 context
        """
        if context:
            return context

        if self.use_cdp:
            if self.browser and self.browser.contexts:
                logger.info("🧩 CDP模式：复用现有浏览器上下文（保留 profile/扩展/登录态）")
                if context_options and context_options.get("storage_state"):
                    logger.warning("⚠️ CDP复用上下文时忽略 storage_state 注入（以真实 profile 为准）")
                return self.browser.contexts[0]

            logger.warning("⚠️ CDP模式未发现现有上下文，降级创建新上下文（可能无扩展态）")
            return await self.browser.new_context(**(context_options or {}))

        return await self.browser.new_context(**(context_options or {}))

    async def _resolve_page(
        self,
        context: BrowserContext,
        start_url: str = "about:blank",
        prefer_existing_page: bool = False,
    ):
        """
        统一获取可用 page。
        - 默认保持旧行为：创建新页
        - CDP + prefer_existing_page=True 时优先复用已有页面
        - 优先选择已在 start_url 上的页面；否则选择第一个非 about:blank 页面
        """
        if prefer_existing_page and context and getattr(context, "pages", None):
            normalized_start = (start_url or "").strip()

            for existing_page in context.pages:
                try:
                    if normalized_start and normalized_start != "about:blank" and existing_page.url == normalized_start:
                        logger.info(f"♻️ 复用已有页面(精确命中 start_url): {existing_page.url}")
                        return existing_page
                except Exception:
                    continue

            for existing_page in context.pages:
                try:
                    page_url = (existing_page.url or "").strip()
                    if page_url and page_url != "about:blank":
                        logger.info(f"♻️ 复用已有页面(首个非 about:blank 页面): {page_url}")
                        return existing_page
                except Exception:
                    continue

        return await context.new_page()

    async def _capture_suspend_snapshot(self, page: Page, run_id: str, step_id: str) -> str:
        from datetime import datetime
        screenshot_dir = "memory/workspace/screenshots"
        os.makedirs(screenshot_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_step = (step_id or "unknown").replace("/", "_").replace(" ", "_")
        screenshot_path = os.path.join(screenshot_dir, f"suspend_{run_id}_{safe_step}_{ts}.png")
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            return screenshot_path
        except Exception:
            return ""

    async def _capture_viewport_screenshot_base64(
        self,
        page: Page,
        image_type: str = "jpeg",
        quality: int = 60,
    ) -> str:
        """
        捕获当前可视窗口截图（非 full_page），用于多模态模型输入，控制体积。
        """
        try:
            screenshot_kwargs = {
                "full_page": False,
                "type": image_type,
                "animations": "disabled",
            }
            if image_type == "jpeg":
                screenshot_kwargs["quality"] = quality
            screenshot_bytes = await page.screenshot(
                **screenshot_kwargs
            )
            return base64.b64encode(screenshot_bytes).decode("ascii")
        except Exception:
            return ""

    @staticmethod
    def _resolve_visual_click_point(action, viewport: dict) -> tuple[float, float]:
        """
        从 AI 动作中解析视觉坐标点，并裁剪到当前 viewport 内。
        """
        x = getattr(action, "click_x", None)
        y = getattr(action, "click_y", None)
        if x is None or y is None:
            raise Exception("VisualClickError: click/type 动作缺少 click_x 或 click_y")

        width = int((viewport or {}).get("width") or 1280)
        height = int((viewport or {}).get("height") or 800)
        max_x = max(1, width - 1)
        max_y = max(1, height - 1)

        x = float(x)
        y = float(y)
        x = min(max(0.0, x), float(max_x))
        y = min(max(0.0, y), float(max_y))
        return x, y

    @staticmethod
    def _normalize_url_candidate(raw_value: str):
        if not raw_value:
            return None
        value = raw_value.strip()
        if not value:
            return None
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("//"):
            return f"https:{value}"
        if value.startswith("/"):
            return None
        if "." in value.split("/")[0]:
            return f"https://{value}"
        return None

    def _infer_bootstrap_url(self, blueprint, start_index: int):
        if not blueprint.steps:
            return None
        idx = min(max(start_index, 0), len(blueprint.steps) - 1)
        current = blueprint.steps[idx]

        # 1) 优先使用当前步骤的 URL 断言
        inferred = self._normalize_url_candidate(current.pre_check.expected_url_contains)
        if inferred:
            return inferred

        # 2) 回溯最近一个 navigate 目标
        for step in reversed(blueprint.steps[: idx + 1]):
            if step.action.action_type == "navigate":
                inferred = self._normalize_url_candidate(step.action.target)
                if inferred:
                    return inferred

        # 3) 再尝试全局第一个 navigate
        for step in blueprint.steps:
            if step.action.action_type == "navigate":
                inferred = self._normalize_url_candidate(step.action.target)
                if inferred:
                    return inferred
        return None

    @staticmethod
    def _is_risky_step_for_replay(step, risky_keywords: list[str]) -> bool:
        action = (step.action.action_type or "").lower()
        if action != "click":
            return False

        target = (step.action.target or "").lower()
        desc = (step.description or "").lower()
        fp_text = ""
        if getattr(step.action, "fingerprint", None):
            fp_text = (step.action.fingerprint.text or "").lower()
        haystack = f"{target} {desc} {fp_text}"
        return any(k.lower() in haystack for k in risky_keywords if k)

    async def _attempt_wrong_page_recovery(self, page: Page, step) -> bool:
        """
        当 URL 看似正确但 DOM 不匹配时，尝试修复页面上下文。
        """
        expected = (step.pre_check.expected_url_contains or "").strip()
        current_url = page.url or ""

        # 1) 预期URL片段仍匹配：优先 reload，修复同URL不同状态
        if expected and expected in current_url:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=10000)
                await page.wait_for_timeout(1200)
                return True
            except Exception:
                pass

        # 2) 能推断出目标URL就强制回到目标页面
        inferred = self._normalize_url_candidate(expected)
        if inferred:
            try:
                await page.goto(inferred, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1200)
                return True
            except Exception:
                pass

        # 3) 最后尝试浏览器后退一步
        try:
            resp = await page.go_back(wait_until="domcontentloaded", timeout=8000)
            await page.wait_for_timeout(1000)
            return resp is not None
        except Exception:
            return False

    async def _replay_recent_subflow(
        self,
        page: Page,
        blueprint,
        step_index: int,
        healer: PopupHealer,
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
                try:
                    await healer.heal(page)
                    await self._execute_deterministic_step(page, replay_step)
                except Exception:
                    return False
        return True

    def _save_run_report(self, run_id: str):
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
    def _is_domain_allowed(target_url: str, allowed_domains: list[str]) -> bool:
        if not allowed_domains:
            return True
        host = (urlparse(target_url).hostname or "").lower()
        if not host:
            return False
        for domain in allowed_domains:
            d = domain.lower().strip()
            if host == d or host.endswith(f".{d}"):
                return True
        return False

    @staticmethod
    def _contains_blocked_keyword(raw_text: str, blocked_keywords: list[str]) -> bool:
        if not blocked_keywords:
            return False
        lower_text = (raw_text or "").lower()
        return any(k.lower().strip() and k.lower().strip() in lower_text for k in blocked_keywords)

    @staticmethod
    def _build_goal_with_notes(spec: TaskSpec) -> str:
        if not spec.notes:
            return spec.goal
        notes_text = "\n".join([f"- {item}" for item in spec.notes])
        return f"{spec.goal}\n\n执行注意事项:\n{notes_text}"

    async def run_task_from_spec(
        self,
        spec_input,
        agent,
        context: BrowserContext = None,
        replay_repair_agent=None,
        disable_fallback_recovery: bool = False,
        ai_decision_use_vision: bool = True,
        prefer_existing_page: bool = False,
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
        if replay_repair_agent is not None:
            logger.warning("⚠️ run_task_from_spec 当前是动态执行模式，replay_repair_agent 参数暂未使用。")
        if disable_fallback_recovery:
            logger.warning("⚠️ disable_fallback_recovery 在动态执行模式下暂未启用。")
        return await self.run_dynamic_task(
            goal=self._build_goal_with_notes(spec),
            agent=agent,
            context=context,
            max_steps=spec.max_steps,
            manual_review=spec.manual_review,
            start_url=spec.start_url,
            allowed_domains=spec.allowed_domains,
            blocked_keywords=spec.blocked_keywords,
            max_consecutive_failures=spec.failure_policy.max_consecutive_failures,
            failure_mode=spec.failure_policy.mode,
            ai_decision_use_vision=ai_decision_use_vision,
            risky_actions_policy=spec.risky_actions_policy,
            success_criteria=spec.success_criteria,
            prefer_existing_page=prefer_existing_page,
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
        ai_decision_use_vision: bool = True,
        risky_actions_policy=None,
        success_criteria=None,
        prefer_existing_page: bool = False,
    ):
        from datetime import datetime
        logger.info(f"▶️ 开始执行动态自适应任务: {goal} (manual_review={manual_review})")
        allowed_domains = allowed_domains or []
        blocked_keywords = blocked_keywords or []
        
        task_name = "Task_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        recorder = TraceRecorder(task_name=task_name)
        
        healer = PopupHealer()

        # 动态模式也创建 run_id，写入证据链与最终报告
        run_state = self.state_machine.start_or_resume(
            trace_path=f"dynamic:{task_name}",
            task_name=f"{task_name}::dynamic",
            total_steps=max_steps,
            resume=False,
        )
        run_id = run_state.run_id
        self.state_machine.add_event(run_id, "dynamic_run_started", detail=f"start_url={start_url}")

        if not context:
            context_options = {'viewport': {'width': 1280, 'height': 800}}
            # 非CDP模式下才注入 storage_state；CDP复用真实profile上下文
            if (not self.use_cdp) and self.state_path and os.path.exists(self.state_path):
                logger.info(f"🍪 发现缓存！正在加载本地浏览器状态: {self.state_path}")
                context_options['storage_state'] = self.state_path
            context = await self._resolve_context(context=context, context_options=context_options)

        page = await self._resolve_page(
            context=context,
            start_url=start_url,
            prefer_existing_page=prefer_existing_page,
        )
        if start_url != "about:blank" and (page.url or "") != start_url:
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
        step_count = 0
        consecutive_failures = 0
        trace_saved = False

        while step_count < max_steps:
            step_count += 1
            logger.info(f"\n" + "="*40 + f"\n--- 第 {step_count} 步 ---")
            
            current_url = page.url
            await page.wait_for_timeout(1000) 
            dom_snapshot = await DomParser.get_interactive_elements(page)

            # 先自动判定完成（避免多余模型调用）
            if success_criteria and getattr(success_criteria, "check_every_step", True):
                done, reason = await self._check_success_criteria(page, success_criteria)
                if done:
                    logger.info("✅ 已满足完成条件，自动结束动态任务。")
                    self.state_machine.add_event(run_id, "dynamic_done_by_criteria", detail=reason, step_index=step_count - 1)
                    recorder.save_to_disk()
                    trace_saved = True
                    await self._save_browser_state(context)
                    self.state_machine.mark_run_completed(run_id)
                    self._save_run_report(run_id)
                    break
            
            history_str = "\n".join(action_history)
            viewport = page.viewport_size or {"width": 1280, "height": 800}
            screenshot_base64 = (
                await self._capture_viewport_screenshot_base64(page)
                if ai_decision_use_vision
                else ""
            )
            next_action = await agent.decide_next_action(
                goal,
                history_str,
                current_url,
                dom_snapshot,
                screenshot_base64=screenshot_base64,
                screenshot_mime="image/jpeg",
                viewport_width=viewport.get("width"),
                viewport_height=viewport.get("height"),
            )

            step_id = f"dyn_step_{step_count}"
            self.state_machine.mark_step_started(run_id, step_count - 1, step_id)
            self.state_machine.add_event(
                run_id,
                "dynamic_agent_decision",
                detail=json.dumps(
                    {
                        "action_type": next_action.action_type,
                        "target": next_action.target,
                        "input_value": next_action.input_value,
                        "click_x": next_action.click_x,
                        "click_y": next_action.click_y,
                        "thought": (next_action.thought or "")[:800],
                        "url": current_url,
                    },
                    ensure_ascii=False,
                ),
                step_index=step_count - 1,
                step_id=step_id,
            )
            
            stable_target = next_action.target
            current_action_log = f"Skipped unknown action: {next_action.action_type}"
            action_success = False

            if next_action.action_type == "done":
                logger.info("🎉 AI 认为任务已完成！")
                recorder.save_to_disk()
                trace_saved = True
                await self._save_browser_state(context)
                self.state_machine.mark_run_completed(run_id)
                self._save_run_report(run_id)
                break
            else:
                action_raw_text = f"{next_action.action_type} {next_action.target or ''} {next_action.thought or ''}"
                if self._contains_blocked_keyword(action_raw_text, blocked_keywords):
                    current_action_log = f"BLOCKED by keyword policy: {next_action.action_type} on {next_action.target}"
                    logger.warning(f"🛡️ 动作已拦截: {current_action_log}")
                    action_success = False
                else:
                    # 风险动作闸门：命中关键词时阻断或要求人工确认
                    if risky_actions_policy and getattr(risky_actions_policy, "enabled", False):
                        matched = self._match_keyword(action_raw_text, getattr(risky_actions_policy, "keywords", []))
                        if matched:
                            # 允许域名/URL 白名单进一步约束
                            allow_domains = getattr(risky_actions_policy, "allowed_domains", []) or []
                            allow_url_contains = getattr(risky_actions_policy, "allowed_url_contains", []) or []
                            domain_ok = self._is_domain_allowed(page.url or "", allow_domains) if allow_domains else True
                            url_ok = any(s in (page.url or "") for s in allow_url_contains) if allow_url_contains else True

                            gate_detail = f"matched_keyword={matched} domain_ok={domain_ok} url_ok={url_ok} current_url={page.url}"
                            self.state_machine.add_event(
                                run_id,
                                "risky_action_detected",
                                detail=gate_detail,
                                step_index=step_count - 1,
                                step_id=step_id,
                            )

                            if not (domain_ok and url_ok):
                                logger.error("🛑 风险动作不在白名单范围内，已阻断并挂起。")
                                self.state_machine.mark_run_suspended(
                                    run_id,
                                    step_count - 1,
                                    step_id,
                                    f"RiskyActionBlocked: {gate_detail}",
                                )
                                self._save_run_report(run_id)
                                raise Exception(f"RiskyActionBlocked: {gate_detail}")

                            mode = getattr(risky_actions_policy, "mode", "require_confirm")
                            if mode == "block":
                                logger.error("🛑 风险动作策略=block，已挂起等待人工处理。")
                                self.state_machine.mark_run_suspended(
                                    run_id,
                                    step_count - 1,
                                    step_id,
                                    f"RiskyActionBlocked(mode=block): {gate_detail}",
                                )
                                self._save_run_report(run_id)
                                raise Exception(f"RiskyActionBlocked(mode=block): {gate_detail}")

                            # require_confirm
                            logger.warning("⚠️ 检测到风险动作，等待人工确认(y/n)。")
                            choice = await self._prompt_human_choice(
                                prompt=(
                                    f"\n[风险动作确认] step={step_id}\n"
                                    f"- url: {page.url}\n"
                                    f"- action: {next_action.action_type}\n"
                                    f"- target: {next_action.target}\n"
                                    f"- matched: {matched}\n"
                                    "是否允许继续执行？(y/n): "
                                ),
                                timeout_seconds=getattr(risky_actions_policy, "max_confirm_wait_seconds", 600),
                                default="n",
                                allowed=["y", "n"],
                            )
                            self.state_machine.add_event(
                                run_id,
                                "risky_action_human_confirm",
                                detail=f"choice={choice} {gate_detail}",
                                step_index=step_count - 1,
                                step_id=step_id,
                            )
                            if choice != "y":
                                logger.error("🛑 人工未批准风险动作，已挂起。")
                                self.state_machine.mark_run_suspended(
                                    run_id,
                                    step_count - 1,
                                    step_id,
                                    f"RiskyActionDenied: {gate_detail}",
                                )
                                self._save_run_report(run_id)
                                raise Exception(f"RiskyActionDenied: {gate_detail}")

                    logger.info(f"⚡ 自动执行: [{next_action.action_type}] 目标: {next_action.target}")
                    
                    max_attempts = 4
                    for attempt in range(max_attempts):
                        try:
                            if next_action.target and "hexa-id" in next_action.target:
                                stable_target = await DomParser.get_stable_selector(page, next_action.target)
                            
                            if next_action.action_type == "navigate":
                                if not self._is_domain_allowed(stable_target, allowed_domains):
                                    raise Exception(f"Navigation blocked by allowed_domains policy: {stable_target}")
                                await page.goto(stable_target) 
                                
                            elif next_action.action_type == "click":
                                click_x, click_y = self._resolve_visual_click_point(
                                    next_action, viewport
                                )
                                stable_target = f"coord:{int(click_x)},{int(click_y)}"
                                await page.mouse.click(click_x, click_y)
                                
                            elif next_action.action_type == "type":
                                click_x, click_y = self._resolve_visual_click_point(
                                    next_action, viewport
                                )
                                stable_target = f"coord:{int(click_x)},{int(click_y)}"
                                await page.mouse.click(click_x, click_y)
                                await page.keyboard.type(next_action.input_value or "", delay=80)
                                
                            await page.wait_for_timeout(1500) 
                            current_action_log = f"Executed {next_action.action_type} on {stable_target}"
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
                            # 如果是被弹窗遮挡、不可点击或超时，触发自愈！
                            if "timeout" in error_msg or "intercepted" in error_msg or "not visible" in error_msg:
                                logger.warning(f"🛑 动作受阻 (可能被弹窗拦截)。")
                                
                                if attempt < max_attempts - 1:
                                    logger.info("🚑 [录制阶段] 唤醒 Healer 扫描异常弹窗...")
                                    await healer.heal(page)
                                    logger.info("✨ Healer 处理完毕。给前端动画一点时间，即将强行重试主线动作...")
                                    await page.wait_for_timeout(2000) 
                                    continue # 无条件进入下一次循环，重试主线动作！
                                        
                            # 如果自愈失败，或者不是弹窗导致的错误，记录错误并等待人工发落
                            logger.error(f"❌ 动作执行失败: {e}")
                            current_action_log = f"FAILED to execute {next_action.action_type} on {stable_target}"
                            break

            if action_success:
                consecutive_failures = 0
                self.state_machine.mark_step_success(run_id, step_count - 1, step_id)
                self.state_machine.add_event(
                    run_id,
                    "dynamic_step_success",
                    detail=current_action_log,
                    step_index=step_count - 1,
                    step_id=step_id,
                )
            else:
                consecutive_failures += 1
                self.state_machine.add_event(
                    run_id,
                    "dynamic_step_failed",
                    detail=current_action_log,
                    step_index=step_count - 1,
                    step_id=step_id,
                )

            # 动作后也检查完成条件（避免模型误判/漏判）
            if action_success and success_criteria and getattr(success_criteria, "check_every_step", True):
                done, reason = await self._check_success_criteria(page, success_criteria)
                if done:
                    logger.info("✅ 动作后已满足完成条件，自动结束动态任务。")
                    self.state_machine.add_event(run_id, "dynamic_done_by_criteria", detail=reason, step_index=step_count - 1, step_id=step_id)
                    recorder.save_to_disk()
                    trace_saved = True
                    await self._save_browser_state(context)
                    self.state_machine.mark_run_completed(run_id)
                    self._save_run_report(run_id)
                    break

            if not manual_review:
                if action_success:
                    fp_dict = {}
                    if not str(stable_target).startswith("coord:"):
                        fp_dict = await DomParser.get_element_fingerprint(page, stable_target)
                    action_history.append(current_action_log + " -> [SUCCESS] - Action completed. DO NOT repeat this target. Move to the next step.")
                    recorder.record_step(
                        current_url,
                        next_action.action_type,
                        stable_target,
                        next_action.input_value,
                        next_action.thought,
                        is_optional=False,
                        fingerprint_dict=fp_dict
                    )
                else:
                    action_history.append(current_action_log + " -> [FAILED] - Try alternative target or sequence.")
                    if consecutive_failures >= max_consecutive_failures:
                        logger.error(
                            f"🛑 连续失败达到阈值 ({consecutive_failures}/{max_consecutive_failures})，failure_mode={failure_mode}"
                        )
                        if failure_mode == "stop":
                            self.state_machine.mark_run_failed(
                                run_id,
                                step_count - 1,
                                step_id,
                                f"DynamicFailureThresholdReached: {current_action_log}",
                            )
                            self._save_run_report(run_id)
                            break
                continue

            # ==========================================
            # 人工审核与录制落盘
            # ==========================================
            loop = asyncio.get_running_loop()
            prompt_msg = "\n👉 操作对吗？(y: 对 / o: 对，但设为【可选跳过】 / n: 错 / done: 结束): "
            user_input = await loop.run_in_executor(None, input, prompt_msg)
            user_input = user_input.strip().lower()

            if user_input in ['done', 'd', 'quit']:
                logger.info("🛑 任务结束，正在保存轨迹...")
                if action_success:
                    fp_dict = {}
                    if not str(stable_target).startswith("coord:"):
                        fp_dict = await DomParser.get_element_fingerprint(page, stable_target)
                    recorder.record_step(
                        current_url,
                        next_action.action_type,
                        stable_target,
                        next_action.input_value,
                        next_action.thought,
                        fingerprint_dict=fp_dict
                    )
                recorder.save_to_disk()
                trace_saved = True
                await self._save_browser_state(context)
                self.state_machine.mark_run_completed(run_id)
                self._save_run_report(run_id)
                break
            
            if user_input == 'o':
                logger.info("✅ 验收通过，并标记为【可选步骤 (Optional)】。")
                if action_success:
                    action_history.append(current_action_log + " -> [SUCCESS (OPTIONAL)] - Action completed. DO NOT repeat this target. Move to the next step.")
                    recorder.record_step(
                        current_url,
                        next_action.action_type,
                        stable_target,
                        next_action.input_value,
                        next_action.thought,
                        is_optional=True
                    )
                continue
            
            if user_input == 'n':
                logger.warning("🚫 动作标记为错误，不录制。")
                action_history.append(current_action_log + " -> [USER MARKED AS INCORRECT] - Do not repeat this.")
                continue
            
            if user_input != 'y' and user_input != '':
                action_history.append(current_action_log + f" -> [USER HINT: {user_input}]")
                continue

            logger.info("✅ 验收通过，已录制到暂存区。")
            if action_success:
                action_history.append(current_action_log + " -> [SUCCESS] - Action completed. DO NOT repeat this target. Move to the next step.")
                recorder.record_step(
                    current_url,
                    next_action.action_type,
                    stable_target,
                    next_action.input_value,
                    next_action.thought,
                    is_optional=False
                )
            else:
                if consecutive_failures >= max_consecutive_failures and failure_mode == "stop":
                    logger.error(
                        f"🛑 连续失败达到阈值 ({consecutive_failures}/{max_consecutive_failures})，手动模式下停止任务。"
                    )
                    self.state_machine.mark_run_failed(
                        run_id,
                        step_count - 1,
                        step_id,
                        f"DynamicFailureThresholdReached(manual): {current_action_log}",
                    )
                    self._save_run_report(run_id)
                    break

        if not trace_saved:
            logger.info("💾 达到步数上限或流程自然结束，自动保存当前轨迹。")
            recorder.save_to_disk()
            await self._save_browser_state(context)
            # 达到上限视作失败（可据 future policy 调整）
            if self.state_machine.get_by_run_id(run_id).status == "running":
                self.state_machine.mark_run_failed(
                    run_id,
                    step_count - 1 if step_count > 0 else 0,
                    f"dyn_step_{step_count}" if step_count > 0 else "dyn_step_0",
                    "DynamicMaxStepsReached",
                )
                self._save_run_report(run_id)

        await page.close()

    async def _execute_deterministic_step(self, page, step):
        """
        执行确定性的单一节点。
        这里复用了我们之前修复过的逻辑：妥善处理 Navigate 和 Pre-check 的先后顺序。
        """
        logger.info(f"⏳ 步骤 [{step.step_id}]: {step.description}")
        pre = step.pre_check
        act = step.action

        # ==================================
        # 1. 特殊处理 Navigate (先跳再等)
        # ==================================
        if act.action_type == "navigate":
            logger.info(f"   ⚙️ 动作: navigate -> {act.target}")
            await page.goto(act.target)
            
            if pre.expected_dom_selector and pre.expected_dom_selector != "body":
                logger.info(f"   🔍 导航后校验 DOM: {pre.expected_dom_selector}")
                await page.wait_for_selector(pre.expected_dom_selector, timeout=pre.timeout_ms)
            return

        # ==================================
        # 1.5 页面守卫：执行动作前先确认在该步骤规定页面
        # ==================================
        await self._ensure_step_page_guard(page, step)

        # ==================================
        # 2. 增强前置校验与可见元素寻找 (Pre-check & Filter)
        # ==================================
        actual_target = act.target
        target_locator = None

        # 🧠 闭包：智能寻找页面上真实可见的匹配元素，防止死等隐藏的移动端节点
        async def get_visible_locator(selector, timeout_ms):
            for _ in range(max(1, int(timeout_ms / 250))):
                base_loc = page.locator(selector)
                count = await base_loc.count()
                for i in range(count):
                    loc = base_loc.nth(i)
                    if await loc.is_visible():
                        return loc  # 找到第一个真实可见的元素，立即返回
                await page.wait_for_timeout(250)
            raise Exception(f"Timeout: 没有找到可见的元素 -> {selector}")

        if pre.expected_dom_selector and pre.expected_dom_selector != "body":
            try:
                # 🚀 放弃死板的 wait_for_selector，改用我们的智能可见性轮询
                target_locator = await get_visible_locator(pre.expected_dom_selector, pre.timeout_ms)
            except Exception:
                logger.warning(f"⚠️ 原始定位器失效或被遮挡: {pre.expected_dom_selector}，启动特征模糊搜索...")
                
                fp = getattr(act, 'fingerprint', None)
                if fp:
                    fuzzy_selector = await DomParser.fuzzy_find_by_fingerprint(page, fp.model_dump())
                    if fuzzy_selector:
                        try:
                            logger.info(f"🔍 模糊搜索生成的候选定位器: {fuzzy_selector}")
                            # 🚀 自愈搜索也使用智能轮询
                            target_locator = await get_visible_locator(fuzzy_selector, 3000)
                            actual_target = fuzzy_selector # 替换靶标日志，便于观察
                            logger.info("✅ 模糊匹配自愈成功！")
                        except Exception:
                            logger.warning("❌ 模糊搜索也未能找到可见元素...")
                            raise Exception("DOM matching failed completely.")
                else:
                    raise

        # 如果没有触发前置校验，提供一个兜底
        if not target_locator:
            target_locator = page.locator(actual_target).first

        # ==================================
        # 3. 稳健的拟人化动作执行 (Action)
        # ==================================
        logger.info(f"   ⚙️ 动作: {act.action_type} -> {actual_target}")
        
        humanoid_delay = random.uniform(1.5, 3.0) * 1000
        await page.wait_for_timeout(humanoid_delay) 
        
        if act.action_type == "click":
            # 🚀 这里直接使用我们千辛万苦找出来的 target_locator，绝对是可见的！
            await target_locator.hover(timeout=5000)
            await page.wait_for_timeout(300)
            await target_locator.click(timeout=5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass
            
        elif act.action_type == "type":
            await target_locator.hover(timeout=5000)
            await target_locator.press_sequentially(act.input_value, delay=100, timeout=5000)
            
        elif act.action_type == "wait_for_timeout":
            await page.wait_for_timeout(1000)

    async def _execute_override_action(self, page: Page, action_type: str, target: str = None, input_value: str = None):
        """
        执行 AI 修复器给出的临时动作，不修改原 trace 文件。
        """
        action_type = (action_type or "").strip().lower()
        if action_type == "navigate":
            if not target:
                raise Exception("override navigate 缺少 target URL")
            await page.goto(target, wait_until="domcontentloaded")
            return

        if action_type == "click":
            if not target:
                raise Exception("override click 缺少 target selector")
            loc = page.locator(target).first
            await loc.hover(timeout=5000)
            await page.wait_for_timeout(200)
            await loc.click(timeout=5000)
            return

        if action_type == "type":
            if not target:
                raise Exception("override type 缺少 target selector")
            loc = page.locator(target).first
            await loc.hover(timeout=5000)
            await loc.fill("")
            await loc.type(input_value or "", delay=70, timeout=5000)
            return

        if action_type == "wait_for_timeout":
            delay_ms = 1000
            try:
                if input_value:
                    delay_ms = int(float(input_value) * 1000) if "." in str(input_value) else int(input_value)
            except Exception:
                delay_ms = 1000
            await page.wait_for_timeout(max(200, delay_ms))
            return

        raise Exception(f"override 不支持的动作类型: {action_type}")

    async def _run_mismatch_actions(self, page: Page, step) -> bool:
        actions = getattr(step, "on_mismatch_actions", None) or []
        if not actions:
            return False
        for idx, action in enumerate(actions, start=1):
            logger.info(
                f"🧭 [PageGuard] 执行回退动作 {idx}/{len(actions)}: {action.action_type} -> {action.target}"
            )
            await self._execute_override_action(
                page=page,
                action_type=action.action_type,
                target=action.target,
                input_value=action.input_value,
            )
            await page.wait_for_timeout(800)
        return True

    async def _ensure_step_page_guard(self, page: Page, step):
        """
        步骤执行前页面守卫：
        - 当前 URL 不符合 guard 时，先执行 on_mismatch_actions
        - 若仍不符合，再尝试基于 URL 片段自动回跳
        """
        required_url = getattr(step, "guard_url_contains", None) or step.pre_check.expected_url_contains
        if not required_url:
            return
        required_url = required_url.strip()
        if required_url in ("", "body"):
            return

        retry_limit = max(1, int(getattr(step, "guard_retry_limit", 2)))
        for attempt in range(1, retry_limit + 1):
            if required_url in (page.url or ""):
                return

            logger.warning(
                f"⚠️ [PageGuard] 步骤页面不匹配(step={step.step_id}) "
                f"expect contains='{required_url}', current='{page.url}', attempt={attempt}/{retry_limit}"
            )

            used_custom = False
            try:
                used_custom = await self._run_mismatch_actions(page, step)
            except Exception as e:
                logger.warning(f"⚠️ [PageGuard] 自定义回退动作执行失败: {e}")

            if required_url in (page.url or ""):
                return

            if not used_custom:
                inferred = self._normalize_url_candidate(required_url)
                if inferred:
                    try:
                        logger.info(f"🧭 [PageGuard] 自动回跳到目标页面: {inferred}")
                        await page.goto(inferred, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(800)
                    except Exception as e:
                        logger.warning(f"⚠️ [PageGuard] 自动回跳失败: {e}")
                else:
                    try:
                        logger.info("🧭 [PageGuard] 尝试后退恢复页面")
                        await page.go_back(wait_until="domcontentloaded", timeout=8000)
                        await page.wait_for_timeout(700)
                    except Exception:
                        pass

            if required_url in (page.url or ""):
                return

        raise Exception(
            f"PageGuardError: step={step.step_id} 期望页面包含 '{required_url}'，但当前为 '{page.url}'"
        )

    @staticmethod
    def _build_recent_steps_text(blueprint, current_index: int, window: int = 5) -> str:
        start = max(0, current_index - window)
        chunks = []
        for idx in range(start, current_index):
            s = blueprint.steps[idx]
            chunks.append(
                f"[{idx}] step_id={s.step_id} desc={s.description} "
                f"action={s.action.action_type} target={s.action.target} optional={s.is_optional}"
            )
        return "\n".join(chunks) if chunks else "No previous steps."

    async def run_from_trace(
        self,
        trace_path: str,
        context=None,
        viewport: dict = None,
        state_path: str = None,
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
    ):
        """
        克隆回放模式：读取本地 JSON 轨迹，脱离大模型，进行高速确定性执行。
        """
        from hexaflow.agents.planner import WorkflowBlueprint
        import os
        
        if not os.path.exists(trace_path):
            raise FileNotFoundError(f"找不到轨迹文件: {trace_path}")
            
        logger.info(f"📂 正在加载黄金轨迹: {trace_path}")
        with open(trace_path, 'r', encoding='utf-8') as f:
            trace_data = f.read()
        
        # 利用 Pydantic 的反序列化能力，将 JSON 文本瞬间转为强类型对象
        blueprint = WorkflowBlueprint.model_validate_json(trace_data)
        logger.info(f"▶️ 开始回放任务: {blueprint.task_name} (共 {len(blueprint.steps)} 步)")
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

        # ==========================================
        # 🚀 核心改造：支持动态注入不同的测试环境
        # ==========================================
        if not context:
            # 1. 设置窗口大小（默认桌面，可传入移动端尺寸）
            vp = viewport or {'width': 1280, 'height': 800}
            context_options = {'viewport': vp}
            
            # 2. 伪造设备指纹 (User-Agent)
            if user_agent:
                context_options['user_agent'] = user_agent
                
            # 3. 动态决定使用哪个账号的缓存数据（仅非CDP时生效）
            actual_state = state_path if state_path is not None else getattr(self, 'state_path', None)
            if (not self.use_cdp) and actual_state and os.path.exists(actual_state):
                logger.info(f"🍪 发现缓存！正在加载本地浏览器状态: {actual_state}")
                context_options['storage_state'] = actual_state
            elif not self.use_cdp:
                logger.info("✨ 未使用缓存，正以全新无痕环境启动...")

            context = await self._resolve_context(context=context, context_options=context_options)

        page = await context.new_page()
        if start_index < len(blueprint.steps):
            first_pending_step = blueprint.steps[start_index]
            if (
                page.url == "about:blank"
                and first_pending_step.action.action_type != "navigate"
            ):
                bootstrap_url = self._infer_bootstrap_url(blueprint, start_index)
                if bootstrap_url:
                    logger.info(f"🧭 回放预热: 当前是 about:blank，自动导航到 {bootstrap_url}")
                    await page.goto(bootstrap_url, wait_until="domcontentloaded")
        await self._human_handoff_if_needed(
            page, checkpoint="回放启动检查", enabled=human_handoff_on_auth
        )

        healer = PopupHealer()

        for step_index, step in enumerate(blueprint.steps[start_index:], start=start_index):
            self.state_machine.mark_step_started(run_state.run_id, step_index, step.step_id)
            max_attempts = 5  # 主线重试 + 多层恢复 + AI修复
            for attempt in range(max_attempts):
                try:
                    await self._execute_deterministic_step(page, step)
                    self.state_machine.mark_step_success(run_state.run_id, step_index, step.step_id)
                    break # 成功执行，跳出重试循环
                    
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
                    # 如果是被弹窗遮挡、不可点击或超时，触发自愈！
                    if getattr(step, 'is_optional', False) and ("timeout" in error_msg or "not visible" in error_msg):
                        logger.info(f"⏭️ [可选步骤] 元素未出现，安全跳过: {step.step_id}")
                        self.state_machine.mark_step_skipped(
                            run_state.run_id,
                            step_index,
                            step.step_id,
                            "optional step skipped due to timeout or invisibility",
                        )
                        break

                    if (
                        "timeout" in error_msg
                        or "intercepted" in error_msg
                        or "not visible" in error_msg
                        or "dom matching failed" in error_msg
                    ):
                        logger.warning(f"🛑 步骤 [{step.step_id}] 受阻。原因: 元素不可操作或超时。")
                        
                        if attempt < max_attempts - 1:
                            if attempt == 0:
                                logger.info("🚑 [回放阶段] 尝试 Healer 自愈...")
                                healed = await healer.heal(page)
                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_healer",
                                    detail=f"step={step.step_id} healed={healed}",
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )
                                await page.wait_for_timeout(1200)
                                continue

                            if attempt == 1:
                                logger.info("↩️ [回放阶段] 尝试同URL上下文修复(reload/back)...")
                                fixed = await self._attempt_wrong_page_recovery(page, step)
                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_wrong_page",
                                    detail=f"step={step.step_id} fixed={fixed} url={page.url}",
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )
                                await page.wait_for_timeout(1200)
                                continue

                            if attempt == 2:
                                logger.info("🔁 [回放阶段] 尝试小流程回放修复(前2步)...")
                                repaired = await self._replay_recent_subflow(
                                    page=page,
                                    blueprint=blueprint,
                                    step_index=step_index,
                                    healer=healer,
                                    window=subflow_window,
                                    skip_risky=subflow_skip_risky,
                                    risky_keywords=subflow_risky_keywords,
                                )
                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_subflow",
                                    detail=(
                                        f"step={step.step_id} repaired={repaired} "
                                        f"window={subflow_window} skip_risky={subflow_skip_risky}"
                                    ),
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )
                                await page.wait_for_timeout(1200)
                                continue

                            if attempt == 3 and replay_repair_agent:
                                logger.info("🧠 [回放阶段] 唤醒 AI 修复器分析最近步骤并生成修复动作...")
                                dom_snapshot = await DomParser.get_interactive_elements(page)
                                screenshot_base64 = await self._capture_viewport_screenshot_base64(page)
                                recent_steps = self._build_recent_steps_text(
                                    blueprint=blueprint,
                                    current_index=step_index,
                                    window=repair_context_window,
                                )
                                decision = await replay_repair_agent.repair_failed_replay_step(
                                    task_name=blueprint.task_name,
                                    recent_steps=recent_steps,
                                    current_url=page.url,
                                    dom_snapshot=dom_snapshot,
                                    failed_step_desc=step.description,
                                    failed_action_type=step.action.action_type,
                                    failed_target=step.action.target,
                                    last_error=str(e),
                                    screenshot_base64=screenshot_base64,
                                    screenshot_mime="image/jpeg",
                                )

                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_ai_decision",
                                    detail=(
                                        f"step={step.step_id} strategy={decision.strategy} "
                                        f"confidence={decision.confidence:.2f} thought={decision.thought}"
                                    ),
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )

                                if decision.strategy == "skip_step":
                                    reason = decision.skip_reason or "AI 建议跳过该步骤"
                                    logger.warning(f"⏭️ [AI修复] 跳过步骤: {step.step_id} | {reason}")
                                    self.state_machine.mark_step_skipped(
                                        run_state.run_id, step_index, step.step_id, reason
                                    )
                                    break

                                if decision.strategy == "human_handoff":
                                    logger.warning("👤 [AI修复] 建议人工接管后继续")
                                    await self._human_handoff_if_needed(
                                        page,
                                        checkpoint=f"AI建议人工接管(step_id={step.step_id})",
                                        enabled=True,
                                    )
                                    await page.wait_for_timeout(800)
                                    continue

                                if decision.strategy in ("retry_with_new_selector", "replace_action"):
                                    try:
                                        if decision.strategy == "retry_with_new_selector":
                                            action_type = step.action.action_type
                                            target = decision.target
                                            input_value = step.action.input_value
                                        else:
                                            action_type = decision.action_type or step.action.action_type
                                            target = decision.target
                                            input_value = decision.input_value

                                        await self._execute_override_action(
                                            page=page,
                                            action_type=action_type,
                                            target=target,
                                            input_value=input_value,
                                        )
                                        logger.info(
                                            f"✅ [AI修复] 临时动作执行成功: action={action_type} target={target}"
                                        )
                                        self.state_machine.add_event(
                                            run_state.run_id,
                                            event="recovery_ai_applied",
                                            detail=f"step={step.step_id} action={action_type} target={target}",
                                            step_index=step_index,
                                            step_id=step.step_id,
                                        )
                                        self.state_machine.mark_step_success(
                                            run_state.run_id, step_index, step.step_id
                                        )
                                        break
                                    except Exception as ai_exec_err:
                                        logger.warning(f"⚠️ [AI修复] 临时动作执行失败: {ai_exec_err}")
                                        self.state_machine.add_event(
                                            run_state.run_id,
                                            event="recovery_ai_failed",
                                            detail=f"step={step.step_id} error={ai_exec_err}",
                                            step_index=step_index,
                                            step_id=step.step_id,
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
                            else:
                                self.state_machine.mark_run_failed(
                                    run_state.run_id, step_index, step.step_id, detail
                                )
                            self._save_run_report(run_state.run_id)
                            raise
                    else:
                        # 如果是网络断开等底层错误，直接抛出
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
                        else:
                            self.state_machine.mark_run_failed(
                                run_state.run_id, step_index, step.step_id, detail
                            )
                        self._save_run_report(run_state.run_id)
                        raise
                
        self.state_machine.mark_run_completed(run_state.run_id)
        report_paths = self._save_run_report(run_state.run_id)
        logger.info("🎉 轨迹回放圆满完成！")
        return report_paths
        # loop = asyncio.get_running_loop()
        # await loop.run_in_executor(None, input, "\n👉 回放已完成，请查看浏览器现场。按【回车键】关闭浏览器并结束任务...")
        # await page.close()

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
        healer: PopupHealer,
        human_handoff_on_auth: bool,
        replay_repair_agent=None,
        task_name: str = "",
        recent_steps_text: str = "",
        max_attempts: int = 5,
    ) -> bool:
        for attempt in range(max_attempts):
            try:
                await self._execute_deterministic_step(page, step)
                return True
            except Exception as e:
                error_msg = str(e).lower()
                handled = await self._human_handoff_if_needed(
                    page,
                    checkpoint=f"循环步骤失败人工检查(step_id={step.step_id})",
                    enabled=human_handoff_on_auth,
                )
                if handled and attempt < max_attempts - 1:
                    await page.wait_for_timeout(800)
                    continue

                if "timeout" in error_msg or "intercepted" in error_msg or "not visible" in error_msg or "dom matching failed" in error_msg:
                    if attempt == 0:
                        await healer.heal(page)
                        await page.wait_for_timeout(1000)
                        continue
                    if attempt == 1:
                        await self._attempt_wrong_page_recovery(page, step)
                        await page.wait_for_timeout(900)
                        continue
                    if attempt == 2 and replay_repair_agent:
                        try:
                            dom_snapshot = await DomParser.get_interactive_elements(page)
                            screenshot_base64 = await self._capture_viewport_screenshot_base64(page)
                            decision = await replay_repair_agent.repair_failed_replay_step(
                                task_name=task_name,
                                recent_steps=recent_steps_text or "No previous steps.",
                                current_url=page.url,
                                dom_snapshot=dom_snapshot,
                                failed_step_desc=step.description,
                                failed_action_type=step.action.action_type,
                                failed_target=step.action.target,
                                last_error=str(e),
                                screenshot_base64=screenshot_base64,
                                screenshot_mime="image/jpeg",
                            )
                            if decision.strategy == "skip_step":
                                return True
                            if decision.strategy == "human_handoff":
                                await self._human_handoff_if_needed(
                                    page,
                                    checkpoint=f"AI建议人工接管(step_id={step.step_id})",
                                    enabled=True,
                                )
                                continue
                            if decision.strategy in ("retry_with_new_selector", "replace_action"):
                                if decision.strategy == "retry_with_new_selector":
                                    action_type = step.action.action_type
                                    target = decision.target
                                    input_value = step.action.input_value
                                else:
                                    action_type = decision.action_type or step.action.action_type
                                    target = decision.target
                                    input_value = decision.input_value
                                await self._execute_override_action(
                                    page=page,
                                    action_type=action_type,
                                    target=target,
                                    input_value=input_value,
                                )
                                return True
                        except Exception:
                            pass
                if attempt >= max_attempts - 1:
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
        state_path: str = None,
        user_agent: str = None,
        human_handoff_on_auth: bool = True,
        replay_repair_agent=None,
        repair_context_window: int = 5,
    ):
        """
        循环任务执行器：
        - 依据 trace 中的 loop_marker(start/end) 定义循环区间
        - 循环次数由外部参数指定
        - 仅当“整轮循环步骤全部成功”才计为完成一次
        - 失败则整轮重试，直到成功或超过 max_iteration_retries
        """
        from hexaflow.agents.planner import WorkflowBlueprint

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
        logger.info(
            f"🔁 循环任务启动: run_id={run_state.run_id} loop={loop_name} "
            f"range=[{loop_start}, {loop_end}] iterations={loop_iterations}"
        )

        if not context:
            vp = viewport or {"width": 1280, "height": 800}
            context_options = {"viewport": vp}
            if user_agent:
                context_options["user_agent"] = user_agent
            actual_state = state_path if state_path is not None else getattr(self, "state_path", None)
            if (not self.use_cdp) and actual_state and os.path.exists(actual_state):
                context_options["storage_state"] = actual_state
            context = await self._resolve_context(context=context, context_options=context_options)

        page = await context.new_page()
        first_bootstrap = self._infer_bootstrap_url(blueprint, 0)
        if first_bootstrap:
            await page.goto(first_bootstrap, wait_until="domcontentloaded")
        await self._human_handoff_if_needed(page, checkpoint="循环任务启动检查", enabled=human_handoff_on_auth)

        healer = PopupHealer()
        completed_loops = 0

        try:
            # A) 循环前步骤，只执行一次
            pre_steps = blueprint.steps[:loop_start]
            for idx, step in enumerate(pre_steps):
                recent = self._build_recent_steps_text(blueprint, idx, repair_context_window)
                ok = await self._execute_step_for_loop(
                    page=page,
                    step=step,
                    healer=healer,
                    human_handoff_on_auth=human_handoff_on_auth,
                    replay_repair_agent=replay_repair_agent,
                    task_name=blueprint.task_name,
                    recent_steps_text=recent,
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
                        recent = self._build_recent_steps_text(
                            blueprint, step_index, repair_context_window
                        )
                        ok = await self._execute_step_for_loop(
                            page=page,
                            step=step,
                            healer=healer,
                            human_handoff_on_auth=human_handoff_on_auth,
                            replay_repair_agent=replay_repair_agent,
                            task_name=blueprint.task_name,
                            recent_steps_text=recent,
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
                    logger.warning(f"⚠️ 第 {i} 次循环未成功，本轮重试。")

            # C) 循环后步骤，只执行一次
            post_steps = blueprint.steps[loop_end + 1:]
            for idx2, step in enumerate(post_steps, start=loop_end + 1):
                recent = self._build_recent_steps_text(blueprint, idx2, repair_context_window)
                ok = await self._execute_step_for_loop(
                    page=page,
                    step=step,
                    healer=healer,
                    human_handoff_on_auth=human_handoff_on_auth,
                    replay_repair_agent=replay_repair_agent,
                    task_name=blueprint.task_name,
                    recent_steps_text=recent,
                )
                if not ok:
                    raise Exception(f"循环后置步骤失败: {step.step_id}")

            self.state_machine.mark_run_completed(run_state.run_id)
            report_paths = self._save_run_report(run_state.run_id)
            logger.info("🎉 循环任务执行完成")
            return {
                "completed_loops": completed_loops,
                "target_loops": loop_iterations,
                "remaining_loops": max(0, loop_iterations - completed_loops),
                "report_paths": report_paths,
            }
        except Exception as e:
            detail = str(e)
            screenshot_path = await self._capture_suspend_snapshot(page, run_state.run_id, "loop_task")
            if screenshot_path:
                detail = f"{detail}\n[screenshot]: {screenshot_path}"
            self.state_machine.mark_run_suspended(run_state.run_id, loop_start, "loop_task", detail)
            self._save_run_report(run_state.run_id)
            raise

    async def run_loop_task_from_spec(
        self,
        trace_path: str,
        spec_input,
        context=None,
        replay_repair_agent=None,
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
        )

    async def run_manual_record_task(self, goal: str, agent, start_url: str = "https://www.google.com", context: BrowserContext = None):
        """
        人工专家示教模式：用户手动点击，系统拦截并由 AI 分析记录
        """
        from datetime import datetime
        import asyncio
        from hexaflow.core.trace_recorder import TraceRecorder
        
        logger.info(f"🎥 开始人工演示录制模式: {goal}")
        task_name = "ManualTask_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        recorder = TraceRecorder(task_name=task_name)
        
        if not context:
            context_options = {'viewport': {'width': 1280, 'height': 800}}
            actual_state = getattr(self, 'state_path', None)
            if (not self.use_cdp) and actual_state and os.path.exists(actual_state):
                logger.info(f"🍪 发现缓存！加载本地浏览器状态: {actual_state}")
                context_options['storage_state'] = actual_state
            context = await self._resolve_context(context=context, context_options=context_options)
            
        page = await context.new_page()
        action_queue = asyncio.Queue()
        
        # 暴露给浏览器的回调函数，用于接收点击事件
        async def on_user_action(action_payload):
            await action_queue.put(action_payload)
            
        await context.expose_function("reportUserAction", on_user_action)
        
        # 注入全局 JS，拦截真实的物理点击
        js_injector = """
        // 注意：这里不需要 () => {} 包裹，add_init_script 会直接按顺序执行这里的代码
        if (!window._hexaRecorderInjected) {
            window._hexaRecorderInjected = true;
            window._hexaLastTypeReport = null;
            
            document.addEventListener('click', (e) => {
                // 如果是脚本代点的，或者是鼠标右键，则忽略
                if (window._isAutomated) return; 
                if (e.button !== 0 || !e.isTrusted) return;
                
                // 🛑 拦截动作，防止页面跳转或触发前端真实逻辑
                e.preventDefault();
                e.stopPropagation();
                
                // 向上寻找有意义的可点击父元素 (处理点击到 icon 内部 <path> 的情况)
                let el = e.target;
                while (el && el.nodeType === 1 && !el.innerText && !el.getAttribute('aria-label') && el.parentElement) {
                    if (['BUTTON', 'A'].includes(el.tagName)) break;
                    el = el.parentElement;
                }
                if (!el || el.nodeType !== 1) el = e.target; // 兜底：退回原始点击元素
                
                // 安全地提取多维指纹
                const fp = {
                    tag_name: el.tagName ? el.tagName.toLowerCase() : "unknown",
                    text: (el.innerText || el.value || "").trim().substring(0, 50).replace(/\\n/g, ' '),
                    aria_label: el.getAttribute ? (el.getAttribute('aria-label') || "") : "",
                    placeholder: el.placeholder || "",
                    classes: (el.classList ? Array.from(el.classList).join(' ') : "")
                };
                
                // 提取用于录制的候选选择器
                let target_selector = "body";
                if (fp.text) target_selector = `text="${fp.text}"`;
                else if (el.id) target_selector = `#${el.id}`;
                else target_selector = fp.tag_name;
                
                // 给当前元素打上临时标记，方便 AI 确认后 Playwright 准确代点
                const tempId = 'hexa-manual-' + Math.random().toString(36).substr(2, 9);
                if (el.setAttribute) el.setAttribute('data-manual-target', tempId);
                
                // 呼叫 Python 端
                if (window.reportUserAction) {
                    window.reportUserAction({
                        action_type: 'click',
                        target_selector: target_selector,
                        exact_selector: `[data-manual-target="${tempId}"]`,
                        fingerprint: fp,
                        url: window.location.href
                    });
                }
            }, { capture: true }); // 使用捕获阶段优先拦截

            // 记录输入动作：在 change 阶段上报，避免每个按键都刷一条
            document.addEventListener('change', (e) => {
                if (window._isAutomated) return;
                if (!e.isTrusted) return;
                const el = e.target;
                if (!el || el.nodeType !== 1) return;

                const tag = el.tagName ? el.tagName.toLowerCase() : '';
                const isEditable = el.isContentEditable || ['input', 'textarea', 'select'].includes(tag);
                if (!isEditable) return;

                let inputValue = '';
                if (el.isContentEditable) inputValue = (el.innerText || '').trim();
                else inputValue = (el.value || '').trim();
                if (!inputValue) return;

                const fp = {
                    tag_name: tag || "unknown",
                    text: (el.innerText || el.value || "").trim().substring(0, 50).replace(/\\n/g, ' '),
                    aria_label: el.getAttribute ? (el.getAttribute('aria-label') || "") : "",
                    placeholder: el.placeholder || "",
                    classes: (el.classList ? Array.from(el.classList).join(' ') : "")
                };

                let target_selector = tag || 'input';
                if (el.id) target_selector = `#${el.id}`;
                else if (fp.aria_label) target_selector = `${tag}[aria-label="${fp.aria_label}"]`;
                else if (fp.placeholder) target_selector = `${tag}[placeholder="${fp.placeholder}"]`;

                // 去抖：相同目标+相同值+相同URL在2秒内不重复上报
                const reportKey = `${window.location.href}|${target_selector}|${inputValue}`;
                const now = Date.now();
                if (window._hexaLastTypeReport && window._hexaLastTypeReport.key === reportKey && now - window._hexaLastTypeReport.ts < 2000) {
                    return;
                }
                window._hexaLastTypeReport = { key: reportKey, ts: now };

                if (window.reportUserAction) {
                    window.reportUserAction({
                        action_type: 'type',
                        target_selector: target_selector,
                        exact_selector: null,
                        input_value: inputValue,
                        fingerprint: fp,
                        url: window.location.href
                    });
                }
            }, { capture: true });
        }
        """
        # 确保每个页面/刷新后都注入拦截器
        # 确保每个页面/刷新后都注入拦截器
        await context.add_init_script(script=js_injector)
        
        # 1. 打开初始网页
        await page.goto(start_url)
        
        # 👇 2. 新增：自动将“打开网页”作为轨迹的第一步悄悄录制下来！
        recorder.record_step(
            current_url=start_url,
            action_type="navigate",
            target=start_url,
            description=f"访问初始网页: {start_url}"
        )
        
        logger.info("\n" + "="*50)
        logger.info(f"👉 浏览器已准备好！请在弹出的页面中开始操作: {start_url}")
        logger.info("="*50 + "\n")
        
        while True:
            # 阻塞等待前端传回的点击数据
            action_data = await action_queue.get()
            fp = action_data.get('fingerprint', {})
            target_sel = action_data.get('target_selector', '')
            exact_sel = action_data.get('exact_selector', '')
            input_value = action_data.get('input_value', None)
            
            if action_data.get('action_type') == 'type':
                logger.info(f"\n⌨️ 检测到你的输入: <{fp.get('tag_name')}> value='{(input_value or '')[:60]}'")
            else:
                logger.info(f"\n⚡ 检测到你的点击: <{fp.get('tag_name')}> '{fp.get('text')}'")
            
            # AI 意图分析
            screenshot_base64 = await self._capture_viewport_screenshot_base64(page)
            analysis = await agent.analyze_manual_action(
                goal,
                action_data,
                screenshot_base64=screenshot_base64,
                screenshot_mime="image/jpeg",
            )
            logger.info(f"💡 AI 意图理解: {analysis.thought}")
            logger.info(f"📝 拟录制描述: {analysis.description}")
            
            loop = asyncio.get_running_loop()
            prompt_msg = (
                "\n👉 录制这步操作吗？"
                "(y: 录制并放行 / o: 设为可选并放行 / "
                "ls: 录制并标记循环开始 / le: 录制并标记循环结束 / "
                "n: 舍弃 / done: 结束录制): "
            )
            user_input = await loop.run_in_executor(None, input, prompt_msg)
            user_input = user_input.strip().lower()
            
            if user_input in ['done', 'd', 'quit']:
                logger.info("🛑 示教录制结束，正在保存轨迹...")
                recorder.save_to_disk()
                if getattr(self, 'state_path', None):
                    os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
                    await context.storage_state(path=self.state_path)
                break
                
            elif user_input in ['y', 'o', '', 'ls', 'le']:
                is_opt = (user_input == 'o')
                loop_marker = None
                if user_input == 'ls':
                    loop_marker = 'start'
                elif user_input == 'le':
                    loop_marker = 'end'
                recorder.record_step(
                    current_url=action_data['url'],
                    action_type=action_data['action_type'],
                    target=target_sel,
                    input_value=input_value,
                    description=analysis.description,
                    is_optional=is_opt,
                    fingerprint_dict=fp,
                    loop_marker=loop_marker,
                    loop_name="main_loop" if loop_marker else None,
                )
                
                if action_data['action_type'] == 'click' and exact_sel:
                    logger.info("✅ 步骤已录制！正在代您执行真实的点击，让页面继续流转...")
                    try:
                        # 开启白名单，绕过我们的拦截器代点，然后关闭白名单
                        await page.evaluate("window._isAutomated = true;")
                        await page.locator(exact_sel).first.click(timeout=3000)
                        await page.evaluate("window._isAutomated = false;")
                    except Exception as e:
                        # 页面如果因点击发生了导航，上面的设 false 可能会报错，这是正常现象，直接忽略
                        logger.debug(f"释放点击动作后续状态变更: {e}")
                else:
                    logger.info("✅ 输入步骤已录制，页面已是用户真实输入后的状态，无需代点。")
            else:
                logger.info("🚫 已舍弃该操作，该点击不会生效，请重新选择目标。")

        await page.close()
