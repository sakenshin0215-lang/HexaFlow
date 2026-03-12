from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hexaflow.agents.skills.popup import PopupSkillAgent


@dataclass(frozen=True)
class AgentSkillSpec:
    skill_id: str
    name: str
    description: str
    tools: tuple[str, ...]
    trigger_signals: tuple[str, ...]
    factory: Callable[[], object]


_SKILL_SPECS: tuple[AgentSkillSpec, ...] = (
    AgentSkillSpec(
        skill_id="popup",
        name="Popup Skill",
        description="Detect and handle blocking popups/modals before core action retry.",
        tools=("popup_healer", "layered_modal_close"),
        trigger_signals=("intercepted", "overlay", "modal", "popup", "mask", "not visible"),
        factory=PopupSkillAgent,
    ),
)


def get_skill_specs() -> tuple[AgentSkillSpec, ...]:
    """Return all registered skill specs (metadata only)."""
    return _SKILL_SPECS


def default_skill_agents(enabled_skill_ids: tuple[str, ...] = ("popup",)) -> list[object]:
    """
    Build default runtime skill agent instances from registry.
    No filesystem scanning; deterministic by registry.
    """
    enabled = set(enabled_skill_ids)
    agents: list[object] = []
    for spec in _SKILL_SPECS:
        if spec.skill_id not in enabled:
            continue
        agents.append(spec.factory())
    return agents

