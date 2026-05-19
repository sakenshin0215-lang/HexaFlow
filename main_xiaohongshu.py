import asyncio
import os
from pathlib import Path

from instructor.core.exceptions import InstructorRetryException
from openai import AuthenticationError
from playwright._impl._errors import Error as PlaywrightError

from hexaflow.agents.react_agent import ReActAgent
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.core.engine import HexaEngine


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if value:
        return value
    raise RuntimeError(f"缺少环境变量: {name}")


async def main():
    os.environ.setdefault("CDP_PORT", "9333")
    ai_decision_use_vision = os.getenv("XHS_USE_VISION", "0") == "1"
    model_name = os.getenv("XHS_MODEL", "openai/gpt-oss-120b")
    task_spec_path = os.getenv(
        "XHS_TASK_SPEC_PATH",
        "memory/workspace/task_specs/xiaohongshu_browse_assist.json",
    )
    if not Path(task_spec_path).exists():
        raise FileNotFoundError(f"任务文件不存在: {task_spec_path}")

    agent = ReActAgent(
        api_key=_require_env("OPENAI_API_KEY"),
        base_url=_require_env("OPENAI_BASE_URL"),
        model_name=model_name,
    )

    use_cdp = os.getenv("USE_CDP", "1") == "1"
    cdp_start_url = os.getenv("CDP_START_URL", "https://www.xiaohongshu.com/explore")

    engine = HexaEngine(headless=False)
    try:
        await engine.start(
            use_cdp=use_cdp,
            cdp_config=CDPConfig() if use_cdp else None,
            cdp_start_url=cdp_start_url,
        )
    except PlaywrightError as exc:
        message = str(exc)
        if "Browser.setDownloadBehavior" in message and "Browser context management is not supported" in message:
            raise RuntimeError(
                "CDP 连接到了一个不被 Playwright 完整支持的调试端点。"
                "通常是因为当前 CDP_PORT 已被别的程序占用，或者连到的不是标准 Chrome/Chromium。"
                "请改用新的端口重试，例如 `CDP_PORT=9333 python main_xiaohongshu.py`。"
            ) from exc
        raise

    try:
        try:
            result = await engine.run_task_from_spec(
                task_spec_path,
                agent=agent,
                ai_decision_use_vision=ai_decision_use_vision,
            )
        except AuthenticationError as exc:
            raise RuntimeError(
                "模型调用鉴权失败，请检查 OPENAI_API_KEY / OPENAI_BASE_URL 是否可用，"
                f"以及该网关是否支持模型 {model_name}。"
            ) from exc
        except InstructorRetryException as exc:
            message = str(exc)
            if "401" in message or "invalid_api_key" in message.lower():
                raise RuntimeError(
                    "模型调用鉴权失败，请检查 OPENAI_API_KEY / OPENAI_BASE_URL 是否可用，"
                    f"以及该网关是否支持模型 {model_name}。"
                ) from exc
            raise

        print("\n小红书浏览辅助运行结果:")
        print(result)
        input("\n执行完毕，按回车退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
