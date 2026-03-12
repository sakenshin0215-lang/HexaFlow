import logging

from playwright.async_api import Page

from hexaflow.browser.token_selector import ensure_quote_token
from hexaflow.tools.dom_parser import DomParser
from hexaflow.tools.tool_runtime import execute_tool


logger = logging.getLogger("HexaEngine")


class EngineActionMixin:
    async def _execute_override_action(
        self, page: Page, action_type: str, target: str = None, input_value: str = None
    ):
        action_type = (action_type or "").strip().lower()
        target = self._canonicalize_runtime_target(target or "") if target else target

        if action_type == "navigate":
            if not target:
                raise Exception("override navigate 缺少 target URL")
            await page.goto(target, wait_until="domcontentloaded", timeout=15000)
            return

        if action_type == "click":
            if not target:
                raise Exception("override click 缺少 target selector")
            await self._robust_click(page, selector=target)
            return

        if action_type == "type":
            if not target:
                raise Exception("override type 缺少 target selector")
            loc, _ = await self._resolve_input_locator(page, target)
            await self._robust_input_text(page, loc, input_value or "", press_enter=False)
            return

        if action_type == "click_type_enter":
            if not target:
                raise Exception("override click_type_enter 缺少 target selector")
            loc, _ = await self._resolve_input_locator(page, target)
            await self._robust_input_text(page, loc, input_value or "", press_enter=True)
            return

        if action_type == "press_enter":
            if target:
                loc = page.locator(target).first
                await loc.click(timeout=5000)
                await loc.press("Enter", timeout=5000)
            else:
                await page.keyboard.press("Enter")
            return

        if action_type == "refresh":
            await page.reload(wait_until="domcontentloaded", timeout=15000)
            return

        if action_type == "summarize":
            # Non-operational action: no DOM interaction by design.
            return

        if action_type == "call_tool":
            tool_name = (target or "").strip() or "summarize_page"
            if not tool_name:
                raise Exception("override call_tool 缺少 tool 名称(target)")
            dom_snapshot = await DomParser.get_interactive_elements(page)
            agent = getattr(self, "_runtime_tool_agent", None)
            goal = getattr(self, "_runtime_tool_goal", "") or "Replay tool call"
            history = getattr(self, "_runtime_tool_history", "") or ""
            summary_text = await execute_tool(
                tool_name=tool_name,
                agent=agent,
                goal=goal,
                history=history,
                current_url=page.url,
                dom_snapshot=dom_snapshot,
                instruction=(input_value or ""),
            )
            logger.info("🛠️ [Tool:%s]\n%s", tool_name, (summary_text or "")[:1200])
            return

        if action_type == "wait_for_timeout":
            delay_ms = 1000
            try:
                if input_value:
                    delay_ms = (
                        int(float(input_value) * 1000)
                        if "." in str(input_value)
                        else int(input_value)
                    )
            except Exception:
                delay_ms = 1000
            await page.wait_for_timeout(max(200, delay_ms))
            return

        if action_type == "ensure_quote_token":
            symbol = (input_value or "USDT").strip().upper()
            selector = target or ".dex-select-value-box button"
            await ensure_quote_token(
                page=page,
                target_symbol=symbol,
                button_selector=selector,
            )
            return

        if action_type == "click_relative":
            raw = (input_value or target or "").strip()
            if not raw:
                raise Exception("override click_relative 缺少 input_value/target，格式应为 'x_ratio,y_ratio'")
            try:
                x_ratio_str, y_ratio_str = [x.strip() for x in raw.split(",", 1)]
                x_ratio = float(x_ratio_str)
                y_ratio = float(y_ratio_str)
            except Exception as e:
                raise Exception(f"override click_relative 参数格式错误: {raw} ({e})")

            x_ratio = max(0.0, min(1.0, x_ratio))
            y_ratio = max(0.0, min(1.0, y_ratio))
            vp = page.viewport_size
            if vp:
                width, height = int(vp["width"]), int(vp["height"])
            else:
                width, height = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
                width, height = int(width), int(height)
            click_x = int(width * x_ratio)
            click_y = int(height * y_ratio)

            async def point_score(x: int, y: int) -> int:
                return await page.evaluate(
                    """([px, py]) => {
                        const el = document.elementFromPoint(px, py);
                        if (!el) return 0;
                        const style = window.getComputedStyle(el);
                        if (!style) return 1;
                        if (style.visibility === 'hidden' || style.display === 'none') return 1;
                        if (style.pointerEvents === 'none') return 1;
                        if (el.tagName === 'HTML' || el.tagName === 'BODY') return 1;
                        let score = 2;
                        const clickableTags = new Set(['BUTTON', 'A', 'INPUT', 'LABEL', 'SELECT', 'TEXTAREA']);
                        if (clickableTags.has(el.tagName)) score += 2;
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        if (role === 'button' || role === 'option' || role === 'tab') score += 2;
                        if (el.closest('button,[role=\"button\"],a,[role=\"option\"],[role=\"tab\"]')) score += 2;
                        return score;
                    }""",
                    [x, y],
                )

            offsets = [0, -6, 6]
            raw_candidates = []
            for dx in offsets:
                for dy in offsets:
                    cx = max(1, min(width - 1, click_x + dx))
                    cy = max(1, min(height - 1, click_y + dy))
                    raw_candidates.append((cx, cy))

            scored = []
            for cx, cy in raw_candidates:
                try:
                    score = await point_score(cx, cy)
                except Exception:
                    score = 0
                scored.append((score, cx, cy))
            scored.sort(reverse=True, key=lambda t: t[0])
            candidates = [(cx, cy) for score, cx, cy in scored if score > 0]
            if not candidates:
                candidates = [(click_x, click_y)]

            last_err = None
            for cx, cy in candidates:
                try:
                    await page.mouse.move(cx, cy)
                    await page.mouse.click(cx, cy, delay=80)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
            if last_err:
                raise last_err
            await page.wait_for_timeout(400)
            return

        if action_type == "click_relative_type_enter":
            raw = (target or "").strip()
            if not raw:
                raise Exception("override click_relative_type_enter 缺少 target，格式应为 'x_ratio,y_ratio'")
            await self._execute_override_action(
                page=page,
                action_type="click_relative",
                target=raw,
                input_value=None,
            )
            await page.keyboard.type(input_value or "", delay=70)
            await page.keyboard.press("Enter")
            return

        raise Exception(f"override 不支持的动作类型: {action_type}")
