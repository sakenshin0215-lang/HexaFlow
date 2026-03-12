from __future__ import annotations

import os
import re
import logging
from typing import Optional

from hexaflow.agents.schemas import ReplayRepairDecision


logger = logging.getLogger("PopupSkillAgent")


class PopupSkillAgent:
    """
    Unified popup/overlay skill agent.
    - Strategy layer: should_handle / propose_repair
    - Runtime layer: dismiss top-most popup, layered modal cleanup, optional LLM PopupHealer
    """

    _BLOCKING_MARKERS = [
        "intercepts pointer events",
        "intercepted",
        "overlay",
        "modal",
        "popup",
        "mask",
        "dialog",
        "not visible",
        "timeout",
        "no_progress",
        "robust click failed",
    ]

    _CLOSE_TEXTS = [
        "关闭",
        "跳过",
        "我知道了",
        "我已知晓",
        "知道了",
        "好的",
        "close",
        "dismiss",
        "skip",
        "cancel",
        "got it",
    ]

    def __init__(self):
        self._popup_healer = None

    # ---------- Strategy layer ----------
    def should_handle(self, *, last_error: str, dom_snapshot: str) -> bool:
        err = (last_error or "").lower()
        dom = (dom_snapshot or "").lower()
        return any(k in err for k in self._BLOCKING_MARKERS) or any(
            k in dom for k in ["role=\"dialog\"", "aria-modal", "dex-dialog", "modal", "popup"]
        )

    @staticmethod
    def _extract_hexa_id(line: str) -> Optional[str]:
        m = re.search(r"\[ID:\s*(hexa-\d+)\]", line, re.IGNORECASE)
        return m.group(1) if m else None

    def _find_close_selector(self, dom_snapshot: str) -> Optional[str]:
        dom = dom_snapshot or ""
        # Prefer close-like lines in DOM snapshot and map to stable hexa-id first.
        for line in dom.splitlines():
            line_l = line.lower()
            if any(t in line_l for t in self._CLOSE_TEXTS) or "close" in line_l:
                hid = self._extract_hexa_id(line)
                if hid:
                    return f'[hexa-id="{hid}"]'

        # Generic fallback selectors.
        return (
            "button[aria-label='Close'],"
            "button[aria-label='关闭'],"
            "[role='button']:has-text('关闭'),"
            "button:has-text('关闭'),"
            "[role='button']:has-text('我知道了'),"
            "button:has-text('我知道了')"
        )

    def propose_repair(
        self,
        *,
        failed_target: str,
        dom_snapshot: str,
    ) -> Optional[ReplayRepairDecision]:
        selector = self._find_close_selector(dom_snapshot)
        if not selector:
            return None
        return ReplayRepairDecision(
            thought=(
                "Popup skill agent detected blocking modal/overlay and proposes closing "
                "the top-most blocking layer first."
            ),
            strategy="replace_action",
            action_type="click",
            target=selector,
            confidence=0.9,
        )

    # ---------- Runtime layer ----------
    async def dismiss_topmost_popup_once(self, page) -> bool:
        try:
            selector = await page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 8 && r.height > 8;
                    };
                    const zScore = (el) => {
                        const s = window.getComputedStyle(el);
                        const z = parseInt(s.zIndex || '0', 10);
                        return Number.isFinite(z) ? z : 0;
                    };
                    const bad = /下一步|上一步|next|previous/i;
                    const goodText = /关闭|跳过|我知道了|知道了|好的|ok|got\\s*it|close|dismiss/i;
                    const goodIcon = /close|okds-close|icon-close|dismiss/i;

                    const nodes = Array.from(document.querySelectorAll(
                        "dialog,[role='dialog'],[aria-modal='true'],.dex-dialog,.dex-dialog-container,[data-testid*='popup' i],[class*='modal' i]"
                    )).filter(isVisible);
                    if (!nodes.length) return '';

                    nodes.sort((a, b) => zScore(b) - zScore(a));

                    for (const root of nodes) {
                        const clickables = Array.from(
                            root.querySelectorAll("button,[role='button'],a,.icon-close,.dex-okds-close,.close")
                        ).filter(isVisible);
                        for (const el of clickables) {
                            const txt = ((el.innerText || el.textContent || '').trim());
                            if (bad.test(txt)) continue;
                            const cls = (el.className || '').toString();
                            const aria = (el.getAttribute('aria-label') || '');
                            const closeLike =
                                txt === '×' || txt === '✕' || txt === 'x' || txt === 'X' ||
                                goodText.test(txt) || goodIcon.test(cls) || goodIcon.test(aria);
                            if (!closeLike) continue;
                            el.setAttribute('data-hexa-topmost-close', '1');
                            return '[data-hexa-topmost-close="1"]';
                        }
                    }
                    return '';
                }"""
            )
            if not selector:
                return False
            await page.locator(selector).first.click(timeout=1800, force=True)
            await page.wait_for_timeout(220)
            return True
        except Exception:
            return False

    async def dismiss_blocking_modal(self, page) -> bool:
        closed_any = False
        for _ in range(4):
            ok = await self.dismiss_topmost_popup_once(page)
            if not ok:
                break
            closed_any = True
        if closed_any:
            logger.info("🧩 [ModalClose] 已按 z-index 连续关闭上层弹窗")
            return True

        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(180)
        except Exception:
            pass

        close_selectors = [
            "button:has-text('关闭')",
            "button:has-text('跳过')",
            "button:has-text('Skip')",
            "button[aria-label*='close' i]",
            "button[aria-label*='关闭' i]",
            "[role='button'][aria-label*='close' i]",
            "[role='button'][aria-label*='关闭' i]",
            "i[aria-label*='close' i]",
            "i[aria-label*='关闭' i]",
            ".icon-close",
            ".close",
        ]
        forbidden = ["下一步", "Next"]
        for sel in close_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                for i in range(count):
                    cand = loc.nth(i)
                    if not await cand.is_visible():
                        continue
                    text = (await cand.inner_text() or "").strip()
                    if any(k in text for k in forbidden):
                        continue
                    await cand.click(timeout=2200, force=True)
                    await page.wait_for_timeout(260)
                    return True
            except Exception:
                continue
        return False

    async def try_popup_healer(self, page, *, enabled: bool = False) -> bool:
        if not enabled:
            return False
        try:
            if self._popup_healer is None:
                from hexaflow.agents.skills.popup.popup_healer import PopupHealer

                self._popup_healer = PopupHealer(
                    model_name=os.getenv("POPUP_HEALER_MODEL", "openai/gpt-oss-120b"),
                    api_key=os.getenv("POPUP_HEALER_APIKEY"),
                    base_url=os.getenv("POPUP_HEALER_BASE_URL"),
                )
            logger.info("🧰 [PopupHealer] 尝试清理遮挡弹窗...")
            ok = await self._popup_healer.heal(page)
            if ok:
                logger.info("✅ [PopupHealer] 弹窗助手处理成功")
                return True
            logger.info("ℹ️ [PopupHealer] 未成功处理，转 AI 修复")
            return False
        except Exception as e:
            logger.warning(f"⚠️ [PopupHealer] 调用失败，转 AI 修复: {e}")
            return False

    @staticmethod
    def extract_text_hint_from_selector(selector: str) -> str:
        raw = (selector or "").strip()
        if not raw:
            return ""
        m = re.search(r"has-text\((['\"])(.*?)\1\)", raw)
        if m:
            return (m.group(2) or "").strip()
        m = re.search(r'text\s*=\s*["\']([^"\']+)["\']', raw)
        if m:
            return (m.group(1) or "").strip()
        return ""

    async def is_target_text_inside_dialog(self, page, target: str) -> bool:
        txt = self.extract_text_hint_from_selector(target)
        if not txt:
            return False
        try:
            dialog_selector = (
                f'dialog:has-text("{txt}"), '
                f'[role="dialog"]:has-text("{txt}"), '
                f'[aria-modal="true"]:has-text("{txt}")'
            )
            return await page.locator(dialog_selector).first.is_visible(timeout=400)
        except Exception:
            return False
