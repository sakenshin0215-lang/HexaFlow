import asyncio
import os
from hexaflow.agents.react_agent import ReActAgent
from hexaflow.core.engine import HexaEngine
from hexaflow.browser.cdp_runtime import CDPConfig

async def main():
    # 1. 选择模型
    agent1 = ReActAgent(
        api_key= os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        model_name= "openai/gpt-oss-120b"
    )

    agent2 = ReActAgent(
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        model_name="qwen3-vl:8b-instruct"
    )
    
    engine = HexaEngine(headless=False)
    task_spec_path = "memory/workspace/task_specs/okx_web3_demo.json"
    use_cdp = os.getenv("USE_CDP", "1") == "1"
    cdp_start_url = os.getenv("CDP_START_URL", "https://www.okx.com/web3")

    # 2. 启动引擎并按 DSL 执行
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig() if use_cdp else None,
        cdp_start_url=cdp_start_url
    )
    try:
        await engine.run_task_from_spec(task_spec_path, agent=agent1)
        
        input("\n执行完毕，按回车键退出...")
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())
