import os
import json
import asyncio
import logging
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
    def __init__(self, cache_file: str = "memory/workspace/popup/popup_cache.json"):
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
    def __init__(self, model_name: str = os.getenv("POPUP_HEALER_MODEL"), api_key: str = os.getenv("OPENAI_API_KEY"), base_url: str = os.getenv("OPENAI_BASE_URL")):
        self.cache = PopupCache()
        self.model_name = model_name
        raw_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.client = instructor.from_openai(raw_client, mode=instructor.Mode.JSON)
        
        self.system_prompt = """
        You are an autonomous browser agent. Your task is to close/skip an unexpected popup.
        Analyze the provided Popup HTML.
        
        CRITICAL RULES:
        1. Identify the 'Close' (X), 'Skip', 'Cancel', or 'I understand' button.
        2. DO NOT use dynamic/hashed classes (e.g., `.css-1y2x`).
        3. Prefer Playwright's native text selector: `text="我知道了"` or `text="Skip"`.
        4. If using XPath, you MUST use `.` to include nested text, e.g., `xpath=//button[contains(., 'Close')]`.
        """

    async def _observe_clean_popups(self, page: Page) -> str:
        """注入 JS，连拍提取脱水版的弹窗 HTML"""
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

    async def _ask_llm(self, popup_html: str) -> str:
        prompt = f"Popup HTML:\n```html\n{popup_html}\n```\nFind the selector to close this popup."
        logger.info("🧠 [Healer] 遇到未知弹窗，正在呼叫大模型思考破解方案...")
        
        call_kwargs = {
            "model": self.model_name,
            "response_model": PopupSolution,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt}
            ]
        }
        
        try:
            solution = await self.client.chat.completions.create(**call_kwargs)
            logger.info(f"💡 [Healer 思路]: {solution.thought}")
            return solution.selector
        except Exception as e:
            logger.error(f"❌ [Healer 决策失败]: {e}")
            return ""

    async def heal(self, page: Page) -> bool:
        """核心自愈入口：扫描弹窗 -> 匹配缓存/呼叫AI -> 关闭 -> 验证"""
        popup_html = await self._observe_clean_popups(page)
        if not popup_html:
            logger.warning("⚠️ [Healer] 未扫描到明显的弹窗结构。")
            return False
            
        fingerprint = self.cache.extract_skeleton(popup_html)
        solution_selector = self.cache.find_match(fingerprint)
        used_cache = True
        
        if not solution_selector:
            used_cache = False
            solution_selector = await self._ask_llm(popup_html)
            
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
            
            await page.locator(final_selector).first.click(timeout=5000)
            await page.wait_for_timeout(2000)
            
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