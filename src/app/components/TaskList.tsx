import React, { useEffect, useRef, useState } from "react";
import { Loader2, ListChecks } from "lucide-react";

const CYAN = "#00D9FF";
const PURPLE = "#8B5CFF";
const GREEN = "#00D97E";
const RED = "#FF4D4D";
const BORDER = "rgba(255,255,255,0.1)";
const UI = "'JetBrains Mono', 'Space Mono', monospace";

export interface Task {
  id: string;
  title: string;
  description: string;
  status: "pending" | "in_progress" | "completed" | "failed" | "skipped";
  tool_calls: string[];
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface ExecutionPlan {
  id: string;
  user_request: string;
  tasks: Task[];
  created_at: string;
  updated_at: string;
  current_task_index: number;
}

export interface StreamEvent {
  type: string;
  steps?: { id: string; text: string; status: string }[];
  plan?: ExecutionPlan;
  message?: string;
  error?: string;
}

interface TaskListProps {
  events: StreamEvent[];
  isExecuting: boolean;
  onToggleExpand?: (taskId: string) => void;
  expandedTasks?: Set<string>;
}

const VALID_STATUSES = ["pending", "in_progress", "completed", "failed", "skipped"];

export function normalizePlan(plan: any): ExecutionPlan | null {
  if (!plan) return null;

  if (Array.isArray(plan)) {
    const tasks: Task[] = plan
      .filter((s) => s && typeof s === "object")
      .map((s, i) => ({
        id: String(s.id ?? i),
        title: String(s.text ?? ""),
        description: "",
        status: VALID_STATUSES.includes(s.status) ? (s.status as Task["status"]) : "pending",
        tool_calls: [],
        error: null,
        started_at: s.status === "in_progress" ? new Date().toISOString() : null,
        completed_at: s.status === "completed" ? new Date().toISOString() : null,
      }))
      .filter((t) => t.title.trim().length > 0);
    if (tasks.length === 0) return null;
    return {
      id: "plan",
      user_request: "",
      tasks,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      current_task_index: Math.max(0, tasks.findIndex((t) => t.status === "in_progress")),
    };
  }

  if (Array.isArray(plan.tasks)) {
    return {
      id: plan.id || "plan",
      user_request: plan.user_request || "",
      tasks: plan.tasks,
      created_at: plan.created_at || new Date().toISOString(),
      updated_at: plan.updated_at || new Date().toISOString(),
      current_task_index: plan.current_task_index ?? 0,
    };
  }

  if (Array.isArray(plan.steps)) {
    return normalizePlan(plan.steps);
  }

  return null;
}

export default function TaskList({ events, isExecuting }: TaskListProps) {
  const [plan, setPlan] = useState<ExecutionPlan | null>(null);
  const processedRef = useRef(0);

  useEffect(() => {
    const newEvents = events.slice(processedRef.current);
    processedRef.current = events.length;

    for (const event of newEvents) {
      if (event.type !== "plan_updated" && event.type !== "plan_created") continue;
      const next = normalizePlan(event.steps ?? event.plan ?? null);
      if (next) setPlan(next);
    }
  }, [events]);

  useEffect(() => {
    if (events.length === 0) setPlan(null);
  }, [events]);

  if (!plan) return null;

  const totalCount = plan.tasks.length;
  const completedCount = plan.tasks.filter((t) => t.status === "completed").length;
  const failedCount = plan.tasks.filter((t) => t.status === "failed").length;
  const percent =
    totalCount > 0
      ? Math.round(((completedCount + failedCount) / totalCount) * 100)
      : 0;

  return (
    <div
      data-no-drag
      onMouseDown={(e) => e.stopPropagation()}
      style={{
        background: "#0D0D12",
        border: `1px solid ${BORDER}`,
        borderRadius: "12px",
        padding: "12px",
        fontFamily: UI,
        display: "flex",
        flexDirection: "column",
        flexShrink: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "10px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <ListChecks size={16} style={{ color: CYAN }} />
          <span style={{ fontWeight: 600, fontSize: "12px", color: "white" }}>План</span>
          <span
            style={{
              background: isExecuting ? "rgba(0,217,255,0.2)" : "rgba(255,255,255,0.1)",
              color: isExecuting ? CYAN : "rgba(255,255,255,0.5)",
              padding: "2px 8px",
              borderRadius: "4px",
              fontSize: "9px",
              fontWeight: 500,
            }}
          >
            {isExecuting ? "ВЫПОЛНЯЕТСЯ" : "ЗАВЕРШЁН"}
          </span>
        </div>
        <span style={{ color: CYAN, fontWeight: 500, fontSize: "11px" }}>
          {completedCount}/{totalCount}
        </span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
        {plan.tasks.map((t) => {
          const color =
            t.status === "completed"
              ? GREEN
              : t.status === "in_progress"
              ? CYAN
              : t.status === "failed"
              ? RED
              : "rgba(255,255,255,0.35)";
          const mark =
            t.status === "completed" ? "✓" : t.status === "failed" ? "✕" : "▸";
          return (
            <div
              key={t.id}
              style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "12px" }}
            >
              <span style={{ color, width: "14px", flexShrink: 0, textAlign: "center" }}>
                {t.status === "in_progress" ? (
                  <Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} />
                ) : (
                  mark
                )}
              </span>
              <span
                style={{
                  color,
                  opacity: t.status === "pending" ? 0.6 : 1,
                  textDecoration: t.status === "completed" ? "line-through" : "none",
                  lineHeight: 1.4,
                }}
              >
                {t.title}
              </span>
            </div>
          );
        })}
      </div>

      <div
        style={{
          marginTop: "10px",
          height: "4px",
          background: "rgba(255,255,255,0.05)",
          borderRadius: "2px",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${percent}%`,
            background: `linear-gradient(90deg, ${CYAN}, ${PURPLE})`,
            transition: "width 0.3s ease-out",
          }}
        />
      </div>
    </div>
  );
}
