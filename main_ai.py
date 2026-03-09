import asyncio
import os
import json

from hexaflow.agents.heal_agent import AIHealAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine


async def main():
    task_spec_path = os.getenv(
        "AI_TASK_SPEC_PATH",
        "memory/workspace/task_specs/okx_web3_ai_full.json",
    )
    spec_start_url = "https://www.okx.com/web3"
    try:
        with open(task_spec_path, "r", encoding="utf-8") as f:
            spec_data = json.load(f)
            spec_start_url = spec_data.get("start_url", spec_start_url) or spec_start_url
    except Exception:
        pass

    use_cdp = os.getenv("USE_CDP", "1") == "1"
    cdp_start_url = os.getenv("CDP_START_URL", spec_start_url)
    ai_repair_only = os.getenv("AI_REPAIR_ONLY", "1") == "1"
    ai_decision_use_vision = os.getenv("AI_DECISION_USE_VISION", "1") == "1"

    ai_agent = AIHealAgent(
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        model_name="qwen3-vl:8b-instruct"
    )

    engine = HexaEngine(headless=False)
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig(
            user_data_dir=os.getenv("CHROME_USER_DATA_DIR", "/Users/kenshinnb/pw-profiles/okx-chrome"),
            profile_directory=os.getenv("CHROME_PROFILE_DIRECTORY", "Default"),
        )
        if use_cdp
        else None,
        cdp_start_url=cdp_start_url,
    )

    try:
        result = await engine.run_task_from_spec(
            spec_input=task_spec_path,
            agent=ai_agent,
            replay_repair_agent=ai_agent,
            disable_fallback_recovery=ai_repair_only,
            ai_decision_use_vision=ai_decision_use_vision,
        )
        print("\nAI 全流程运行结果:")
        print(result)
        if isinstance(result, dict):
            ai_log = result.get("ai_action_log")
            if ai_log:
                print(f"🤖 AI操作日志(JSON): {ai_log.get('json_path')}")
                print(f"🤖 AI操作日志(MD):   {ai_log.get('md_path')}")
        input("\n执行完毕，按回车退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
