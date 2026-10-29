"""Plan store for Code Agent - manages task plans."""
from typing import Dict, Any, List, Optional


class PlanStore:
    _instance = None
    _steps: List[Dict[str, Any]] = []

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def update(self, steps: List[Dict[str, Any]], explanation: str = None) -> Dict[str, Any]:
        if not isinstance(steps, list) or not steps:
            return {
                "success": False,
                "error": "Plan must contain at least one step.",
            }
        valid_statuses = {"pending", "in_progress", "completed"}
        in_progress_count = 0
        pending_indexes = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                return {"success": False, "error": f"Step {index + 1} must be an object."}
            step_id = str(step.get("id", "")).strip()
            text = str(step.get("text", "")).strip()
            status = str(step.get("status", "")).strip()
            if not step_id:
                return {"success": False, "error": f"Step {index + 1} is missing id."}
            if not text:
                return {"success": False, "error": f"Step {step_id} is missing text."}
            if status not in valid_statuses:
                return {"success": False, "error": f"Step {step_id} has invalid status: {status}"}
            if status == "in_progress":
                in_progress_count += 1
            elif status == "pending":
                pending_indexes.append(index)

        if in_progress_count > 1:
            return {
                "success": False,
                "error": f"At most one step can be in_progress, found {in_progress_count}",
            }

        # A plan with nothing in_progress is normal: it was just created, or
        # everything finished. Normalize instead of rejecting — a model that
        # marked the last step completed must be able to close the plan out.
        if in_progress_count == 0 and pending_indexes:
            steps = [dict(s) for s in steps]
            steps[pending_indexes[0]]["status"] = "in_progress"

        self._steps = steps
        return {
            "success": True,
            "steps": self._steps,
            "explanation": explanation,
        }

    def get_plan(self) -> List[Dict[str, Any]]:
        return self._steps

    def clear(self):
        self._steps = []


_plan_store = PlanStore()


def get_plan_store() -> PlanStore:
    return _plan_store
