import re

from playwright.async_api import Page


async def _click_token_option(page: Page, target: str, timeout_ms: int = 8000):
    # 1) 优先使用 option 容器里的 button（你给的 DOM 结构）
    candidates = [
        page.locator(f"div[role='option']:has-text('{target}') button").first,
        page.locator(f".dex-dropdown-option:has-text('{target}') button").first,
        page.get_by_role("option", name=target).locator("button").first,
        page.get_by_role("button", name=target).first,
        page.get_by_text(target, exact=True).first,
    ]

    last_err = None
    for loc in candidates:
        try:
            await loc.wait_for(state="visible", timeout=2500)
            await loc.scroll_into_view_if_needed(timeout=2000)
            try:
                await loc.click(timeout=3000)
            except Exception:
                # 避免被动画/遮挡导致点击失败
                await loc.click(timeout=3000, force=True)
            return
        except Exception as e:
            last_err = e
            continue

    raise Exception(f"找不到可点击币种选项: {target}; last_error={last_err}")


async def ensure_quote_token(
    page: Page,
    target_symbol: str = "USDT",
    button_selector: str = ".dex-select-value-box button",
    timeout_ms: int = 8000,
) -> str:
    """
    Ensure a DEX token dropdown currently selects `target_symbol`.
    If already selected, do nothing. Otherwise open dropdown and select target.
    Returns the final selected symbol.
    """
    target = (target_symbol or "").strip().upper()
    if not target:
        raise ValueError("target_symbol 不能为空")

    btn = page.locator(button_selector).first
    await btn.wait_for(state="visible", timeout=timeout_ms)

    raw = (await btn.inner_text()).strip()
    current = re.split(r"\s+", raw)[0].upper() if raw else ""
    if current == target:
        return current

    await btn.click(timeout=timeout_ms)
    # 等待下拉选项渲染到页面（portal 场景）
    try:
        await page.locator("div[role='option'], .dex-dropdown-option").first.wait_for(
            state="visible", timeout=timeout_ms
        )
    except Exception:
        pass

    await _click_token_option(page, target, timeout_ms=timeout_ms)

    await page.wait_for_timeout(500)
    raw_after = (await btn.inner_text()).strip()
    final_symbol = re.split(r"\s+", raw_after)[0].upper() if raw_after else ""
    if final_symbol != target:
        raise Exception(f"切换币种失败，当前={final_symbol}, 期望={target}")
    return final_symbol