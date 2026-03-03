# hexaflow/agents/planner.py
from typing import List, Optional
from pydantic import BaseModel, Field

class PreCheck(BaseModel):
    expected_url_contains: Optional[str] = Field(None, description="URL assertion")
    expected_dom_selector: str = Field(description="Stable CSS/XPath selector")
    timeout_ms: int = Field(default=5000)

class StepAction(BaseModel):
    action_type: str = Field(description="[navigate, click, type, wait_for_timeout, scroll]")
    target: Optional[str] = Field(None, description="Stable target locator")
    input_value: Optional[str] = Field(None)

class FlowStep(BaseModel):
    step_id: str = Field(description="Unique step ID")
    description: str = Field(description="Step description")
    pre_check: PreCheck
    action: StepAction
    is_optional: bool = Field(default=False, description="如果为 True，回放时找不到元素将静默跳过")

class WorkflowBlueprint(BaseModel):
    task_name: str = Field(description="Task name")
    steps: List[FlowStep] = Field(description="List of execution steps")