import os
import json
import asyncio
import logging
import base64
import mimetypes
from difflib import SequenceMatcher
from typing import Optional
from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import Page
from pydantic import BaseModel, Field
import instructor
from openai import AsyncOpenAI

logger = logging.getLogger("PopupHealer")

# ==========================================
# 1. 结构化输出 Schema
# ==========================================
class PopupSolution(BaseModel):
    thought: str = Field(description="Analyze the popup HTML. What kind of popup is it? Where is the close/skip button?")
    selector: str = Field(description="Playwright selector (e.g., text='Skip' or css/xpath) to close the popup. DO NOT use dynamic hashed classes.")

# ==========================================
# 2. 弹窗记忆缓存库 (复用你的优秀逻辑)
# ==========================================
class PopupCache:
    def __init__(self, cache_file: str = "workspace/popup/popup_cache.json"):
        self.cache_file = cache_file
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        self.experiences = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save(self):
        with open(self.cache_file, 'w', encoding='utf-8') as f:
            json.dump(self.experiences, f, ensure_ascii=False, indent=2)

    @staticmethod
    def extract_skeleton(html_content: str) -> str:
        """提取纯净的 DOM 拓扑骨架，并融合核心文本特征（结构+文字双重指纹）"""
        if not html_content: return ""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            roots = [e for e in soup.contents if not isinstance(e, NavigableString)]
            if not roots: return ""

            def build_sig(node) -> str:
                # 不再无视文本，而是提取前几个字符作为指纹的一部分
                if isinstance(node, NavigableString):
                    text = str(node).strip()
                    # 只取前8个字符，忽略过长的动态文本，保留核心语义
                    return f'"{text[:8]}"' if text else ""
                    
                tag = node.name.upper()
                children_sigs = [build_sig(c) for c in node.children]
                # 过滤掉空的子节点签名
                children_sigs = [c for c in children_sigs if c] 
                
                return f"{tag}[{','.join(children_sigs)}]" if children_sigs else tag

            return build_sig(roots[0])
        except Exception:
            return ""

    def find_match(self, new_fingerprint: str, threshold: float = 0.85) -> Optional[str]:
        if not new_fingerprint: return None
        best_match, highest_score = None, 0.0

        for cached_fp, data in self.experiences.items():
            score = SequenceMatcher(None, new_fingerprint, cached_fp).ratio()
            if score > highest_score:
                highest_score = score
                best_match = cached_fp

        if highest_score >= threshold:
            logger.info(f"⚡ [Cache] 命中历史弹窗处理经验！(相似度: {highest_score:.2f})")
            return self.experiences[best_match].get("selector")
        return None

    def find_match_with_relaxed_threshold(
        self, new_fingerprint: str, primary_threshold: float = 0.85, relaxed_threshold: float = 0.72
    ) -> Optional[str]:
        """
        先严格匹配，再宽松匹配，尽量复用历史弹窗方案，减少 LLM 调用。
        """
        strict = self.find_match(new_fingerprint, threshold=primary_threshold)
        if strict:
            return strict

        if not new_fingerprint:
            return None
        best_match, highest_score = None, 0.0
        for cached_fp, data in self.experiences.items():
            score = SequenceMatcher(None, new_fingerprint, cached_fp).ratio()
            if score > highest_score:
                highest_score = score
                best_match = cached_fp
        if best_match and highest_score >= relaxed_threshold:
            logger.info(f"⚠️ [Cache] 宽松命中历史弹窗经验 (相似度: {highest_score:.2f})")
            return self.experiences[best_match].get("selector")
        logger.info(f"ℹ️ [Cache] 未命中弹窗经验 (最佳相似度: {highest_score:.2f})")
        return None

    def save_experience(self, fingerprint: str, selector: str):
        self.experiences[fingerprint] = {"selector": selector}
        self._save()

    def invalidate(self, fingerprint: str):
        if fingerprint in self.experiences:
            del self.experiences[fingerprint]
            self._save()

