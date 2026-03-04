import logging
from typing import Optional
from pydantic import BaseModel, Field
import instructor
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ReActAgent")

# ==========================================
# 1. 动态单步决策 Schema
# ==========================================
class NextAction(BaseModel):
    thought: str = Field(description="Step-by-step reasoning: What is the goal? What is on the screen right now? What should I do next?")
    action_type: str = Field(description="MUST be one of: [navigate, click, type, done]")
    target: Optional[str] = Field(None, description="If action is 'navigate', put URL here. If 'click' or 'type', put the exact ID bracket here, e.g., '[hexa-id=\"hexa-5\"]'")
    input_value: Optional[str] = Field(None, description="Text to input if action_type is 'type'.")

class AnalyzedAction(BaseModel):
    thought: str = Field(description="Analyze why the user clicked this element to achieve the goal.")
    description: str = Field(description="A concise description of the step, e.g., '点击登录按钮' or '点击搜索框'")


class ReplayRepairDecision(BaseModel):
    thought: str = Field(description="Why the previous step failed and what fix is most reliable now.")
    strategy: str = Field(
        description=(
            "MUST be one of: [retry_with_new_selector, replace_action, skip_step, human_handoff, no_fix]"
        )
    )
    action_type: Optional[str] = Field(
        None, description="Used when strategy=replace_action. One of [navigate, click, type, wait_for_timeout]."
    )
    target: Optional[str] = Field(
        None, description="New selector/URL when strategy=retry_with_new_selector or replace_action."
    )
    input_value: Optional[str] = Field(None, description="Input value when action_type='type'.")
    skip_reason: Optional[str] = Field(None, description="Reason when strategy=skip_step.")
    confidence: float = Field(default=0.5, ge=0, le=1)

# ==========================================
# 2. 动态大脑核心逻辑
# ==========================================
class ReActAgent:
    def __init__(self, api_key: str, base_url: str, model_name: str):
        self.model_name = model_name
        self.client = instructor.from_openai(AsyncOpenAI(api_key=api_key, base_url=base_url))
        self.system_prompt = """
        You are an autonomous Web RPA Agent. 
        You will be given a User Goal, your Action History, and the Current Screen's interactive elements (with hexa-id).
        
        CRITICAL RULES:
        1. If the current page matches the goal, output action_type='done'.
        2. If you need to click or type, you MUST use the exact CSS selector format: '[hexa-id="hexa-X"]' based on the provided Current Screen data.
        3. Do NOT guess selectors. Only use the IDs provided in the Current Screen.
        4. Explain your logic in the 'thought' field before acting.
        """

    async def decide_next_action(self, goal: str, history: str, current_url: str, dom_snapshot: str) -> NextAction:
        prompt = f"""
        USER GOAL: {goal}
        
        ACTION HISTORY SO FAR:
        {history if history else "No actions taken yet."}
        
        CURRENT URL: {current_url}
        
        CURRENT SCREEN (Interactive Elements):
        {dom_snapshot}
        
        Based on the above, what is the SINGLE next action to take?
        """
        
        call_kwargs = {
            "model": self.model_name,
            "response_model": NextAction,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt}
            ]
        }
        
        try:
            action = await self.client.chat.completions.create(**call_kwargs)
            logger.info(f"🧠 [Agent 思考]: {action.thought}")
            return action
        except Exception as e:
            logger.error(f"❌ [Agent 决策失败]: {e}")
            raise

    async def analyze_manual_action(self, goal: str, action_data: dict) -> AnalyzedAction:
        """AI 旁观者：分析用户的物理点击动作"""
        fp = action_data.get('fingerprint', {})
        action_type = action_data.get('action_type', 'click')
        input_value = action_data.get('input_value', '')
        prompt = f"""
        USER GOAL: {goal}
        
        The user just manually performed an action:
        - Action type: {action_type}
        - Input value: {input_value if input_value else "(none)"}
        
        The target element has the following properties:
        - Tag: {fp.get('tag_name')}
        - Text: {fp.get('text')}
        - Aria-label: {fp.get('aria_label')}
        - Classes: {fp.get('classes')}
        
        Analyze why the user made this action to achieve the goal, and provide a short step description in Chinese.
        If action_type is 'type', the description should mention input content intention.
        """
        
        call_kwargs = {
            "model": self.model_name,
            "response_model": AnalyzedAction,
            "messages": [
                {"role": "system", "content": "You are a helpful RPA action analyzer."},
                {"role": "user", "content": prompt}
            ]
        }
        
        try:
            logger.info("🧠 [Agent] 正在分析用户的操作意图...")
            analysis = await self.client.chat.completions.create(**call_kwargs)
            return analysis
        except Exception as e:
            logger.error(f"❌ AI 分析失败: {e}")
            return AnalyzedAction(thought="Failed to analyze.", description="执行点击操作")

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
    ) -> ReplayRepairDecision:
        prompt = f"""
        TASK NAME: {task_name}
        RECENT REPLAY STEPS:
        {recent_steps}

        FAILED STEP:
        - description: {failed_step_desc}
        - action_type: {failed_action_type}
        - target: {failed_target}
        - error: {last_error}

        CURRENT URL: {current_url}
        CURRENT INTERACTIVE DOM:
        {dom_snapshot}

        Decide the best single recovery strategy now.
        """

        system_prompt = """
        You are a replay-repair agent for web automation.
        Your task is to recover from a failed deterministic step with minimal risk.

        STRICT RULES:
        1) Prefer retry_with_new_selector when intent is still same but selector changed.
        2) Use replace_action when current page state changed and another action is needed.
        3) Use skip_step only when this step is clearly optional/redundant.
        4) Use human_handoff for login/captcha/2FA/wallet unlock/signature scenarios.
        5) If no reliable fix, use no_fix.
        6) Never invent impossible selectors; prefer concise text selectors or robust css.
        """

        call_kwargs = {
            "model": self.model_name,
            "response_model": ReplayRepairDecision,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            decision = await self.client.chat.completions.create(**call_kwargs)
            logger.info(
                f"🧠 [ReplayRepair] strategy={decision.strategy} confidence={decision.confidence:.2f} thought={decision.thought}"
            )
            return decision
        except Exception as e:
            logger.error(f"❌ [ReplayRepair] 决策失败: {e}")
            return ReplayRepairDecision(
                thought="repair model failed",
                strategy="no_fix",
                confidence=0.0,
            )
