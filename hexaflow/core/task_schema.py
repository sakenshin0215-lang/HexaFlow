import json
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class FailurePolicy(BaseModel):
    mode: Literal["continue", "stop"] = Field(
        default="continue",
        description="continue: keep trying after failures; stop: stop run after threshold",
    )
    max_consecutive_failures: int = Field(default=3, ge=1, le=50)


class LoopPolicy(BaseModel):
    enabled: bool = Field(default=False)
    loop_name: str = Field(default="main_loop")
    iterations: int = Field(default=1, ge=1, le=10000)
    max_iteration_retries: int = Field(default=30, ge=1, le=100000)


class RiskyActionsPolicy(BaseModel):
    enabled: bool = Field(default=False, description="Enable risky action gate.")
    mode: Literal["block", "require_confirm"] = Field(
        default="require_confirm",
        description="block: always stop when risky detected; require_confirm: ask human to approve/deny.",
    )
    keywords: List[str] = Field(
        default_factory=lambda: [
            "确认",
            "提交",
            "购买",
            "卖出",
            "转账",
            "提现",
            "发送",
            "授权",
            "approve",
            "sign",
            "signature",
            "swap",
            "pay",
            "checkout",
            "place order",
            "connect wallet",
        ],
        description="If action text matches these keywords, treat as risky.",
    )
    allowed_domains: List[str] = Field(
        default_factory=list,
        description="Optional allowlist. If set, risky actions are only allowed on these domains/subdomains.",
    )
    allowed_url_contains: List[str] = Field(
        default_factory=list,
        description="Optional allowlist. If set, risky actions are only allowed when current URL contains one of these strings.",
    )
    max_confirm_wait_seconds: int = Field(
        default=600, ge=10, le=86400, description="Max wait for human confirmation when mode=require_confirm."
    )


class SuccessCriterion(BaseModel):
    type: Literal["url_contains", "text_present", "dom_selector_present"] = Field(
        description="How to determine success."
    )
    value: str = Field(description="Substring/text/selector value depending on type.")
    case_insensitive: bool = Field(default=True)


class SuccessCriteria(BaseModel):
    mode: Literal["any", "all"] = Field(
        default="any", description="any: any criterion satisfied; all: all must be satisfied."
    )
    criteria: List[SuccessCriterion] = Field(default_factory=list)
    check_every_step: bool = Field(
        default=True,
        description="If true, auto-check completion before/after each step in dynamic mode.",
    )
    max_wait_ms: int = Field(
        default=0,
        ge=0,
        le=300000,
        description="Optional wait time budget when checking criteria (0 means no extra waiting).",
    )


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
    loop_policy: LoopPolicy = Field(default_factory=LoopPolicy)
    risky_actions_policy: RiskyActionsPolicy = Field(default_factory=RiskyActionsPolicy)
    success_criteria: SuccessCriteria = Field(default_factory=SuccessCriteria)

    @classmethod
    def from_json_file(cls, path: str) -> "TaskSpec":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.model_validate(data)