# ==========================================
# 3. 弹窗自愈专家 (Healer Agent)
# ==========================================
class PopupHealer:
    def __init__(self, model_name: str = "meta-llama/llama-4-scout-17b-16e-instruct", api_key: str = os.getenv("POPUP_HEALER_APIKEY"), base_url: str = os.getenv("POPUP_HEALER_BASEURL")):
        self.cache = PopupCache()
        self.model_name = model_name
        raw_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.client = instructor.from_openai(raw_client, mode=instructor.Mode.JSON)
        
        self.system_prompt = """
        You are an autonomous browser agent. Your task is to close/skip an unexpected popup.
        Analyze the provided Popup HTML.
        
        CRITICAL RULES:
        1. Identify the 'Close' (X), 'Skip', 'Cancel', or 'I understand' button.
        1.1 If there are multiple stacked popups, ALWAYS handle the top-most popup first (highest z-index / visually front-most), then re-check remaining popups.
        2. DO NOT use dynamic/hashed classes (e.g., `.css-1y2x`).
        3. Prefer Playwright's native text selector: `text="我知道了"` or `text="Skip"`.
        3.1 Prefer explicit close controls in this strict order:
             a) top-right X / close icon / aria-label contains close
             b) close-like text: 关闭 / 跳过 / Skip / Close / Cancel
             c) acknowledgement text: 我知道了 / 我已知晓 / Got it
        3.2 NEVER click tutorial progression buttons such as 下一步 / 上一步 / Next / Previous when a close option exists.
        4. If using XPath, you MUST use `.` to include nested text, e.g., `xpath=//button[contains(., 'Close')]`.
        5. Return ONE selector for the immediate top-most blocking popup only. After closing, caller will invoke you again if needed.
        """

    @staticmethod
    def _bytes_to_data_url(binary: bytes, mime: str = "image/png") -> str:
        if not binary:
            return ""
        b64 = base64.b64encode(binary).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    async def _capture_popup_viewport_data_url(self, page: Page) -> str:
        """
        仅截取当前视口，作为弹窗视觉上下文传给 LLM。
        """
        try:
            await self._wait_popup_settle(page)
            img_bytes = await page.screenshot(full_page=False, scale="css", type="png")
            return self._bytes_to_data_url(img_bytes, mime="image/png")
        except Exception:
            return ""

    @staticmethod
    async def _wait_popup_settle(page: Page):
        """
        弹窗截图前的轻量稳定等待，避免“刚弹出就截”导致视觉不完整。
        """
        settle_ms = int(os.getenv("POPUP_SCREENSHOT_SETTLE_MS", "700"))
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=1200)
        except Exception:
            pass
        try:
            await page.wait_for_timeout(max(120, settle_ms))
        except Exception:
            pass

    async def _observe_clean_popups(self, page: Page) -> str:
        """注入 JS，连拍提取脱水版的弹窗 HTML"""
        await self._wait_popup_settle(page)
        script = """() => {
            const popups = document.querySelectorAll('dialog, [role="dialog"], [class*="modal" i], [class*="banner" i], [style*="z-index"]');
            let result = [];
            
            function cleanNode(node) {
                if (node.nodeType !== 1) return null;
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || 
                    style.visibility === 'hidden' || 
                    style.opacity === '0' || 
                    style.pointerEvents === 'none') {
                    return null;
                }
                if (['SCRIPT', 'STYLE', 'IFRAME'].includes(node.tagName)) return null;
                
                let clone = node.cloneNode(false);
                let hasValid = false;
                for (let child of node.childNodes) {
                    if (child.nodeType === 3) {
                        let text = child.textContent.trim();
                        if (text) {
                            if (text.length > 50) text = text.substring(0, 50) + '...';
                            clone.appendChild(document.createTextNode(text));
                            hasValid = true;
                        }
                    } else if (child.nodeType === 1) {
                        let cleaned = cleanNode(child);
                        if (cleaned) { clone.appendChild(cleaned); hasValid = true; }
                    }
                }
                return (hasValid || ['BUTTON', 'A', 'INPUT', 'SVG'].includes(clone.tagName)) ? clone : null;
            }

            Array.from(popups).forEach(p => {
                if (p.offsetWidth > 0 || p.offsetHeight > 0) {
                    let c = cleanNode(p);
                    if (c) result.push(c.outerHTML);
                }
            });
            return result.join('\\n');
        }"""
        
        for _ in range(3):
            try:
                raw_html = await page.evaluate(script)
                if raw_html and len(raw_html.strip()) > 10:
                    return raw_html[:8000]
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return ""

    async def _ask_llm(self, popup_html: str, screenshot_data_url: str = "") -> str:
        prompt = (
            "Analyze this popup and return a selector to close/skip it.\n"
            "If HTML and screenshot conflict, trust screenshot visibility first.\n"
            "Important: choose selector for top-most blocking popup first; avoid 下一步/Next when close is available.\n"
            f"Popup HTML:\n```html\n{popup_html}\n```"
        )
        logger.info("🧠 [Healer] 遇到未知弹窗，正在呼叫大模型思考破解方案...")
        
        user_content = [{"type": "text", "text": prompt}]
        if screenshot_data_url:
            user_content.append({"type": "image_url", "image_url": {"url": screenshot_data_url}})

        call_kwargs = {
            "model": self.model_name,
            "response_model": PopupSolution,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_content},
            ]
        }
        
        try:
            solution = await self.client.chat.completions.create(**call_kwargs)
            logger.info(f"💡 [Healer 思路]: {solution.thought}")
            return solution.selector
        except Exception as e:
            logger.error(f"❌ [Healer 决策失败]: {e}")
            return ""

    async def _click_selector_robust(self, page: Page, selector: str) -> bool:
        if not selector:
            return False
        loc = page.locator(selector).first
        try:
            await loc.wait_for(state="visible", timeout=2500)
        except Exception:
            return False

        # 1) normal click
        try:
            await loc.click(timeout=2200)
            return True
        except Exception:
            pass

        # 2) force click (for mask-intercepted popups)
        try:
            await loc.click(timeout=2200, force=True)
            return True
        except Exception:
            pass

        # 3) center coordinate click
        try:
            box = await loc.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                await page.mouse.click(x, y, delay=50)
                return True
        except Exception:
            pass

        # 4) JS click fallback
        try:
            await loc.evaluate(
                """(el) => {
                    el.click();
                    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                }"""
            )
            return True
        except Exception:
            return False

    async def _fallback_close_actions(self, page: Page) -> bool:
        fallback_selectors = [
            "button[aria-label='Close']",
            "button[aria-label='关闭']",
            "[role='button'][aria-label='Close']",
            "[role='button'][aria-label='关闭']",
            "text='关闭'",
            "text='跳过'",
            "text='知道了'",
            "text='我知道了'",
            "text='稍后'",
            "text='以后再说'",
        ]
        for sel in fallback_selectors:
            if await self._click_selector_robust(page, sel):
                return True

        # common modal close hotspot (top-right in dialog)
        try:
            size = page.viewport_size or {}
            w, h = int(size.get("width", 0)), int(size.get("height", 0))
            if w > 120 and h > 120:
                await page.mouse.click(int(w * 0.92), int(h * 0.12), delay=40)
                return True
        except Exception:
            pass
        return False

    async def heal(self, page: Page) -> bool:
        """核心自愈入口：扫描弹窗 -> 匹配缓存/呼叫AI -> 关闭 -> 验证"""
        popup_html = await self._observe_clean_popups(page)
        if not popup_html:
            logger.warning("⚠️ [Healer] 未扫描到明显的弹窗结构。")
            return False
            
        fingerprint = self.cache.extract_skeleton(popup_html)
        if not fingerprint:
            logger.info("ℹ️ [Cache] 弹窗指纹为空，跳过缓存匹配。")
        else:
            logger.info(f"🧩 [Cache] 开始匹配弹窗经验库，指纹长度={len(fingerprint)}")
        solution_selector = self.cache.find_match_with_relaxed_threshold(fingerprint)
        used_cache = True
        
        if not solution_selector:
            used_cache = False
            screenshot_data_url = await self._capture_popup_viewport_data_url(page)
            if screenshot_data_url:
                logger.info("🖼️ [Healer] 已附带视口截图进行弹窗视觉识别")
            solution_selector = await self._ask_llm(popup_html, screenshot_data_url=screenshot_data_url)
        else:
            logger.info("🗂️ [Cache] 本次将优先使用缓存中的弹窗关闭方案")
            
        if not solution_selector:
            return False
            
        logger.info(f"🛠️ [Healer] 尝试执行关闭动作: {solution_selector}")
        try:
            # 清理选择器可能带有的前缀
            sel = solution_selector.strip()
            if sel.startswith("xpath="): sel = sel[6:]
            if sel.startswith("text=") or sel.startswith("has-text="): final_selector = sel
            elif sel.startswith("//") or sel.startswith("(//"): final_selector = f"xpath={sel}"
            else: final_selector = sel

            clicked = await self._click_selector_robust(page, final_selector)
            if not clicked:
                logger.info("ℹ️ [Healer] 主选择器点击失败，尝试通用关闭策略...")
                clicked = await self._fallback_close_actions(page)
            if not clicked:
                raise Exception(f"所有关闭动作都失败: {final_selector}")
            await page.wait_for_timeout(500)
            
            # 闭环验证
            verify_html = await self._observe_clean_popups(page)
            verify_fingerprint = self.cache.extract_skeleton(verify_html)
            
            if not verify_html or verify_fingerprint != fingerprint or len(verify_html) < len(popup_html) * 0.95:
                logger.info("✅ [Healer] 弹窗已清除！(验证通过)")
                if not used_cache:
                    logger.info(f"💾 [Cache] 正在将此弹窗杀手经验写入本地缓存...")
                    self.cache.save_experience(fingerprint, solution_selector)
                return True
            else:
                logger.warning("⚠️ [Healer] 验证失败，弹窗似乎依然存在。")
                # if used_cache: 
                #     self.cache.invalidate(fingerprint)
                return False
                
        except Exception as e:
            logger.error(f"❌ [Healer] 执行清理动作失败: {e}")
            if used_cache: self.cache.invalidate(fingerprint)
            return False
