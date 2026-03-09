import asyncio
import os

from hexaflow.core.engine import HexaEngine
from hexaflow.browser.cdp_runtime import CDPConfig
from hexaflow.agents.heal_agent import AIHealAgent


async def main():
    engine = HexaEngine(headless=False)
    enable_ai_repair = os.getenv("ENABLE_REPLAY_AI_REPAIR", "1") == "1"
    ai_repair_only = os.getenv("AI_REPAIR_ONLY", "0") == "1"
    repair_agent = None
    if enable_ai_repair:
        repair_agent = AIHealAgent(
            api_key=os.getenv("REPLAY_REPAIR_API_KEY", "ollama"),
            base_url=os.getenv("REPLAY_REPAIR_BASE_URL", "http://localhost:11434/v1"),
            model_name=os.getenv("REPLAY_REPAIR_MODEL", "qwen3-vl:8b-instruct")
        )

    suspended = engine.state_machine.list_runs(status="suspended", limit=10)

    if not suspended:
        print("当前没有挂起任务。")
        return

    print("\n最近挂起任务:")
    for idx, run in enumerate(suspended, start=1):
        print(
            f"{idx}. run_id={run.run_id} task={run.task_name} "
            f"step={run.current_step_index}/{run.total_steps} updated_at={run.updated_at}"
        )
        if run.last_error:
            print(f"   last_error: {run.last_error[:180]}")

    selected = input("\n输入要恢复的 run_id（直接回车=恢复第1条）: ").strip()
    run = suspended[0] if not selected else engine.state_machine.get_by_run_id(selected)
    if not run:
        raise ValueError("找不到该 run_id，请重试。")

    print(f"\n准备恢复 run_id={run.run_id} trace={run.trace_path}")
    events = engine.state_machine.list_events(run.run_id, limit=8)
    if events:
        print("最近事件:")
        for e in reversed(events):
            info = f" - [{e.created_at}] {e.event}"
            if e.step_id:
                info += f" step={e.step_id}"
            if e.detail:
                detail = e.detail.replace("\n", " ")
                info += f" detail={detail[:180]}"
            print(info)

    use_cdp = os.getenv("USE_CDP", "1") == "1"
    cdp_start_url = os.getenv("CDP_START_URL", "https://www.okx.com/web3")
    await engine.start(
        use_cdp=use_cdp,
        cdp_config=CDPConfig() if use_cdp else None,
        cdp_start_url=cdp_start_url
    )
    try:
        report_paths = await engine.run_from_trace(
            trace_path=run.trace_path,
            run_id=run.run_id,
            resume=True,
            suspend_on_failure=True,
            replay_repair_agent=repair_agent,
            repair_context_window=int(os.getenv("REPAIR_CONTEXT_WINDOW", "5")),
            disable_fallback_recovery=ai_repair_only,
        )
        if report_paths:
            print(f"\n📄 报告(JSON): {report_paths['json_path']}")
            print(f"📝 报告(MD):   {report_paths['md_path']}")
            heal_paths = report_paths.get("heal_log")
            if heal_paths:
                print(f"🩹 修复清单(JSON): {heal_paths.get('json_path')}")
                print(f"🩹 修复清单(MD):   {heal_paths.get('md_path')}")
        input("\n恢复执行结束，按回车退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
