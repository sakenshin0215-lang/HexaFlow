import asyncio
import json
import os
from dotenv import load_dotenv

from hexaflow.agents.heal_agent import AIHealAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine
from hexaflow.tools.runtime_config import load_runtime_config

load_dotenv()

async def main():
    cfg, cfg_path = load_runtime_config()
    task_spec_path = cfg["ai"]["task_spec_path"]
    spec_start_url = "https://www.okx.com/web3"
    try:
        with open(task_spec_path, "r", encoding="utf-8") as f:
            spec_data = json.load(f)
            spec_start_url = spec_data.get("start_url", spec_start_url) or spec_start_url
    except Exception:
        pass

    use_cdp = bool(cfg["browser"]["use_cdp"])
    cdp_start_url = cfg["browser"]["cdp_start_url"] or spec_start_url
    ai_repair_only = bool(cfg["ai"]["ai_repair_only"])
    ai_decision_use_vision = bool(cfg["ai"]["decision_use_vision"])

    ai_agent = AIHealAgent(
        api_key=os.getenv("OLLAMA_API_KEY", ""),
        base_url=os.getenv("OLLAMA_BASE_URL", ""),
        model_name=os.getenv("OLLAMA_MODEL", ""),
    )

    ai_agent_openai = AIHealAgent(
        api_key=os.getenv("SILICONFLOW_API_KEY", ""),
        base_url=os.getenv("SILICONFLOW_BASE_URL", ""),
        model_name=os.getenv("SILICONFLOW_MODEL", ""),
    )

    engine = HexaEngine(headless=bool(cfg["browser"]["headless"]))
    print(f"Using runtime config: {cfg_path}")
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig(
            user_data_dir=cfg["browser"]["user_data_dir"],
            profile_directory=cfg["browser"]["profile_directory"],
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
