import os
import re
import asyncio
import hashlib
import logging
import time
from pathlib import Path

from playwright.async_api import Page

from hexaflow.tools.dom_parser import DomParser


logger = logging.getLogger("HexaEngine")


class EngineSupportMixin:
    @staticmethod
    def _canonicalize_runtime_target(target: str) -> str:
        t = (target or "").strip()
        if not t:
            return t
        m_text = re.match(r"""^\s*\[\s*text\s*=\s*(['"])(.+?)\1\s*\]\s*$""", t, re.IGNORECASE)
        if m_text:
            txt = (m_text.group(2) or "").replace('"', '\\"').strip()
            if txt:
                return f'text="{txt}"'
        m = re.search(r"\bhexa-(\d+)\b", t, re.IGNORECASE)
        if m:
            return f'[hexa-id="hexa-{m.group(1)}"]'
        m2 = re.search(r"""\[\s*id\s*=\s*["'](hexa-\d+)["']\s*\]""", t, re.IGNORECASE)
        if m2:
            return f'[hexa-id="{m2.group(1)}"]'
        return t

    @staticmethod
    def _trace_wildcard_to_regex_text(raw_text: str) -> str:
        """
        Trace 通配符语法：
        - 使用 *** 表示任意动态片段
        例如：买入 *** RIVER ($***)
        """
        txt = raw_text or ""
        escaped = re.escape(txt)
        # 将 \*\*\* 还原为非贪婪通配
        pattern = escaped.replace(r"\*\*\*", ".*?")
        return pattern

    def _apply_trace_wildcard_selector(self, selector: str) -> str:
        """
        将 trace 中带 *** 的动态 selector 转为 Playwright 可执行的 regex selector。
        目前支持：
        1) text="...***..."
        2) text='...***...'
        3) tag:has-text("...***...")
        4) tag:has-text('...***...')
        5) 纯文本中包含 ***（按 text 正则处理）
        """
        raw = (selector or "").strip()
        if not raw or "***" not in raw:
            return raw

        # text="..."/text='...'
        m_text = re.match(r"""^\s*text\s*=\s*(['"])(.*)\1\s*$""", raw)
        if m_text:
            inner = m_text.group(2)
            pattern = self._trace_wildcard_to_regex_text(inner)
            return f"text=/{pattern}/i"

        # tag:has-text("...") / tag:has-text('...')
        m_has_text = re.match(
            r"""^\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*:\s*has-text\((['"])(.*)\2\)\s*$""",
            raw,
        )
        if m_has_text:
            tag = m_has_text.group(1)
            inner = m_has_text.group(3)
            pattern = self._trace_wildcard_to_regex_text(inner)
            # 用 tag + text 正则组合，避免 has-text 字符串精确匹配失效
            return f"{tag} >> text=/{pattern}/i"

        # 兜底：当作 text 正则
        pattern = self._trace_wildcard_to_regex_text(raw)
        return f"text=/{pattern}/i"

    @staticmethod
    def _remove_previous_screenshots(screenshot_dir: str, prefix: str):
        """
        覆盖式截图：每次新截图前删除同类历史截图，避免目录持续膨胀。
        """
        try:
            p = Path(screenshot_dir)
            if not p.exists():
                return
            for f in p.glob(f"{prefix}*.png"):
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    continue
        except Exception:
            pass

    def _cleanup_screenshot_cache(self, screenshot_dir: str = "workspace/screenshots"):
        """
        轻量自动清理截图目录：
        - 删除超过保留天数的文件
        - 超过最大文件数时删除最旧文件
        """
        interval_sec = int(os.getenv("SCREENSHOT_CLEANUP_INTERVAL_SEC", "120"))
        max_files = int(os.getenv("SCREENSHOT_MAX_FILES", "400"))
        retention_days = int(os.getenv("SCREENSHOT_RETENTION_DAYS", "3"))

        now = time.time()
        last_ts = float(getattr(self, "_last_screenshot_cleanup_ts", 0.0) or 0.0)
        if now - last_ts < max(10, interval_sec):
            return
        setattr(self, "_last_screenshot_cleanup_ts", now)

        try:
            p = Path(screenshot_dir)
            p.mkdir(parents=True, exist_ok=True)
            files = [f for f in p.glob("*.png") if f.is_file()]
            if not files:
                return

            removed = 0
            # 1) 删除过期文件
            if retention_days > 0:
                expire_before = now - retention_days * 86400
                for f in files:
                    try:
                        if f.stat().st_mtime < expire_before:
                            f.unlink(missing_ok=True)
                            removed += 1
                    except Exception:
                        continue

            # 2) 文件数超限时，删除最旧文件
            files = [f for f in p.glob("*.png") if f.is_file()]
            if max_files > 0 and len(files) > max_files:
                files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                for f in files[max_files:]:
                    try:
                        f.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        continue

            if removed > 0:
                logger.info(
                    f"🧹 [Screenshots] 自动清理完成: removed={removed}, keep_max={max_files}, retention_days={retention_days}"
                )
        except Exception as e:
            logger.debug(f"[Screenshots] cleanup skipped: {e}")

    async def _capture_suspend_snapshot(self, page: Page, run_id: str, step_id: str) -> str:
        from datetime import datetime
        screenshot_dir = "workspace/screenshots"
        os.makedirs(screenshot_dir, exist_ok=True)
        self._remove_previous_screenshots(screenshot_dir, "suspend_")
        self._cleanup_screenshot_cache(screenshot_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_step = (step_id or "unknown").replace("/", "_").replace(" ", "_")
        screenshot_path = os.path.join(screenshot_dir, f"suspend_{run_id}_{safe_step}_{ts}.png")
        try:
            await self._wait_for_page_settle(page)
            use_full_page = await self._should_use_full_page_screenshot(page)
            await page.screenshot(path=screenshot_path, full_page=use_full_page, scale="css")
            return screenshot_path
        except Exception:
            return ""

    async def _capture_heal_visual_pack(self, page: Page, run_id: str, step_id: str) -> list[str]:
        """
        修复阶段视觉包：
        1) 视口截图（优先，弹窗最清晰）
        2) 全页截图（若像素可控）
        3) 视口中央裁剪
        4) 右下角裁剪（常见浮层/确认弹窗区域）
        """
        from datetime import datetime
        screenshot_dir = "workspace/screenshots"
        os.makedirs(screenshot_dir, exist_ok=True)
        self._remove_previous_screenshots(screenshot_dir, "heal_")
        self._cleanup_screenshot_cache(screenshot_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_step = (step_id or "unknown").replace("/", "_").replace(" ", "_")
        prefix = os.path.join(screenshot_dir, f"heal_{run_id}_{safe_step}_{ts}")
        paths: list[str] = []

        try:
            await self._wait_for_page_settle(page)
            viewport_path = f"{prefix}_viewport.png"
            await page.screenshot(path=viewport_path, full_page=False, scale="css")
            paths.append(viewport_path)
        except Exception:
            pass

        try:
            if await self._should_use_full_page_screenshot(page):
                full_path = f"{prefix}_full.png"
                await page.screenshot(path=full_path, full_page=True, scale="css")
                paths.append(full_path)
        except Exception:
            pass

        try:
            vp = page.viewport_size or {}
            width = int(vp.get("width", 0))
            height = int(vp.get("height", 0))
            if width > 100 and height > 100:
                # center crop
                cw = max(220, int(width * 0.55))
                ch = max(220, int(height * 0.55))
                cx = max(0, int((width - cw) / 2))
                cy = max(0, int((height - ch) / 2))
                center_path = f"{prefix}_center.png"
                await page.screenshot(
                    path=center_path,
                    scale="css",
                    clip={"x": cx, "y": cy, "width": cw, "height": ch},
                )
                paths.append(center_path)

                # bottom-right crop
                rw = max(220, int(width * 0.45))
                rh = max(180, int(height * 0.45))
                rx = max(0, width - rw)
                ry = max(0, height - rh)
                br_path = f"{prefix}_bottom_right.png"
                await page.screenshot(
                    path=br_path,
                    scale="css",
                    clip={"x": rx, "y": ry, "width": rw, "height": rh},
                )
                paths.append(br_path)
        except Exception:
            pass

        return paths

    async def _should_use_full_page_screenshot(self, page: Page) -> bool:
        max_pixels = int(os.getenv("SCREENSHOT_MAX_PIXELS", "33177600"))
        try:
            dims = await page.evaluate(
                """() => {
                    const de = document.documentElement || {};
                    const body = document.body || {};
                    const width = Math.max(
                        de.scrollWidth || 0,
                        body.scrollWidth || 0,
                        window.innerWidth || 0
                    );
                    const height = Math.max(
                        de.scrollHeight || 0,
                        body.scrollHeight || 0,
                        window.innerHeight || 0
                    );
                    return { width, height };
                }"""
            )
            width = int(dims.get("width", 0))
            height = int(dims.get("height", 0))
            pixels = width * height
            if width <= 0 or height <= 0:
                return False
            if pixels > max_pixels:
                logger.warning(
                    f"⚠️ 全页截图像素过大({pixels})，超过上限({max_pixels})，自动降级为视口截图"
                )
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def _normalize_snapshot_for_stability(dom_snapshot: str) -> str:
        lines = []
        for raw in (dom_snapshot or "").splitlines():
            line = re.sub(r"\[ID:\s*hexa-\d+\]\s*", "", raw).strip()
            if not line:
                continue
            lowered = line.lower()
            if any(token in lowered for token in [
                'text="下一步"', 'text="跳过"', 'text="关闭"', 'text="知道了"',
                'text="稍后"', 'text="以后再说"', 'aria-label="关闭"', 'aria-label="close"',
            ]):
                continue
            # 归一化实时波动文本（价格/倒计时/区块高度等），避免误判“状态已推进”
            line = re.sub(r"\$?\d+(?:\.\d+)?", "<num>", line)
            line = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "<time>", line)
            lines.append(line)
        return "\n".join(lines[:120])

    async def _wait_for_page_settle(
        self,
        page: Page,
        timeout_ms: int = 3200,
        sample_interval_ms: int = 250,
        stable_rounds: int = 2,
    ):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 2000))
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=1200)
        except Exception:
            pass

        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        last_signature = None
        stable_count = 0

        while asyncio.get_running_loop().time() < deadline:
            try:
                current_url = page.url or ""
                dom_snapshot = await DomParser.get_interactive_elements(page)
                normalized = self._normalize_snapshot_for_stability(dom_snapshot)
                signature = hashlib.sha1(
                    f"{current_url}\n{normalized}".encode("utf-8", errors="ignore")
                ).hexdigest()
                if signature == last_signature and normalized:
                    stable_count += 1
                else:
                    stable_count = 0
                    last_signature = signature
                if stable_count >= stable_rounds:
                    return
            except Exception:
                pass
            await page.wait_for_timeout(sample_interval_ms)

    async def _compute_page_state_signature(self, page: Page) -> str:
        current_url = page.url or ""
        dom_snapshot = await DomParser.get_interactive_elements(page)
        normalized = self._normalize_snapshot_for_stability(dom_snapshot)
        return hashlib.sha1(
            f"{current_url}\n{normalized}".encode("utf-8", errors="ignore")
        ).hexdigest()

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

    async def _attempt_generic_recovery(self, page: Page) -> bool:
        """
        无步骤上下文时的通用回退：先刷新，再后退。
        """
        try:
            await page.reload(wait_until="domcontentloaded", timeout=10000)
            await page.wait_for_timeout(1000)
            return True
        except Exception:
            pass
        try:
            resp = await page.go_back(wait_until="domcontentloaded", timeout=8000)
            await page.wait_for_timeout(800)
            return resp is not None
        except Exception:
            return False

    async def _dismiss_topmost_popup_once(self, page: Page) -> bool:
        """
        按 z-index 关闭当前最上层弹窗（仅一次）：
        - 优先 X/close 控件
        - 其次“关闭/跳过/我知道了/好的”
        - 排除“下一步/上一步”
        """
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
                        // 先找 close-like 控件
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
                            return '[data-hexa-topmost-close=\"1\"]';
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

    async def _dismiss_blocking_modal(self, page: Page) -> bool:
        """
        通用遮挡弹窗关闭器：
        - 优先关闭按钮（X/关闭/跳过）
        - 避免默认点击“下一步”导致教程链反复出现
        """
        # 分层清理：最多连续关闭 4 层顶层弹窗
        closed_any = False
        for _ in range(4):
            ok = await self._dismiss_topmost_popup_once(page)
            if not ok:
                break
            closed_any = True
        if closed_any:
            logger.info("🧩 [ModalClose] 已按 z-index 连续关闭上层弹窗")
            return True

        async def _click_candidates(candidates, *, forbidden_texts=None) -> bool:
            forbidden_texts = forbidden_texts or []
            for loc in candidates:
                try:
                    count = await loc.count()
                except Exception:
                    continue
                count = min(count, 8)
                for i in range(count):
                    cand = loc.nth(i)
                    try:
                        if not await cand.is_visible():
                            continue
                        text = ((await cand.inner_text()) or "").strip()
                        if any(t.lower() in text.lower() for t in forbidden_texts if t):
                            continue
                        await cand.scroll_into_view_if_needed(timeout=800)
                        await cand.click(timeout=2200, force=True)
                        await page.wait_for_timeout(250)
                        return True
                    except Exception:
                        continue
            return False

        # 规则0：优先处理“最上层弹窗”的关闭意图，避免点到页面底层同名元素
        try:
            dialog_roots = page.locator(
                "dialog,[role='dialog'],[aria-modal='true'],.dex-dialog,.dex-dialog-container"
            )
            root_count = min(await dialog_roots.count(), 6)
            roots_with_box = []
            for i in range(root_count):
                root = dialog_roots.nth(i)
                try:
                    if not await root.is_visible():
                        continue
                    box = await root.bounding_box()
                    if not box:
                        continue
                    area = float(box.get("width", 0)) * float(box.get("height", 0))
                    roots_with_box.append((area, root, box))
                except Exception:
                    continue

            # 大弹窗优先（通常遮挡最强）
            roots_with_box.sort(key=lambda x: x[0], reverse=True)

            for _, root, box in roots_with_box:
                forbidden = ["下一步", "上一步", "Next", "Previous"]

                # A. 最稳健：close aria / class / icon close
                if await _click_candidates(
                    [
                        root.locator("button[aria-label*='close' i],button[aria-label*='关闭' i]"),
                        root.locator("[role='button'][aria-label*='close' i],[role='button'][aria-label*='关闭' i]"),
                        root.locator("button:has(i[class*='close' i]),button:has(i[class*='okds-close' i])"),
                        root.locator("button:has(svg[class*='close' i]),button:has(svg[aria-label*='close' i])"),
                        root.locator("button[class*='close' i],.icon-close,.dex-okds-close,.close"),
                    ],
                    forbidden_texts=forbidden,
                ):
                    logger.info("🧩 [ModalClose] 已命中弹窗关闭按钮(X/close)")
                    return True

                # B. 文案关闭（不包含下一步/上一步）
                if await _click_candidates(
                    [
                        root.get_by_role("button", name="关闭", exact=False),
                        root.get_by_role("button", name="跳过", exact=False),
                        root.get_by_role("button", name="Skip", exact=False),
                        root.get_by_role("button", name="Close", exact=False),
                    ],
                    forbidden_texts=forbidden,
                ):
                    logger.info("🧩 [ModalClose] 已命中文案关闭按钮")
                    return True

                # C. 无显式关闭时，接受“我知道了/知道了/Got it”作为低优先级兜底
                if await _click_candidates(
                    [
                        root.get_by_role("button", name="我知道了", exact=False),
                        root.get_by_role("button", name="知道了", exact=False),
                        root.get_by_role("button", name="Got it", exact=False),
                    ],
                    forbidden_texts=forbidden,
                ):
                    logger.info("🧩 [ModalClose] 未找到X，已使用“我知道了/知道了”兜底")
                    return True

                # D. 最后兜底：点击弹窗框体右上角（常见 X 位置）
                try:
                    x = float(box["x"]) + float(box["width"]) - min(28.0, float(box["width"]) * 0.04)
                    y = float(box["y"]) + min(22.0, float(box["height"]) * 0.08)
                    await page.mouse.click(x, y, delay=50)
                    await page.wait_for_timeout(220)
                    logger.info("🧩 [ModalClose] 已点击弹窗右上角坐标兜底")
                    return True
                except Exception:
                    pass
        except Exception:
            pass

        # 规则1（高优先级）：
        # 若检测到教程/引导弹窗（含“下一步/上一步”），强制点右上角关闭（X），绝不点下一步。
        try:
            guide_close_selector = await page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 12 && r.height > 12;
                    };
                    const hasGuideText = (el) => {
                        const t = (el.innerText || '').replace(/\\s+/g, ' ');
                        return /下一步|上一步|Next|Previous/i.test(t);
                    };
                    const dialogRoots = Array.from(document.querySelectorAll('dialog,[role=\"dialog\"],[aria-modal=\"true\"],.dex-dialog'));
                    const guide = dialogRoots.find((d) => isVisible(d) && hasGuideText(d));
                    if (!guide) return '';

                    const closeCandidates = guide.querySelectorAll(
                        \"button[aria-label*='close' i],button[aria-label*='关闭' i],[role='button'][aria-label*='close' i],[role='button'][aria-label*='关闭' i],button:has(i),button:has(svg),.icon-close,.close\"
                    );
                    for (const el of closeCandidates) {
                        if (!isVisible(el)) continue;
                        const txt = (el.innerText || '').trim();
                        if (/下一步|上一步|next|previous/i.test(txt)) continue;
                        if (txt === '×' || txt === '✕' || txt === 'x' || txt === 'X' || txt === '' || /关闭|close/i.test(txt)) {
                            el.setAttribute('data-hexa-guide-close', '1');
                            return '[data-hexa-guide-close=\"1\"]';
                        }
                    }
                    return '';
                }"""
            )
            if guide_close_selector:
                try:
                    await page.locator(guide_close_selector).first.click(timeout=2000, force=True)
                    await page.wait_for_timeout(300)
                    logger.info("🧩 [ModalClose] 检测到教程弹窗，已优先点击右上角关闭")
                    return True
                except Exception:
                    pass
        except Exception:
            pass

        # 规则1兜底：检测到教程弹窗但未命中关闭按钮时，点击弹窗框体右上角。
        try:
            click_point = await page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 40 && r.height > 40;
                    };
                    const hasGuideText = (el) => {
                        const t = (el.innerText || '').replace(/\\s+/g, ' ');
                        return /下一步|上一步|Next|Previous/i.test(t);
                    };
                    const roots = Array.from(document.querySelectorAll('dialog,[role=\"dialog\"],[aria-modal=\"true\"],.dex-dialog'));
                    const guide = roots.find((d) => isVisible(d) && hasGuideText(d));
                    if (!guide) return null;
                    const r = guide.getBoundingClientRect();
                    return {
                        x: Math.max(8, Math.min(window.innerWidth - 8, r.right - Math.min(32, r.width * 0.04))),
                        y: Math.max(8, Math.min(window.innerHeight - 8, r.top + Math.min(26, r.height * 0.08))),
                    };
                }"""
            )
            if click_point and isinstance(click_point, dict):
                x = float(click_point.get("x", 0))
                y = float(click_point.get("y", 0))
                if x > 0 and y > 0:
                    await page.mouse.click(x, y, delay=50)
                    await page.wait_for_timeout(300)
                    logger.info("🧩 [ModalClose] 教程弹窗未命中按钮，已点击弹窗右上角关闭区域")
                    return True
        except Exception:
            pass

        # 规则1.5：通用 Esc 关闭（很多弹窗支持）
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
        # 明确排除“下一步”
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
                    try:
                        await cand.scroll_into_view_if_needed(timeout=800)
                    except Exception:
                        pass
                    await cand.click(timeout=2500, force=True)
                    await page.wait_for_timeout(400)
                    return True
            except Exception:
                continue

        # 特殊兜底：尝试点击视口右上角（常见 X 位置）
        try:
            vp = page.viewport_size or {}
            w = int(vp.get("width", 0))
            h = int(vp.get("height", 0))
            if w > 120 and h > 120:
                x = int(w * 0.93)
                y = int(h * 0.12)
                await page.mouse.click(x, y, delay=60)
                await page.wait_for_timeout(350)
                return True
        except Exception:
            pass
        return False

    async def _try_popup_healer(self, page: Page) -> bool:
        if not self.enable_popup_healer:
            return False
        try:
            if self.popup_healer is None:
                from hexaflow.tools.popup_healer import PopupHealer

                model = (
                    os.getenv("POPUP_HEALER_MODEL")
                    or os.getenv("REPLAY_REPAIR_MODEL")
                    or os.getenv("OPENAI_MODEL")
                )
                api_key = (
                    os.getenv("POPUP_HEALER_API_KEY")
                    or os.getenv("REPLAY_REPAIR_API_KEY")
                    or os.getenv("OPENAI_API_KEY")
                    or "dummy"
                )
                base_url = (
                    os.getenv("POPUP_HEALER_BASE_URL")
                    or os.getenv("REPLAY_REPAIR_BASE_URL")
                    or os.getenv("OPENAI_BASE_URL")
                )
                self.popup_healer = PopupHealer(
                    model_name="openai/gpt-oss-120b",
                    api_key=os.getenv("POPUP_HEALER_APIKEY"),
                    base_url=os.getenv("POPUP_HEALER_BASE_URL"),
                )
            logger.info("🧰 [PopupHealer] 尝试清理遮挡弹窗...")
            ok = await self.popup_healer.heal(page)
            if ok:
                logger.info("✅ [PopupHealer] 弹窗助手处理成功")
                return True
            logger.info("ℹ️ [PopupHealer] 未成功处理，转 AI 修复")
            return False
        except Exception as e:
            logger.warning(f"⚠️ [PopupHealer] 调用失败，转 AI 修复: {e}")
            return False

    async def _selector_to_click_ratio(self, page: Page, selector: str):
        """
        将 selector 解析为屏幕相对坐标 (x_ratio, y_ratio)。
        仅用于 AI 修复点击动作的坐标化执行。
        """
        if not selector:
            return None
        import re

        async def _box_to_ratio(box):
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            vp = page.viewport_size
            if vp:
                width, height = int(vp["width"]), int(vp["height"])
            else:
                width, height = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
                width, height = int(width), int(height)
            if width <= 1 or height <= 1:
                return None
            x_ratio = max(0.0, min(1.0, cx / width))
            y_ratio = max(0.0, min(1.0, cy / height))
            return (x_ratio, y_ratio)

        async def _from_locator(loc):
            try:
                # 等待短暂渲染，防止刚切页 count=0
                await loc.first.wait_for(state="visible", timeout=2000)
            except Exception:
                pass
            try:
                count = await loc.count()
            except Exception:
                return None
            for i in range(count):
                cand = loc.nth(i)
                try:
                    if not await cand.is_visible():
                        continue
                    await cand.scroll_into_view_if_needed(timeout=1200)
                    box = await cand.bounding_box()
                    if not box:
                        continue
                    ratio = await _box_to_ratio(box)
                    if ratio:
                        return ratio
                except Exception:
                    continue
            return None

        candidates = [page.locator(selector)]
        # 常见纠错：模型把 hexa-id 写成 id
        m_id = re.search(r"\[id\s*=\s*['\"](hexa-\d+)['\"]\]", selector)
        if m_id:
            hid = m_id.group(1)
            candidates.append(page.locator(f"[hexa-id=\"{hid}\"]"))
            candidates.append(page.locator(f"[data-hexa-id=\"{hid}\"]"))
        # 常见 text selector 回退：a:has-text('X') / div:has-text("X")
        m = re.search(r"has-text\((['\"])(.*?)\1\)", selector)
        if m:
            txt = m.group(2).strip()
            if txt:
                candidates.append(page.get_by_text(txt, exact=False))
                candidates.append(page.locator(f"text={txt}"))
                # 输入框常见兜底：模型误用 input:has-text('xxx')
                candidates.append(page.get_by_placeholder(txt, exact=False))
                candidates.append(page.locator(f'input[placeholder*="{txt}"]:not([type="hidden"]):not([readonly])'))
                candidates.append(page.locator(f'textarea[placeholder*="{txt}"]:not([readonly])'))
                candidates.append(page.locator(f'input[aria-label*="{txt}"]:not([type="hidden"]):not([readonly])'))
                candidates.append(page.locator(f'textarea[aria-label*="{txt}"]:not([readonly])'))
                candidates.append(page.locator(f'input[value*="{txt}"]:not([type="hidden"]):not([readonly])'))
                candidates.append(page.locator('.dex-input-box input.dex-input-input:not([type="hidden"]):not([readonly])'))

        for loc in candidates:
            ratio = await _from_locator(loc)
            if ratio:
                return ratio

        return None

    def _normalize_input_selector_candidates(self, selector: str) -> list[str]:
        sel = (selector or "").strip()
        if not sel:
            return []
        candidates = [sel]
        m = re.search(r"input:has-text\((['\"])(.*?)\1\)", sel)
        if m:
            txt = (m.group(2) or "").strip()
            if txt:
                candidates.extend(
                    [
                        # OKX DEX 场景：优先真实输入框，排除 hidden/readonly 输入
                        f'.dex-input-box input.dex-input-input[placeholder*="{txt}"]:not([type="hidden"]):not([readonly])',
                        f'input[placeholder*="{txt}"]:not([type="hidden"]):not([readonly])',
                        f'textarea[placeholder*="{txt}"]:not([readonly])',
                        f'input[aria-label*="{txt}"]:not([type="hidden"]):not([readonly])',
                        f'textarea[aria-label*="{txt}"]:not([readonly])',
                        f'input[value*="{txt}"]:not([type="hidden"]):not([readonly])',
                        '.dex-input-box input.dex-input-input:not([type="hidden"]):not([readonly])',
                        'input.dex-input-input:not([type="hidden"]):not([readonly])',
                        'input[inputmode="decimal"]:not([type="hidden"]):not([readonly])',
                        "textarea:not([readonly])",
                        'input:not([type="hidden"]):not([readonly])',
                    ]
                )
        # 若模型直接给了 input:has-text(...)，加一层通用候选避免彻底 miss
        if "input:has-text(" in sel:
            candidates.extend(
                [
                    '.dex-input-box input.dex-input-input:not([type="hidden"]):not([readonly])',
                    'input[inputmode="decimal"]:not([type="hidden"]):not([readonly])',
                    'input:not([type="hidden"]):not([readonly])',
                ]
            )
        return candidates

    async def _resolve_input_locator(self, page: Page, selector: str):
        candidates = self._normalize_input_selector_candidates(selector)
        last_err = None
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=1500)
                editable = await loc.evaluate(
                    """(el) => {
                        const tag = (el.tagName || '').toLowerCase();
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        const type = (el.getAttribute('type') || '').toLowerCase();
                        const disabled = !!el.disabled;
                        const readonly = !!el.readOnly;
                        const style = window.getComputedStyle(el);
                        const hiddenByStyle = style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0';
                        const rect = el.getBoundingClientRect();
                        const hiddenByRect = rect.width <= 0 || rect.height <= 0;
                        const badInputType = type === 'hidden';
                        return tag === 'input' || tag === 'textarea' || tag === 'select' ||
                               el.isContentEditable || role === 'textbox' || role === 'searchbox'
                               ? (!disabled && !readonly && !badInputType && !hiddenByStyle && !hiddenByRect)
                               : false;
                    }"""
                )
                if editable:
                    return loc, sel
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise Exception(f"无法定位可编辑输入框: {selector}")

    async def _robust_input_text(self, page: Page, locator, text: str, press_enter: bool = False):
        import random
        value = "" if text is None else str(text)
        
        # 🚀 优化 1：确保输入框在视野内且可交互
        try:
            await locator.wait_for(state="visible", timeout=3000)
            await locator.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass # 尽力而为

        # 🚀 优化 2：拟人化聚焦
        await locator.hover(timeout=3000)
        await page.wait_for_timeout(random.randint(100, 200))
        await locator.click(timeout=3000, delay=random.randint(50, 100))
        
        # 🚀 优化 3：稳健清空原有内容
        try:
            # 优先用全选+删除的方式清空，对 React/Vue 最友好
            await page.keyboard.press("Meta+A") # Mac
            await page.keyboard.press("Control+A") # Windows
            await page.keyboard.press("Backspace")
            await locator.clear(timeout=1000)
        except Exception:
            try:
                await locator.fill("")
            except Exception:
                pass
                
        # 🚀 优化 4：逐字键入，加入随机延迟
        await locator.press_sequentially(value, delay=random.randint(70, 120), timeout=5000)

        # 校验是否真正写入 (保留你原有的校验兜底逻辑)
        try:
            typed_value = await locator.input_value(timeout=1200)
        except Exception:
            typed_value = ""

        if (typed_value or "") != value:
            # 如果没写进去，用 JS 强行赋值并派发 React 需要的 input/change 事件
            await locator.evaluate(
                """(el, v) => {
                    const proto = Object.getPrototypeOf(el);
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) {
                        setter.call(el, v);
                    } else {
                        el.value = v;
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )

        if press_enter:
            await page.wait_for_timeout(random.randint(200, 400))
            await locator.press("Enter", timeout=5000)

    @staticmethod
    def _extract_text_hint_from_selector(selector: str) -> str:
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

    async def _is_click_intent_satisfied(self, page: Page, step_desc: str, target: str) -> bool:
        txt = self._extract_text_hint_from_selector(target)
        if not txt:
            return False
        try:
            if not await page.get_by_text(txt, exact=False).first.is_visible(timeout=500):
                return False
        except Exception:
            return False

        # 常见“已选中 tab”判定：点击后不再是 <a>，旧 selector 会失效
        try:
            active_sel = (
                f'[aria-selected="true"]:has-text("{txt}"), '
                f'[aria-current="page"]:has-text("{txt}"), '
                f'[aria-current="true"]:has-text("{txt}"), '
                f'[data-state="active"]:has-text("{txt}"), '
                f'.active:has-text("{txt}"), .is-active:has-text("{txt}")'
            )
            if await page.locator(active_sel).first.is_visible(timeout=500):
                return True
        except Exception:
            pass

        return False

    async def _is_target_text_inside_dialog(self, page: Page, target: str) -> bool:
        txt = self._extract_text_hint_from_selector(target)
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

    def _build_click_candidate_locators(self, page: Page, selector: str):
        raw = (selector or "").strip()
        txt = self._extract_text_hint_from_selector(raw)
        if not txt:
            m_aria = re.search(r"aria-label\s*=\s*['\"]([^'\"]+)['\"]", raw, re.IGNORECASE)
            if m_aria:
                txt = (m_aria.group(1) or "").strip()
        candidates = []
        resolved_testids = []

        # 按你要求的执行优先级：
        # 1) get_by_role 2) get_by_text 3) get_by_test_id 4) css 5) xpath
        if txt:
            role_names = ["button", "checkbox", "switch", "radio", "tab", "link", "menuitem", "option"]

            # 对“连接/确认/开始”等短文案，优先在顶层弹窗里找，避免点到页面背景里的同名按钮
            if len(txt) <= 4:
                esc = txt.replace('"', '\\"')
                candidates.extend(
                    [
                        page.locator("[role='dialog'],dialog,[aria-modal='true'],.dex-dialog,.dex-dialog-container").locator(
                            f"button:has-text('{txt}')"
                        ),
                        page.locator("[role='dialog'],dialog,[aria-modal='true'],.dex-dialog,.dex-dialog-container").locator(
                            f"[role='button']:has-text('{txt}')"
                        ),
                        page.locator(f".wallet button:has-text('{txt}'), .wallet [role='button']:has-text('{txt}')"),
                    ]
                )

            for role in role_names:
                candidates.append(page.get_by_role(role, name=txt, exact=False))
            # 有些站点把“可点击容器”挂在 generic role 上，给一层宽松兜底
            candidates.append(page.get_by_role("generic", name=txt, exact=False))

        if txt:
            candidates.append(page.get_by_text(txt, exact=False))

        # get_by_test_id：从 selector 中提取 data-testid
        testid = ""
        m = re.search(r'data-testid\s*=\s*["\']([^"\']+)["\']', raw, re.IGNORECASE)
        if m:
            testid = (m.group(1) or "").strip()
        if testid:
            resolved_testids.append(testid)
        # 文本 selector 常见格式：[data-testid="xxx"] / [data-testid='xxx']
        m_css_tid = re.search(r'\[\s*data-testid\s*=\s*["\']([^"\']+)["\']\s*\]', raw, re.IGNORECASE)
        if m_css_tid:
            tid = (m_css_tid.group(1) or "").strip()
            if tid:
                resolved_testids.append(tid)

        # hexa-id 到 testid 运行时翻译：补一条 evaluate 映射候选
        m_hexa_for_runtime = re.search(r'hexa-id\s*=\s*["\'](hexa-\d+)["\']', raw, re.IGNORECASE)
        if not m_hexa_for_runtime:
            m_hexa_for_runtime = re.search(r'\[id\s*=\s*["\'](hexa-\d+)["\']\]', raw, re.IGNORECASE)
        if m_hexa_for_runtime:
            hid = m_hexa_for_runtime.group(1)
            candidates.insert(0, page.locator(f'[hexa-id="{hid}"]'))
            candidates.insert(1, page.locator(f'[data-hexa-id="{hid}"]'))

        for tid in list(dict.fromkeys([x for x in resolved_testids if x])):
            candidates.append(page.get_by_test_id(tid))

        # css（不得已）
        if raw and not (raw.startswith("xpath=") or raw.startswith("//") or raw.startswith("(//")):
            candidates.append(page.locator(raw))
            if txt:
                escaped = txt.replace('"', '\\"')
                candidates.extend(
                    [
                        page.locator(f'button:has-text("{escaped}")'),
                        page.locator(f'[role="button"]:has-text("{escaped}")'),
                        page.locator(f'[role="checkbox"]:has-text("{escaped}")'),
                        page.locator(f'label:has-text("{escaped}")'),
                        page.locator(f'a:has-text("{escaped}")'),
                    ]
                )

        # xpath（尽量避免，最后才尝试）
        if raw and (raw.startswith("xpath=") or raw.startswith("//") or raw.startswith("(//")):
            candidates.append(page.locator(raw))

        return candidates

    async def _try_click_locator(self, page: Page, loc) -> bool:
        import random # 确保文件顶部有 import random

        try:
            count = await loc.count()
        except Exception:
            return False
            
        count = min(count, 8)
        for i in range(count):
            cand = loc.nth(i)
            try:
                # 🚀 优化 1：等待元素真正在 DOM 中就绪并可见
                await cand.wait_for(state="visible", timeout=3000)
                if not await cand.is_visible():
                    continue
                await cand.scroll_into_view_if_needed(timeout=1200)
                # 增加拟人化停顿，让前端框架有时间绑定事件
                await page.wait_for_timeout(random.randint(150, 350))
            except Exception:
                continue

            # 🚀 优化 2：四段式降级点击策略
            # 策略 A: 拟人化常规点击 (带模拟按下释放的 delay)
            try:
                await cand.click(timeout=2000, delay=random.randint(50, 150))
                return True
            except Exception as e:
                logger.debug(f"常规点击失败, 尝试降级: {e}")

            # 策略 B: 强制穿透点击 (无视透明浮层遮挡)
            try:
                await cand.click(timeout=2000, force=True, delay=random.randint(50, 100))
                return True
            except Exception as e:
                logger.debug(f"强制点击失败, 尝试坐标点击: {e}")

            # 策略 C: 鼠标中心点绝对坐标点击（适配复杂嵌套元素）
            try:
                box = await cand.bounding_box()
                if box:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    # 移动鼠标过去并稍微晃动
                    await page.mouse.move(cx, cy)
                    await page.wait_for_timeout(1000)
                    await page.mouse.click(cx, cy, delay=random.randint(40, 80))
                    # 坐标点击作为有效尝试
                    return True
            except Exception as e:
                logger.debug(f"坐标点击失败, 尝试 JS 注入: {e}")

            # 策略 D: 终极兜底，直接触发底层 JS 事件
            try:
                await cand.evaluate(
                    """(el) => {
                        el.click();
                        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    }"""
                )
                return True
            except Exception:
                continue
                
        return False

    async def _robust_click(self, page: Page, selector: str = "", preferred_locator=None):
        candidates = []
        raw = (selector or "").strip()

        # ref/hexa-id -> data-testid 映射（优先 get_by_test_id）
        m_hexa = re.search(r'hexa-id\s*=\s*["\'](hexa-\d+)["\']', raw, re.IGNORECASE)
        if not m_hexa:
            m_hexa = re.search(r'\[id\s*=\s*["\'](hexa-\d+)["\']\]', raw, re.IGNORECASE)
        if m_hexa:
            hid = m_hexa.group(1)
            try:
                tid = await page.locator(f'[hexa-id="{hid}"]').first.get_attribute("data-testid")
                if tid:
                    candidates.append(page.get_by_test_id(tid))
            except Exception:
                pass

        if preferred_locator is not None:
            candidates.append(preferred_locator)
        candidates.extend(self._build_click_candidate_locators(page, selector))
        for loc in candidates:
            ok = await self._try_click_locator(page, loc)
            if ok:
                return

        # 分层弹窗兜底：若上层弹窗遮挡，按 z-index 连续关闭后重试目标点击
        for _ in range(3):
            try:
                dismissed = await self._dismiss_topmost_popup_once(page)
            except Exception:
                dismissed = False
            if not dismissed:
                break
            await page.wait_for_timeout(1000)
            for loc in candidates:
                ok = await self._try_click_locator(page, loc)
                if ok:
                    return
        raise Exception(f"Robust click failed for selector: {selector}")
