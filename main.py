import asyncio
import os
from hexaflow.agents.react_agent import ReActAgent
from hexaflow.core.engine import HexaEngine

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

    # 2. 启动引擎并按 DSL 执行
    await engine.start()
    try:
        await engine.run_task_from_spec(task_spec_path, agent=agent2)
        
        input("\n执行完毕，按回车键退出...")
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())
