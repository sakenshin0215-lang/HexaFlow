import asyncio
import logging
import os

from playwright.async_api import BrowserContext, async_playwright

from hexaflow.browser.cdp_runtime import CDPConfig, ensure_cdp_browser
from hexaflow.core.engine_action_mixin import EngineActionMixin
from hexaflow.core.engine_ai_heal_mixin import EngineAIHealMixin
from hexaflow.core.engine_dynamic_mixin import EngineDynamicMixin
from hexaflow.core.engine_element_monitor_mixin import EngineElementMonitorMixin
from hexaflow.core.engine_guard_mixin import EngineGuardMixin
from hexaflow.core.engine_loop_mixin import EngineLoopMixin
from hexaflow.core.engine_record_mixin import EngineRecordMixin
from hexaflow.core.engine_replay_mixin import EngineReplayMixin
from hexaflow.core.engine_support_mixin import EngineSupportMixin
from hexaflow.core.run_reporter import RunReporter
from hexaflow.core.state_machine import StateMachine


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] Engine: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("HexaEngine")


class HexaEngine(
    EngineGuardMixin,
    EngineElementMonitorMixin,
    EngineActionMixin,
    EngineAIHealMixin,
    EngineDynamicMixin,
    EngineReplayMixin,
    EngineLoopMixin,
    EngineRecordMixin,
    EngineSupportMixin,
):
    def __init__(
        self,
        headless: bool = False,
        state_path: str = "workspace/auth_state.json",
        runtime_db_path: str = "workspace/state/runtime.db",
    ):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.state_path = state_path
        self.state_machine = StateMachine(db_path=runtime_db_path)
        self.run_reporter = RunReporter(self.state_machine)
        self.use_cdp = False
        self.cdp_config = None
        self.disable_fallback_recovery_runtime = False
        self.enable_popup_healer = os.getenv("ENABLE_POPUP_HEALER", "0") == "1"
        self.popup_healer = None

    async def start(
        self,
        use_cdp: bool = False,
        cdp_config: CDPConfig = None,
        cdp_start_url: str = "about:blank",
    ):
        logger.info("🚀 正在启动 Playwright 引擎...")
        self.playwright = await async_playwright().start()
        self.use_cdp = use_cdp

        if use_cdp:
            self.cdp_config = cdp_config or CDPConfig()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, ensure_cdp_browser, self.cdp_config, cdp_start_url)
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_config.endpoint)
            logger.info(
                f"✅ 已通过CDP连接浏览器: {self.cdp_config.endpoint} | "
                f"user_data_dir={self.cdp_config.user_data_dir} | "
                f"profile={self.cdp_config.profile_directory}"
            )
            return

        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        logger.info("✅ 浏览器启动成功 (Playwright launch)")

    async def stop(self):
        action_task = getattr(self, "_element_sidecar_action_task", None)
        if action_task is not None:
            action_task.cancel()
            try:
                await action_task
            except BaseException:
                pass
        sidecar = getattr(self, "_element_sidecar", None)
        if sidecar is not None:
            try:
                sidecar.stop()
            except Exception:
                pass
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

    async def _resolve_context(self, context: BrowserContext = None, context_options: dict = None):
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
