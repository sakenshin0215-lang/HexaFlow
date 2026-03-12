import asyncio

from hexaflow.core.engine import HexaEngine
from hexaflow.agents.react_agent import ReActAgent
from hexaflow.browser.cdp_runtime import CDPConfig


async def main():
    goal = "在加密货币交易所查看实时行情数据"
    start_url = "https://web3.okx.com/zh-hans/token/bsc/0xda7ad9dea9397cffddae2f8a052b82f1484252b3"
    use_cdp = True
    headless = False
    cdp_start_url = "https://web3.okx.com/zh-hans/token/bsc/0xda7ad9dea9397cffddae2f8a052b82f1484252b3"
    cdp_user_data_dir = "/Users/kenshinnb/pw-profiles/okx-chrome"
    cdp_profile = "Default"

    ai_provider = {
        "provider": "openai_compatible",
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
        "model_name": "qwen3-vl:8b-instruct",
    }

    engine = HexaEngine(headless=headless)

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
        api_key=ai_provider["api_key"],
        base_url=ai_provider["base_url"],
        model_name=ai_provider["model_name"],
        provider=ai_provider["provider"],
    )

    await engine.run_manual_record_task(
        goal=goal,
        agent=agent,
        start_url=start_url,
    )

    await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
