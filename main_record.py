import asyncio
import os
from hexaflow.core.engine import HexaEngine
from hexaflow.agents.react_agent import ReActAgent
from hexaflow.browser.cdp_runtime import CDPConfig

async def main():
    engine = HexaEngine(headless=False)
    use_cdp = os.getenv("USE_CDP", "1") == "1"
    cdp_start_url = os.getenv("CDP_START_URL", "https://www.okx.com/web3")
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig(
        user_data_dir="/Users/kenshinnb/pw-profiles/okx-chrome",
        profile_directory="Default",
        ) if use_cdp else None,
        cdp_start_url=cdp_start_url
    )
    
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
