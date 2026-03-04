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
        prompt = f"""
        USER GOAL: {goal}
        
        The user just manually clicked an element with the following properties:
        - Tag: {fp.get('tag_name')}
        - Text: {fp.get('text')}
        - Aria-label: {fp.get('aria_label')}
        - Classes: {fp.get('classes')}
        
        Analyze why the user made this action to achieve the goal, and provide a short step description in Chinese.
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