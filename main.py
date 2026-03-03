import asyncio
import os
from hexaflow.agents.react_agent import ReActAgent
from hexaflow.core.engine import HexaEngine

async def main():
    # 1. 告诉 AI 你的任务
    task_goal = "帮我打开 OKX 的 Web3 首页 (https://www.okx.com/web3)，等页面加载出来后，点击左上角的行情, 然后再点击OKX Boost, 然后输出有哪些代币。注意：因为是跨域或复杂的SPA，等DOM出现了再点。"
    
    # 2. 生成确定性的 JSON 蓝图
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

    # 3. 启动引擎并执行
    await engine.start()
    try:
        await engine.run_dynamic_task(goal=task_goal, agent=agent2)
        
        input("\n执行完毕，按回车键退出...")
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())