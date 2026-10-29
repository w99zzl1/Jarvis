"""
Execution Plan and Event Models for Dynamic Agent Planning.

This module defines the core data structures for the planning and execution system:
- Task: individual unit of work with id, title, description, status
- ExecutionPlan: ordered list of tasks with metadata
- ExecutionEvent: structured events for streaming to frontend
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EventType(str, Enum):
    PLAN_CREATED = "plan_created"
    PLAN_UPDATED = "plan_updated"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    EXECUTION_ERROR = "execution_error"
    EXECUTION_COMPLETED = "execution_completed"
    AGENT_MESSAGE = "agent_message"


class Task(BaseModel):
    """Individual task in the execution plan."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    tool_calls: list[str] = Field(default_factory=list)  # Tool names used in this task
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def to_dict(self) -> dict:
        d = self.model_dump()
        d["status"] = self.status.value
        if self.started_at:
            d["started_at"] = self.started_at.isoformat()
        if self.completed_at:
            d["completed_at"] = self.completed_at.isoformat()
        return d


class ExecutionPlan(BaseModel):
    """Complete execution plan with ordered tasks."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    user_request: str
    tasks: list[Task] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    current_task_index: int = 0

    def to_dict(self) -> dict:
        d = self.model_dump()
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        d["tasks"] = [t.to_dict() for t in self.tasks]
        return d

    @property
    def current_task(self) -> Task | None:
        if 0 <= self.current_task_index < len(self.tasks):
            return self.tasks[self.current_task_index]
        return None

    @property
    def completed_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED)

    @property
    def total_count(self) -> int:
        return len(self.tasks)

    @property
    def is_complete(self) -> bool:
        return all(t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED) for t in self.tasks)

    @property
    def progress_text(self) -> str:
        return f"{self.completed_count}/{self.total_count} tasks Completed"

    def add_task(self, title: str, description: str = "") -> Task:
        task = Task(title=title, description=description)
        self.tasks.append(task)
        self.updated_at = datetime.now()
        return task

    def insert_task(self, index: int, title: str, description: str = "") -> Task:
        task = Task(title=title, description=description)
        self.tasks.insert(index, task)
        self.updated_at = datetime.now()
        if index <= self.current_task_index:
            self.current_task_index += 1
        return task

    def update_task_status(self, task_id: str, status: TaskStatus, error: str | None = None) -> bool:
        for i, task in enumerate(self.tasks):
            if task.id == task_id:
                task.status = status
                if status == TaskStatus.IN_PROGRESS:
                    task.started_at = datetime.now()
                elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    task.completed_at = datetime.now()
                if error:
                    task.error = error
                self.updated_at = datetime.now()
                # Advance to next pending task if current completed
                if status == TaskStatus.COMPLETED and i == self.current_task_index:
                    self._advance_to_next_pending()
                return True
        return False

    def _advance_to_next_pending(self):
        while self.current_task_index < len(self.tasks):
            if self.tasks[self.current_task_index].status == TaskStatus.PENDING:
                break
            self.current_task_index += 1

    def record_tool_call(self, task_id: str, tool_name: str) -> bool:
        for task in self.tasks:
            if task.id == task_id:
                if tool_name not in task.tool_calls:
                    task.tool_calls.append(tool_name)
                self.updated_at = datetime.now()
                return True
        return False


class ExecutionEvent(BaseModel):
    """Structured event for streaming to frontend."""
    type: EventType
    timestamp: datetime = Field(default_factory=datetime.now)
    plan: ExecutionPlan | None = None
    task_id: str | None = None
    task: Task | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: dict | None = None
    message: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = self.model_dump()
        d["type"] = self.type.value
        d["timestamp"] = self.timestamp.isoformat()
        if self.plan:
            d["plan"] = self.plan.to_dict()
        if self.task:
            d["task"] = self.task.to_dict()
        return d

    @classmethod
    def plan_created(cls, plan: ExecutionPlan) -> ExecutionEvent:
        return cls(type=EventType.PLAN_CREATED, plan=plan)

    @classmethod
    def plan_updated(cls, plan: ExecutionPlan) -> ExecutionEvent:
        return cls(type=EventType.PLAN_UPDATED, plan=plan)

    @classmethod
    def task_started(cls, plan: ExecutionPlan, task: Task) -> ExecutionEvent:
        return cls(type=EventType.TASK_STARTED, plan=plan, task=task, task_id=task.id)

    @classmethod
    def task_completed(cls, plan: ExecutionPlan, task: Task) -> ExecutionEvent:
        return cls(type=EventType.TASK_COMPLETED, plan=plan, task=task, task_id=task.id)

    @classmethod
    def task_failed(cls, plan: ExecutionPlan, task: Task, error: str) -> ExecutionEvent:
        return cls(type=EventType.TASK_FAILED, plan=plan, task=task, task_id=task.id, error=error)

    @classmethod
    def tool_started(cls, plan: ExecutionPlan, task_id: str, tool_name: str, tool_args: dict) -> ExecutionEvent:
        return cls(type=EventType.TOOL_STARTED, plan=plan, task_id=task_id, tool_name=tool_name, tool_args=tool_args)

    @classmethod
    def tool_completed(cls, plan: ExecutionPlan, task_id: str, tool_name: str, tool_result: dict) -> ExecutionEvent:
        return cls(type=EventType.TOOL_COMPLETED, plan=plan, task_id=task_id, tool_name=tool_name, tool_result=tool_result)

    @classmethod
    def execution_error(cls, plan: ExecutionPlan | None, error: str, task_id: str | None = None) -> ExecutionEvent:
        return cls(type=EventType.EXECUTION_ERROR, plan=plan, error=error, task_id=task_id)

    @classmethod
    def execution_completed(cls, plan: ExecutionPlan, final_message: str) -> ExecutionEvent:
        return cls(type=EventType.EXECUTION_COMPLETED, plan=plan, message=final_message)

    @classmethod
    def agent_message(cls, message: str) -> ExecutionEvent:
        return cls(type=EventType.AGENT_MESSAGE, message=message)


# Structured Output Schema for LLM Plan Generation
class PlanTaskSchema(BaseModel):
    """Schema for a single task in LLM-generated plan."""
    title: str = Field(..., description="Short descriptive title of the task")
    description: str = Field(default="", description="Detailed description of what this task entails")


class ExecutionPlanSchema(BaseModel):
    """Schema for LLM to generate structured execution plan."""
    tasks: list[PlanTaskSchema] = Field(..., min_items=1, description="Ordered list of tasks to execute")
    reasoning: str = Field(default="", description="Brief reasoning for this plan structure")


# JSON Schema for Structured Output (for providers that support it)
EXECUTION_PLAN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["title", "description"],
                "additionalProperties": False
            },
            "minItems": 1
        },
        "reasoning": {"type": "string"}
    },
    "required": ["tasks", "reasoning"],
    "additionalProperties": False
}