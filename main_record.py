import asyncio
import os
from hexaflow.core.engine import HexaEngine
from hexaflow.agents.react_agent import ReActAgent

async def main():
    engine = HexaEngine(headless=False)
    await engine.start()
    
    agent = ReActAgent(
        api_key= os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        model_name= "openai/gpt-oss-120b"
    )
    
    agent = ReActAgent(
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        model_name="qwen3-vl:8b-instruct"
    )

    # 定义你要录制的目标和起始网址
    goal = "在加密货币交易所查看实时行情数据"
    await engine.run_manual_record_task(
        goal=goal,
        agent=agent,
        start_url="https://web3.okx.com/"  
    )
    
    await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())