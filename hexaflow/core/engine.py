import os
import asyncio
import logging
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Page, BrowserContext
from hexaflow.core.trace_recorder import TraceRecorder
from hexaflow.core.state_machine import StateMachine
from hexaflow.core.run_reporter import RunReporter
from hexaflow.core.task_schema import TaskSpec
from hexaflow.tools.dom_parser import DomParser
from hexaflow.agents.react_agent import NextAction
from hexaflow.tools.popup_healer import PopupHealer

import random

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
        
    async def start(self):
        """启动浏览器环境"""
        logger.info("🚀 正在启动 Playwright 引擎...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        logger.info("✅ 浏览器启动成功")

    async def stop(self):
        """关闭浏览器环境"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("🛑 引擎已关闭")

    async def _save_browser_state(self, context: BrowserContext):
        if self.state_path:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            await context.storage_state(path=self.state_path)
            logger.info(f"💾 浏览器状态(Cookie/缓存)已永久保存至: {self.state_path}")

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

    async def run_task_from_spec(self, spec_input, agent, context: BrowserContext = None):
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
    ):
        from datetime import datetime
        logger.info(f"▶️ 开始执行动态自适应任务: {goal} (manual_review={manual_review})")
        allowed_domains = allowed_domains or []
        blocked_keywords = blocked_keywords or []
        
        task_name = "Task_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        recorder = TraceRecorder(task_name=task_name)
        
        healer = PopupHealer()

        if not context:
            context_options = {'viewport': {'width': 1280, 'height': 800}}
            # 如果存在历史状态文件，则作为“记忆”注入到新浏览器中
            if self.state_path and os.path.exists(self.state_path):
                logger.info(f"🍪 发现缓存！正在加载本地浏览器状态: {self.state_path}")
                context_options['storage_state'] = self.state_path
                
            context = await self.browser.new_context(**context_options)

        page = await context.new_page()
        await page.goto(start_url)
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
            
            history_str = "\n".join(action_history)
            next_action = await agent.decide_next_action(goal, history_str, current_url, dom_snapshot)
            
            stable_target = next_action.target
            current_action_log = f"Skipped unknown action: {next_action.action_type}"
            action_success = False

            if next_action.action_type == "done":
                logger.info("🎉 AI 认为任务已完成！")
                recorder.save_to_disk()
                trace_saved = True
                await self._save_browser_state(context)
                break
            else:
                action_raw_text = f"{next_action.action_type} {next_action.target or ''} {next_action.thought or ''}"
                if self._contains_blocked_keyword(action_raw_text, blocked_keywords):
                    current_action_log = f"BLOCKED by keyword policy: {next_action.action_type} on {next_action.target}"
                    logger.warning(f"🛡️ 动作已拦截: {current_action_log}")
                    action_success = False
                else:
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
                                locator = page.locator(stable_target).first
                                await locator.hover(timeout=5000)
                                await page.wait_for_timeout(300)
                                await locator.click(timeout=5000)
                                
                            elif next_action.action_type == "type":
                                locator = page.locator(stable_target).first
                                await locator.hover(timeout=5000)
                                await locator.press_sequentially(next_action.input_value, delay=100, timeout=5000)
                                
                            await page.wait_for_timeout(1500) 
                            current_action_log = f"Executed {next_action.action_type} on {stable_target}"
                            action_success = True
                            break
                            
                        except Exception as e:
                            error_msg = str(e).lower()
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
            else:
                consecutive_failures += 1

            if not manual_review:
                if action_success:
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
                    break

        if not trace_saved:
            logger.info("💾 达到步数上限或流程自然结束，自动保存当前轨迹。")
            recorder.save_to_disk()
            await self._save_browser_state(context)

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
                
            # 3. 动态决定使用哪个账号的缓存数据
            # 如果传了 state_path 就用传的，没传就用引擎初始化的 self.state_path
            actual_state = state_path if state_path is not None else getattr(self, 'state_path', None)
            
            if actual_state and os.path.exists(actual_state):
                logger.info(f"🍪 发现缓存！正在加载本地浏览器状态: {actual_state}")
                context_options['storage_state'] = actual_state
            else:
                logger.info("✨ 未使用缓存，正以全新无痕环境启动...")
                
            context = await self.browser.new_context(**context_options)

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

        healer = PopupHealer()

        for step_index, step in enumerate(blueprint.steps[start_index:], start=start_index):
            self.state_machine.mark_step_started(run_state.run_id, step_index, step.step_id)
            max_attempts = 4  # 主线重试 + 多层恢复
            for attempt in range(max_attempts):
                try:
                    await self._execute_deterministic_step(page, step)
                    self.state_machine.mark_step_success(run_state.run_id, step_index, step.step_id)
                    break # 成功执行，跳出重试循环
                    
                except Exception as e:
                    error_msg = str(e).lower()
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
            if actual_state and os.path.exists(actual_state):
                logger.info(f"🍪 发现缓存！加载本地浏览器状态: {actual_state}")
                context_options['storage_state'] = actual_state
            context = await self.browser.new_context(**context_options)
            
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
            
            logger.info(f"\n⚡ 检测到你的点击: <{fp.get('tag_name')}> '{fp.get('text')}'")
            
            # AI 意图分析
            analysis = await agent.analyze_manual_action(goal, action_data)
            logger.info(f"💡 AI 意图理解: {analysis.thought}")
            logger.info(f"📝 拟录制描述: {analysis.description}")
            
            loop = asyncio.get_running_loop()
            prompt_msg = "\n👉 录制这步操作吗？(y: 录制并放行 / o: 设为可选并放行 / n: 舍弃 / done: 结束录制): "
            user_input = await loop.run_in_executor(None, input, prompt_msg)
            user_input = user_input.strip().lower()
            
            if user_input in ['done', 'd', 'quit']:
                logger.info("🛑 示教录制结束，正在保存轨迹...")
                recorder.save_to_disk()
                if getattr(self, 'state_path', None):
                    os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
                    await context.storage_state(path=self.state_path)
                break
                
            elif user_input in ['y', 'o', '']:
                is_opt = (user_input == 'o')
                recorder.record_step(
                    current_url=action_data['url'],
                    action_type=action_data['action_type'],
                    target=target_sel,
                    input_value=None,
                    description=analysis.description,
                    is_optional=is_opt,
                    fingerprint_dict=fp
                )
                
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
                logger.info("🚫 已舍弃该操作，该点击不会生效，请重新选择目标。")

        await page.close()
