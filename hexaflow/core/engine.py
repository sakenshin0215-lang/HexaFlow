import os
import asyncio
import logging
import json
import hashlib
import re
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Page, BrowserContext
from hexaflow.browser.cdp_runtime import CDPConfig, ensure_cdp_browser
from hexaflow.core.trace_recorder import TraceRecorder
from hexaflow.core.state_machine import StateMachine
from hexaflow.core.run_reporter import RunReporter
from hexaflow.core.task_schema import TaskSpec
from hexaflow.core.engine_support_mixin import EngineSupportMixin
from hexaflow.tools.dom_parser import DomParser
from hexaflow.tools.token_selector import ensure_quote_token
from hexaflow.agents.react_agent import NextAction

import random

# ==========================================
# 0. 配置日志
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] Engine: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("HexaEngine")

class HexaEngine(EngineSupportMixin):
    def __init__(
        self,
        headless: bool = False,
        state_path: str = "memory/workspace/auth_state.json",
        runtime_db_path: str = "memory/workspace/state/runtime.db",
    ):
        """
        初始化执行引擎
        :param headless: 是否无头模式运行。调试时建议设为 False 观看浏览器动作
        """
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
        
    async def start(self, use_cdp: bool = False, cdp_config: CDPConfig = None, cdp_start_url: str = "about:blank"):
        """启动浏览器环境"""
        logger.info("🚀 正在启动 Playwright 引擎...")
        self.playwright = await async_playwright().start()
        self.use_cdp = use_cdp

        if use_cdp:
            self.cdp_config = cdp_config or CDPConfig()
            # ensure_cdp_browser 是同步函数，用线程池避免阻塞事件循环
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, ensure_cdp_browser, self.cdp_config, cdp_start_url
            )
            self.browser = await self.playwright.chromium.connect_over_cdp(
                self.cdp_config.endpoint
            )
            logger.info(
                f"✅ 已通过CDP连接浏览器: {self.cdp_config.endpoint} | "
                f"user_data_dir={self.cdp_config.user_data_dir} | "
                f"profile={self.cdp_config.profile_directory}"
            )
            return

        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        logger.info("✅ 浏览器启动成功 (Playwright launch)")

    async def stop(self):
        """关闭浏览器环境"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("🛑 引擎已关闭")

    async def _detect_human_intervention_reason(self, page: Page) -> str:
        url = (page.url or "").lower()
        if any(k in url for k in ["login", "signin", "auth", "verify", "captcha", "challenge"]):
            return f"URL 命中登录/验证路径: {page.url}"

        # 通用站点上“登录/Sign in”非常常见（如 Google 首页），避免误判。
        # 非认证路径下仅检查高置信度人工介入信号。
        text_checks = [
            "重新登录", "验证码", "人机验证", "二次验证", "双重验证",
            "Verify", "CAPTCHA", "2FA", "Authentication challenge",
            "解锁钱包", "Unlock", "Wallet password", "请输入密码"
        ]
        for t in text_checks:
            try:
                if await page.get_by_text(t, exact=False).first.is_visible(timeout=800):
                    return f"页面检测到人工验证文案: {t}"
            except Exception:
                continue
        return ""

    async def _human_handoff_if_needed(self, page: Page, checkpoint: str = "", enabled: bool = True) -> bool:
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

    async def _save_browser_state(self, context: BrowserContext):
        if self.state_path:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            await context.storage_state(path=self.state_path)
            logger.info(f"💾 浏览器状态(Cookie/缓存)已永久保存至: {self.state_path}")

    async def _resolve_context(self, context: BrowserContext = None, context_options: dict = None):
        """
        统一获取可用 context：
        - 传入 context 时优先使用
        - CDP 模式默认复用 browser.contexts[0]（保留真实 profile、扩展、登录态）
        - 非 CDP 模式创建新 context
        """
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

    async def _run_ai_heal_agent(
        self,
        page: Page,
        ai_heal_agent,
        task_name: str,
        recent_steps: str,
        failed_step_desc: str,
        failed_action_type: str,
        failed_target: str,
        last_error: str,
        failed_input_value: str = None,
        step_id: str = "",
        step_index: int = -1,
        run_id: str = "",
        validate_coro=None,
        human_handoff_on_auth: bool = True,
        max_attempts: int = 3,
        heal_log: dict = None,
        expected_url_contains: str = "",
    ):
        """
        AI 修复链路：
        1) 传入最近步骤 + 当前DOM + 截图 + 上一次尝试历史
        2) 执行 AI 提议动作后验证是否恢复
        3) 连续失败 max_attempts 次后，触发人工接管
        """
        if not ai_heal_agent or not hasattr(ai_heal_agent, "repair_failed_replay_step"):
            return {"resolved": False, "outcome": "no_agent", "detail": "missing ai heal agent"}

        avoid_modal_dismiss = False
        try:
            if failed_action_type in ("click", "type", "click_type_enter"):
                avoid_modal_dismiss = await self._is_target_text_inside_dialog(page, failed_target)
        except Exception:
            avoid_modal_dismiss = False

        attempts_log = []
        for attempt in range(1, max_attempts + 1):
            try:
                if failed_action_type == "click" and await self._is_click_intent_satisfied(page, failed_step_desc, failed_target):
                    self._append_heal_record(
                        heal_log,
                        phase="ai_validate",
                        step_id=step_id,
                        step_index=step_index,
                        result="ok",
                        detail=f"attempt={attempt}/{max_attempts} intent_already_satisfied target={failed_target}",
                    )
                    logger.info(f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 检测到步骤意图已满足，结束修复")
                    return {
                        "resolved": True,
                        "outcome": "applied",
                        "detail": "intent_already_satisfied",
                    }
            except Exception:
                pass

            # 优先尝试关闭遮挡弹窗（X/关闭/跳过），避免进入“下一步”教程链
            try:
                dismissed = False
                if not avoid_modal_dismiss:
                    dismissed = await self._dismiss_blocking_modal(page)
                if dismissed:
                    self._append_heal_record(
                        heal_log,
                        phase="modal_dismiss",
                        step_id=step_id,
                        step_index=step_index,
                        result="ok",
                        detail=f"attempt={attempt}/{max_attempts}",
                    )
                    if validate_coro:
                        try:
                            await validate_coro()
                            logger.info(f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 关闭弹窗后验证成功")
                            return {
                                "resolved": True,
                                "outcome": "applied",
                                "detail": "modal_dismiss_then_validated",
                            }
                        except Exception:
                            pass
            except Exception:
                pass

            # 修复期间保持页面上下文稳定，防止误点后漂移到错误页面
            if expected_url_contains and expected_url_contains not in (page.url or ""):
                inferred = self._normalize_url_candidate(expected_url_contains)
                if inferred:
                    try:
                        await page.goto(inferred, wait_until="domcontentloaded", timeout=12000)
                        await page.wait_for_timeout(600)
                    except Exception:
                        pass

            screenshot_paths = await self._capture_heal_visual_pack(
                page, run_id or "aiheal", step_id or "unknown"
            )
            screenshot_path = screenshot_paths[0] if screenshot_paths else ""
            await self._wait_for_page_settle(page)
            dom_snapshot = await DomParser.get_interactive_elements(page)
            attempts_text = "\n".join(
                [f"{idx + 1}. {item}" for idx, item in enumerate(attempts_log)]
            ) or "None."
            composed_error = (
                f"{last_error}\n"
                f"[ai_heal_attempt]={attempt}/{max_attempts}\n"
                f"[screenshots]={screenshot_paths if screenshot_paths else 'N/A'}\n"
                f"[previous_attempts]\n{attempts_text}"
            )

            try:
                decision = await ai_heal_agent.repair_failed_replay_step(
                    task_name=task_name,
                    recent_steps=recent_steps,
                    current_url=page.url,
                    dom_snapshot=dom_snapshot,
                    failed_step_desc=failed_step_desc,
                    failed_action_type=failed_action_type,
                    failed_target=failed_target,
                    last_error=composed_error,
                    screenshot_path=screenshot_path or "",
                    screenshot_paths=screenshot_paths,
                )
            except TypeError:
                # 兼容旧版 agent 签名（不支持 screenshot_path）
                decision = await ai_heal_agent.repair_failed_replay_step(
                    task_name=task_name,
                    recent_steps=recent_steps,
                    current_url=page.url,
                    dom_snapshot=dom_snapshot,
                    failed_step_desc=failed_step_desc,
                    failed_action_type=failed_action_type,
                    failed_target=failed_target,
                    last_error=composed_error,
                )

            if run_id:
                self.state_machine.add_event(
                    run_id,
                    event="ai_heal_decision",
                    detail=(
                        f"step={step_id} attempt={attempt}/{max_attempts} "
                        f"strategy={decision.strategy} confidence={decision.confidence:.2f} "
                        f"thought={decision.thought}"
                    ),
                    step_index=step_index,
                    step_id=step_id,
                )
            self._append_heal_record(
                heal_log,
                phase="ai_decision",
                step_id=step_id,
                step_index=step_index,
                result=decision.strategy,
                detail=(
                    f"attempt={attempt}/{max_attempts} confidence={decision.confidence:.2f} "
                    f"target={decision.target or ''} action={decision.action_type or ''} "
                    f"screenshots={screenshot_paths if screenshot_paths else []}"
                ),
            )

            if decision.strategy == "skip_step":
                reason = decision.skip_reason or "AI heal: step skipped"
                self._append_heal_record(
                    heal_log,
                    phase="ai_apply",
                    step_id=step_id,
                    step_index=step_index,
                    result="skipped",
                    detail=reason,
                )
                return {"resolved": True, "outcome": "skipped", "detail": reason}

            if decision.strategy == "human_handoff":
                await self._human_handoff_if_needed(
                    page,
                    checkpoint=f"AI修复建议人工接管(step_id={step_id or 'dynamic'})",
                    enabled=human_handoff_on_auth,
                )
                self._append_heal_record(
                    heal_log,
                    phase="ai_apply",
                    step_id=step_id,
                    step_index=step_index,
                    result="human_handoff",
                    detail="manual intervention requested",
                )
                if validate_coro:
                    try:
                        await validate_coro()
                        logger.info(f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 人工接管后验证成功")
                        return {
                            "resolved": True,
                            "outcome": "human_handoff",
                            "detail": "human intervention validated",
                        }
                    except Exception as handoff_err:
                        logger.warning(
                            f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 人工接管后验证失败: {handoff_err}"
                        )
                        attempts_log.append(
                            f"strategy=human_handoff validated_failed error={handoff_err}"
                        )
                        continue
                attempts_log.append("strategy=human_handoff but no validate_coro")
                continue

            if decision.strategy in ("retry_with_new_selector", "replace_action"):
                if decision.strategy == "retry_with_new_selector":
                    action_type = failed_action_type
                    target = decision.target
                    input_value = failed_input_value
                    if not target:
                        self._append_heal_record(
                            heal_log,
                            phase="ai_apply",
                            step_id=step_id,
                            step_index=step_index,
                            result="failed",
                            detail="retry_with_new_selector returned empty target",
                        )
                        attempts_log.append("retry_with_new_selector returned empty target")
                        continue
                else:
                    action_type = decision.action_type or failed_action_type
                    target = decision.target
                    input_value = decision.input_value
                used_new_target = bool(target and target != failed_target)

                # AI修复点击动作统一坐标化执行（不直接按文本/selector点击）
                if action_type in ("click", "click_type_enter"):
                    ratio = None
                    # 优先用 AI 给的 target 解析坐标，失败则回退原失败 target
                    if target:
                        ratio = await self._selector_to_click_ratio(page, target)
                    if (not ratio) and failed_target:
                        ratio = await self._selector_to_click_ratio(page, failed_target)
                    if ratio:
                        x_ratio, y_ratio = ratio
                        if action_type == "click":
                            action_type = "click_relative"
                            input_value = f"{x_ratio:.6f},{y_ratio:.6f}"
                            target = None
                        else:
                            # click_type_enter -> 坐标点击后键盘输入回车
                            action_type = "click_relative_type_enter"
                            target = f"{x_ratio:.6f},{y_ratio:.6f}"
                    else:
                        # 坐标不可用时回退 selector 执行，避免“坐标解析失败”卡死
                        fallback_selector = target or failed_target
                        if fallback_selector:
                            self._append_heal_record(
                                heal_log,
                                phase="ai_apply",
                                step_id=step_id,
                                step_index=step_index,
                                result="fallback_selector",
                                detail=f"coordinate_resolve_failed, fallback_selector={fallback_selector}",
                            )
                            logger.warning(
                                f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 坐标解析失败，回退 selector 执行: {fallback_selector}"
                            )
                            target = fallback_selector
                        else:
                            self._append_heal_record(
                                heal_log,
                                phase="ai_apply",
                                step_id=step_id,
                                step_index=step_index,
                                result="failed",
                                detail=f"coordinate_resolve_failed target={target} fallback_target={failed_target}",
                            )
                            attempts_log.append(
                                f"coordinate_resolve_failed target={target} fallback_target={failed_target}"
                            )
                            logger.warning(
                                f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 坐标解析失败，进入下一轮"
                            )
                            continue
                pre_attempt_sig = ""
                try:
                    pre_attempt_sig = await self._compute_page_state_signature(page)
                except Exception:
                    pre_attempt_sig = ""
                exec_err = None
                try:
                    await self._execute_override_action(
                        page=page,
                        action_type=action_type,
                        target=target,
                        input_value=input_value,
                    )
                except Exception as e:
                    exec_err = e
                    self._append_heal_record(
                        heal_log,
                        phase="ai_apply",
                        step_id=step_id,
                        step_index=step_index,
                        result="failed",
                        detail=f"action={action_type} target={target} exec_error={exec_err}",
                    )
                    logger.warning(
                        f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 动作执行失败: {exec_err}"
                    )

                # 每次修复动作后都立即验证一次“原步骤是否已恢复”
                # 注意：若 AI 已切换到新 target，强行用旧 target 验证会误判失败。
                # 这种情况下优先认定本次修复已应用成功，交给主流程继续。
                if used_new_target and decision.strategy == "retry_with_new_selector":
                    self._append_heal_record(
                        heal_log,
                        phase="ai_validate",
                        step_id=step_id,
                        step_index=step_index,
                        result="ok",
                        detail=(
                            f"attempt={attempt}/{max_attempts} "
                            f"skip_old_target_validation old={failed_target} new={target}"
                        ),
                    )
                    logger.info(
                        f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 已应用新selector，跳过旧目标强验证"
                    )
                    return {
                        "resolved": True,
                        "outcome": "applied",
                        "detail": f"action={action_type} target={target}",
                    }

                if exec_err is None:
                    try:
                        post_action_sig = await self._compute_page_state_signature(page)
                        if post_action_sig != pre_attempt_sig:
                            self._append_heal_record(
                                heal_log,
                                phase="ai_validate",
                                step_id=step_id,
                                step_index=step_index,
                                result="ok",
                                detail=f"attempt={attempt}/{max_attempts} state_signature_changed",
                            )
                            logger.info(
                                f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 检测到状态签名变化，视为修复成功"
                            )
                            return {
                                "resolved": True,
                                "outcome": "applied",
                                "detail": "state_signature_changed",
                            }
                    except Exception:
                        pass

                if validate_coro:
                    try:
                        await validate_coro()
                        self._append_heal_record(
                            heal_log,
                            phase="ai_validate",
                            step_id=step_id,
                            step_index=step_index,
                            result="ok",
                            detail=f"attempt={attempt}/{max_attempts}",
                        )
                        logger.info(f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 验证成功")
                        return {
                            "resolved": True,
                            "outcome": "applied",
                            "detail": f"action={action_type} target={target}",
                        }
                    except Exception as validate_err:
                        try:
                            if failed_action_type == "click" and await self._is_click_intent_satisfied(page, failed_step_desc, failed_target):
                                self._append_heal_record(
                                    heal_log,
                                    phase="ai_validate",
                                    step_id=step_id,
                                    step_index=step_index,
                                    result="ok",
                                    detail=f"attempt={attempt}/{max_attempts} intent_satisfied_after_validate_error target={failed_target}",
                                )
                                logger.info(
                                    f"✅ [AI修复] 尝试 {attempt}/{max_attempts} 虽验证报错但步骤意图已满足，结束修复"
                                )
                                return {
                                    "resolved": True,
                                    "outcome": "applied",
                                    "detail": "intent_satisfied_after_validate_error",
                                }
                        except Exception:
                            pass
                        self._append_heal_record(
                            heal_log,
                            phase="ai_validate",
                            step_id=step_id,
                            step_index=step_index,
                            result="failed",
                            detail=f"attempt={attempt}/{max_attempts} error={validate_err}",
                        )
                        logger.warning(
                            f"⚠️ [AI修复] 尝试 {attempt}/{max_attempts} 验证失败: {validate_err}"
                        )
                        attempts_log.append(
                            f"strategy={decision.strategy} action={action_type} target={target} "
                            f"exec_error={exec_err} validate_error={validate_err}"
                        )
                        continue

                # 无 validate 时，以动作执行结果判定
                if exec_err is None:
                    self._append_heal_record(
                        heal_log,
                        phase="ai_apply",
                        step_id=step_id,
                        step_index=step_index,
                        result="applied",
                        detail=f"action={action_type} target={target}",
                    )
                    return {
                        "resolved": True,
                        "outcome": "applied",
                        "detail": f"action={action_type} target={target}",
                    }
                attempts_log.append(
                    f"strategy={decision.strategy} action={action_type} target={target} exec_error={exec_err}"
                )
                continue

            attempts_log.append(
                f"strategy={decision.strategy} no executable fix returned"
            )
            self._append_heal_record(
                heal_log,
                phase="ai_apply",
                step_id=step_id,
                step_index=step_index,
                result="no_fix",
                detail=f"strategy={decision.strategy}",
            )

        # 三次仍失败，要求人工介入
        await self._human_handoff_if_needed(
            page,
            checkpoint=f"AI修复连续失败{max_attempts}次(step_id={step_id or 'dynamic'})",
            enabled=True,
        )
        return {
            "resolved": False,
            "outcome": "failed_after_retries",
            "detail": "\n".join(attempts_log),
        }

    async def _replay_recent_subflow(
        self,
        page: Page,
        blueprint,
        step_index: int,
        window: int = 2,
        skip_risky: bool = True,
        risky_keywords: list[str] = None,
    ) -> bool:
        """
        回放前 N 步小流程，尝试把页面状态带回正确上下文。
        """
        if risky_keywords is None:
            risky_keywords = [
                "确认",
                "提交",
                "兑换",
                "swap",
                "approve",
                "授权",
                "sign",
                "签名",
                "购买",
                "buy",
                "sell",
                "下单",
                "pay",
                "付款",
                "connect wallet",
                "连接钱包",
            ]
        start = max(0, step_index - window)
        if start >= step_index:
            return False

        for idx in range(start, step_index):
            replay_step = blueprint.steps[idx]
            if skip_risky and self._is_risky_step_for_replay(replay_step, risky_keywords):
                continue
            try:
                await self._execute_deterministic_step(page, replay_step)
            except Exception:
                return False
        return True

    def _save_run_report(self, run_id: str):
        try:
            report_paths = self.run_reporter.save_report(run_id)
            logger.info(
                f"🧾 执行报告已生成: json={report_paths['json_path']} md={report_paths['md_path']}"
            )
            return report_paths
        except Exception as e:
            logger.error(f"❌ 报告生成失败(run_id={run_id}): {e}")
            return None

    @staticmethod
    def _create_heal_log(task_name: str, run_id: str, trace_path: str):
        from datetime import datetime
        return {
            "task_name": task_name,
            "run_id": run_id,
            "trace_path": trace_path,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "ended_at": "",
            "status": "running",
            "records": [],
        }

    @staticmethod
    def _append_heal_record(heal_log: dict, **kwargs):
        from datetime import datetime
        if heal_log is None:
            return
        row = {"ts": datetime.now().isoformat(timespec="seconds")}
        row.update(kwargs)
        heal_log["records"].append(row)

    def _save_heal_log(self, heal_log: dict):
        if not heal_log:
            return None
        from datetime import datetime
        os.makedirs("memory/workspace/reports", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = heal_log.get("run_id", "unknown")
        base = f"heal_{run_id}_{ts}"
        json_path = os.path.join("memory/workspace/reports", f"{base}.json")
        md_path = os.path.join("memory/workspace/reports", f"{base}.md")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(heal_log, f, ensure_ascii=False, indent=2)

        lines = [
            f"# 修复清单 - {heal_log.get('task_name', '')}",
            "",
            f"- RunID: `{heal_log.get('run_id', '')}`",
            f"- Trace: `{heal_log.get('trace_path', '')}`",
            f"- Status: `{heal_log.get('status', '')}`",
            f"- Started: `{heal_log.get('started_at', '')}`",
            f"- Ended: `{heal_log.get('ended_at', '')}`",
            "",
            "## 记录",
            "",
        ]
        records = heal_log.get("records", [])
        if not records:
            lines.append("- 无修复记录")
        else:
            for i, rec in enumerate(records, start=1):
                lines.append(
                    f"{i}. [{rec.get('ts')}] phase={rec.get('phase', '')} "
                    f"step={rec.get('step_id', '')} result={rec.get('result', '')} "
                    f"detail={rec.get('detail', '')}"
                )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"🩹 修复清单已生成: json={json_path} md={md_path}")
        return {"json_path": json_path, "md_path": md_path}

    def _save_ai_action_log(self, task_name: str, actions: list[dict]):
        from datetime import datetime
        os.makedirs("memory/workspace/reports", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_task = (task_name or "ai_task").replace("/", "_").replace(" ", "_")
        base = f"ai_actions_{safe_task}_{ts}"
        json_path = os.path.join("memory/workspace/reports", f"{base}.json")
        md_path = os.path.join("memory/workspace/reports", f"{base}.md")

        payload = {
            "task_name": task_name,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total_actions": len(actions),
            "actions": actions,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        lines = [f"# AI 操作日志 - {task_name}", "", f"- total_actions: {len(actions)}", "", "## Actions", ""]
        if not actions:
            lines.append("- 无动作")
        else:
            for i, a in enumerate(actions, start=1):
                lines.append(
                    f"{i}. step={a.get('step')} action={a.get('action_type')} target={a.get('target')} "
                    f"success={a.get('success')} note={a.get('note', '')}"
                )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"🤖 AI操作日志已生成: json={json_path} md={md_path}")
        return {"json_path": json_path, "md_path": md_path}

    @staticmethod
    def _is_domain_allowed(target_url: str, allowed_domains: list[str]) -> bool:
        if not allowed_domains:
            return True
        host = (urlparse(target_url).hostname or "").lower()
        if not host:
            return False
        for domain in allowed_domains:
            d = domain.lower().strip()
            if host == d or host.endswith(f".{d}"):
                return True
        return False

    @staticmethod
    def _contains_blocked_keyword(raw_text: str, blocked_keywords: list[str]) -> bool:
        if not blocked_keywords:
            return False
        lower_text = (raw_text or "").lower()
        return any(k.lower().strip() and k.lower().strip() in lower_text for k in blocked_keywords)

    @staticmethod
    def _build_goal_with_notes(spec: TaskSpec) -> str:
        if not spec.notes:
            return spec.goal
        notes_text = "\n".join([f"- {item}" for item in spec.notes])
        return f"{spec.goal}\n\n执行注意事项:\n{notes_text}"

    async def run_task_from_spec(
        self,
        spec_input,
        agent,
        context: BrowserContext = None,
        replay_repair_agent=None,
        disable_fallback_recovery: bool = False,
        ai_decision_use_vision: bool = True,
    ):
        if isinstance(spec_input, TaskSpec):
            spec = spec_input
        elif isinstance(spec_input, str):
            spec = TaskSpec.from_json_file(spec_input)
        elif isinstance(spec_input, dict):
            spec = TaskSpec.model_validate(spec_input)
        else:
            raise ValueError("Unsupported task spec input type")

        logger.info(f"📘 载入任务DSL: {spec.task_name}")
        return await self.run_dynamic_task(
            goal=self._build_goal_with_notes(spec),
            agent=agent,
            context=context,
            max_steps=spec.max_steps,
            manual_review=spec.manual_review,
            start_url=spec.start_url,
            allowed_domains=spec.allowed_domains,
            blocked_keywords=spec.blocked_keywords,
            max_consecutive_failures=spec.failure_policy.max_consecutive_failures,
            failure_mode=spec.failure_policy.mode,
            replay_repair_agent=replay_repair_agent,
            disable_fallback_recovery=disable_fallback_recovery,
            ai_decision_use_vision=ai_decision_use_vision,
            completion_checks=[c.model_dump() for c in spec.completion_checks],
            completion_logic=spec.completion_logic,
        )

    async def run_dynamic_task(
        self,
        goal: str,
        agent,
        context: BrowserContext = None,
        max_steps: int = 15,
        manual_review: bool = True,
        start_url: str = "about:blank",
        allowed_domains: list[str] = None,
        blocked_keywords: list[str] = None,
        max_consecutive_failures: int = 999999,
        failure_mode: str = "continue",
        human_handoff_on_auth: bool = True,
        replay_repair_agent=None,
        repair_context_window: int = 5,
        disable_fallback_recovery: bool = False,
        ai_decision_use_vision: bool = True,
        completion_checks: list[dict] = None,
        completion_logic: str = "any",
    ):
        from datetime import datetime
        logger.info(f"▶️ 开始执行动态自适应任务: {goal} (manual_review={manual_review})")
        self.disable_fallback_recovery_runtime = disable_fallback_recovery
        allowed_domains = allowed_domains or []
        blocked_keywords = blocked_keywords or []
        
        task_name = "Task_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        recorder = TraceRecorder(task_name=task_name)
        
        ai_heal_agent = replay_repair_agent or (
            agent if hasattr(agent, "repair_failed_replay_step") else None
        )

        if not context:
            context_options = {'viewport': {'width': 1280, 'height': 800}}
            # 非CDP模式下才注入 storage_state；CDP复用真实profile上下文
            if (not self.use_cdp) and self.state_path and os.path.exists(self.state_path):
                logger.info(f"🍪 发现缓存！正在加载本地浏览器状态: {self.state_path}")
                context_options['storage_state'] = self.state_path
            context = await self._resolve_context(context=context, context_options=context_options)

        page = await context.new_page()
        await page.goto(start_url)
        await self._human_handoff_if_needed(
            page, checkpoint="动态任务启动检查", enabled=human_handoff_on_auth
        )
        if start_url != "about:blank":
            recorder.record_step(
                current_url=start_url,
                action_type="navigate",
                target=start_url,
                description=f"访问初始网页: {start_url}",
            )
        
        action_history = []
        ai_action_records = []
        step_count = 0
        consecutive_failures = 0
        stagnant_action_counts = {}
        trace_saved = False
        trace_path = None
        completion_checks = completion_checks or []

        async def evaluate_completion(last_action: dict | None = None):
            if not completion_checks:
                return False, "no_completion_checks"
            results = []
            details = []
            for item in completion_checks:
                ctype = (item.get("check_type") or "").strip()
                value = str(item.get("value") or "").strip()
                if not ctype or not value:
                    continue
                ok = False
                try:
                    if ctype == "url_contains":
                        ok = value in (page.url or "")
                    elif ctype == "text_visible":
                        ok = await page.get_by_text(value, exact=False).first.is_visible(timeout=500)
                    elif ctype == "selector_visible":
                        ok = await page.locator(value).first.is_visible(timeout=500)
                    elif ctype == "action_target_contains":
                        if last_action and last_action.get("success"):
                            expected_action = (item.get("action_type") or "").strip().lower()
                            actual_action = str(last_action.get("action_type") or "").strip().lower()
                            if expected_action and expected_action != actual_action:
                                ok = False
                            else:
                                target = str(last_action.get("target") or "")
                                thought = str(last_action.get("thought") or "")
                                target_hint = self._extract_text_hint_from_selector(target)
                                haystack = f"{target} {target_hint} {thought}".lower()
                                ok = value.lower() in haystack
                except Exception:
                    ok = False
                results.append(ok)
                details.append(f"{ctype}({value})={'ok' if ok else 'miss'}")
            if not results:
                return False, "no_valid_checks"
            if completion_logic == "all":
                return all(results), "; ".join(details)
            return any(results), "; ".join(details)

        while step_count < max_steps:
            step_count += 1
            logger.info(f"\n" + "="*40 + f"\n--- 第 {step_count} 步 ---")
            
            current_url = page.url
            await page.wait_for_timeout(1000) 
            await self._wait_for_page_settle(page)
            dom_snapshot = await DomParser.get_interactive_elements(page)
            step_screenshot = ""
            if ai_decision_use_vision:
                step_screenshot = await self._capture_suspend_snapshot(
                    page, task_name, f"ai_step_{step_count}"
                )
            
            history_str = "\n".join(action_history)
            try:
                next_action = await agent.decide_next_action(
                    goal, history_str, current_url, dom_snapshot, screenshot_path=step_screenshot
                )
            except TypeError:
                next_action = await agent.decide_next_action(goal, history_str, current_url, dom_snapshot)
            
            stable_target = next_action.target
            current_action_log = f"Skipped unknown action: {next_action.action_type}"
            action_success = False
            no_progress_detected = False
            progress_context = None

            if next_action.action_type == "done":
                logger.info("🎉 AI 认为任务已完成！")
                trace_path = recorder.save_to_disk()
                trace_saved = True
                await self._save_browser_state(context)
                break
            else:
                action_raw_text = f"{next_action.action_type} {next_action.target or ''} {next_action.thought or ''}"
                if self._contains_blocked_keyword(action_raw_text, blocked_keywords):
                    current_action_log = f"BLOCKED by keyword policy: {next_action.action_type} on {next_action.target}"
                    logger.warning(f"🛡️ 动作已拦截: {current_action_log}")
                    action_success = False
                else:
                    logger.info(f"⚡ 自动执行: [{next_action.action_type}] 目标: {next_action.target}")
                    pre_state_signature = await self._compute_page_state_signature(page)

                    async def perform_current_action(fast_validate: bool = False):
                        pre_action_sig = ""
                        pre_checkbox_state = None
                        pre_page_count = 0
                        pre_inside_dialog = False
                        if next_action.action_type in ("click", "press_enter"):
                            pre_action_sig = await self._compute_page_state_signature(page)
                            try:
                                pre_page_count = len(page.context.pages)
                            except Exception:
                                pre_page_count = 0

                        if next_action.target and "hexa-id" in (next_action.target or ""):
                            stable = await DomParser.get_stable_selector(page, next_action.target)
                        else:
                            stable = next_action.target
                        try:
                            if next_action.action_type == "click" and stable:
                                pre_inside_dialog = await self._is_target_text_inside_dialog(page, stable)
                        except Exception:
                            pre_inside_dialog = False

                        if next_action.action_type == "navigate":
                            if not self._is_domain_allowed(stable, allowed_domains):
                                raise Exception(f"Navigation blocked by allowed_domains policy: {stable}")
                            await page.goto(stable)
                        elif next_action.action_type == "refresh":
                            await page.reload(wait_until="domcontentloaded", timeout=15000)
                        elif next_action.action_type == "click":
                            if not stable:
                                raise Exception("click 动作缺少 target 选择器")
                            try:
                                loc = page.locator(stable).first
                                pre_checkbox_state = await loc.evaluate(
                                    """(el) => {
                                        const toState = (n) => {
                                            if (!n) return null;
                                            const aria = n.getAttribute && n.getAttribute('aria-checked');
                                            if (aria !== null && aria !== undefined && aria !== '') return String(aria);
                                            if (typeof n.checked === 'boolean') return String(!!n.checked);
                                            return null;
                                        };
                                        let n = null;
                                        if (el.matches && el.matches('[role="checkbox"],input[type="checkbox"]')) {
                                            n = el;
                                        } else {
                                            n =
                                                el.closest('[role="checkbox"],label') ||
                                                (el.querySelector && el.querySelector('[role="checkbox"],input[type="checkbox"]')) ||
                                                null;
                                        }
                                        if (n && n.matches && n.matches('label')) {
                                            const inner = n.querySelector('[role="checkbox"],input[type="checkbox"]');
                                            if (inner) n = inner;
                                        }
                                        return toState(n);
                                    }"""
                                )
                            except Exception:
                                pre_checkbox_state = None
                            await self._robust_click(page, selector=stable)
                        elif next_action.action_type == "type":
                            if not stable:
                                raise Exception("type 动作缺少 target 选择器")
                            locator, stable = await self._resolve_input_locator(page, stable)
                            await self._robust_input_text(page, locator, next_action.input_value or "", press_enter=False)
                        elif next_action.action_type == "click_type_enter":
                            if not stable:
                                raise Exception("click_type_enter 动作缺少 target 选择器")
                            locator = page.locator(stable).first
                            await locator.hover(timeout=5000)
                            await locator.click(timeout=5000)
                            is_editable = False
                            try:
                                is_editable = bool(
                                    await locator.evaluate(
                                        """(el) => {
                                            const tag = (el.tagName || '').toLowerCase();
                                            const role = (el.getAttribute('role') || '').toLowerCase();
                                            return tag === 'input' || tag === 'textarea' || tag === 'select' ||
                                                   el.isContentEditable || role === 'textbox' || role === 'searchbox';
                                        }"""
                                    )
                                )
                            except Exception:
                                is_editable = False
                            # 非输入控件时，退化为 click，避免 fill() 抛错
                            if is_editable:
                                await self._robust_input_text(page, locator, next_action.input_value or "", press_enter=True)
                            else:
                                input_loc, stable = await self._resolve_input_locator(page, stable)
                                await self._robust_input_text(page, input_loc, next_action.input_value or "", press_enter=True)
                        elif next_action.action_type == "press_enter":
                            if stable:
                                locator = page.locator(stable).first
                                await locator.click(timeout=5000)
                                await locator.press("Enter", timeout=5000)
                            else:
                                await page.keyboard.press("Enter")
                        else:
                            raise Exception(f"Unsupported dynamic action: {next_action.action_type}")
                        if not fast_validate:
                            await page.wait_for_timeout(1500)
                        else:
                            await page.wait_for_timeout(250)

                        if pre_action_sig and next_action.action_type in ("click", "press_enter"):
                            if next_action.action_type == "click" and pre_checkbox_state is not None and stable:
                                try:
                                    loc = page.locator(stable).first
                                    post_checkbox_state = await loc.evaluate(
                                        """(el) => {
                                            const toState = (n) => {
                                                if (!n) return null;
                                                const aria = n.getAttribute && n.getAttribute('aria-checked');
                                                if (aria !== null && aria !== undefined && aria !== '') return String(aria);
                                                if (typeof n.checked === 'boolean') return String(!!n.checked);
                                                return null;
                                            };
                                            let n = null;
                                            if (el.matches && el.matches('[role="checkbox"],input[type="checkbox"]')) {
                                                n = el;
                                            } else {
                                                n =
                                                    el.closest('[role="checkbox"],label') ||
                                                    (el.querySelector && el.querySelector('[role="checkbox"],input[type="checkbox"]')) ||
                                                    null;
                                            }
                                            if (n && n.matches && n.matches('label')) {
                                                const inner = n.querySelector('[role="checkbox"],input[type="checkbox"]');
                                                if (inner) n = inner;
                                            }
                                            return toState(n);
                                        }"""
                                    )
                                    if post_checkbox_state is not None and post_checkbox_state != pre_checkbox_state:
                                        return stable
                                except Exception:
                                    pass
                            post_action_sig = await self._compute_page_state_signature(page)
                            if pre_action_sig == post_action_sig:
                                # 1) 点击后新开页面/扩展页，视为动作有效
                                try:
                                    post_page_count = len(page.context.pages)
                                    if post_page_count > pre_page_count:
                                        return stable
                                except Exception:
                                    pass

                                # 2) 点击前在弹窗中，点击后目标弹窗消失/目标不可见，视为推进
                                if next_action.action_type == "click" and stable:
                                    try:
                                        if pre_inside_dialog:
                                            still_in_dialog = await self._is_target_text_inside_dialog(page, stable)
                                            if not still_in_dialog:
                                                return stable
                                        loc = page.locator(stable).first
                                        if not await loc.is_visible(timeout=300):
                                            return stable
                                    except Exception:
                                        pass

                                raise Exception(
                                    f"No observable state change after {next_action.action_type} on {stable}"
                                )
                        return stable

                    max_attempts = 3 if disable_fallback_recovery else 4
                    for attempt in range(max_attempts):
                        try:
                            stable_target = await perform_current_action()
                            current_action_log = f"Executed {next_action.action_type} on {stable_target}"
                            ai_action_records.append(
                                {
                                    "step": step_count,
                                    "action_type": next_action.action_type,
                                    "target": stable_target,
                                    "input_value": next_action.input_value,
                                    "thought": next_action.thought,
                                    "success": True,
                                    "note": "executed",
                                }
                            )
                            action_success = True
                            break
                            
                        except Exception as e:
                            error_msg = str(e).lower()
                            handled = await self._human_handoff_if_needed(
                                page,
                                checkpoint=f"动态步骤失败前人工检查(step={step_count})",
                                enabled=human_handoff_on_auth,
                            )
                            if handled:
                                await page.wait_for_timeout(800)
                                continue
                            # 回退链：先通用回退，再进入 AI 修复
                            blocked_like = (
                                "timeout" in error_msg
                                or "intercepted" in error_msg
                                or "not visible" in error_msg
                                or "no observable state change" in error_msg
                                or "no_progress" in error_msg
                                or "robust click failed" in error_msg
                            )
                            if not blocked_like and next_action.action_type in ("click", "click_type_enter"):
                                blocked_like = True
                            if blocked_like:
                                logger.warning(
                                    "🛑 动作受阻 (timeout/intercepted/not visible/no_progress)。"
                                )

                                if attempt < max_attempts - 1:
                                    if attempt == 0:
                                        healed_by_popup = await self._try_popup_healer(page)
                                        if healed_by_popup:
                                            await page.wait_for_timeout(600)
                                            continue
                                    if attempt == 0 and (not disable_fallback_recovery):
                                        logger.info("↩️ [动态阶段] 尝试回退修复(reload/back)...")
                                        await self._attempt_generic_recovery(page)
                                        await page.wait_for_timeout(1200)
                                        continue
                                    if (attempt == 0 and disable_fallback_recovery) or (attempt == 1 and ai_heal_agent):
                                        if not ai_heal_agent:
                                            continue
                                        logger.info("🧠 [动态阶段] 启动 AI 修复 Agent（最多3次）...")
                                        recent_steps = "\n".join(
                                            action_history[-repair_context_window:]
                                        ) or "No previous steps."
                                        heal_result = await self._run_ai_heal_agent(
                                            page=page,
                                            ai_heal_agent=ai_heal_agent,
                                            task_name=task_name,
                                            recent_steps=recent_steps,
                                            failed_step_desc=next_action.thought or "dynamic action failed",
                                            failed_action_type=next_action.action_type,
                                            failed_target=stable_target or next_action.target,
                                            failed_input_value=next_action.input_value,
                                            last_error=str(e),
                                            step_id=f"dynamic_step_{step_count}",
                                            validate_coro=lambda: perform_current_action(fast_validate=True),
                                            human_handoff_on_auth=human_handoff_on_auth,
                                            max_attempts=3,
                                            expected_url_contains=current_url,
                                        )
                                        ai_action_records.append(
                                            {
                                                "step": step_count,
                                                "action_type": next_action.action_type,
                                                "target": stable_target or next_action.target,
                                                "input_value": next_action.input_value,
                                                "thought": next_action.thought,
                                                "success": bool(heal_result.get("resolved")),
                                                "note": f"ai_heal={heal_result.get('outcome')}: {heal_result.get('detail')}",
                                            }
                                        )
                                        if heal_result.get("resolved"):
                                            current_action_log = (
                                                f"AI healed {next_action.action_type} on "
                                                f"{stable_target or next_action.target}"
                                            )
                                            action_success = True
                                            break
                                        continue

                            logger.error(f"❌ 动作执行失败: {e}")
                            ai_action_records.append(
                                {
                                    "step": step_count,
                                    "action_type": next_action.action_type,
                                    "target": stable_target or next_action.target,
                                    "input_value": next_action.input_value,
                                    "thought": next_action.thought,
                                    "success": False,
                                    "note": str(e),
                                }
                            )
                            current_action_log = f"FAILED to execute {next_action.action_type} on {stable_target}"
                            break

            if action_success:
                post_url = page.url
                post_state_signature = await self._compute_page_state_signature(page)
                progress_context = {
                    "pre_url": current_url,
                    "post_url": post_url,
                    "changed": pre_state_signature != post_state_signature,
                }
                stagnation_key = f"{next_action.action_type}|{stable_target}|{current_url}"
                if pre_state_signature == post_state_signature:
                    stagnant_action_counts[stagnation_key] = stagnant_action_counts.get(stagnation_key, 0) + 1
                else:
                    stagnant_action_counts.pop(stagnation_key, None)

                if stagnant_action_counts.get(stagnation_key, 0) >= 1:
                    no_progress_detected = True
                    action_success = False
                    current_action_log = (
                        f"NO_PROGRESS after {next_action.action_type} on {stable_target} "
                        f"(state unchanged, current_url={current_url})"
                    )
                    logger.warning(f"⚠️ 无状态变化，判定为未推进: {current_action_log}")
                    ai_action_records.append(
                        {
                            "step": step_count,
                            "action_type": next_action.action_type,
                            "target": stable_target,
                            "input_value": next_action.input_value,
                            "thought": next_action.thought,
                            "success": False,
                            "note": "no_progress_same_state",
                        }
                    )

            if action_success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            last_action_ctx = {
                "action_type": next_action.action_type,
                "target": stable_target or next_action.target,
                "thought": next_action.thought,
                "success": action_success,
            }
            completed, completion_detail = await evaluate_completion(last_action=last_action_ctx)
            if completed:
                logger.info(f"✅ 命中完成条件，任务结束: {completion_detail}")
                trace_path = recorder.save_to_disk()
                trace_saved = True
                await self._save_browser_state(context)
                break

            if not manual_review:
                if action_success:
                    fp_dict = await DomParser.get_element_fingerprint(page, stable_target)
                    result_url = progress_context["post_url"] if progress_context else page.url
                    action_history.append(
                        current_action_log
                        + f" -> [SUCCESS] - Action completed. result_url={result_url}. DO NOT repeat this target. Move to the next step."
                    )
                    recorder.record_step(
                        current_url,
                        next_action.action_type,
                        stable_target,
                        next_action.input_value,
                        next_action.thought,
                        is_optional=False,
                        fingerprint_dict=fp_dict
                    )
                else:
                    failure_suffix = "Try alternative target or sequence."
                    if no_progress_detected:
                        failure_suffix = (
                            "This action caused no visible page change. DO NOT repeat this target. "
                            "Choose a more specific row/button/modal close action instead."
                        )
                    action_history.append(current_action_log + f" -> [FAILED] - {failure_suffix}")
                    if consecutive_failures >= max_consecutive_failures:
                        logger.error(
                            f"🛑 连续失败达到阈值 ({consecutive_failures}/{max_consecutive_failures})，failure_mode={failure_mode}"
                        )
                        if failure_mode == "stop":
                            break
                continue

            # ==========================================
            # 人工审核与录制落盘
            # ==========================================
            loop = asyncio.get_running_loop()
            prompt_msg = "\n👉 操作对吗？(y: 对 / o: 对，但设为【可选跳过】 / n: 错 / done: 结束): "
            user_input = await loop.run_in_executor(None, input, prompt_msg)
            user_input = user_input.strip().lower()

            if user_input in ['done', 'd', 'quit']:
                logger.info("🛑 任务结束，正在保存轨迹...")
                if action_success:
                    fp_dict = await DomParser.get_element_fingerprint(page, stable_target)
                    recorder.record_step(
                        current_url,
                        next_action.action_type,
                        stable_target,
                        next_action.input_value,
                        next_action.thought,
                        fingerprint_dict=fp_dict
                    )
                trace_path = recorder.save_to_disk()
                trace_saved = True
                await self._save_browser_state(context)
                break
            
            if user_input == 'o':
                logger.info("✅ 验收通过，并标记为【可选步骤 (Optional)】。")
                if action_success:
                    action_history.append(current_action_log + " -> [SUCCESS (OPTIONAL)] - Action completed. DO NOT repeat this target. Move to the next step.")
                    recorder.record_step(
                        current_url,
                        next_action.action_type,
                        stable_target,
                        next_action.input_value,
                        next_action.thought,
                        is_optional=True
                    )
                continue
            
            if user_input == 'n':
                logger.warning("🚫 动作标记为错误，不录制。")
                action_history.append(current_action_log + " -> [USER MARKED AS INCORRECT] - Do not repeat this.")
                continue
            
            if user_input != 'y' and user_input != '':
                action_history.append(current_action_log + f" -> [USER HINT: {user_input}]")
                continue

            logger.info("✅ 验收通过，已录制到暂存区。")
            if action_success:
                action_history.append(current_action_log + " -> [SUCCESS] - Action completed. DO NOT repeat this target. Move to the next step.")
                recorder.record_step(
                    current_url,
                    next_action.action_type,
                    stable_target,
                    next_action.input_value,
                    next_action.thought,
                    is_optional=False
                )
            else:
                if consecutive_failures >= max_consecutive_failures and failure_mode == "stop":
                    logger.error(
                        f"🛑 连续失败达到阈值 ({consecutive_failures}/{max_consecutive_failures})，手动模式下停止任务。"
                    )
                    break

        if not trace_saved:
            logger.info("💾 达到步数上限或流程自然结束，自动保存当前轨迹。")
            trace_path = recorder.save_to_disk()
            await self._save_browser_state(context)

        await page.close()
        ai_log_paths = self._save_ai_action_log(task_name=task_name, actions=ai_action_records)
        self.disable_fallback_recovery_runtime = False
        return {
            "task_name": task_name,
            "trace_path": trace_path,
            "ai_action_log": ai_log_paths,
            "total_actions": len(ai_action_records),
        }

    async def _execute_deterministic_step(self, page, step, fast_mode: bool = False):
        """
        执行确定性的单一节点。
        这里复用了我们之前修复过的逻辑：妥善处理 Navigate 和 Pre-check 的先后顺序。
        """
        logger.info(f"⏳ 步骤 [{step.step_id}]: {step.description}")
        pre = step.pre_check
        act = step.action

        # ==================================
        # 1. 特殊处理 Navigate (先跳再等)
        # ==================================
        if act.action_type == "navigate":
            logger.info(f"   ⚙️ 动作: navigate -> {act.target}")
            await page.goto(act.target)
            
            if pre.expected_dom_selector and pre.expected_dom_selector != "body":
                logger.info(f"   🔍 导航后校验 DOM: {pre.expected_dom_selector}")
                await page.wait_for_selector(pre.expected_dom_selector, timeout=pre.timeout_ms)
            return

        # ==================================
        # 1.5 页面守卫：执行动作前先确认在该步骤规定页面
        # ==================================
        await self._ensure_step_page_guard(page, step)

        # ==================================
        # 2. 增强前置校验与可见元素寻找 (Pre-check & Filter)
        # ==================================
        actual_target = act.target
        target_locator = None

        # 🧠 闭包：智能寻找页面上真实可见的匹配元素，防止死等隐藏的移动端节点
        async def get_visible_locator(selector, timeout_ms):
            for _ in range(max(1, int(timeout_ms / 250))):
                base_loc = page.locator(selector)
                count = await base_loc.count()
                for i in range(count):
                    loc = base_loc.nth(i)
                    if await loc.is_visible():
                        return loc  # 找到第一个真实可见的元素，立即返回
                await page.wait_for_timeout(250)
            raise Exception(f"Timeout: 没有找到可见的元素 -> {selector}")

        if pre.expected_dom_selector and pre.expected_dom_selector != "body":
            try:
                # 🚀 放弃死板的 wait_for_selector，改用我们的智能可见性轮询
                target_locator = await get_visible_locator(pre.expected_dom_selector, pre.timeout_ms)
            except Exception:
                logger.warning(f"⚠️ 原始定位器失效或被遮挡: {pre.expected_dom_selector}，启动特征模糊搜索...")
                
                fp = getattr(act, 'fingerprint', None)
                if fp:
                    fuzzy_selector = await DomParser.fuzzy_find_by_fingerprint(page, fp.model_dump())
                    if fuzzy_selector:
                        try:
                            logger.info(f"🔍 模糊搜索生成的候选定位器: {fuzzy_selector}")
                            # 🚀 自愈搜索也使用智能轮询
                            target_locator = await get_visible_locator(fuzzy_selector, 3000)
                            actual_target = fuzzy_selector # 替换靶标日志，便于观察
                            logger.info("✅ 模糊匹配自愈成功！")
                        except Exception:
                            logger.warning("❌ 模糊搜索也未能找到可见元素...")
                            raise Exception("DOM matching failed completely.")
                else:
                    raise

        # 如果没有触发前置校验，提供一个兜底
        if not target_locator:
            target_locator = page.locator(actual_target).first

        # ==================================
        # 3. 稳健的拟人化动作执行 (Action)
        # ==================================
        logger.info(f"   ⚙️ 动作: {act.action_type} -> {actual_target}")
        
        if not fast_mode:
            humanoid_delay = random.uniform(1.5, 3.0) * 1000
            await page.wait_for_timeout(humanoid_delay)
        
        if act.action_type == "click":
            await self._robust_click(page, selector=actual_target, preferred_locator=target_locator)
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass
            
        elif act.action_type == "type":
            locator, _ = await self._resolve_input_locator(page, actual_target)
            await self._robust_input_text(page, locator, act.input_value or "", press_enter=False)

        elif act.action_type == "click_type_enter":
            locator, _ = await self._resolve_input_locator(page, actual_target)
            await self._robust_input_text(page, locator, act.input_value or "", press_enter=True)

        elif act.action_type == "press_enter":
            if actual_target:
                await target_locator.click(timeout=5000)
                await target_locator.press("Enter", timeout=5000)
            else:
                await page.keyboard.press("Enter")

        elif act.action_type == "refresh":
            await page.reload(wait_until="domcontentloaded", timeout=15000)
            
        elif act.action_type == "wait_for_timeout":
            await page.wait_for_timeout(1000)

        elif act.action_type == "ensure_quote_token":
            symbol = (act.input_value or "USDT").strip().upper()
            selector = act.target or ".dex-select-value-box button"
            await ensure_quote_token(
                page=page,
                target_symbol=symbol,
                button_selector=selector,
            )

    async def _execute_override_action(self, page: Page, action_type: str, target: str = None, input_value: str = None):
        """
        执行 AI 修复器给出的临时动作，不修改原 trace 文件。
        """
        action_type = (action_type or "").strip().lower()
        if action_type == "navigate":
            if not target:
                raise Exception("override navigate 缺少 target URL")
            await page.goto(target, wait_until="domcontentloaded")
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

        if action_type == "wait_for_timeout":
            delay_ms = 1000
            try:
                if input_value:
                    delay_ms = int(float(input_value) * 1000) if "." in str(input_value) else int(input_value)
            except Exception:
                delay_ms = 1000
            await page.wait_for_timeout(max(200, delay_ms))
            return

        if action_type == "ensure_quote_token":
            # Use input_value as target symbol (preferred), target as optional custom dropdown selector.
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

            # 坐标点击稳健化：中心 + 九宫格候选点 + 点击语义过滤
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
        """
        步骤执行前页面守卫：
        - 当前 URL 不符合 guard 时，先执行 on_mismatch_actions
        - 若仍不符合，再尝试基于 URL 片段自动回跳
        """
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

            # AI-only 模式下禁用 PageGuard 自动回跳，直接失败交给 AI 修复器。
            if self.disable_fallback_recovery_runtime:
                raise Exception(
                    f"PageGuardError: step={step.step_id} 期望页面包含 '{required_url}'，但当前为 '{page.url}'"
                )

            used_custom = False
            try:
                used_custom = await self._run_mismatch_actions(page, step)
            except Exception as e:
                logger.warning(f"⚠️ [PageGuard] 自定义回退动作执行失败: {e}")

            if required_url in (page.url or ""):
                return

            if not used_custom:
                inferred = self._normalize_url_candidate(required_url)
                if inferred:
                    try:
                        logger.info(f"🧭 [PageGuard] 自动回跳到目标页面: {inferred}")
                        await page.goto(inferred, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(800)
                    except Exception as e:
                        logger.warning(f"⚠️ [PageGuard] 自动回跳失败: {e}")
                else:
                    try:
                        logger.info("🧭 [PageGuard] 尝试后退恢复页面")
                        await page.go_back(wait_until="domcontentloaded", timeout=8000)
                        await page.wait_for_timeout(700)
                    except Exception:
                        pass

            if required_url in (page.url or ""):
                return

        raise Exception(
            f"PageGuardError: step={step.step_id} 期望页面包含 '{required_url}'，但当前为 '{page.url}'"
        )

    @staticmethod
    def _build_recent_steps_text(blueprint, current_index: int, window: int = 5) -> str:
        start = max(0, current_index - window)
        chunks = []
        for idx in range(start, current_index):
            s = blueprint.steps[idx]
            chunks.append(
                f"[{idx}] step_id={s.step_id} desc={s.description} "
                f"action={s.action.action_type} target={s.action.target} optional={s.is_optional}"
            )
        return "\n".join(chunks) if chunks else "No previous steps."

    async def run_from_trace(
        self,
        trace_path: str,
        context=None,
        viewport: dict = None,
        state_path: str = None,
        user_agent: str = None,
        resume: bool = True,
        suspend_on_failure: bool = True,
        run_id: str = None,
        subflow_window: int = 2,
        subflow_skip_risky: bool = True,
        subflow_risky_keywords: list[str] = None,
        human_handoff_on_auth: bool = True,
        replay_repair_agent=None,
        repair_context_window: int = 5,
        disable_fallback_recovery: bool = False,
    ):
        """
        克隆回放模式：读取本地 JSON 轨迹，脱离大模型，进行高速确定性执行。
        """
        from hexaflow.agents.planner import WorkflowBlueprint
        import os
        self.disable_fallback_recovery_runtime = disable_fallback_recovery
        
        if not os.path.exists(trace_path):
            raise FileNotFoundError(f"找不到轨迹文件: {trace_path}")
            
        logger.info(f"📂 正在加载黄金轨迹: {trace_path}")
        with open(trace_path, 'r', encoding='utf-8') as f:
            trace_data = f.read()
        
        # 利用 Pydantic 的反序列化能力，将 JSON 文本瞬间转为强类型对象
        blueprint = WorkflowBlueprint.model_validate_json(trace_data)
        logger.info(f"▶️ 开始回放任务: {blueprint.task_name} (共 {len(blueprint.steps)} 步)")
        if run_id:
            run_state = self.state_machine.resume_by_run_id(run_id)
            if run_state.trace_path != trace_path:
                raise ValueError(
                    f"run_id={run_id} 绑定的trace与当前输入不一致: {run_state.trace_path} != {trace_path}"
                )
        else:
            run_state = self.state_machine.start_or_resume(
                trace_path=trace_path,
                task_name=blueprint.task_name,
                total_steps=len(blueprint.steps),
                resume=resume,
            )
        start_index = run_state.current_step_index
        logger.info(
            f"🧭 RunID={run_state.run_id} 状态={run_state.status}，将从步骤索引 {start_index} 开始继续执行"
        )
        heal_log = self._create_heal_log(
            task_name=blueprint.task_name,
            run_id=run_state.run_id,
            trace_path=trace_path,
        )

        # ==========================================
        # 🚀 核心改造：支持动态注入不同的测试环境
        # ==========================================
        if not context:
            # 1. 设置窗口大小（默认桌面，可传入移动端尺寸）
            vp = viewport or {'width': 1280, 'height': 800}
            context_options = {'viewport': vp}
            
            # 2. 伪造设备指纹 (User-Agent)
            if user_agent:
                context_options['user_agent'] = user_agent
                
            # 3. 动态决定使用哪个账号的缓存数据（仅非CDP时生效）
            actual_state = state_path if state_path is not None else getattr(self, 'state_path', None)
            if (not self.use_cdp) and actual_state and os.path.exists(actual_state):
                logger.info(f"🍪 发现缓存！正在加载本地浏览器状态: {actual_state}")
                context_options['storage_state'] = actual_state
            elif not self.use_cdp:
                logger.info("✨ 未使用缓存，正以全新无痕环境启动...")

            context = await self._resolve_context(context=context, context_options=context_options)

        page = await context.new_page()
        if start_index < len(blueprint.steps):
            first_pending_step = blueprint.steps[start_index]
            if (
                page.url == "about:blank"
                and first_pending_step.action.action_type != "navigate"
            ):
                bootstrap_url = self._infer_bootstrap_url(blueprint, start_index)
                if bootstrap_url:
                    logger.info(f"🧭 回放预热: 当前是 about:blank，自动导航到 {bootstrap_url}")
                    await page.goto(bootstrap_url, wait_until="domcontentloaded")
        await self._human_handoff_if_needed(
            page, checkpoint="回放启动检查", enabled=human_handoff_on_auth
        )

        for step_index, step in enumerate(blueprint.steps[start_index:], start=start_index):
            self.state_machine.mark_step_started(run_state.run_id, step_index, step.step_id)
            max_attempts = 2 if disable_fallback_recovery else 4
            for attempt in range(max_attempts):
                try:
                    await self._execute_deterministic_step(page, step)
                    self.state_machine.mark_step_success(run_state.run_id, step_index, step.step_id)
                    break # 成功执行，跳出重试循环
                    
                except Exception as e:
                    error_msg = str(e).lower()
                    handled = await self._human_handoff_if_needed(
                        page,
                        checkpoint=f"回放步骤失败人工检查(step_id={step.step_id})",
                        enabled=human_handoff_on_auth,
                    )
                    if handled and attempt < max_attempts - 1:
                        self.state_machine.add_event(
                            run_state.run_id,
                            event="recovery_human_handoff",
                            detail=f"step={step.step_id} resumed_after_human=True",
                            step_index=step_index,
                            step_id=step.step_id,
                        )
                        await page.wait_for_timeout(800)
                        continue
                    # 如果是被弹窗遮挡、不可点击或超时，触发自愈！
                    if getattr(step, 'is_optional', False) and ("timeout" in error_msg or "not visible" in error_msg):
                        logger.info(f"⏭️ [可选步骤] 元素未出现，安全跳过: {step.step_id}")
                        self.state_machine.mark_step_skipped(
                            run_state.run_id,
                            step_index,
                            step.step_id,
                            "optional step skipped due to timeout or invisibility",
                        )
                        break

                    if disable_fallback_recovery and replay_repair_agent and attempt < max_attempts:
                        logger.info("🧠 [回放阶段] AI_ONLY 模式：任意异常直接进入 AI 修复...")
                        recent_steps = self._build_recent_steps_text(
                            blueprint=blueprint,
                            current_index=step_index,
                            window=repair_context_window,
                        )
                        heal_result = await self._run_ai_heal_agent(
                            page=page,
                            ai_heal_agent=replay_repair_agent,
                            task_name=blueprint.task_name,
                            recent_steps=recent_steps,
                            failed_step_desc=step.description,
                            failed_action_type=step.action.action_type,
                            failed_target=step.action.target,
                            failed_input_value=step.action.input_value,
                            last_error=str(e),
                            step_id=step.step_id,
                            step_index=step_index,
                            run_id=run_state.run_id,
                            validate_coro=lambda: self._execute_deterministic_step(page, step, fast_mode=True),
                            human_handoff_on_auth=human_handoff_on_auth,
                            max_attempts=3,
                            heal_log=heal_log,
                            expected_url_contains=getattr(step, "guard_url_contains", None) or step.pre_check.expected_url_contains or "",
                        )
                        if heal_result.get("resolved"):
                            outcome = heal_result.get("outcome")
                            if outcome == "skipped":
                                reason = heal_result.get("detail") or "AI 建议跳过该步骤"
                                self.state_machine.mark_step_skipped(
                                    run_state.run_id, step_index, step.step_id, reason
                                )
                            else:
                                self.state_machine.mark_step_success(
                                    run_state.run_id, step_index, step.step_id
                                )
                            break
                        self._append_heal_record(
                            heal_log,
                            phase="ai_summary",
                            step_id=step.step_id,
                            step_index=step_index,
                            result=heal_result.get("outcome", "failed"),
                            detail=heal_result.get("detail", ""),
                        )
                        continue

                    if (
                        "timeout" in error_msg
                        or "intercepted" in error_msg
                        or "not visible" in error_msg
                        or "dom matching failed" in error_msg
                    ):
                        logger.warning(f"🛑 步骤 [{step.step_id}] 受阻。原因: 元素不可操作或超时。")
                        
                        if attempt < max_attempts - 1:
                            ai_attempt_index = 0 if disable_fallback_recovery else 2
                            if (not disable_fallback_recovery) and attempt == 0:
                                logger.info("↩️ [回放阶段] 尝试同URL上下文修复(reload/back)...")
                                healed = await self._attempt_wrong_page_recovery(page, step)
                                self._append_heal_record(
                                    heal_log,
                                    phase="fallback_wrong_page",
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    result="ok" if healed else "failed",
                                    detail=f"attempt={attempt + 1}",
                                )
                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_wrong_page",
                                    detail=f"step={step.step_id} healed={healed}",
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )
                                await page.wait_for_timeout(1200)
                                continue

                            if (not disable_fallback_recovery) and attempt == 1:
                                logger.info("🔁 [回放阶段] 尝试小流程回放修复(前2步)...")
                                fixed = await self._replay_recent_subflow(
                                    page=page,
                                    blueprint=blueprint,
                                    step_index=step_index,
                                    window=subflow_window,
                                    skip_risky=subflow_skip_risky,
                                    risky_keywords=subflow_risky_keywords,
                                )
                                self._append_heal_record(
                                    heal_log,
                                    phase="fallback_subflow",
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    result="ok" if fixed else "failed",
                                    detail=(
                                        f"attempt={attempt + 1} window={subflow_window} "
                                        f"skip_risky={subflow_skip_risky}"
                                    ),
                                )
                                self.state_machine.add_event(
                                    run_state.run_id,
                                    event="recovery_subflow",
                                    detail=f"step={step.step_id} fixed={fixed} url={page.url}",
                                    step_index=step_index,
                                    step_id=step.step_id,
                                )
                                await page.wait_for_timeout(1200)
                                continue

                            if attempt == ai_attempt_index and replay_repair_agent:
                                logger.info("🧠 [回放阶段] 启动 AI 修复 Agent（最多3次）...")
                                recent_steps = self._build_recent_steps_text(
                                    blueprint=blueprint,
                                    current_index=step_index,
                                    window=repair_context_window,
                                )
                                heal_result = await self._run_ai_heal_agent(
                                    page=page,
                                    ai_heal_agent=replay_repair_agent,
                                    task_name=blueprint.task_name,
                                    recent_steps=recent_steps,
                                    failed_step_desc=step.description,
                                    failed_action_type=step.action.action_type,
                                    failed_target=step.action.target,
                                    failed_input_value=step.action.input_value,
                                    last_error=str(e),
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    run_id=run_state.run_id,
                                    validate_coro=lambda: self._execute_deterministic_step(page, step, fast_mode=True),
                                    human_handoff_on_auth=human_handoff_on_auth,
                                    max_attempts=3,
                                    heal_log=heal_log,
                                    expected_url_contains=getattr(step, "guard_url_contains", None) or step.pre_check.expected_url_contains or "",
                                )
                                if heal_result.get("resolved"):
                                    outcome = heal_result.get("outcome")
                                    if outcome == "skipped":
                                        reason = heal_result.get("detail") or "AI 建议跳过该步骤"
                                        logger.warning(f"⏭️ [AI修复] 跳过步骤: {step.step_id} | {reason}")
                                        self.state_machine.mark_step_skipped(
                                            run_state.run_id, step_index, step.step_id, reason
                                        )
                                    else:
                                        logger.info(f"✅ [AI修复] 已恢复步骤: {step.step_id}")
                                        self.state_machine.mark_step_success(
                                            run_state.run_id, step_index, step.step_id
                                        )
                                    break
                                else:
                                    self.state_machine.add_event(
                                        run_state.run_id,
                                        event="recovery_ai_failed",
                                        detail=(
                                            f"step={step.step_id} outcome={heal_result.get('outcome')} "
                                            f"detail={heal_result.get('detail')}"
                                        ),
                                        step_index=step_index,
                                        step_id=step.step_id,
                                    )
                                    self._append_heal_record(
                                        heal_log,
                                        phase="ai_summary",
                                        step_id=step.step_id,
                                        step_index=step_index,
                                        result=heal_result.get("outcome", "failed"),
                                        detail=heal_result.get("detail", ""),
                                    )
                                    continue

                            if attempt == ai_attempt_index and not replay_repair_agent:
                                logger.warning("⚠️ 未配置 AI 修复 Agent，无法继续自动修复。")
                                self._append_heal_record(
                                    heal_log,
                                    phase="ai_summary",
                                    step_id=step.step_id,
                                    step_index=step_index,
                                    result="no_agent",
                                    detail="replay_repair_agent is None",
                                )
                                continue
                        else:
                            logger.error("❌ 已达到最大重试次数，主线动作依然失败。")
                            screenshot_path = await self._capture_suspend_snapshot(
                                page, run_state.run_id, step.step_id
                            )
                            detail = str(e)
                            if screenshot_path:
                                detail = f"{detail}\n[screenshot]: {screenshot_path}"
                            if suspend_on_failure:
                                self.state_machine.mark_run_suspended(
                                    run_state.run_id, step_index, step.step_id, detail
                                )
                                heal_log["status"] = "suspended"
                            else:
                                self.state_machine.mark_run_failed(
                                    run_state.run_id, step_index, step.step_id, detail
                                )
                                heal_log["status"] = "failed"
                            from datetime import datetime
                            heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
                            self._save_heal_log(heal_log)
                            self._save_run_report(run_state.run_id)
                            raise
                    else:
                        # 如果是网络断开等底层错误，直接抛出
                        screenshot_path = await self._capture_suspend_snapshot(
                            page, run_state.run_id, step.step_id
                        )
                        detail = str(e)
                        if screenshot_path:
                            detail = f"{detail}\n[screenshot]: {screenshot_path}"
                        if suspend_on_failure:
                            self.state_machine.mark_run_suspended(
                                run_state.run_id, step_index, step.step_id, detail
                            )
                            heal_log["status"] = "suspended"
                        else:
                            self.state_machine.mark_run_failed(
                                run_state.run_id, step_index, step.step_id, detail
                            )
                            heal_log["status"] = "failed"
                        from datetime import datetime
                        heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
                        self._save_heal_log(heal_log)
                        self._save_run_report(run_state.run_id)
                        raise
                
        self.state_machine.mark_run_completed(run_state.run_id)
        from datetime import datetime
        heal_log["status"] = "completed"
        heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
        heal_paths = self._save_heal_log(heal_log)
        report_paths = self._save_run_report(run_state.run_id)
        logger.info("🎉 轨迹回放圆满完成！")
        if report_paths is not None:
            report_paths["heal_log"] = heal_paths
        self.disable_fallback_recovery_runtime = False
        return report_paths
        # loop = asyncio.get_running_loop()
        # await loop.run_in_executor(None, input, "\n👉 回放已完成，请查看浏览器现场。按【回车键】关闭浏览器并结束任务...")
        # await page.close()

    @staticmethod
    def _find_loop_range(steps, loop_name: str):
        start_idx = None
        end_idx = None
        for i, step in enumerate(steps):
            if getattr(step, "loop_marker", None) == "start" and getattr(step, "loop_name", None) == loop_name:
                start_idx = i
                break
        if start_idx is None:
            return None, None
        for j in range(start_idx, len(steps)):
            step = steps[j]
            if getattr(step, "loop_marker", None) == "end" and getattr(step, "loop_name", None) == loop_name:
                end_idx = j
                break
        return start_idx, end_idx

    async def _execute_step_for_loop(
        self,
        page: Page,
        step,
        human_handoff_on_auth: bool,
        replay_repair_agent=None,
        task_name: str = "",
        recent_steps_text: str = "",
        max_attempts: int = 5,
        heal_log: dict = None,
        disable_fallback_recovery: bool = False,
    ) -> bool:
        effective_max_attempts = 2 if disable_fallback_recovery else max_attempts
        for attempt in range(effective_max_attempts):
            try:
                await self._execute_deterministic_step(page, step)
                return True
            except Exception as e:
                error_msg = str(e).lower()
                if getattr(step, "is_optional", False) and (
                    "timeout" in error_msg
                    or "intercepted" in error_msg
                    or "not visible" in error_msg
                    or "dom matching failed" in error_msg
                ):
                    logger.info(f"⏭️ [循环可选步骤] 执行失败，安全跳过: {step.step_id}")
                    return True

                handled = await self._human_handoff_if_needed(
                    page,
                    checkpoint=f"循环步骤失败人工检查(step_id={step.step_id})",
                    enabled=human_handoff_on_auth,
                )
                if handled and attempt < max_attempts - 1:
                    await page.wait_for_timeout(800)
                    continue

                if disable_fallback_recovery and replay_repair_agent and attempt < effective_max_attempts:
                    heal_result = await self._run_ai_heal_agent(
                        page=page,
                        ai_heal_agent=replay_repair_agent,
                        task_name=task_name or "loop_task",
                        recent_steps=recent_steps_text or "No previous steps.",
                        failed_step_desc=step.description,
                        failed_action_type=step.action.action_type,
                        failed_target=step.action.target,
                        failed_input_value=step.action.input_value,
                        last_error=str(e),
                        step_id=step.step_id,
                        validate_coro=lambda: self._execute_deterministic_step(page, step, fast_mode=True),
                        human_handoff_on_auth=human_handoff_on_auth,
                        max_attempts=3,
                        heal_log=heal_log,
                        expected_url_contains=getattr(step, "guard_url_contains", None) or step.pre_check.expected_url_contains or "",
                    )
                    if heal_result.get("resolved"):
                        return True
                    self._append_heal_record(
                        heal_log,
                        phase="loop_ai_summary",
                        step_id=step.step_id,
                        result=heal_result.get("outcome", "failed"),
                        detail=heal_result.get("detail", ""),
                    )
                    continue

                if "timeout" in error_msg or "intercepted" in error_msg or "not visible" in error_msg or "dom matching failed" in error_msg:
                    if attempt == 0 and (not disable_fallback_recovery):
                        fixed = await self._attempt_wrong_page_recovery(page, step)
                        self._append_heal_record(
                            heal_log,
                            phase="loop_fallback_wrong_page",
                            step_id=step.step_id,
                            result="ok" if fixed else "failed",
                            detail=f"attempt={attempt + 1}",
                        )
                        await page.wait_for_timeout(1000)
                        continue
                    ai_attempt_index = 0 if disable_fallback_recovery else 1
                    if attempt == ai_attempt_index:
                        if replay_repair_agent:
                            heal_result = await self._run_ai_heal_agent(
                                page=page,
                                ai_heal_agent=replay_repair_agent,
                                task_name=task_name or "loop_task",
                                recent_steps=recent_steps_text or "No previous steps.",
                                failed_step_desc=step.description,
                                failed_action_type=step.action.action_type,
                                failed_target=step.action.target,
                                failed_input_value=step.action.input_value,
                                last_error=str(e),
                                step_id=step.step_id,
                                validate_coro=lambda: self._execute_deterministic_step(page, step, fast_mode=True),
                                human_handoff_on_auth=human_handoff_on_auth,
                                max_attempts=3,
                                heal_log=heal_log,
                                expected_url_contains=getattr(step, "guard_url_contains", None) or step.pre_check.expected_url_contains or "",
                            )
                            if heal_result.get("resolved"):
                                return True
                            self._append_heal_record(
                                heal_log,
                                phase="loop_ai_summary",
                                step_id=step.step_id,
                                result=heal_result.get("outcome", "failed"),
                                detail=heal_result.get("detail", ""),
                            )
                            await page.wait_for_timeout(600)
                            continue
                        if not disable_fallback_recovery:
                            fixed = await self._attempt_generic_recovery(page)
                            self._append_heal_record(
                                heal_log,
                                phase="loop_fallback_generic",
                                step_id=step.step_id,
                                result="ok" if fixed else "failed",
                                detail=f"attempt={attempt + 1}",
                            )
                            await page.wait_for_timeout(900)
                            continue
                if attempt >= effective_max_attempts - 1:
                    return False
        return False

    async def run_loop_from_trace(
        self,
        trace_path: str,
        loop_iterations: int,
        loop_name: str = "main_loop",
        max_iteration_retries: int = 30,
        context=None,
        viewport: dict = None,
        state_path: str = None,
        user_agent: str = None,
        human_handoff_on_auth: bool = True,
        replay_repair_agent=None,
        repair_context_window: int = 5,
        disable_fallback_recovery: bool = False,
    ):
        """
        循环任务执行器：
        - 依据 trace 中的 loop_marker(start/end) 定义循环区间
        - 循环次数由外部参数指定
        - 仅当“整轮循环步骤全部成功”才计为完成一次
        - 失败则整轮重试，直到成功或超过 max_iteration_retries
        """
        from hexaflow.agents.planner import WorkflowBlueprint
        self.disable_fallback_recovery_runtime = disable_fallback_recovery

        if loop_iterations < 1:
            raise ValueError("loop_iterations 必须 >= 1")

        if not os.path.exists(trace_path):
            raise FileNotFoundError(f"找不到轨迹文件: {trace_path}")

        logger.info(f"📂 正在加载循环轨迹: {trace_path}")
        with open(trace_path, "r", encoding="utf-8") as f:
            trace_data = f.read()
        blueprint = WorkflowBlueprint.model_validate_json(trace_data)

        loop_start, loop_end = self._find_loop_range(blueprint.steps, loop_name=loop_name)
        if loop_start is None or loop_end is None or loop_start > loop_end:
            raise ValueError(
                f"未在 trace 中找到有效循环标记: loop_name={loop_name} (start/end)"
            )

        run_state = self.state_machine.start_or_resume(
            trace_path=trace_path,
            task_name=f"{blueprint.task_name}::loop({loop_name})",
            total_steps=len(blueprint.steps),
            resume=False,
        )
        heal_log = self._create_heal_log(
            task_name=f"{blueprint.task_name}::loop({loop_name})",
            run_id=run_state.run_id,
            trace_path=trace_path,
        )
        logger.info(
            f"🔁 循环任务启动: run_id={run_state.run_id} loop={loop_name} "
            f"range=[{loop_start}, {loop_end}] iterations={loop_iterations}"
        )

        if not context:
            vp = viewport or {"width": 1280, "height": 800}
            context_options = {"viewport": vp}
            if user_agent:
                context_options["user_agent"] = user_agent
            actual_state = state_path if state_path is not None else getattr(self, "state_path", None)
            if (not self.use_cdp) and actual_state and os.path.exists(actual_state):
                context_options["storage_state"] = actual_state
            context = await self._resolve_context(context=context, context_options=context_options)

        page = await context.new_page()
        first_bootstrap = self._infer_bootstrap_url(blueprint, 0)
        if first_bootstrap:
            await page.goto(first_bootstrap, wait_until="domcontentloaded")
        await self._human_handoff_if_needed(page, checkpoint="循环任务启动检查", enabled=human_handoff_on_auth)

        completed_loops = 0

        try:
            # A) 循环前步骤，只执行一次
            pre_steps = blueprint.steps[:loop_start]
            for idx, step in enumerate(pre_steps):
                recent = self._build_recent_steps_text(blueprint, idx, repair_context_window)
                ok = await self._execute_step_for_loop(
                    page=page,
                    step=step,
                    human_handoff_on_auth=human_handoff_on_auth,
                    replay_repair_agent=replay_repair_agent,
                    task_name=blueprint.task_name,
                    recent_steps_text=recent,
                    heal_log=heal_log,
                    disable_fallback_recovery=disable_fallback_recovery,
                )
                if not ok:
                    raise Exception(f"循环前置步骤失败: {step.step_id}")

            # B) 循环区间
            loop_steps = blueprint.steps[loop_start: loop_end + 1]
            for i in range(1, loop_iterations + 1):
                iteration_retry = 0
                while True:
                    iteration_retry += 1
                    logger.info(
                        f"🔁 开始第 {i}/{loop_iterations} 次循环尝试 (retry={iteration_retry}/{max_iteration_retries})"
                    )
                    iter_ok = True
                    for offset, step in enumerate(loop_steps):
                        step_index = loop_start + offset
                        recent = self._build_recent_steps_text(
                            blueprint, step_index, repair_context_window
                        )
                        ok = await self._execute_step_for_loop(
                            page=page,
                            step=step,
                            human_handoff_on_auth=human_handoff_on_auth,
                            replay_repair_agent=replay_repair_agent,
                            task_name=blueprint.task_name,
                            recent_steps_text=recent,
                            heal_log=heal_log,
                            disable_fallback_recovery=disable_fallback_recovery,
                        )
                        if not ok:
                            iter_ok = False
                            break

                    if iter_ok:
                        completed_loops += 1
                        remaining = loop_iterations - completed_loops
                        logger.info(
                            f"✅ 循环完成: {completed_loops}/{loop_iterations} (remaining={remaining})"
                        )
                        self.state_machine.add_event(
                            run_state.run_id,
                            event="loop_iteration_completed",
                            detail=f"loop={loop_name} completed={completed_loops} remaining={remaining}",
                        )
                        break

                    if iteration_retry >= max_iteration_retries:
                        raise Exception(
                            f"循环第 {i} 轮重试超过上限({max_iteration_retries})，仍未成功"
                        )

                    self.state_machine.add_event(
                        run_state.run_id,
                        event="loop_iteration_retry",
                        detail=f"loop={loop_name} iteration={i} retry={iteration_retry}",
                    )
                    self._append_heal_record(
                        heal_log,
                        phase="loop_iteration_retry",
                        step_id=f"loop:{loop_name}",
                        result="retry",
                        detail=f"iteration={i} retry={iteration_retry}",
                    )
                    logger.warning(f"⚠️ 第 {i} 次循环未成功，本轮重试。")

            # C) 循环后步骤，只执行一次
            post_steps = blueprint.steps[loop_end + 1:]
            for idx2, step in enumerate(post_steps, start=loop_end + 1):
                recent = self._build_recent_steps_text(blueprint, idx2, repair_context_window)
                ok = await self._execute_step_for_loop(
                    page=page,
                    step=step,
                    human_handoff_on_auth=human_handoff_on_auth,
                    replay_repair_agent=replay_repair_agent,
                    task_name=blueprint.task_name,
                    recent_steps_text=recent,
                    heal_log=heal_log,
                    disable_fallback_recovery=disable_fallback_recovery,
                )
                if not ok:
                    raise Exception(f"循环后置步骤失败: {step.step_id}")

            self.state_machine.mark_run_completed(run_state.run_id)
            from datetime import datetime
            heal_log["status"] = "completed"
            heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
            heal_paths = self._save_heal_log(heal_log)
            report_paths = self._save_run_report(run_state.run_id)
            logger.info("🎉 循环任务执行完成")
            self.disable_fallback_recovery_runtime = False
            return {
                "completed_loops": completed_loops,
                "target_loops": loop_iterations,
                "remaining_loops": max(0, loop_iterations - completed_loops),
                "report_paths": report_paths,
                "heal_log_paths": heal_paths,
            }
        except Exception as e:
            detail = str(e)
            screenshot_path = await self._capture_suspend_snapshot(page, run_state.run_id, "loop_task")
            if screenshot_path:
                detail = f"{detail}\n[screenshot]: {screenshot_path}"
            self.state_machine.mark_run_suspended(run_state.run_id, loop_start, "loop_task", detail)
            from datetime import datetime
            heal_log["status"] = "suspended"
            heal_log["ended_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_heal_log(heal_log)
            self._save_run_report(run_state.run_id)
            self.disable_fallback_recovery_runtime = False
            raise

    async def run_loop_task_from_spec(
        self,
        trace_path: str,
        spec_input,
        context=None,
        replay_repair_agent=None,
        disable_fallback_recovery: bool = False,
    ):
        if isinstance(spec_input, TaskSpec):
            spec = spec_input
        elif isinstance(spec_input, str):
            spec = TaskSpec.from_json_file(spec_input)
        elif isinstance(spec_input, dict):
            spec = TaskSpec.model_validate(spec_input)
        else:
            raise ValueError("Unsupported task spec input type")

        if not spec.loop_policy.enabled:
            raise ValueError("TaskSpec.loop_policy.enabled 为 false，无法运行循环任务。")

        return await self.run_loop_from_trace(
            trace_path=trace_path,
            loop_iterations=spec.loop_policy.iterations,
            loop_name=spec.loop_policy.loop_name,
            max_iteration_retries=spec.loop_policy.max_iteration_retries,
            context=context,
            human_handoff_on_auth=True,
            replay_repair_agent=replay_repair_agent,
            disable_fallback_recovery=disable_fallback_recovery,
        )

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
            actual_state = getattr(self, 'state_path', None)
            if (not self.use_cdp) and actual_state and os.path.exists(actual_state):
                logger.info(f"🍪 发现缓存！加载本地浏览器状态: {actual_state}")
                context_options['storage_state'] = actual_state
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
                if getattr(self, 'state_path', None):
                    os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
                    await context.storage_state(path=self.state_path)
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
