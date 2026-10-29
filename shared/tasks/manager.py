"""
TaskManager — управляет фоновыми задачами.
"""
import json
import time
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from typing import Optional, Callable, Dict, List
from queue import Queue

from shared import paths

from .task import Task, TaskStatus, TaskType, new_task
from .progress_parser import parse_line


# Global event queue for SSE
_event_queue: Queue = Queue()
_max_workers = 3  # max parallel tasks on i7-8550U


class TaskManager:
    """
    Singleton. Управляет жизненным циклом задач.
    
    Usage:
        tm = TaskManager.get()
        task = tm.create_task(TaskType.DOWNLOAD, command, "Скачать Blender")
        # or
        tm.run_in_background(proc, command, TaskType.INSTALL)
    """
    _instance: Optional['TaskManager'] = None
    _lock = threading.Lock()

    def __init__(self):
        self._tasks: Dict[str, Task] = {}
        self._futures: Dict[str, Future] = {}
        self._executor = ThreadPoolExecutor(max_workers=_max_workers)
        self._persist_path = paths.tasks_history_file()
        self._persist_lock = threading.Lock()
        self._sse_clients: List[Queue] = []
        self._sse_lock = threading.Lock()
        self._load()
        print(f"[TaskManager] Initialized with {len(self._tasks)} saved tasks")

    @classmethod
    def get(cls) -> 'TaskManager':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls.__new__(cls)
                    cls._instance.__init__()
        return cls._instance

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self):
        path = self._persist_path
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for d in data:
                        task = Task.from_dict(d)
                        self._tasks[task.id] = task
            except Exception as e:
                print(f"[TaskManager] Failed to load tasks: {e}")

    def _save(self):
        with self._persist_lock:
            path = self._persist_path
            try:
                tasks_data = [t.to_dict() for t in self._tasks.values()]
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(tasks_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[TaskManager] Failed to save tasks: {e}")

    # ── Task CRUD ──────────────────────────────────────────────────────────────

    def create_task(self, task_type: TaskType, command: str,
                    description: str = "", pid: int = 0) -> Task:
        """Создать задачу (не запускает)."""
        task = new_task(task_type, command, description, pid)
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self, status: TaskStatus = None) -> List[Task]:
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        
        # Try to kill process if we have PID
        if task.pid:
            try:
                subprocess.run(['taskkill', '/F', '/PID', str(task.pid)],
                             capture_output=True, timeout=5)
            except:
                pass
        
        task.update(status=TaskStatus.CANCELLED)
        self._save()
        self._broadcast({"event": "task_cancelled", "task_id": task_id})
        return True

    # ── Background execution ───────────────────────────────────────────────────

    def run_in_background(self, proc: subprocess.Popen,
                          command: str,
                          task_type: TaskType = TaskType.CUSTOM) -> Task:
        """
        Запустить процесс в фоне и отслеживать прогресс.
        Возвращает Task с task_id сразу (процесс выполняется в потоке).
        """
        task = self.create_task(task_type, command, pid=proc.pid)
        task.update(status=TaskStatus.RUNNING)
        self._broadcast({"event": "task_started", **task.to_dict()})

        future = self._executor.submit(
            self._monitor_process,
            task,
            proc
        )
        self._futures[task.id] = future
        future.add_done_callback(lambda f: self._on_task_done(task.id))
        
        return task

    def _monitor_process(self, task: Task, proc: subprocess.Popen):
        """Мониторит процесс в фоновом потоке."""
        last_progress_update = 0
        raw_output_lines = []
        
        try:
            # Read stdout line by line
            for line in iter(proc.stdout.readline, ''):
                if not line:
                    break
                
                line = line.rstrip('\r\n')
                raw_output_lines.append(line)
                
                # Parse progress
                progress_info = parse_line(line)
                if progress_info:
                    now = time.time()
                    # Rate limit updates to ~2 per second
                    if now - last_progress_update > 0.5 or progress_info.progress == 100:
                        task.update(
                            progress=progress_info.progress,
                            eta=progress_info.eta,
                            speed=progress_info.speed,
                        )
                        last_progress_update = now
                        self._broadcast({
                            "event": "task_progress",
                            "task_id": task.id,
                            "progress": task.progress,
                            "eta": task.eta,
                            "speed": task.speed,
                        })
                
                # Check if process finished
                if proc.poll() is not None:
                    break
            
            # Wait for final output
            stdout, stderr = proc.communicate(timeout=5)
            if stdout:
                raw_output_lines.append(stdout)
            if stderr:
                raw_output_lines.append(stderr)
            
            # Determine final status
            return_code = proc.returncode
            full_output = '\n'.join(raw_output_lines)
            
            if return_code == 0:
                task.update(
                    status=TaskStatus.COMPLETED,
                    progress=100,
                    result=full_output[:2000],  # Limit result size
                )
                self._broadcast({
                    "event": "task_completed",
                    "task_id": task.id,
                    "result": task.result[:500],
                })
            else:
                task.update(
                    status=TaskStatus.FAILED,
                    error=f"Exit code {return_code}: {full_output[:500]}",
                )
                self._broadcast({
                    "event": "task_failed",
                    "task_id": task.id,
                    "error": task.error,
                })
                
        except Exception as e:
            task.update(status=TaskStatus.FAILED, error=str(e))
            self._broadcast({
                "event": "task_failed",
                "task_id": task.id,
                "error": str(e),
            })

    def _on_task_done(self, task_id: str):
        """Callback when task thread finishes."""
        self._futures.pop(task_id, None)
        task = self._tasks.get(task_id)
        if task:
            self._save()

    # ── SSE Broadcasting ──────────────────────────────────────────────────────

    def subscribe(self) -> Queue:
        """Подписаться на события задач (для SSE)."""
        with self._sse_lock:
            q = Queue()
            self._sse_clients.append(q)
            return q

    def unsubscribe(self, q: Queue):
        with self._sse_lock:
            if q in self._sse_clients:
                self._sse_clients.remove(q)

    def _broadcast(self, event: dict):
        """Отправить событие всем подписчикам."""
        with self._sse_lock:
            for client in list(self._sse_clients):
                try:
                    client.put_nowait(event)
                except:
                    pass

    # ── Utilities ─────────────────────────────────────────────────────────────

    def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)

    def cleanup_old(self, max_age_hours: int = 24):
        """Удалить завершённые задачи старше max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        to_remove = [
            tid for tid, t in self._tasks.items()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            and t.updated_at < cutoff
        ]
        for tid in to_remove:
            self._tasks.pop(tid, None)
        if to_remove:
            self._save()


def emit_task_event(event_type: str, task: Task, extra: dict = None):
    """Хелпер для отправки события."""
    event = {
        "event": event_type,
        "task_id": task.id,
        **task.to_dict(),
    }
    if extra:
        event.update(extra)
    _event_queue.put_nowait(event)