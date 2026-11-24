"""Per-agent plan store for the code agent.

A plan belongs to one conversation/agent instance. The store used to be a
process-wide singleton with a mutable "current" key: two code agents created in
the same process could see and overwrite each other's task list, and callers
had to remember to call ``set_current`` before every use. Now each agent owns
its own ``PlanStore`` instance and the key is fixed when the store is injected.

There is deliberately no global instance and no ``get_plan_store()``: sharing
state implicitly is what caused the cross-conversation leak in the first place.
"""
from typing import Any, Dict, List, Optional


class PlanStore:
    """Holds one or more named plans. Inject one instance per agent."""

    def __init__(self) -> None:
        self._plans: Dict[str, List[Dict[str, Any]]] = {}

    @staticmethod
    def _key(key: Optional[str]) -> str:
        return key or "default"

    def _steps(self, key: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._plans.setdefault(self._key(key), [])

    def update(
        self,
        steps: List[Dict[str, Any]],
        explanation: str = None,
        key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(steps, list) or not steps:
            return {"success": False, "error": "Plan must contain at least one step."}
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

        self._plans[self._key(key)] = steps
        return {"success": True, "steps": steps, "explanation": explanation}

    def get_plan(self, key: Optional[str] = None) -> List[Dict[str, Any]]:
        return list(self._steps(key))

    def clear(self, key: Optional[str] = None) -> None:
        self._plans[self._key(key)] = []

    def describe_current(self, key: Optional[str] = None) -> str:
        """Compact plan state for the model's live system reminder."""
        steps = self.get_plan(key)
        if not steps:
            return ""
        lines = ["[System reminder — current task plan]"]
        for step in steps:
            mark = {
                "completed": "x",
                "in_progress": "~",
                "pending": " ",
            }.get(str(step.get("status")), " ")
            lines.append(f"  [{mark}] {step.get('text', '')}")
        lines.append("Keep working until every step is completed; update the plan as you go.")
        lines.append("Do not end the turn with a promise — finish the action in this reply.")
        return "\n".join(lines)
