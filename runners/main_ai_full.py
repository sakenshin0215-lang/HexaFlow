import asyncio
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexaflow.agents.heal_agent import AIHealAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine


async def main():
    default_spec = "memory/workspace/task_specs/okx_web3_ai_full.json"
    if not Path(default_spec).exists():
        default_spec = "memory/workspace/task_specs/okx_web3_demo.json"

    task_spec_path = os.getenv("AI_TASK_SPEC_PATH", default_spec)
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

    ai_api_key = os.getenv("REPLAY_REPAIR_API_KEY", "ollama")
    ai_base_url = os.getenv("REPLAY_REPAIR_BASE_URL", "http://localhost:11434/v1")
    ai_model = os.getenv("REPLAY_REPAIR_MODEL", "qwen3-vl:8b-instruct")
    ai_agent = AIHealAgent(
        api_key=ai_api_key,
        base_url=ai_base_url,
        model_name=ai_model,
    )

    engine = HexaEngine(headless=False)
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig() if use_cdp else None,
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
