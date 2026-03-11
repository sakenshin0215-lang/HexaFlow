import logging
from typing import Optional

from hexaflow.agents.react_agent import ReActAgent
from hexaflow.agents.schemas import ReplayRepairDecision
from hexaflow.tools.helpers import extract_json_object, image_to_data_url

logger = logging.getLogger("AIHealAgent")


class AIHealAgent(ReActAgent):
    """
    专用于执行失败后的修复决策代理。
    复用 ReActAgent 的客户端与结构化输出协议。
    """

    def __init__(self, api_key: str, base_url: str, model_name: str):
        super().__init__(api_key=api_key, base_url=base_url, model_name=model_name)

    async def repair_failed_replay_step(
        self,
        task_name: str,
        recent_steps: str,
        current_url: str,
        dom_snapshot: str,
        failed_step_desc: str,
        failed_action_type: str,
        failed_target: str,
        last_error: str,
        screenshot_path: str = "",
        screenshot_paths: Optional[list[str]] = None,
    ) -> ReplayRepairDecision:
        screenshot_paths = screenshot_paths or []
        if screenshot_path and screenshot_path not in screenshot_paths:
            screenshot_paths.insert(0, screenshot_path)
        screenshot_paths = screenshot_paths[:4]

        data_urls = []
        for p in screenshot_paths:
            d = image_to_data_url(p)
            if d:
                data_urls.append(d)
        prompt = f"""
        TASK NAME: {task_name}
        RECENT STEPS:
        {recent_steps}

        FAILED STEP:
        - description: {failed_step_desc}
        - action_type: {failed_action_type}
        - target: {failed_target}
        - error + attempts context: {last_error}
        - screenshot_path: {screenshot_path}
        - screenshot_paths: {screenshot_paths}

        CURRENT URL: {current_url}
        CURRENT INTERACTIVE DOM:
        {dom_snapshot}

        Decide one best recovery action now and return ONLY ONE JSON object:
        {{
          "thought": "...",
          "strategy": "retry_with_new_selector|replace_action|skip_step|human_handoff|no_fix",
          "action_type": "navigate|click|type|click_type_enter|press_enter|refresh|wait_for_timeout|ensure_quote_token|click_relative|null",
          "target": "selector-or-url-or-null",
          "input_value": "value-or-null",
          "skip_reason": "reason-or-null",
          "confidence": 0.0
        }}
        """

        system_prompt = """
        You are an autonomous runtime-heal agent for web RPA.
        Goal: recover safely with minimal side effects.

        MUST FOLLOW:
        1) Prefer retry_with_new_selector if intent unchanged.
        2) Use replace_action if page state changed and another action is needed.
        3) Use skip_step only if clearly optional/redundant.
        4) Use human_handoff for login/captcha/2FA/wallet unlock/signature.
        5) If no reliable action, return no_fix.
        6) If previous attempts already failed, avoid repeating the same weak fix.
        7) Prefer selector-based actions. Use click_relative only as a last fallback
           when selector is impossible; input_value format: "x_ratio,y_ratio" in [0,1].
        8) If a tutorial/onboarding modal blocks the page, prefer closing it via top-right
           close button (X/关闭/跳过) instead of clicking 下一步 repeatedly.
        """

        try:
            content = [{"type": "text", "text": prompt}]
            for d in data_urls:
                content.append({"type": "image_url", "image_url": {"url": d}})

            resp = await self.raw_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                temperature=0.1,
            )
            raw = (resp.choices[0].message.content or "").strip()
            payload = extract_json_object(raw)
            decision = ReplayRepairDecision.model_validate(payload)
            logger.info(
                f"🧠 [AIHeal] strategy={decision.strategy} confidence={decision.confidence:.2f} thought={decision.thought}"
            )
            return decision
        except Exception as e:
            logger.error(f"❌ [AIHeal] 决策失败: {e}")
            return ReplayRepairDecision(
                thought="heal model failed",
                strategy="no_fix",
                confidence=0.0,
            )
