"""
Task status tool — allows LLM to query background task status.
"""
import time
from .registry import Tool, ToolResult


class TaskStatusTool(Tool):
    name = "task_status"
    description = "Check background task status."
    category = "task"
    cost = "low"
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["get", "list", "cancel"]},
        },
        "required": [],
    }

    def execute(self, args: dict) -> ToolResult:
        start = time.time()
        tm = self._get_manager()

        action = args.get("action", "get")
        task_id = args.get("task_id")

        try:
            if action == "cancel" and task_id:
                success = tm.cancel_task(task_id)
                return ToolResult(
                    status="success",
                    data={"task_id": task_id, "cancelled": success},
                    metadata={"execution_time": time.time() - start}
                )

            if action == "list" or not task_id:
                tasks = tm.list_tasks()
                task_list = [t.to_dict() for t in tasks[:10]]
                running = tm.get_active_count()
                return ToolResult(
                    status="success",
                    data={"tasks": task_list, "active_count": running, "total": len(task_list)},
                    metadata={"execution_time": time.time() - start}
                )

            task = tm.get_task(task_id)
            if not task:
                return ToolResult(
                    status="error",
                    error=f"Task '{task_id}' not found",
                    metadata={"execution_time": time.time() - start}
                )

            return ToolResult(
                status="success",
                data=task.to_dict(),
                metadata={"execution_time": time.time() - start}
            )

        except Exception as e:
            return ToolResult(
                status="error",
                error=str(e),
                metadata={"execution_time": time.time() - start}
            )


    @staticmethod
    def _get_manager():
        from shared.tasks import TaskManager
        return TaskManager.get()