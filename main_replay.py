import asyncio

from hexaflow.agents.heal_agent import AIHealAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine
from hexaflow.tools.runtime_config import load_runtime_config


async def main():
    cfg, cfg_path = load_runtime_config()
    browser_cfg = cfg.get("browser", {})
    replay_cfg = cfg.get("replay", {})
    agent_cfg = cfg.get("agent", {})
    print(f"Using runtime config: {cfg_path}")

    trace_file = replay_cfg.get(
        "trace_path", "workspace/traces/Task_20260311_195022_20260311_195246.json"
    )

    engine = HexaEngine(headless=browser_cfg.get("headless", False))
    enable_ai_repair = replay_cfg.get("enable_ai_repair", True)
    ai_repair_only = replay_cfg.get("ai_repair_only", False)
    repair_agent = None
    if enable_ai_repair:
        repair_agent = AIHealAgent(
            api_key=agent_cfg.get("api_key", "ollama"),
            base_url=agent_cfg.get("base_url", "http://localhost:11434/v1"),
            model_name=agent_cfg.get("model_name", "qwen3-vl:8b-instruct"),
        )

    use_cdp = browser_cfg.get("use_cdp", True)
    cdp_start_url = browser_cfg.get("cdp_start_url", "https://www.okx.com/web3")
    cdp_user_data_dir = browser_cfg.get("user_data_dir", "/Users/kenshinnb/pw-profiles/okx-chrome")
    cdp_profile = browser_cfg.get("profile_directory", "Default")
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
            repair_context_window=int(replay_cfg.get("repair_context_window", 5)),
            disable_fallback_recovery=ai_repair_only,
        )
        if report_paths:
            print(f"\n📄 报告(JSON): {report_paths['json_path']}")
            print(f"📝 报告(MD):   {report_paths['md_path']}")
            heal_paths = report_paths.get("heal_log")
            if heal_paths:
                print(f"🩹 修复清单(JSON): {heal_paths.get('json_path')}")
                print(f"🩹 修复清单(MD):   {heal_paths.get('md_path')}")

        input("\n执行完毕，按回车键退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
