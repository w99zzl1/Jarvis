import React, { useState, useEffect } from "react";
import { X, CheckCircle, AlertCircle, Download, Clock } from "lucide-react";
import { subscribeTaskEvents, TaskInfo, TaskEvent } from "../api";

const STATUS_LABELS: Record<string, string> = {
  pending: "Ожидает",
  running: "Выполняется",
  completed: "Готово",
  failed: "Ошибка",
  cancelled: "Отменено",
};

interface TaskItemProps {
  task: TaskInfo;
  onCancel?: (id: string) => void;
}

function TaskItem({ task, onCancel }: TaskItemProps) {
  const isActive = task.status === "running" || task.status === "pending";

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "12px",
        padding: "10px 12px",
        background: "rgba(255,255,255,0.05)",
        borderRadius: "8px",
        marginBottom: "8px",
      }}
    >
      {task.status === "completed" && (
        <CheckCircle size={16} color="#22c55e" />
      )}
      {task.status === "failed" && (
        <AlertCircle size={16} color="#ef4444" />
      )}
      {(task.status === "running" || task.status === "pending") && (
        <Download size={16} color="#00D9FF" />
      )}

      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: "13px",
            color: "#e2e8f0",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {task.description || STATUS_LABELS[task.status] || task.status}
        </div>

        {isActive && (
          <div style={{ marginTop: "6px" }}>
            <div
              style={{
                height: "4px",
                background: "rgba(255,255,255,0.1)",
                borderRadius: "2px",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  height: "100%",
                  width: `${task.progress || 0}%`,
                  background: "linear-gradient(90deg, #00D9FF, #00ff88)",
                  borderRadius: "2px",
                  transition: "width 0.5s ease",
                }}
              />
            </div>
          </div>
        )}

        {isActive && (
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              marginTop: "4px",
              fontSize: "11px",
              color: "#64748b",
            }}
          >
            <span>{task.progress || 0}%</span>
            {task.eta && <span>ETA: {task.eta}</span>}
            {task.speed && <span>{task.speed}</span>}
          </div>
        )}

        {task.status === "completed" && task.result && (
          <div
            style={{
              fontSize: "11px",
              color: "#22c55e",
              marginTop: "2px",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {task.result.slice(0, 60)}
          </div>
        )}

        {task.status === "failed" && task.error && (
          <div
            style={{
              fontSize: "11px",
              color: "#ef4444",
              marginTop: "2px",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {task.error.slice(0, 80)}
          </div>
        )}
      </div>

      {isActive && onCancel && (
        <button
          onClick={() => onCancel(task.id)}
          style={{
            background: "transparent",
            border: "none",
            color: "#64748b",
            cursor: "pointer",
            padding: "4px",
            display: "flex",
            alignItems: "center",
          }}
        >
          <X size={14} />
        </button>
      )}
    </div>
  );
}

interface TaskNotificationProps {
  onClose?: () => void;
}

export default function TaskNotification({ onClose }: TaskNotificationProps) {
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [activeTasks, setActiveTasks] = useState<TaskInfo[]>([]);
  const [toasts, setToasts] = useState<
    { id: string; message: string; type: "success" | "error" }[]
  >([]);

  useEffect(() => {
    // Load initial tasks
    getTasks()
      .then(({ tasks: t }) => {
        setTasks(t);
        setActiveTasks(t.filter((x) => x.status === "running" || x.status === "pending"));
      })
      .catch(() => {});

    // Subscribe to task events
    const sub = subscribeTaskEvents((ev: TaskEvent) => {
      if (ev.event === "task_started") {
        const task = ev as unknown as TaskInfo;
        setActiveTasks((prev) => [...prev, task]);
        setTasks((prev) => [...prev, task]);
      } else if (ev.event === "task_progress") {
        const taskId = ev.task_id;
        setTasks((prev) =>
          prev.map((t) =>
            t.id === taskId
              ? { ...t, progress: ev.progress || t.progress, eta: ev.eta || t.eta, speed: ev.speed || t.speed }
              : t
          )
        );
        setActiveTasks((prev) =>
          prev.map((t) =>
            t.id === taskId
              ? { ...t, progress: ev.progress || t.progress, eta: ev.eta || t.eta, speed: ev.speed || t.speed }
              : t
          )
        );
      } else if (ev.event === "task_completed") {
        const taskId = ev.task_id;
        const task = tasks.find((t) => t.id === taskId);
        setActiveTasks((prev) => prev.filter((t) => t.id !== taskId));
        setTasks((prev) =>
          prev.map((t) =>
            t.id === taskId ? { ...t, status: "completed", progress: 100, result: ev.result || "" } : t
          )
        );
        // Show toast
        setToasts((prev) => [
          ...prev,
          {
            id: taskId,
            message: task?.description ? `Готово: ${task.description.slice(0, 40)}` : "Задача завершена",
            type: "success",
          },
        ]);
        // Auto-remove toast after 5s
        setTimeout(() => {
          setToasts((prev) => prev.filter((t) => t.id !== taskId));
        }, 5000);
      } else if (ev.event === "task_failed") {
        const taskId = ev.task_id;
        setActiveTasks((prev) => prev.filter((t) => t.id !== taskId));
        setTasks((prev) =>
          prev.map((t) =>
            t.id === taskId ? { ...t, status: "failed", error: ev.error || "" } : t
          )
        );
        setToasts((prev) => [
          ...prev,
          {
            id: taskId,
            message: `Ошибка: ${(ev.error || "задача не удалась").slice(0, 50)}`,
            type: "error",
          },
        ]);
        setTimeout(() => {
          setToasts((prev) => prev.filter((t) => t.id !== taskId));
        }, 8000);
      } else if (ev.event === "task_cancelled") {
        const taskId = ev.task_id;
        setActiveTasks((prev) => prev.filter((t) => t.id !== taskId));
        setTasks((prev) =>
          prev.map((t) => (t.id === taskId ? { ...t, status: "cancelled" } : t))
        );
      }
    });

    return () => {
      sub.stop();
    };
  }, [tasks]);

  const handleCancel = async (taskId: string) => {
    try {
      await cancelTask(taskId);
    } catch {
      // ignore
    }
  };

  if (activeTasks.length === 0 && toasts.length === 0) {
    return null;
  }

  return (
    <>
      {/* Toast notifications */}
      <div
        style={{
          position: "fixed",
          bottom: "20px",
          right: "20px",
          zIndex: 9999,
          display: "flex",
          flexDirection: "column",
          gap: "8px",
        }}
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "10px",
              padding: "12px 16px",
              background: toast.type === "success" ? "#166534" : "#7f1d1d",
              borderRadius: "8px",
              color: "white",
              fontSize: "13px",
              boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
              maxWidth: "320px",
            }}
          >
            {toast.type === "success" ? (
              <CheckCircle size={16} color="#4ade80" />
            ) : (
              <AlertCircle size={16} color="#f87171" />
            )}
            <span style={{ flex: 1 }}>{toast.message}</span>
            <button
              onClick={() => setToasts((prev) => prev.filter((t) => t.id !== toast.id))}
              style={{
                background: "transparent",
                border: "none",
                color: "rgba(255,255,255,0.6)",
                cursor: "pointer",
              }}
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>

      {/* Mini progress bar at top */}
      {activeTasks.length > 0 && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            height: "3px",
            background: "rgba(255,255,255,0.1)",
            zIndex: 9998,
          }}
        >
          <div
            style={{
              height: "100%",
              background: "linear-gradient(90deg, #00D9FF, #00ff88)",
              transition: "width 0.5s ease",
              width: `${activeTasks[0]?.progress || 0}%`,
            }}
          />
        </div>
      )}
    </>
  );
}

export { TaskItem };
export type { TaskItemProps };