from typing import List, Optional

from pydantic import BaseModel, Field


class NextAction(BaseModel):
    thought: str = Field(
        default="",
        description="Step-by-step reasoning: What is the goal? What is on the screen right now? What should I do next?",
    )
    action_type: str = Field(
        description="MUST be one of: [navigate, click, type, click_type_enter, press_enter, refresh, summarize, done]"
    )
    target: Optional[str] = Field(
        None,
        description='If action is "navigate", put URL here. If click/type/click_type_enter, put selector, e.g., \'[hexa-id="hexa-5"]\'',
    )
    input_value: Optional[str] = Field(
        None, description='Text to input if action_type is "type" or "click_type_enter".'
    )


class AnalyzedAction(BaseModel):
    thought: str = Field(description="Analyze why the user clicked this element to achieve the goal.")
    description: str = Field(
        description="A concise description of the step, e.g., '点击登录按钮' or '点击搜索框'"
    )


class ReplayRepairDecision(BaseModel):
    thought: str = Field(description="Why the previous step failed and what fix is most reliable now.")
    strategy: str = Field(
        description=(
            "MUST be one of: [retry_with_new_selector, replace_action, skip_step, human_handoff, no_fix]"
        )
    )
    action_type: Optional[str] = Field(
        None,
        description="Used when strategy=replace_action. One of [navigate, click, type, click_type_enter, press_enter, refresh, summarize, wait_for_timeout, ensure_quote_token, click_relative].",
    )
    target: Optional[str] = Field(
        None, description="New selector/URL when strategy=retry_with_new_selector or replace_action."
    )
    input_value: Optional[str] = Field(None, description="Input value when action_type='type'.")
    skip_reason: Optional[str] = Field(None, description="Reason when strategy=skip_step.")
    confidence: float = Field(default=0.5, ge=0, le=1)


class ClickCoordinateDecision(BaseModel):
    thought: str = Field(default="")
    use_coordinate: bool = Field(default=False)
    x_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    y_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    candidate_index: Optional[int] = Field(default=None, ge=0)
    confidence: float = Field(default=0.5, ge=0, le=1)


class PreCheck(BaseModel):
    expected_url_contains: Optional[str] = Field(None, description="URL assertion")
    expected_dom_selector: str = Field(description="Stable CSS/XPath selector")
    timeout_ms: int = Field(default=5000)


class PostCheck(BaseModel):
    expected_dom_selector: Optional[str] = Field(
        default=None, description="Selector that must be visible after action"
    )
    absent_dom_selector: Optional[str] = Field(
        default=None, description="Selector that must be absent/hidden after action"
    )
    class_target_selector: Optional[str] = Field(
        default=None,
        description="Optional selector scope for class pattern checks (defaults to action.target).",
    )
    expected_class_pattern: Optional[str] = Field(
        default=None,
        description="ClassName pattern that must match after action. Supports wildcard ***.",
    )
    absent_class_pattern: Optional[str] = Field(
        default=None,
        description="ClassName pattern that must NOT match after action. Supports wildcard ***.",
    )
    timeout_ms: int = Field(default=3000)


class ElementFingerprint(BaseModel):
    tag_name: Optional[str] = Field(None)
    text: Optional[str] = Field(None)
    aria_label: Optional[str] = Field(None)
    placeholder: Optional[str] = Field(None)
    classes: Optional[str] = Field(None)


class StepAction(BaseModel):
    action_type: str = Field(
        description="[navigate, click, type, click_type_enter, press_enter, refresh, summarize, wait_for_timeout, scroll, ensure_quote_token]"
    )
    target: Optional[str] = Field(None, description="Stable target locator")
    input_value: Optional[str] = Field(None)
    fingerprint: Optional[ElementFingerprint] = Field(
        None, description="元素的多维特征指纹，用于回放时模糊定位"
    )


class FlowStep(BaseModel):
    step_id: str = Field(description="Unique step ID")
    description: str = Field(description="Step description")
    pre_check: PreCheck
    action: StepAction
    post_check: Optional[PostCheck] = Field(
        default=None,
        description="Optional post-action assertions. Useful for tab active class/state checks.",
    )
    is_optional: bool = Field(default=False, description="如果为 True，回放时找不到元素将静默跳过")
    guard_url_contains: Optional[str] = Field(
        default=None,
        description="Step page guard. If current URL does not contain this value, recovery actions run first.",
    )
    on_mismatch_actions: List[StepAction] = Field(
        default_factory=list,
        description="Recovery actions executed before this step when page guard fails.",
    )
    guard_retry_limit: int = Field(
        default=2,
        description="How many times to try page-guard recovery before failing this step.",
    )
    loop_marker: Optional[str] = Field(
        default=None,
        description="Loop marker: one of [start, end], used by loop replay mode.",
    )
    loop_name: Optional[str] = Field(
        default=None,
        description="Logical loop name when loop_marker is set.",
    )


class WorkflowBlueprint(BaseModel):
    task_name: str = Field(description="Task name")
    steps: List[FlowStep] = Field(description="List of execution steps")
