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
                let text = (el.innerText || el.value || el.placeholder || '').trim().substring(0, 60).replace(/\\n/g, ' ');
                let ariaLabel = el.getAttribute('aria-label') || '';
                let href = el.getAttribute('href') || '';
                
                // 只保留有意义的元素（有文本或有aria-label的）
                if (text || ariaLabel || tag === 'input') {
                    let info = `[ID: ${hexaId}] <${tag}`;
                    if (text) info += ` text="${text}"`;
                    if (ariaLabel) info += ` aria-label="${ariaLabel}"`;
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
            
            // 优先级 1: 开发者留的测试钩子或无障碍标签 (极其稳定)
            if (el.getAttribute('data-testid')) return `[data-testid="${el.getAttribute('data-testid')}"]`;
            if (el.getAttribute('aria-label')) return `${el.tagName.toLowerCase()}[aria-label="${el.getAttribute('aria-label')}"]`;
            
            // 优先级 2: 用户可见文本 (最符合业务直觉，抗前端重构能力最强)
            let text = (el.innerText || el.value || '').trim();
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