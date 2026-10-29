from typing import Dict, Any
from ..base import Tool
from ...plan_store import get_plan_store


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

    def __init__(self, project_path: str = None):
        pass

    def execute(self, steps: list, explanation: str = None) -> Dict[str, Any]:
        return get_plan_store().update(steps, explanation)
