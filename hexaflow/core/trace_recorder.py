import os
import json
import logging
from datetime import datetime
from pydantic import BaseModel
# 借用我们之前在 planner.py 里定义的严谨结构
from hexaflow.agents.planner import WorkflowBlueprint, FlowStep, PreCheck, StepAction

logger = logging.getLogger("TraceRecorder")

class TraceRecorder:
    def __init__(self, task_name: str, workspace_dir: str = "memory/workspace/traces"):
        self.task_name = task_name
        self.workspace_dir = workspace_dir
        self.steps = []
        self.step_counter = 1
        
        os.makedirs(self.workspace_dir, exist_ok=True)

    def record_step(self, current_url: str, action_type: str, target: str, input_value: str = None, description: str = "", is_optional: bool = False):
        """
        录制一个成功的步骤
        """
        url_core = current_url.split("?")[0].replace("https://", "").replace("http://", "")
        step_id = f"step_{self.step_counter}_{action_type}"
        
        if action_type == "navigate":
            dom_selector = "body"
        else:
            dom_selector = target if target else "body"
            
        # 组装前置校验 (PreCheck)
        pre_check = PreCheck(
            expected_url_contains=url_core,
            expected_dom_selector=dom_selector,
            timeout_ms=5000
        )
        
        # 组装动作 (StepAction)
        step_action = StepAction(
            action_type=action_type,
            target=target,
            input_value=input_value
        )
        
        # 组装完整节点
        flow_step = FlowStep(
            step_id=step_id,
            description=description or f"执行 {action_type} 操作",
            pre_check=pre_check,
            action=step_action,
            is_optional=is_optional
        )
        
        self.steps.append(flow_step)
        logger.info(f"📼 已录制轨迹节点: {step_id}")
        self.step_counter += 1

    def save_to_disk(self) -> str:
        """
        将轨迹写入 JSON 文件
        """
        blueprint = WorkflowBlueprint(
            task_name=self.task_name,
            steps=[step.model_dump() for step in self.steps] 
        )
        
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.task_name}_{timestamp}.json"
        filepath = os.path.join(self.workspace_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            # model_dump_json 是 Pydantic 提供的序列化方法
            f.write(blueprint.model_dump_json(indent=2))
            
        logger.info(f"💾 黄金轨迹已永久保存至: {filepath}")
        return filepath