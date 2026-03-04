from playwright.sync_api import Page


def detect_human_intervention_reason(page: Page) -> str:
    """
    检测当前页面是否需要人工介入（登录/验证码/2FA/钱包解锁等）。
    返回空字符串表示不需要人工。
    """
    url = (page.url or "").lower()
    url_markers = ["login", "signin", "auth", "verify", "captcha", "challenge"]
    if any(marker in url for marker in url_markers):
        return f"URL 命中登录/验证路径: {page.url}"

    text_markers = [
        "登录",
        "重新登录",
        "验证码",
        "验证",
        "人机验证",
        "二次验证",
        "双重验证",
        "Sign in",
        "Log in",
        "Verify",
        "CAPTCHA",
        "2FA",
        "Authentication",
        "解锁钱包",
        "Unlock",
        "Wallet password",
        "请输入密码",
    ]
    for text in text_markers:
        try:
            if page.get_by_text(text, exact=False).first.is_visible(timeout=800):
                return f"页面检测到人工验证文案: {text}"
        except Exception:
            continue
    return ""


def call_human_intervention(reason: str, page: Page, checkpoint: str = ""):
    title = "[人工接管] 检测到需要人工处理"
    line = "=" * 72
    print("\n" + line)
    print(title)
    if checkpoint:
        print("检查点:", checkpoint)
    print("原因:", reason)
    print("当前 URL:", page.url)
    print("请在浏览器里完成操作（按需）：")
    print("1) 重新登录账号")
    print("2) 完成人机验证/验证码")
    print("3) 解锁钱包扩展、签名确认")
    print("完成后回到终端按回车继续自动流程。")
    print(line + "\n")
    input("[等待人工] 完成后按回车继续... ")


def guard_with_human_handoff(page: Page, checkpoint: str = "") -> bool:
    reason = detect_human_intervention_reason(page)
    if not reason:
        return False
    call_human_intervention(reason, page, checkpoint=checkpoint)
    return True

