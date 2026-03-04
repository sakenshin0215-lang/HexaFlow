import asyncio

from hexaflow.core.engine import HexaEngine


async def main():
    engine = HexaEngine(headless=False)
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

    await engine.start()
    try:
        report_paths = await engine.run_from_trace(
            trace_path=run.trace_path,
            run_id=run.run_id,
            resume=True,
            suspend_on_failure=True,
        )
        if report_paths:
            print(f"\n📄 报告(JSON): {report_paths['json_path']}")
            print(f"📝 报告(MD):   {report_paths['md_path']}")
        input("\n恢复执行结束，按回车退出...")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
