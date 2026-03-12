import logging


logger = logging.getLogger("ToolRuntime")


async def execute_tool(
    *,
    tool_name: str,
    agent=None,
    goal: str = "",
    history: str = "",
    current_url: str = "",
    dom_snapshot: str = "",
    screenshot_path: str = "",
    instruction: str = "",
) -> str:
    """
    Execute built-in tool calls.
    Current tools:
    - summarize_page
    """
    name = (tool_name or "").strip().lower()
    if name in ("summarize_page", "summarize"):
        if agent and hasattr(agent, "summarize_page"):
            try:
                return await agent.summarize_page(
                    goal=goal or "Summarize current page",
                    history=history or "",
                    current_url=current_url or "",
                    dom_snapshot=dom_snapshot or "",
                    instruction=instruction or "",
                    screenshot_path=screenshot_path or "",
                )
            except Exception as e:
                logger.warning("⚠️ [ToolRuntime] summarize_page via agent failed: %s", e)
        lines = [ln.strip() for ln in (dom_snapshot or "").splitlines() if ln.strip()]
        return "页面要点（工具降级总结）:\n" + "\n".join(lines[:8])
    raise ValueError(f"Unsupported tool: {tool_name}")

