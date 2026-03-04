from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from hexaflow.browser.cdp_runtime import CDPConfig, ensure_cdp_browser
from hexaflow.browser.human_handoff import guard_with_human_handoff

OKX_WEB3_URL = "https://www.okx.com/web3"


def _click_connect_wallet(page: Page) -> bool:
    try:
        page.get_by_role("button", name="连接钱包").first.click(timeout=5000)
        return True
    except Exception:
        pass
    try:
        page.get_by_role("button", name="Connect Wallet").first.click(timeout=5000)
        return True
    except Exception:
        pass

    selectors = ["text=连接钱包", "text=Connect Wallet", "text=连接", "text=Connect"]
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=3000)
            return True
        except Exception:
            continue

    try:
        page.get_by_role("button").filter(has_text="连接").first.click(timeout=5000)
        return True
    except Exception:
        pass
    try:
        page.get_by_role("button").filter(has_text="Connect").first.click(timeout=5000)
        return True
    except Exception:
        pass
    return False


def _close_common_popups(page: Page):
    for text in ["Accept", "同意", "我同意", "知道了", "Got it", "关闭", "Close"]:
        try:
            page.get_by_role("button", name=text).first.click(timeout=1000)
            return
        except Exception:
            continue


def _click_by_candidates(page: Page, candidates: list[str], timeout_ms: int = 5000) -> bool:
    for sel in candidates:
        try:
            if sel.startswith("role=button:"):
                name = sel.split(":", 1)[1]
                page.get_by_role("button", name=name).first.click(timeout=timeout_ms)
            elif sel.startswith("text="):
                page.get_by_text(sel.split("=", 1)[1], exact=False).first.click(timeout=timeout_ms)
            else:
                page.locator(sel).first.click(timeout=timeout_ms)
            page.wait_for_timeout(900)
            return True
        except Exception:
            continue
    return False


def _fill_amount(page: Page, amount: str) -> bool:
    # 先尝试常见交易输入框
    candidates = [
        "input[placeholder*='0.0']",
        "input[placeholder*='数量']",
        "input[placeholder*='Amount']",
        "input[inputmode='decimal']",
        "input[type='number']",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).filter(has_not_text="搜索").first
            loc.click(timeout=3000)
            loc.fill("")
            loc.fill(amount, timeout=3000)
            page.wait_for_timeout(500)
            return True
        except Exception:
            continue
    return False


def _click_first_token_entry(page: Page) -> bool:
    candidates = [
        "a[href*='/token/']",
        "a[href*='/web3/token/']",
        "div[role='row'] a",
    ]
    for sel in candidates:
        try:
            page.locator(sel).first.click(timeout=5000)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_timeout(1000)
            return True
        except Exception:
            continue
    return False


def _check_risk_ack(page: Page) -> bool:
    # 先点文本/label
    candidates = [
        "text=我已知晓交易风险",
        "text=我已阅读并知晓交易风险",
        "text=I understand the trading risk",
        "label:has-text('我已知晓交易风险')",
        "label:has-text('交易风险')",
    ]
    if _click_by_candidates(page, candidates, timeout_ms=4000):
        return True

    # 再兜底直接点可见 checkbox
    try:
        cb = page.locator("input[type='checkbox']").first
        cb.check(timeout=3000)
        page.wait_for_timeout(500)
        return True
    except Exception:
        return False


