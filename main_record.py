import asyncio

from hexaflow.core.engine import HexaEngine
from hexaflow.agents.react_agent import ReActAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.tools.runtime_config import load_runtime_config


async def main():
    cfg, cfg_path = load_runtime_config()
    browser_cfg = cfg.get("browser", {})
    agent_cfg = cfg.get("agent", {})
    record_cfg = cfg.get("record", {})
    print(f"Using runtime config: {cfg_path}")

    engine = HexaEngine(headless=browser_cfg.get("headless", False))
    use_cdp = browser_cfg.get("use_cdp", True)
    cdp_start_url = browser_cfg.get(
        "cdp_start_url",
        "https://web3.okx.com/zh-hans/token/bsc/0xda7ad9dea9397cffddae2f8a052b82f1484252b3",
    )
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

    agent = ReActAgent(
        api_key=agent_cfg.get("api_key", "ollama"),
        base_url=agent_cfg.get("base_url", "http://localhost:11434/v1"),
        model_name=agent_cfg.get("model_name", "qwen3-vl:8b-instruct"),
    )

    # 定义你要录制的目标和起始网址
    goal = record_cfg.get("goal", "在加密货币交易所查看实时行情数据")
    start_url = record_cfg.get(
        "start_url",
        "https://web3.okx.com/zh-hans/token/bsc/0xda7ad9dea9397cffddae2f8a052b82f1484252b3",
    )
    await engine.run_manual_record_task(
        goal=goal,
        agent=agent,
        start_url=start_url,
    )

    await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
