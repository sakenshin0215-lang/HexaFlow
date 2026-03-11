import logging
from playwright.async_api import Page

logger = logging.getLogger("DomParser")

class DomParser:
    @staticmethod
    async def get_interactive_elements(page: Page) -> str:
        """
        注入 JS，提取页面上所有可见的交互元素，并打上 hexa-id。
        """
        js_code = """
        () => {
            const normalizeText = (s) => (s || '')
                .replace(/\\n+/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();

            const cleanActionText = (raw) => {
                let t = normalizeText(raw);
                if (!t) return '';
                // 去掉尾部涨跌幅，如: "PENGUIN +50%" / "RIVER -12.3%"
                t = t.replace(/\\s+[+-]?\\d+(?:\\.\\d+)?%\\s*$/g, '').trim();
                // 去掉尾部价格，如: "RIVER $12.53"
                t = t.replace(/\\s+\\$?\\d+(?:[.,]\\d+)*(?:[kKmMbB])?\\s*$/g, '').trim();
                // 去掉明显噪音尾词
                t = t.replace(/\\s+(?:buy|sell|trade|swap)$/i, '').trim();
                return t;
            };

            const pickBestText = (el) => {
                // 1) 优先更“局部”的可见子文本，减少把父容器整行文字拼进去
                const candidates = [];
                const nodes = el.querySelectorAll('span, strong, b, em, p, div');
                for (const n of nodes) {
                    const style = window.getComputedStyle(n);
                    if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                    const rect = n.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const txt = cleanActionText(n.innerText || '');
                    if (!txt) continue;
                    if (txt.length > 30) continue;
                    candidates.push(txt);
                    if (candidates.length >= 24) break;
                }
                if (candidates.length > 0) {
                    candidates.sort((a, b) => a.length - b.length);
                    return candidates[0];
                }
                // 2) 回退到元素整体文本
                return cleanActionText(el.innerText || el.value || el.placeholder || '');
            };

            const elements = document.querySelectorAll('button, a, input, label, [role="button"], [role="link"], [role="checkbox"], [tabindex]');;
            let result = "";
            let counter = 0;
            
            elements.forEach((el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                // 过滤掉不可见、不在屏幕内、被隐藏的元素
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0' || rect.width === 0 || rect.height === 0) {
                    return;
                }
                
                // 注入专属 ID，方便 Playwright 直接定位
                const hexaId = `hexa-${counter}`;
                el.setAttribute('hexa-id', hexaId);
                
                let tag = el.tagName.toLowerCase();
                let text = pickBestText(el).substring(0, 60);
                let ariaLabel = el.getAttribute('aria-label') || '';
                let href = el.getAttribute('href') || '';
                let testid = el.getAttribute('data-testid') || '';
                
                // 只保留有意义的元素（有文本或有aria-label的）
                if (text || ariaLabel || tag === 'input') {
                    let info = `[ID: ${hexaId}] <${tag}`;
                    if (text) info += ` text="${text}"`;
                    if (ariaLabel) info += ` aria-label="${ariaLabel}"`;
                    if (testid) info += ` data-testid="${testid}"`;
                    if (href) info += ` href="${href}"`;
                    info += `>`;
                    result += info + "\\n";
                }
                counter++;
            });
            return result || "No interactive elements found on the current screen.";
        }
        """
        try:
            # 等待网络基本空闲，防止抓到白屏
            await page.wait_for_load_state("domcontentloaded")
            dom_snapshot = await page.evaluate(js_code)
            return dom_snapshot
        except Exception as e:
            logger.error(f"提取 DOM 失败: {e}")
            return "Error extracting DOM."
    
    @staticmethod
    async def get_stable_selector(page: Page, target: str) -> str:
        """
        核心改造：将含有 hexa-id 的选择器翻译成原生稳定的定位器。
        调整优先级：优先使用业务文本(:has-text)和测试标签，警惕动态 Hash ID。
        """
        if not target or "hexa-id" not in target:
            return target

        js_code = """
        (el) => {
            if (!el) return null;

            const normalizeText = (s) => (s || '')
                .replace(/\\n+/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            const cleanActionText = (raw) => {
                let t = normalizeText(raw);
                if (!t) return '';
                t = t.replace(/\\s+[+-]?\\d+(?:\\.\\d+)?%\\s*$/g, '').trim();
                t = t.replace(/\\s+\\$?\\d+(?:[.,]\\d+)*(?:[kKmMbB])?\\s*$/g, '').trim();
                t = t.replace(/\\s+(?:buy|sell|trade|swap)$/i, '').trim();
                return t;
            };
            const pickBestText = (root) => {
                const candidates = [];
                const nodes = root.querySelectorAll('span, strong, b, em, p, div');
                for (const n of nodes) {
                    const style = window.getComputedStyle(n);
                    if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                    const rect = n.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const txt = cleanActionText(n.innerText || '');
                    if (!txt) continue;
                    if (txt.length > 30) continue;
                    candidates.push(txt);
                    if (candidates.length >= 24) break;
                }
                if (candidates.length > 0) {
                    candidates.sort((a, b) => a.length - b.length);
                    return candidates[0];
                }
                return cleanActionText(root.innerText || root.value || '');
            };
            
            // 优先级 1: 开发者留的测试钩子或无障碍标签 (极其稳定)
            if (el.getAttribute('data-testid')) return `[data-testid="${el.getAttribute('data-testid')}"]`;
            if (el.getAttribute('aria-label')) return `${el.tagName.toLowerCase()}[aria-label="${el.getAttribute('aria-label')}"]`;
            
            // 优先级 2: 用户可见文本 (最符合业务直觉，抗前端重构能力最强)
            let text = pickBestText(el);
            if (text) {
                // 清理文本：替换换行符，转义双引号，截取前20个字
                text = text.replace(/\\n/g, ' ').replace(/"/g, '\\\\"').substring(0, 20).trim();
                if (text.length > 0) {
                    // 🚀 核心改造：抛弃 tagName:has-text()，直接使用原生 text= 选择器
                    // Playwright 会自动定位到包含该文本的最深层子节点！
                    return `text="${text}"`; 
                }
            }
            
            // 优先级 3: 属性兜底 (如输入框的占位符)
            if (el.placeholder) return `${el.tagName.toLowerCase()}[placeholder="${el.placeholder}"]`;
            
            // 优先级 4: ID 兜底 (增加动态 Hash 过滤机制)
            // 如果 ID 里包含一堆数字或者下划线，极大概率是动态生成的，抛弃它！
            if (el.id && !el.id.match(/[0-9]{3,}/) && !el.id.includes('_')) {
                return `#${el.id}`;
            }
            
            // 优先级 5: 如果是纯粹的跳转链接
            if (el.getAttribute('href')) return `${el.tagName.toLowerCase()}[href="${el.getAttribute('href')}"]`;
            
            return null; // 翻译失败
        }
        """
        try:
            locator = page.locator(target).first
            stable_sel = await locator.evaluate(js_code)
            if stable_sel:
                logger.info(f"🔄 稳定器翻译成功: {target}  -->  {stable_sel}")
                return stable_sel
            return target
        except Exception as e:
            logger.debug(f"翻译选择器失败, 退回原值: {e}")
            return target

    @staticmethod
    async def get_element_fingerprint(page: Page, target: str) -> dict:
        """根据选择器提取元素的多维特征指纹"""
        if not target or target == "body":
            return {}
            
        js_code = """
        (el) => {
            if (!el) return null;
            return {
                tag_name: el.tagName.toLowerCase(),
                text: (el.innerText || el.value || "").trim().substring(0, 50).replace(/\\n/g, ' '),
                aria_label: el.getAttribute('aria-label') || "",
                placeholder: el.placeholder || "",
                classes: Array.from(el.classList).join(' ')
            };
        }
        """
        try:
            locator = page.locator(target).first
            # 等待元素可见，确保能抓到属性
            await locator.wait_for(state="attached", timeout=3000)
            fp = await locator.evaluate(js_code)
            return fp if fp else {}
        except Exception as e:
            logger.debug(f"提取元素指纹失败: {target}, 错误: {e}")
            return {}

    @staticmethod
    async def fuzzy_find_by_fingerprint(page: Page, fingerprint: dict) -> str:
        """根据指纹反向推导可用的选择器（第一层自愈）"""
        if not fingerprint:
            return None
            
        text = fingerprint.get("text")
        tag = fingerprint.get("tag_name")
        aria_label = fingerprint.get("aria_label")
        placeholder = fingerprint.get("placeholder")

        # 策略 1: 文本 + 标签 强匹配
        if text and tag:
            # 过滤掉太短的文本，避免误杀
            if len(text) > 1:
                return f"{tag}:has-text('{text}')"
        
        # 策略 2: Aria-label 匹配
        if aria_label:
            return f"{tag}[aria-label='{aria_label}']" if tag else f"[aria-label='{aria_label}']"
            
        # 策略 3: Placeholder 匹配
        if placeholder:
            return f"{tag}[placeholder='{placeholder}']" if tag else f"[placeholder='{placeholder}']"
            
        # 策略 4: 仅靠可见文本兜底
        if text:
            return f"text='{text}'"

        return None
