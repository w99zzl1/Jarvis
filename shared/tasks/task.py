import uuid
import time
import threading
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    DOWNLOAD = "download"
    INSTALL = "install"
    SCAN = "scan"
    COPY = "copy"
    EXTRACT = "extract"
    CUSTOM = "custom"


@dataclass
class Task:
    id: str
    type: TaskType
    status: TaskStatus
    description: str
    command: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: int = 0
    eta: str = ""
    speed: str = ""
    result: str = ""
    error: str = ""
    pid: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d['status'] = self.status.value if isinstance(self.status, TaskStatus) else self.status
        d['type'] = self.type.value if isinstance(self.type, TaskType) else self.type
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'Task':
        d = d.copy()
        if 'status' in d and isinstance(d['status'], str):
            d['status'] = TaskStatus(d['status'])
        if 'type' in d and isinstance(d['type'], str):
            d['type'] = TaskType(d['type'])
        return cls(**d)

    def update(self, progress: int = None, eta: str = None, speed: str = None,
               status: TaskStatus = None, result: str = None, error: str = None):
        self.updated_at = time.time()
        if progress is not None:
            self.progress = progress
        if eta is not None:
            self.eta = eta
        if speed is not None:
            self.speed = speed
        if status is not None:
            self.status = status
        if result is not None:
            self.result = result
        if error is not None:
            self.error = error

    def __repr__(self) -> str:
        return f"<Task {self.id[:8]} [{self.status.value}] {self.progress}%>"


def create_task_id() -> str:
    return str(uuid.uuid4())


def new_task(task_type: TaskType, command: str, description: str = "", pid: int = 0) -> Task:
    return Task(
        id=create_task_id(),
        type=task_type,
        status=TaskStatus.PENDING,
        command=command,
        description=description or command[:80],
    )