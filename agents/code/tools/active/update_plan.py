from typing import Dict, Any
from ..base import Tool
from ...plan_store import PlanStore


class UpdatePlanTool(Tool):
    name = "update_plan"
    description = (
        "Updates the step-by-step task plan. Each step: id (string), "
        "text (up to 7 words), status (pending | in_progress | completed). "
        "Rule: exactly ONE step can be in_progress at a time. When a step is done — "
        "mark it completed and the next one in_progress. When all done — "
        "all steps completed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "List of plan steps",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Unique step id"},
                        "text": {"type": "string", "description": "Step text, up to 7 words"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["id", "text", "status"],
                },
            },
            "explanation": {
                "type": "string",
                "description": "Brief explanation of why the plan was changed (optional)",
            },
        },
        "required": ["steps"],
    }

    def __init__(self, project_path: str = None, plan_key: str = None,
                 store: PlanStore | None = None):
        # The store is injected by the factory so the tool and the engine share
        # exactly one plan instance for this agent. A standalone instance gets
        # its own private store; no process-wide state is involved.
        self.plan_key = plan_key
        self.store = store if store is not None else PlanStore()

    def execute(self, steps: list, explanation: str = None) -> Dict[str, Any]:
        return self.store.update(steps, explanation, key=self.plan_key)
