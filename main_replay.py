import asyncio

from hexaflow.agents.react_agent import ReActAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine


async def main():
    trace_file = "workspace/traces/Task_20260312_153124_20260312_153520.json"
    use_cdp = True
    headless = False
    cdp_start_url = "https://www.okx.com/web3"
    cdp_user_data_dir = "/Users/kenshinnb/pw-profiles/okx-chrome"
    cdp_profile = "Default"
    enable_ai_repair = True
    ai_repair_only = False
    repair_context_window = 5
    enabled_skill_ids = ["popup"]  # e.g. ["popup"] / [] to disable all skills
    save_reports = False  # default off

    ai_provider = {
        "provider": "openai_compatible",
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
        "model_name": "qwen3-vl:8b-instruct",
    }

    engine = HexaEngine(headless=headless, save_reports=save_reports)
    repair_agent = None
    if enable_ai_repair:
        repair_agent = ReActAgent(
            api_key=ai_provider["api_key"],
            base_url=ai_provider["base_url"],
            model_name=ai_provider["model_name"],
            provider=ai_provider["provider"],
            enabled_skill_ids=enabled_skill_ids,
        )

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
        report_paths = await engine.run_from_trace(
            trace_path=trace_file,
            replay_repair_agent=repair_agent,
            repair_context_window=repair_context_window,
            disable_fallback_recovery=ai_repair_only,
        )
        engine.print_report_summary(report_paths)

        input("\n执行完毕，按回车键退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
