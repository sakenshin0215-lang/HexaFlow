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
        
        # 1. 组装基础的通用调用参数
        call_kwargs = {
            "model": self.model_name,
            "response_model": NextAction,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt}
            ]
        }
        
        # 2. 动态参数注入：如果是 Qwen3.5 模型，关闭 Think 模式
        # 这里使用 lower() 保证兼容 qwen3.5, Qwen-3.5 等各种命名习惯
        if "qwen3.5" in self.model_name.lower() or "qwen-3.5" in self.model_name.lower():
            logger.info(f" Qwen3.5, enable_thinking=False")
            call_kwargs["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": False}
            }
        
        try:
            # 3. 将参数解包传入 client
            action = await self.client.chat.completions.create(**call_kwargs)
            logger.info(f"🧠 [Agent 思考]: {action.thought}")
            return action
        except Exception as e:
            logger.error(f"❌ [Agent 决策失败]: {e}")
            raise