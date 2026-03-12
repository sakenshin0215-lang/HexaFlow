from __future__ import annotations

from typing import Optional

from hexaflow.agents.schemas import ReplayRepairDecision
from hexaflow.agents.skills import default_skill_agents, get_skill_specs


class SpecialAgentRouter:
    """Route repair requests to registered skills only when relevant."""

    def __init__(self, agents: Optional[list] = None):
        self.agents = list(agents or [])

    @classmethod
    def default(
        cls,
        enabled_skill_ids: tuple[str, ...] = ("popup",),
    ) -> "SpecialAgentRouter":
        return cls(agents=default_skill_agents(enabled_skill_ids=enabled_skill_ids))

    @staticmethod
    def describe_registered_skills() -> list[dict]:
        """Expose skill metadata for logging/docs/UI without loading files dynamically."""
        return [
            {
                "skill_id": s.skill_id,
                "name": s.name,
                "description": s.description,
                "tools": list(s.tools),
                "trigger_signals": list(s.trigger_signals),
            }
            for s in get_skill_specs()
        ]

    def route_repair(
        self,
        *,
        last_error: str,
        dom_snapshot: str,
        failed_target: str,
    ) -> Optional[ReplayRepairDecision]:
        for agent in self.agents:
            try:
                if not agent.should_handle(last_error=last_error, dom_snapshot=dom_snapshot):
                    continue
                decision = agent.propose_repair(
                    failed_target=failed_target,
                    dom_snapshot=dom_snapshot,
                )
                if decision:
                    return decision
            except Exception:
                continue
        return None
