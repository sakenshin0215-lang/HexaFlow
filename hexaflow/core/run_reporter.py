import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List

from hexaflow.core.state_machine import RunEvent, RunState, StateMachine


class RunReporter:
    def __init__(self, state_machine: StateMachine, report_dir: str = "memory/workspace/reports"):
        self.state_machine = state_machine
        self.report_dir = report_dir
        os.makedirs(self.report_dir, exist_ok=True)

    def build_report(self, run_id: str, event_limit: int = 300) -> Dict[str, Any]:
        run: RunState = self.state_machine.get_by_run_id(run_id)
        if not run:
            raise ValueError(f"run_id 不存在: {run_id}")

        events: List[RunEvent] = self.state_machine.list_events(run_id, limit=event_limit)
        events = list(reversed(events))
        resume_count = sum(1 for e in events if e.event == "run_resumed")
        failure_points = []
        for e in events:
            if e.event not in ("run_failed", "run_suspended"):
                continue
            screenshot_path = None
            if e.detail:
                m = re.search(r"\[screenshot\]:\s*(.+)", e.detail)
                if m:
                    screenshot_path = m.group(1).strip()
            failure_points.append(
                {
                    "step_index": e.step_index,
                    "step_id": e.step_id,
                    "event": e.event,
                    "detail": e.detail,
                    "screenshot": screenshot_path,
                    "created_at": e.created_at,
                }
            )

        completed_steps = min(run.current_step_index, run.total_steps)
        success_rate = round((completed_steps / run.total_steps) * 100, 2) if run.total_steps else 0.0
        report = {
            "run_id": run.run_id,
            "task_name": run.task_name,
            "trace_path": run.trace_path,
            "status": run.status,
            "total_steps": run.total_steps,
            "completed_steps": completed_steps,
            "success_rate": success_rate,
            "resume_count": resume_count,
            "last_error": run.last_error,
            "failure_points": failure_points,
            "timeline": [
                {
                    "id": e.id,
                    "event": e.event,
                    "step_index": e.step_index,
                    "step_id": e.step_id,
                    "detail": e.detail,
                    "created_at": e.created_at,
                }
                for e in events
            ],
            "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        return report

    def save_report(self, run_id: str) -> Dict[str, str]:
        report = self.build_report(run_id)
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"run_{run_id}_{report['status']}_{now}"
        json_path = os.path.join(self.report_dir, f"{base}.json")
        md_path = os.path.join(self.report_dir, f"{base}.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(self._to_markdown(report))

        return {"json_path": json_path, "md_path": md_path}

    @staticmethod
    def _to_markdown(report: Dict[str, Any]) -> str:
        lines = [
            f"# Run Report: {report['run_id']}",
            "",
            "## Summary",
            f"- Task: {report['task_name']}",
            f"- Status: {report['status']}",
            f"- Trace: {report['trace_path']}",
            f"- Success Rate: {report['success_rate']}%",
            f"- Steps: {report['completed_steps']}/{report['total_steps']}",
            f"- Resume Count: {report['resume_count']}",
            f"- Generated At (UTC): {report['generated_at']}",
        ]

        if report.get("last_error"):
            lines.extend(["", "## Last Error", "", f"```text\n{report['last_error']}\n```"])

        lines.extend(["", "## Failure Points"])
        if report["failure_points"]:
            for fp in report["failure_points"]:
                lines.append(
                    f"- [{fp['created_at']}] {fp['event']} step_index={fp['step_index']} step_id={fp['step_id']}"
                )
                if fp.get("detail"):
                    lines.append(f"  - detail: {fp['detail']}")
                if fp.get("screenshot"):
                    lines.append(f"  - screenshot: {fp['screenshot']}")
        else:
            lines.append("- None")

        lines.extend(["", "## Timeline (Recent)"])
        for e in report["timeline"][-20:]:
            lines.append(
                f"- [{e['created_at']}] {e['event']} step_index={e['step_index']} step_id={e['step_id']}"
            )

        lines.append("")
        return "\n".join(lines)

