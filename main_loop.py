import asyncio
import os

from hexaflow.agents.react_agent import ReActAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine


async def main():
    trace_file = "memory/workspace/traces/ManualTask_20260304_191722_20260304_192019.json"
    task_spec_path = "memory/workspace/task_specs/okx_web3_demo.json"

    use_cdp = os.getenv("USE_CDP", "1") == "1"
    cdp_start_url = os.getenv("CDP_START_URL", "https://www.okx.com/web3")
    enable_ai_repair = os.getenv("ENABLE_REPLAY_AI_REPAIR", "1") == "1"
    repair_agent = None
    if enable_ai_repair:
        repair_agent = ReActAgent(
            api_key=os.getenv("REPLAY_REPAIR_API_KEY", "ollama"),
            base_url=os.getenv("REPLAY_REPAIR_BASE_URL", "http://localhost:11434/v1"),
            model_name=os.getenv("REPLAY_REPAIR_MODEL", "qwen3-vl:8b-instruct"),
        )

    engine = HexaEngine(headless=False)
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig(
        user_data_dir="/Users/kenshinnb/pw-profiles/okx-chrome",
        profile_directory="Default",
        ) if use_cdp else None,
        cdp_start_url=cdp_start_url
    )
    try:
        result = await engine.run_loop_task_from_spec(
            trace_path=trace_file,
            spec_input=task_spec_path,
            replay_repair_agent=repair_agent,
        )
        print("\n循环任务结果:")
        print(result)
        input("\n执行完毕，按回车退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())

