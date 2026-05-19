import asyncio
import os
from hexaflow.core.engine import HexaEngine
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.agents.react_agent import ReActAgent

async def main():
    
    trace_file = "memory/workspace/traces/ManualTask_20260304_191722_20260304_192019.json" 
    
    engine = HexaEngine(headless=False) 
    enable_ai_repair = os.getenv("ENABLE_REPLAY_AI_REPAIR", "1") == "1"
    repair_agent = None
    if enable_ai_repair:
        repair_agent = ReActAgent(
            api_key=os.getenv("REPLAY_REPAIR_API_KEY", "ollama"),
            base_url=os.getenv("REPLAY_REPAIR_BASE_URL", "http://localhost:11434/v1"),
            model_name=os.getenv("REPLAY_REPAIR_MODEL", "qwen3-vl:8b-instruct")
        )

    use_cdp = os.getenv("USE_CDP", "1") == "1"
    cdp_start_url = os.getenv("CDP_START_URL", "https://www.okx.com/web3")
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig() if use_cdp else None,
        cdp_start_url=cdp_start_url
    )
    
    try:
        report_paths = await engine.run_from_trace(
            trace_path=trace_file,
            replay_repair_agent=repair_agent,
            repair_context_window=int(os.getenv("REPAIR_CONTEXT_WINDOW", "5")),
        )
        if report_paths:
            print(f"\n📄 报告(JSON): {report_paths['json_path']}")
            print(f"📝 报告(MD):   {report_paths['md_path']}")
        
        input("\n执行完毕，按回车键退出...")
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())
