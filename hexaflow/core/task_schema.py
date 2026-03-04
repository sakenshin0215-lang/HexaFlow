import json
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class FailurePolicy(BaseModel):
    mode: Literal["continue", "stop"] = Field(
        default="continue",
        description="continue: keep trying after failures; stop: stop run after threshold",
    )
    max_consecutive_failures: int = Field(default=3, ge=1, le=50)


class TaskSpec(BaseModel):
    task_name: str = Field(default="DynamicTask")
    goal: str = Field(description="Primary business goal")
    notes: List[str] = Field(default_factory=list, description="Execution notes/constraints")
    start_url: str = Field(default="about:blank")
    manual_review: bool = Field(default=False)
    max_steps: int = Field(default=15, ge=1, le=200)
    allowed_domains: List[str] = Field(
        default_factory=list,
        description="If set, navigation is only allowed to these domains (or subdomains).",
    )
    blocked_keywords: List[str] = Field(
        default_factory=list,
        description="Action text containing these keywords will be blocked.",
    )
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)

    @classmethod
    def from_json_file(cls, path: str) -> "TaskSpec":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.model_validate(data)

