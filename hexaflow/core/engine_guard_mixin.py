import asyncio
import logging

from playwright.async_api import Page


logger = logging.getLogger("HexaEngine")


class EngineGuardMixin:
    async def _detect_human_intervention_reason(self, page: Page) -> str:
        url = (page.url or "").lower()
        if any(k in url for k in ["login", "signin", "auth", "verify", "captcha", "challenge"]):
            return f"URL 命中登录/验证路径: {page.url}"

        # 非认证路径下仅检查高置信度人工介入信号
        text_checks = [
            "重新登录",
            "验证码",
            "人机验证",
            "二次验证",
            "双重验证",
            "Verify",
            "CAPTCHA",
            "2FA",
            "Authentication challenge",
            "解锁钱包",
            "Unlock",
            "Wallet password",
            "请输入密码",
        ]
        for t in text_checks:
            try:
                if await page.get_by_text(t, exact=False).first.is_visible(timeout=800):
                    return f"页面检测到人工验证文案: {t}"
            except Exception:
                continue
        return ""

    async def _human_handoff_if_needed(
        self, page: Page, checkpoint: str = "", enabled: bool = True
    ) -> bool:
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

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input, "[等待人工] 完成后按回车继续... ")
        return True

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

            handled = await self._human_handoff_if_needed(
                page,
                checkpoint=f"PageGuard step={step.step_id}",
                enabled=True,
            )
            if handled and required_url in (page.url or ""):
                return

            used_actions = await self._run_mismatch_actions(page, step)
            if used_actions and required_url in (page.url or ""):
                return

            inferred = self._normalize_url_candidate(required_url)
            if inferred:
                logger.info(f"🧭 [PageGuard] 自动回跳到目标页面: {inferred}")
                await page.goto(inferred, wait_until="domcontentloaded", timeout=12000)
                await page.wait_for_timeout(800)
                if required_url in (page.url or ""):
                    return

        raise Exception(
            f"PageGuard failed: step={step.step_id}, expect contains='{required_url}', current='{page.url}'"
        )
