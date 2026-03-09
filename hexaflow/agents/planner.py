# hexaflow/agents/planner.py
from typing import List, Optional
from pydantic import BaseModel, Field

class PreCheck(BaseModel):
    expected_url_contains: Optional[str] = Field(None, description="URL assertion")
    expected_dom_selector: str = Field(description="Stable CSS/XPath selector")
    timeout_ms: int = Field(default=5000)

class ElementFingerprint(BaseModel):
    tag_name: Optional[str] = Field(None)
    text: Optional[str] = Field(None)
    aria_label: Optional[str] = Field(None)
    placeholder: Optional[str] = Field(None)
    classes: Optional[str] = Field(None)

class StepAction(BaseModel):
    action_type: str = Field(description="[navigate, click, type, click_type_enter, press_enter, refresh, wait_for_timeout, scroll, ensure_quote_token]")
    target: Optional[str] = Field(None, description="Stable target locator")
    input_value: Optional[str] = Field(None)
    fingerprint: Optional[ElementFingerprint] = Field(None, description="元素的多维特征指纹，用于回放时模糊定位")

class FlowStep(BaseModel):
    step_id: str = Field(description="Unique step ID")
    description: str = Field(description="Step description")
    pre_check: PreCheck
    action: StepAction
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
