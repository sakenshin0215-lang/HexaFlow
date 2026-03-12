import logging
from playwright.async_api import BrowserContext


logger = logging.getLogger("HexaEngine")


class EngineRecordMixin:
    async def run_manual_record_task(self, goal: str, agent, start_url: str = "https://www.google.com", context: BrowserContext = None):
        """
        人工专家示教模式：用户手动点击，系统拦截并由 AI 分析记录
        """
        from datetime import datetime
        import asyncio
        from hexaflow.core.trace_recorder import TraceRecorder
        
        logger.info(f"🎥 开始人工演示录制模式: {goal}")
        task_name = "ManualTask_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        recorder = TraceRecorder(task_name=task_name)
        
        if not context:
            context_options = {'viewport': {'width': 1280, 'height': 800}}
            context = await self._resolve_context(context=context, context_options=context_options)
            
        page = await context.new_page()
        action_queue = asyncio.Queue()
        
        # 暴露给浏览器的回调函数，用于接收点击事件
        async def on_user_action(action_payload):
            await action_queue.put(action_payload)
            
        await context.expose_function("reportUserAction", on_user_action)
        
        # 注入全局 JS，拦截真实的物理点击
        js_injector = """
        // 注意：这里不需要 () => {} 包裹，add_init_script 会直接按顺序执行这里的代码
        if (!window._hexaRecorderInjected) {
            window._hexaRecorderInjected = true;
            window._hexaLastTypeReport = null;
            
            document.addEventListener('click', (e) => {
                // 如果是脚本代点的，或者是鼠标右键，则忽略
                if (window._isAutomated) return; 
                if (e.button !== 0 || !e.isTrusted) return;
                
                // 🛑 拦截动作，防止页面跳转或触发前端真实逻辑
                e.preventDefault();
                e.stopPropagation();
                
                // 向上寻找有意义的可点击父元素 (处理点击到 icon 内部 <path> 的情况)
                let el = e.target;
                while (el && el.nodeType === 1 && !el.innerText && !el.getAttribute('aria-label') && el.parentElement) {
                    if (['BUTTON', 'A'].includes(el.tagName)) break;
                    el = el.parentElement;
                }
                if (!el || el.nodeType !== 1) el = e.target; // 兜底：退回原始点击元素
                
                // 安全地提取多维指纹
                const fp = {
                    tag_name: el.tagName ? el.tagName.toLowerCase() : "unknown",
                    text: (el.innerText || el.value || "").trim().substring(0, 50).replace(/\\n/g, ' '),
                    aria_label: el.getAttribute ? (el.getAttribute('aria-label') || "") : "",
                    placeholder: el.placeholder || "",
                    classes: (el.classList ? Array.from(el.classList).join(' ') : "")
                };
                
                // 提取用于录制的候选选择器
                let target_selector = "body";
                if (fp.text) target_selector = `text="${fp.text}"`;
                else if (el.id) target_selector = `#${el.id}`;
                else target_selector = fp.tag_name;
                
                // 给当前元素打上临时标记，方便 AI 确认后 Playwright 准确代点
                const tempId = 'hexa-manual-' + Math.random().toString(36).substr(2, 9);
                if (el.setAttribute) el.setAttribute('data-manual-target', tempId);
                
                // 呼叫 Python 端
                if (window.reportUserAction) {
                    window.reportUserAction({
                        action_type: 'click',
                        target_selector: target_selector,
                        exact_selector: `[data-manual-target="${tempId}"]`,
                        fingerprint: fp,
                        url: window.location.href
                    });
                }
            }, { capture: true }); // 使用捕获阶段优先拦截

            // 记录输入动作：在 change 阶段上报，避免每个按键都刷一条
            document.addEventListener('change', (e) => {
                if (window._isAutomated) return;
                if (!e.isTrusted) return;
                const el = e.target;
                if (!el || el.nodeType !== 1) return;

                const tag = el.tagName ? el.tagName.toLowerCase() : '';
                const isEditable = el.isContentEditable || ['input', 'textarea', 'select'].includes(tag);
                if (!isEditable) return;

                let inputValue = '';
                if (el.isContentEditable) inputValue = (el.innerText || '').trim();
                else inputValue = (el.value || '').trim();
                if (!inputValue) return;

                const fp = {
                    tag_name: tag || "unknown",
                    text: (el.innerText || el.value || "").trim().substring(0, 50).replace(/\\n/g, ' '),
                    aria_label: el.getAttribute ? (el.getAttribute('aria-label') || "") : "",
                    placeholder: el.placeholder || "",
                    classes: (el.classList ? Array.from(el.classList).join(' ') : "")
                };

                let target_selector = tag || 'input';
                if (el.id) target_selector = `#${el.id}`;
                else if (fp.aria_label) target_selector = `${tag}[aria-label="${fp.aria_label}"]`;
                else if (fp.placeholder) target_selector = `${tag}[placeholder="${fp.placeholder}"]`;

                // 去抖：相同目标+相同值+相同URL在2秒内不重复上报
                const reportKey = `${window.location.href}|${target_selector}|${inputValue}`;
                const now = Date.now();
                if (window._hexaLastTypeReport && window._hexaLastTypeReport.key === reportKey && now - window._hexaLastTypeReport.ts < 2000) {
                    return;
                }
                window._hexaLastTypeReport = { key: reportKey, ts: now };

                if (window.reportUserAction) {
                    window.reportUserAction({
                        action_type: 'type',
                        target_selector: target_selector,
                        exact_selector: null,
                        input_value: inputValue,
                        fingerprint: fp,
                        url: window.location.href
                    });
                }
            }, { capture: true });
        }
        """
        # 确保每个页面/刷新后都注入拦截器
        # 确保每个页面/刷新后都注入拦截器
        await context.add_init_script(script=js_injector)
        
        # 1. 打开初始网页
        await page.goto(start_url)
        
        # 👇 2. 新增：自动将“打开网页”作为轨迹的第一步悄悄录制下来！
        recorder.record_step(
            current_url=start_url,
            action_type="navigate",
            target=start_url,
            description=f"访问初始网页: {start_url}"
        )
        
        logger.info("\n" + "="*50)
        logger.info(f"👉 浏览器已准备好！请在弹出的页面中开始操作: {start_url}")
        logger.info("="*50 + "\n")
        
        while True:
            # 阻塞等待前端传回的点击数据
            action_data = await action_queue.get()
            fp = action_data.get('fingerprint', {})
            target_sel = action_data.get('target_selector', '')
            exact_sel = action_data.get('exact_selector', '')
            input_value = action_data.get('input_value', None)
            
            if action_data.get('action_type') == 'type':
                logger.info(f"\n⌨️ 检测到你的输入: <{fp.get('tag_name')}> value='{(input_value or '')[:60]}'")
            else:
                logger.info(f"\n⚡ 检测到你的点击: <{fp.get('tag_name')}> '{fp.get('text')}'")
            
            # AI 意图分析
            analysis = await agent.analyze_manual_action(goal, action_data)
            logger.info(f"💡 AI 意图理解: {analysis.thought}")
            logger.info(f"📝 拟录制描述: {analysis.description}")
            
            loop = asyncio.get_running_loop()
            prompt_msg = (
                "\n👉 录制这步操作吗？"
                "(y: 录制并放行 / o: 设为可选并放行 / "
                "ls: 录制并标记循环开始 / le: 录制并标记循环结束 / "
                "n: 舍弃 / done: 结束录制): "
            )
            user_input = await loop.run_in_executor(None, input, prompt_msg)
            user_input = user_input.strip().lower()
            
            if user_input in ['done', 'd', 'quit']:
                logger.info("🛑 示教录制结束，正在保存轨迹...")
                recorder.save_to_disk()
                break
                
            elif user_input in ['y', 'o', '', 'ls', 'le']:
                is_opt = (user_input == 'o')
                loop_marker = None
                if user_input == 'ls':
                    loop_marker = 'start'
                elif user_input == 'le':
                    loop_marker = 'end'
                recorder.record_step(
                    current_url=action_data['url'],
                    action_type=action_data['action_type'],
                    target=target_sel,
                    input_value=input_value,
                    description=analysis.description,
                    is_optional=is_opt,
                    fingerprint_dict=fp,
                    loop_marker=loop_marker,
                    loop_name="main_loop" if loop_marker else None,
                )
                
                if action_data['action_type'] == 'click' and exact_sel:
                    logger.info("✅ 步骤已录制！正在代您执行真实的点击，让页面继续流转...")
                    try:
                        # 开启白名单，绕过我们的拦截器代点，然后关闭白名单
                        await page.evaluate("window._isAutomated = true;")
                        await page.locator(exact_sel).first.click(timeout=3000)
                        await page.evaluate("window._isAutomated = false;")
                    except Exception as e:
                        # 页面如果因点击发生了导航，上面的设 false 可能会报错，这是正常现象，直接忽略
                        logger.debug(f"释放点击动作后续状态变更: {e}")
                else:
                    logger.info("✅ 输入步骤已录制，页面已是用户真实输入后的状态，无需代点。")
            else:
                logger.info("🚫 已舍弃该操作，该点击不会生效，请重新选择目标。")

        await page.close()
