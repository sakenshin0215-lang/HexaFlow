import asyncio
import logging
from playwright.async_api import async_playwright, Page, BrowserContext
from hexaflow.core.trace_recorder import TraceRecorder
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
    def __init__(self, headless: bool = False):
        """
        初始化执行引擎
        :param headless: 是否无头模式运行。调试时建议设为 False 观看浏览器动作
        """
        self.headless = headless
        self.playwright = None
        self.browser = None
        
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

    async def run_dynamic_task(self, goal: str, agent, context: BrowserContext = None, max_steps: int = 15):
        from datetime import datetime
        logger.info(f"▶️ 开始执行动态自适应任务: {goal}")
        
        task_name = "Task_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        recorder = TraceRecorder(task_name=task_name)
        
        healer = PopupHealer()

        if not context:
            context = await self.browser.new_context(viewport={'width': 1280, 'height': 800})
            
        page = await context.new_page()
        await page.goto("about:blank")
        
        action_history = []
        step_count = 0

        while step_count < max_steps:
            step_count += 1
            logger.info(f"\n" + "="*40 + f"\n--- 第 {step_count} 步 ---")
            
            current_url = page.url
            await page.wait_for_timeout(1000) 
            dom_snapshot = await DomParser.get_interactive_elements(page)
            
            history_str = "\n".join(action_history)
            next_action = await agent.decide_next_action(goal, history_str, current_url, dom_snapshot)
            
            stable_target = next_action.target

            if next_action.action_type == "done":
                logger.info("🎉 AI 认为任务已完成！")
            else:
                logger.info(f"⚡ 自动执行: [{next_action.action_type}] 目标: {next_action.target}")
                
                max_attempts = 4
                action_success = False
                
                for attempt in range(max_attempts):
                    try:
                        if next_action.target and "hexa-id" in next_action.target:
                            stable_target = await DomParser.get_stable_selector(page, next_action.target)
                            
                        if next_action.action_type == "navigate":
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
            
            # ==========================================
            # 人工审核与录制落盘
            # ==========================================
            import asyncio
            loop = asyncio.get_running_loop()
            prompt_msg = "\n👉 操作对吗？(y: 对 / o: 对，但设为【可选跳过】 / n: 错 / done: 结束): "
            user_input = await loop.run_in_executor(None, input, prompt_msg)
            user_input = user_input.strip().lower()

            if user_input in ['done', 'd', 'quit']:
                logger.info("🛑 任务结束，正在保存轨迹...")
                if next_action.action_type != "done":
                    recorder.record_step(current_url, next_action.action_type, stable_target, next_action.input_value, next_action.thought)
                recorder.save_to_disk()
                break
                
            elif user_input == 'o':
                logger.info("✅ 验收通过，并标记为【可选步骤 (Optional)】。")
                if next_action.action_type != "done":
                    action_history.append(current_action_log + " -> [SUCCESS (OPTIONAL)] - Action completed. DO NOT repeat this target. Move to the next step.")
                    recorder.record_step(current_url, next_action.action_type, stable_target, next_action.input_value, next_action.thought, is_optional=True)
                continue
                
            elif user_input == 'n':
                logger.warning("🚫 动作标记为错误，不录制。")
                action_history.append(current_action_log + " -> [USER MARKED AS INCORRECT] - Do not repeat this.")
                continue
                
            elif user_input != 'y' and user_input != '':
                action_history.append(current_action_log + f" -> [USER HINT: {user_input}]")
                continue

            else:
                logger.info("✅ 验收通过，已录制到暂存区。")
                if next_action.action_type != "done":
                    action_history.append(current_action_log + " -> [SUCCESS] - Action completed. DO NOT repeat this target. Move to the next step.")
                    recorder.record_step(current_url, next_action.action_type, stable_target, next_action.input_value, next_action.thought, is_optional=False)

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
        # 2. 常规动作前置校验 (Pre-check)
        # ==================================
        if pre.expected_url_contains:
            logger.info(f"   🔍 动作前校验 URL 包含: {pre.expected_url_contains}")
            try:
                await page.wait_for_url(f"**/*{pre.expected_url_contains}*", timeout=pre.timeout_ms)
            except Exception:
                if pre.expected_url_contains not in page.url:
                    raise Exception(f"URL 校验失败。当前: {page.url}")

        if pre.expected_dom_selector and pre.expected_dom_selector != "body":
            logger.info(f"   🔍 动作前校验 DOM 元素: {pre.expected_dom_selector}")
            await page.wait_for_selector(pre.expected_dom_selector, timeout=pre.timeout_ms)

        # ==================================
        # 3. 稳健的拟人化动作执行 (Action)
        # ==================================
        logger.info(f"   ⚙️ 动作: {act.action_type} -> {act.target}")
        
        humanoid_delay = random.uniform(1.5, 3.0) * 1000
        await page.wait_for_timeout(humanoid_delay) 
        
        if act.action_type == "click":
            locator = page.locator(act.target).first
            await locator.hover(timeout=5000)
            await page.wait_for_timeout(300)
            await locator.click(timeout=5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass
            
        elif act.action_type == "type":
            locator = page.locator(act.target).first
            await locator.hover(timeout=5000)
            await locator.press_sequentially(act.input_value, delay=100, timeout=5000)
            
        elif act.action_type == "wait_for_timeout":
            await page.wait_for_timeout(1000)

    async def run_from_trace(self, trace_path: str, context=None):
        """
        克隆回放模式：读取本地 JSON 轨迹，脱离大模型，进行高速确定性执行。
        """
        import os
        from hexaflow.agents.planner import WorkflowBlueprint
        
        if not os.path.exists(trace_path):
            raise FileNotFoundError(f"找不到轨迹文件: {trace_path}")
            
        logger.info(f"📂 正在加载黄金轨迹: {trace_path}")
        with open(trace_path, 'r', encoding='utf-8') as f:
            trace_data = f.read()
        
        # 利用 Pydantic 的反序列化能力，将 JSON 文本瞬间转为强类型对象
        blueprint = WorkflowBlueprint.model_validate_json(trace_data)
        logger.info(f"▶️ 开始回放任务: {blueprint.task_name} (共 {len(blueprint.steps)} 步)")

        # 如果没有传入账号上下文，就新建一个（未来多账号并发就是在这里传不同的 context 进来）
        if not context:
            context = await self.browser.new_context(viewport={'width': 1280, 'height': 800})
            
        page = await context.new_page()

        healer = PopupHealer()

        for step in blueprint.steps:
            max_attempts = 2 # 允许失败重试 1 次
            for attempt in range(max_attempts):
                try:
                    await self._execute_deterministic_step(page, step)
                    break # 成功执行，跳出重试循环
                    
                except Exception as e:
                    error_msg = str(e).lower()
                    # 如果是被弹窗遮挡、不可点击或超时，触发自愈！
                    if getattr(step, 'is_optional', False) and ("timeout" in error_msg or "not visible" in error_msg):
                        logger.info(f"⏭️ [可选步骤] 元素未出现，安全跳过: {step.step_id}")
                        break

                    if "timeout" in error_msg or "intercepted" in error_msg or "not visible" in error_msg:
                        logger.warning(f"🛑 步骤 [{step.step_id}] 受阻。原因: 元素不可操作或超时。")
                        
                        if attempt < max_attempts - 1:
                            logger.info("🚑 [回放阶段] 正在唤醒 Healer Agent 扫描异常弹窗...")
                            await healer.heal(page)
                            
                            logger.info("✨ Healer 处理完毕。给前端动画一点时间，即将强行重试主线动作...")
                            await page.wait_for_timeout(2000) 
                            continue # 强制重试当前步骤！
                        else:
                            logger.error("❌ 已达到最大重试次数，主线动作依然失败。")
                            raise e
                    else:
                        # 如果是网络断开等底层错误，直接抛出
                        raise e
                
        logger.info("🎉 轨迹回放圆满完成！")
        # loop = asyncio.get_running_loop()
        # await loop.run_in_executor(None, input, "\n👉 回放已完成，请查看浏览器现场。按【回车键】关闭浏览器并结束任务...")
        # await page.close()
