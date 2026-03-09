import asyncio
from playwright.async_api import async_playwright
from hexaflow.browser.cdp_runtime import CDPConfig, ensure_cdp_browser
from hexaflow.tools.token_selector import ensure_quote_token

async def main():
    cfg = CDPConfig(
        user_data_dir="/Users/kenshinnb/pw-profiles/okx-chrome",
        profile_directory="Default",
    )
    ensure_cdp_browser(cfg, start_url="https://www.okx.com/web3")
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp(cfg.endpoint)
    ctx = browser.contexts[0]
    page = await ctx.new_page()
    await page.goto("https://web3.okx.com/zh-hans/token/bsc/0xda7ad9dea9397cffddae2f8a052b82f1484252b3", wait_until="domcontentloaded")

    final_symbol = await ensure_quote_token(
        page,
        target_symbol="USDT",
        button_selector=".dex-select-value-box button"
    )
    print("final_symbol =", final_symbol)
    input("检查页面后回车退出...")
    await browser.close()
    await p.stop()

asyncio.run(main())