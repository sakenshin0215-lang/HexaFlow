import asyncio

from hexaflow.agents.heal_agent import AIHealAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine
from hexaflow.tools.runtime_config import load_runtime_config


async def main():
    cfg, cfg_path = load_runtime_config()
    browser_cfg = cfg.get("browser", {})
    loop_cfg = cfg.get("loop", {})
    agent_cfg = cfg.get("agent", {})
    print(f"Using runtime config: {cfg_path}")

    trace_file = loop_cfg.get(
        "trace_path", "workspace/traces/ManualTask_20260311_150422_20260311_150615.json"
    )
    task_spec_path = loop_cfg.get("task_spec_path", "workspace/task_specs/okx_web3_demo.json")

    use_cdp = browser_cfg.get("use_cdp", True)
    cdp_start_url = browser_cfg.get(
        "cdp_start_url",
        "https://web3.okx.com/zh-hans/token/bsc/0xda7ad9dea9397cffddae2f8a052b82f1484252b3",
    )
    cdp_user_data_dir = browser_cfg.get("user_data_dir", "/Users/kenshinnb/pw-profiles/okx-chrome")
    cdp_profile = browser_cfg.get("profile_directory", "Default")
    enable_ai_repair = loop_cfg.get("enable_ai_repair", True)
    ai_repair_only = loop_cfg.get("ai_repair_only", True)
    repair_agent = None
    if enable_ai_repair:
        repair_agent = AIHealAgent(
            api_key=agent_cfg.get("api_key", "ollama"),
            base_url=agent_cfg.get("base_url", "http://localhost:11434/v1"),
            model_name=agent_cfg.get("model_name", "qwen3-vl:8b-instruct"),
        )

    engine = HexaEngine(headless=browser_cfg.get("headless", False))
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig(
            user_data_dir=cdp_user_data_dir,
            profile_directory=cdp_profile,
        ) if use_cdp else None,
        cdp_start_url=cdp_start_url
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
        heal_paths = result.get("heal_log_paths") if isinstance(result, dict) else None
        if heal_paths:
            print(f"\n🩹 修复清单(JSON): {heal_paths.get('json_path')}")
            print(f"🩹 修复清单(MD):   {heal_paths.get('md_path')}")
        input("\n执行完毕，按回车退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