def run_okx_cdp(config: CDPConfig | None = None):
    """
    对应原 run_okx_cdp.py：
    - 自动确保 CDP Chrome 可用
    - 打开 OKX Web3 页面
    """
    config = config or CDPConfig()
    ensure_cdp_browser(config, start_url=OKX_WEB3_URL)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(config.endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(OKX_WEB3_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        print("已打开:", page.url)
        print("标题:", page.title())
        print("CDP:", config.endpoint)
        print("UserData:", config.user_data_dir)
        print("Profile:", config.profile_directory)

        input("按回车结束（不会自动关闭你手动打开的Chrome）...")
        browser.close()


def run_okx_cdp_connect_wallet(config: CDPConfig | None = None):
    """
    对应原 run_okx_cdp2.py：
    - 自动确保 CDP Chrome 可用
    - 点击连接钱包
    - 遇到登录/验证/解锁需求时，自动呼叫人工后继续
    """
    config = config or CDPConfig()
    ensure_cdp_browser(config, start_url=OKX_WEB3_URL)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(config.endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(OKX_WEB3_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        print("已打开:", page.url)
        print("标题:", page.title())
        print("CDP:", config.endpoint)
        print("UserData:", config.user_data_dir)
        print("Profile:", config.profile_directory)

        guard_with_human_handoff(page, checkpoint="进入 OKX Web3 首页后检查登录状态")
        _close_common_popups(page)

        popup = None
        try:
            with context.expect_page(timeout=8000) as pinfo:
                ok = _click_connect_wallet(page)
                if not ok:
                    raise PWTimeout("没找到连接钱包按钮")
            popup = pinfo.value
        except Exception:
            ok = _click_connect_wallet(page)
            if not ok:
                print("未找到连接钱包按钮，转人工处理。")
                guard_with_human_handoff(page, checkpoint="连接钱包按钮未命中")
                ok = _click_connect_wallet(page)
                if not ok:
                    print("❌ 人工介入后仍未成功点击连接钱包，请检查页面结构。")
                    browser.close()
                    return

        print("✅ 已尝试点击连接钱包")

        if popup:
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            print("捕获到新页面/弹窗 URL:", popup.url)
            guard_with_human_handoff(popup, checkpoint="钱包弹窗/新页面校验")

        guard_with_human_handoff(page, checkpoint="主页面最终状态校验")
        input("按回车结束（不会自动关闭你手动打开的Chrome）...")
        browser.close()


def run_okx_web3_trade_flow(config: CDPConfig | None = None, amount: str = "0.01"):
    """
    CDP + 人工接管 的 OKX Web3 自动流程：
    1) 进入网站
    2) 点击行情
    3) 点击 OKX Boost
    4) 点击第一个币进入详情
    5) 勾选“我已知晓交易风险”
    6) 点击“开始交易”
    7) 输入 amount（默认 0.01）
    """
    config = config or CDPConfig()
    ensure_cdp_browser(config, start_url=OKX_WEB3_URL)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(config.endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(OKX_WEB3_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        print("已打开:", page.url)
        guard_with_human_handoff(page, checkpoint="打开首页后检查登录/验证状态")
        _close_common_popups(page)

        steps = [
            ("点击【行情】", lambda: _click_by_candidates(page, ["text=行情", "text=Market"])),
            ("点击【OKX Boost】", lambda: _click_by_candidates(page, ["text=OKX Boost"])),
            ("点击第一个币进入详情", lambda: _click_first_token_entry(page)),
            ("勾选【我已知晓交易风险】", lambda: _check_risk_ack(page)),
            ("点击【开始交易】", lambda: _click_by_candidates(page, ["text=开始交易", "text=Start Trading"])),
            (f"输入交易数量 {amount}", lambda: _fill_amount(page, amount)),
        ]

        for step_name, action in steps:
            ok = action()
            if ok:
                print(f"✅ {step_name}")
                guard_with_human_handoff(page, checkpoint=step_name)
                continue

            print(f"⚠️ {step_name} 自动执行失败，转人工接管后重试。")
            guard_with_human_handoff(page, checkpoint=step_name)
            ok = action()
            if not ok:
                print(f"❌ {step_name} 人工接管后仍失败，请检查当前页面结构。")
                input("请手动处理后按回车结束...")
                browser.close()
                return

            print(f"✅ {step_name}（人工接管后重试成功）")

        print("🎉 流程执行完成。你可以继续手动确认交易。")
        input("按回车结束（不会自动关闭你手动打开的Chrome）...")
        browser.close()
