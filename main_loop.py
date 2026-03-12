import asyncio

from hexaflow.agents.react_agent import ReActAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine


async def main():
    trace_file = "workspace/traces/Task_20260312_153124_20260312_153520.json"
    task_spec_path = "workspace/task_specs/okx_web3_ai_token.json"
    use_cdp = True
    headless = False
    # cdp_start_url = "https://web3.okx.com/zh-hans/token/bsc/0xda7ad9dea9397cffddae2f8a052b82f1484252b3"
    cdp_user_data_dir = "/Users/kenshinnb/pw-profiles/okx-chrome"
    cdp_profile = "Default"
    enable_ai_repair = True
    ai_repair_only = True
    enabled_skill_ids = ["popup"]  # e.g. ["popup"] / [] to disable all skills
    save_reports = False  # default off

    ai_provider = {
        "provider": "openai_compatible",
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
        "model_name": "qwen3-vl:8b-instruct",
    }

    repair_agent = None
    if enable_ai_repair:
        repair_agent = ReActAgent(
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
        ) if use_cdp else None,
        # cdp_start_url=cdp_start_url
    )
    try:
        result = await engine.run_loop_task_from_spec(
            trace_path=trace_file,
            spec_input=task_spec_path,
            replay_repair_agent=repair_agent,
            disable_fallback_recovery=ai_repair_only,
        )
        print("\n循环任务结果:")
        print(result)
        engine.print_report_summary(result)
        input("\n执行完毕，按回车退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
