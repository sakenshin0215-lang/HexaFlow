import asyncio
import json

from hexaflow.agents.react_agent import ReActAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine

async def main():
    # ===== Main-level runtime config (no runtime_config loader) =====
    task_spec_path = "workspace/task_specs/okx_web3_ai_token.json"
    use_cdp = True
    headless = False
    cdp_user_data_dir = "/Users/kenshinnb/pw-profiles/okx-chrome"
    cdp_profile = "Default"
    ai_repair_only = True
    ai_decision_use_vision = True
    enabled_skill_ids = ["popup"]  # e.g. ["popup"] / [] to disable all skills
    save_reports = False  # default off

    # AI provider config: edit directly here when switching provider.
    ai_provider = {
        "provider": "openai_compatible",
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
        "model_name": "qwen3-vl:8b-instruct",
    }

    spec_start_url = "https://www.okx.com/web3"
    try:
        with open(task_spec_path, "r", encoding="utf-8") as f:
            spec_data = json.load(f)
            spec_start_url = spec_data.get("start_url", spec_start_url) or spec_start_url
    except Exception:
        pass

    cdp_start_url = spec_start_url

    ai_agent = ReActAgent(
        api_key=ai_provider["api_key"],
        base_url=ai_provider["base_url"],
        model_name=ai_provider["model_name"],
        provider=ai_provider["provider"],
        enabled_skill_ids=enabled_skill_ids,
    )

    engine = HexaEngine(headless=headless, save_reports=save_reports)
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig(
            user_data_dir=cdp_user_data_dir,
            profile_directory=cdp_profile,
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
        engine.print_report_summary(result)
        input("\n执行完毕，按回车退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
