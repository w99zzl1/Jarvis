import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { Send, Square, Paperclip, Image as ImageIcon } from "lucide-react";
import ModelSelector from "./ModelSelector";
import Markdown from "./Markdown";
import {
  cleanText,
  formatBytes,
  useChat,
  type ActivityEntry,
  type Message,
  type FileAttachment,
} from "../useChat";
import { uploadFiles } from "../api";
import { normalizePlan, type ExecutionPlan } from "./TaskList";
import { PROVIDER_LABELS, type ProviderId } from "../models";

interface DashboardProps {
  isListening: boolean;
  isSpeaking: boolean;
  setIsSpeaking: (v: boolean) => void;
}

const TOOL_TONES = [
  "text-primary-fixed-dim",
  "text-secondary",
  "text-tertiary-fixed-dim",
];

/** Tools the code agent needs to function; disabling one is allowed but risky. */
const CORE_TOOLS = new Set([
  "read_file",
  "write_file",
  "edit_file",
  "shell",
  "update_plan",
  "get_tools",
]);

const TOOL_ICONS: Record<string, string> = {
  read_file: "description",
  write_file: "note_add",
  edit_file: "edit_note",
  update_plan: "checklist",
  shell: "terminal",
  web_search: "public",
  search_content: "search",
  git_status: "account_tree",
  git_commit: "commit",
  get_tools: "extension",
  disk_tree: "folder_open",
};

function ToolToggle({
  tool,
  enabled,
  core,
  onToggle,
}: {
  tool: { name: string; description: string; group: string };
  enabled: boolean;
  core: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      className={`flex items-center justify-between gap-2 rounded-lg p-2 transition-colors ${
        enabled ? "bg-surface-container-high/30" : "bg-surface-container-high/10 opacity-60"
      }`}
    >
      <div className="flex min-w-0 flex-col">
        <div className="flex items-center gap-1.5">
          <span className="font-body-sm text-body-sm text-on-surface">{tool.name}</span>
          {core && (
            <span
              title="Core tool: disabling it may break the agent"
              className="rounded bg-error-container/30 px-1 font-label-sm text-label-sm text-error"
            >
              CORE
            </span>
          )}
        </div>
        <span className="truncate font-label-sm text-label-sm text-on-surface-variant">
          {tool.description || tool.group}
        </span>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={enabled}
        onClick={() => onToggle()}
        title={enabled ? "Выключить инструмент" : "Включить инструмент"}
        className={`relative h-4 w-8 shrink-0 rounded-full transition-colors ${
          enabled ? "bg-primary-container" : "bg-surface-container-highest"
        }`}
      >
        <span
          className={`absolute top-0.5 h-3 w-3 rounded-full shadow transition-transform ${
            enabled
              ? "translate-x-4 bg-on-primary-container"
              : "translate-x-0.5 bg-on-surface-variant"
          }`}
        />
      </button>
    </div>
  );
}

function clockOf(ts: number) {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtDuration(ms?: number) {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${ms.toFixed(1)}ms`;
  return `${(ms / 1000).toFixed(3)}s`;
}

function baseName(p: string) {
  if (!p) return "";
  const parts = p.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

// ============================================================
// SIDEBAR
// ============================================================

function Sidebar({
  onOpenHistory,
  onOpenSettings,
}: {
  onOpenHistory: () => void;
  onOpenSettings: () => void;
}) {
  return (
    <aside className="fixed left-0 top-0 z-50 flex h-full w-20 flex-col items-center justify-between border-r border-outline-variant/20 bg-surface-container-lowest py-space-md">
      <div className="flex w-full flex-col items-center gap-space-md">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-surface-container-high">
          <span className="material-symbols-outlined text-[22px] text-primary-container">token</span>
        </div>

        <button
          type="button"
          title="История чата"
          onClick={onOpenHistory}
          className="flex h-12 w-12 items-center justify-center rounded-xl text-on-surface-variant transition-all hover:bg-surface-container-high hover:text-primary"
        >
          <span className="material-symbols-outlined text-[20px]">history</span>
        </button>
      </div>

      <div className="flex w-full flex-col items-center gap-space-sm">
        <button
          type="button"
          title="System Settings"
          onClick={onOpenSettings}
          className="flex h-12 w-12 items-center justify-center rounded-xl text-on-surface-variant transition-all hover:bg-surface-container-high hover:text-on-surface"
        >
          <span className="material-symbols-outlined text-[20px]">tune</span>
        </button>
        <div className="mt-space-xs h-2 w-2 animate-pulse-fast rounded-full bg-primary-container" />
      </div>
    </aside>
  );
}

function fmtAgo(updatedSec: number): string {
  if (!updatedSec) return "";
  const diff = Math.max(0, Date.now() / 1000 - updatedSec);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function HistoryDrawer({
  open,
  onClose,
  items,
  loading,
  activeId,
  onSelect,
  onDelete,
  onNew,
}: {
  open: boolean;
  onClose: () => void;
  items: { id: string; title: string; turns: number; updated: number }[];
  loading: boolean;
  activeId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onNew: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[60]">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className="absolute left-20 top-0 flex h-full w-[340px] max-w-[80vw] flex-col gap-space-sm border-r border-outline-variant/30 bg-surface-container-low/95 p-space-md shadow-2xl backdrop-blur-2xl">
        <div className="flex items-center justify-between">
          <span className="font-headline-sm text-headline-sm font-medium text-on-surface">
            История чата
          </span>
          <button
            type="button"
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-on-surface-variant transition-colors hover:bg-surface-container-high hover:text-on-surface"
          >
            <span className="material-symbols-outlined text-[20px]">close</span>
          </button>
        </div>

        <button
          type="button"
          onClick={onNew}
          className="flex items-center justify-center gap-2 rounded-lg border border-primary-container/30 py-2 font-label-md text-label-md font-medium text-primary-container transition-colors hover:bg-primary-container/10"
        >
          <span className="material-symbols-outlined text-[18px]">add</span>
          Новый чат
        </button>

        <div className="flex min-h-0 flex-1 flex-col gap-space-xs overflow-y-auto pr-1">
          {loading && (
            <span className="p-2 font-label-sm text-label-sm uppercase text-on-surface-variant">
              загрузка…
            </span>
          )}
          {!loading && items.length === 0 && (
            <span className="p-2 font-label-sm text-label-sm uppercase text-on-surface-variant">
              пока пусто
            </span>
          )}
          {items.map((c) => (
            <div
              key={c.id}
              className={`group flex items-center gap-1 rounded-lg p-1 transition-colors ${
                activeId === c.id
                  ? "bg-surface-container-high"
                  : "hover:bg-surface-container-high/50"
              }`}
            >
              <button
                type="button"
                onClick={() => onSelect(c.id)}
                className="flex min-w-0 flex-1 flex-col items-start rounded-md px-2 py-1 text-left"
              >
                <span className="w-full truncate font-body-sm text-body-sm text-on-surface">
                  {c.title}
                </span>
                <span className="font-label-sm text-label-sm text-on-surface-variant">
                  {c.turns} сообщ. · {fmtAgo(c.updated)}
                </span>
              </button>
              <button
                type="button"
                title="Удалить"
                onClick={() => onDelete(c.id)}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-on-surface-variant opacity-0 transition-all hover:bg-error-container/20 hover:text-error group-hover:opacity-100"
              >
                <span className="material-symbols-outlined text-[16px]">delete</span>
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ============================================================
// HEADER
// ============================================================

function Header({
  modelLabel,
  isListening,
  isSpeaking,
  latencyMs,
  contextChars,
  tps,
}: {
  modelLabel: string;
  isListening: boolean;
  isSpeaking: boolean;
  latencyMs: number | null;
  contextChars: number;
  tps: number;
}) {
  const state = isSpeaking ? "Speaking" : isListening ? "Listening" : "Online";

  return (
    <header className="z-40 h-14 w-full shrink-0 border-b border-outline-variant/20 bg-background/80 backdrop-blur">
      <div className="flex h-14 w-full items-center justify-between px-gutter">
        <div className="flex items-center gap-space-md">
          <span className="font-headline-sm text-headline-sm font-medium tracking-tight text-on-surface">
            J.A.R.V.I.S.
          </span>
          <span className="flex items-center gap-1.5 text-body-sm text-body-sm text-on-surface-variant">
            <span className="h-1.5 w-1.5 rounded-full bg-primary-container" />
            {state}
          </span>
        </div>

        <div className="flex items-center gap-space-md text-body-sm text-body-sm text-on-surface-variant">
          <span className="hidden max-w-[220px] truncate sm:inline">{modelLabel}</span>
          <span className="tabular-nums">
            {latencyMs != null ? `${Math.round(latencyMs)} ms` : "—"}
          </span>
        </div>
      </div>
    </header>
  );
}

// ============================================================
// HUD TELEMETRY STRIP
// ============================================================

export function HudStrip({
  modelLabel,
  isListening,
  isSpeaking,
  isExecuting,
  latencyMs,
  contextChars,
  tps,
  dockVisible,
  onToggleDock,
  onClearContext,
  onToggleModels,
  children,
}: {
  modelLabel: string;
  isListening: boolean;
  isSpeaking: boolean;
  isExecuting: boolean;
  latencyMs: number | null;
  contextChars: number;
  tps: number;
  dockVisible: boolean;
  onToggleDock: () => void;
  onClearContext: () => void;
  onToggleModels: () => void;
  children?: React.ReactNode;
}) {
  const state = isSpeaking
    ? "Speaking"
    : isListening
    ? "Listening"
    : isExecuting
    ? "Executing"
    : "Idle";

  return (
    <div className="relative flex w-full flex-wrap items-center justify-between gap-space-sm rounded-lg border border-outline-variant/20 bg-surface-container-low/60 px-space-md py-2">
      <div className="flex items-center gap-space-md">
        <span className="flex items-center gap-1.5 text-body-sm text-body-sm text-on-surface-variant">
          <span className="h-1.5 w-1.5 rounded-full bg-primary-container" />
          {state}
        </span>
      </div>

      <div className="flex items-center gap-space-md">
        <button
          type="button"
          id="modelTriggerBtn"
          data-model-trigger
          onClick={onToggleModels}
          className="group flex items-center gap-space-xs rounded-lg border border-outline-variant/25 px-2.5 py-1 text-body-sm text-body-sm text-on-surface transition-colors hover:border-primary-container/40 hover:text-primary"
        >
          <span className="material-symbols-outlined text-[15px] text-on-surface-variant">memory</span>
          <span className="max-w-[220px] truncate">{modelLabel}</span>
          <span className="material-symbols-outlined text-[14px] text-on-surface-variant transition-transform group-hover:translate-y-0.5">
            expand_more
          </span>
        </button>

        <span className="hidden text-body-sm text-body-sm tabular-nums text-on-surface-variant lg:inline">
          {latencyMs != null ? `${Math.round(latencyMs)} ms` : "—"}
        </span>

        <div className="flex items-center gap-1">
          <button
            type="button"
            id="toggleTaskPanel"
            onClick={onToggleDock}
            title="Toggle Active Mission Pipeline HUD"
            className={`flex h-8 w-8 items-center justify-center rounded-lg transition-colors ${
              dockVisible
                ? "bg-surface-container-high text-primary"
                : "text-on-surface-variant hover:bg-surface-container-high hover:text-primary"
            }`}
          >
            <span className="material-symbols-outlined text-[17px]">checklist</span>
          </button>
          <button
            type="button"
            id="toggleClearContext"
            onClick={onClearContext}
            title="Clear Context Buffer"
            className="flex h-8 w-8 items-center justify-center rounded-lg text-on-surface-variant transition-colors hover:bg-surface-container-high hover:text-error"
          >
            <span className="material-symbols-outlined text-[17px]">layers_clear</span>
          </button>
        </div>
      </div>

      {children}
    </div>
  );
}

// ============================================================
// WELCOME NODE
// ============================================================

function WelcomeNode() {
  return (
    <div className="flex shrink-0 flex-col justify-between gap-space-sm rounded-lg border border-outline-variant/20 bg-surface-container-low/40 p-space-md md:flex-row md:items-center">
      <div className="flex items-center gap-space-md">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-surface-container-high">
          <span className="material-symbols-outlined text-[18px] text-primary-container">blur_on</span>
        </div>
        <div className="flex flex-col">
          <span className="font-headline-sm text-headline-sm tracking-tight text-on-surface">
            JARVIS Core Engine
          </span>
          <span className="font-body-sm text-body-sm text-on-surface-variant">
            Neural workspace ready.
          </span>
        </div>
      </div>
      <div className="flex items-center gap-space-xs self-start md:self-auto">
        <span className="text-body-sm text-body-sm text-on-surface-variant">Cluster</span>
        <span className="text-body-sm text-body-sm text-on-surface">US-EAST-ALPHA (v4.2.1-prod)</span>
      </div>
    </div>
  );
}

// ============================================================
// PIPELINE EXECUTION BLOCK
// ============================================================

export function PipelineBlock({
  activity,
  open,
  onToggle,
  running,
}: {
  activity: ActivityEntry[];
  open: boolean;
  onToggle: () => void;
  running: boolean;
}) {
  const tools = activity.filter((a) => a.kind === "tool");
  const done = tools.filter((a) => a.status === "done").length;
  const failed = tools.filter((a) => a.status === "failed").length;
  const total = tools.length;
  const finished = done + failed;
  const percent = total > 0 ? Math.round((finished / total) * 100) : 0;
  const runtime = tools.reduce((acc, t) => acc + (t.duration ?? 0), 0);
  const errors = failed > 0 ? `${failed} error state${failed > 1 ? "s" : ""}` : "Zero error states";
  // A tool that reports its own progress (the disk scanner) drives the stage bar
  // while it runs: the per-tool count would sit still for the whole scan.
  const live = tools.find((t) => t.status === "running" && t.progress);
  const stagePercent = live?.progress
    ? Math.round(Math.max(0, Math.min(100, live.progress.percent)))
    : percent;

  return (
    <div className="flex w-full flex-col gap-2">
      <div className="flex items-center gap-space-sm pl-1">
        <div className="flex h-6 w-6 items-center justify-center rounded-lg bg-surface-container-high text-primary-container">
          <span className="material-symbols-outlined text-[14px]">smart_toy</span>
        </div>
        <span className="font-label-md text-label-md text-on-surface-variant">
          Pipeline
        </span>
        <span className="h-1.5 w-1.5 rounded-full bg-primary-container" />
      </div>

      <div className="w-full overflow-hidden rounded-lg border border-outline-variant/20 bg-surface-container-low/60">
        <div className="flex flex-wrap items-center justify-between gap-space-sm bg-surface-container-lowest/60 px-space-md py-space-sm">
          <div className="flex items-center gap-space-sm">
            <span className="material-symbols-outlined text-[18px] text-primary-container">verified</span>
            <div className="flex flex-col">
              <span className="font-label-sm text-label-sm font-semibold text-on-surface">
                {running
                  ? `${total} Sub-routine${total === 1 ? "" : "s"} Dispatched`
                  : `${total} Sub-routine${total === 1 ? "" : "s"} Dispatched & Verified`}
              </span>
              <span className="font-label-sm text-label-sm text-on-surface-variant">
                Total computation runtime: {fmtDuration(runtime)} • {errors}
              </span>
            </div>
          </div>
          <button
            type="button"
            onClick={onToggle}
            className="flex items-center gap-1 rounded bg-surface-container-high px-space-sm py-1 text-primary transition-colors hover:bg-surface-container-highest"
          >
            <span className="font-label-sm text-label-sm uppercase">
              {open ? "Hide Telemetry" : `Show Details (${total} tools)`}
            </span>
            <span
              className="material-symbols-outlined text-[15px] transition-transform"
              style={{ transform: open ? "rotate(0deg)" : "rotate(180deg)" }}
            >
              expand_less
            </span>
          </button>
        </div>

        {/* Live scan status, always visible. A C:\ scan runs up to 29s and the
            scanner's first sample only lands 2-3s in, so the bar is drawn at 0%
            the moment the tool starts - hidden behind "Show Details" it read as
            a frozen app. */}
        {live?.progress && (
          <div className="flex w-full flex-col gap-1 border-t border-on-surface-variant/10 px-space-md py-space-sm">
            <div className="flex items-center justify-between text-on-surface-variant">
              <span className="font-label-sm text-label-sm uppercase tracking-wide">
                {live.progress.phase === "pending"
                  ? "Сканирование — запуск…"
                  : `Сканирование (${live.progress.nodes.toLocaleString()} узлов, ${formatBytes(
                      live.progress.bytes,
                    )})`}
              </span>
              <span className="font-label-sm text-label-sm font-bold text-primary">
                {Math.round(Math.max(0, Math.min(100, live.progress.percent)))}%
              </span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-container-highest">
              <div
                className="h-full rounded-full bg-primary-container transition-all duration-300"
                style={{
                  width: `${Math.max(0, Math.min(100, live.progress.percent))}%`,
                }}
              />
            </div>
            <span className="font-label-sm text-label-sm text-on-surface-variant">
              оценка • в очереди {live.progress.queued.toLocaleString()} папок •{" "}
              {live.progress.elapsed.toFixed(1)} с
            </span>
          </div>
        )}

        {open && (
          <div className="flex flex-col gap-space-md p-space-md">
            <div className="flex w-full flex-col gap-1.5">
              <div className="flex items-center justify-between text-on-surface-variant">
                <span className="font-label-sm text-label-sm uppercase tracking-wide">
                  {live?.progress
                    ? `Сканирование... (${live.progress.nodes.toLocaleString()} узлов, ${formatBytes(
                        live.progress.bytes,
                      )})`
                    : `Execution Stage (${finished}/${total} Completed)`}
                </span>
                <span className="font-label-sm text-label-sm font-bold text-primary">
                  {stagePercent}%
                </span>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-container-highest">
                <div
                  className="h-full rounded-full bg-primary-container transition-all duration-300"
                  style={{ width: `${stagePercent}%` }}
                />
              </div>
              {live?.progress && (
                <span className="font-label-sm text-label-sm text-on-surface-variant">
                  оценка • в очереди {live.progress.queued.toLocaleString()} папок •{" "}
                  {live.progress.elapsed.toFixed(1)} с
                </span>
              )}
            </div>

            {total > 0 && (
              <div className="grid grid-cols-1 gap-space-sm md:grid-cols-3">
                {tools.slice(0, 6).map((tool, i) => (
                  <div
                    key={tool.id}
                    className="flex flex-col gap-1 rounded-lg bg-surface-container-high/50 p-space-sm"
                  >
                    <div className="flex items-center justify-between">
                      <span
                        className={`font-label-sm text-label-sm ${TOOL_TONES[i % TOOL_TONES.length]}`}
                      >
                        {String(i + 1).padStart(2, "0")}. {tool.text.split(":")[0]}
                      </span>
                      <span
                        className={`h-2 w-2 rounded-full ${
                          tool.status === "failed"
                            ? "bg-error"
                            : tool.status === "running"
                            ? "animate-pulse bg-tertiary-fixed-dim"
                            : "bg-primary-container"
                        }`}
                      />
                    </div>
                    <span className="truncate font-body-sm text-body-sm text-on-surface">
                      {tool.text.split(":").slice(1).join(":").trim() || tool.text}
                    </span>
                    <span className="font-label-sm text-label-sm text-on-surface-variant">
                      {fmtDuration(tool.duration)}
                      {tool.status === "running" ? " • running" : ""}
                      {tool.status === "failed" ? " • failed" : ""}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {activity.length > 0 && (
              <div className="flex flex-col gap-1 overflow-x-auto rounded-lg bg-surface-container-lowest p-space-sm font-label-sm text-label-sm text-on-surface-variant">
                <div className="flex items-center justify-between pb-1 text-on-surface-variant">
                  <span>EXECUTION LOG // REALTIME AUDIT</span>
                  <span className={failed > 0 ? "text-error" : "text-primary-container"}>
                    {failed > 0 ? "SYS_ERR" : running ? "BUSY" : "SYS_OK"}
                  </span>
                </div>
                {activity.slice(-8).map((a) => (
                  <div key={a.id} className="text-on-surface">
                    <span className="text-primary-container">&gt;</span>{" "}
                    <span
                      className={
                        a.status === "failed"
                          ? "text-error"
                          : a.kind === "say"
                          ? "text-on-surface-variant"
                          : "text-on-surface"
                      }
                    >
                      {a.text}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// MESSAGES
// ============================================================

export function UserMessage({ text, createdAt }: { text: string; createdAt: number }) {
  return (
    <div className="flex w-full max-w-2xl flex-col items-end gap-1.5 self-end">
      <div className="flex items-center gap-2 pr-1">
        <span className="font-label-sm text-label-sm text-on-surface-variant">OPERATOR // T.STARK</span>
        <span className="font-label-sm text-label-sm text-outline">{clockOf(createdAt)}</span>
      </div>
      <div className="rounded-lg border border-outline-variant/20 bg-surface-container-high/40 px-space-md py-space-sm text-on-surface">
        <p className="whitespace-pre-wrap break-words font-body-md text-body-md leading-relaxed">
          {text}
        </p>
      </div>
    </div>
  );
}

export function AssistantMessage({
  message,
  open,
  onToggle,
  onRegenerate,
  helpful,
  onHelpful,
  busy,
}: {
  message: Message;
  open: boolean;
  onToggle: () => void;
  onRegenerate: () => void;
  helpful: boolean;
  onHelpful: () => void;
  busy: boolean;
}) {
  const activity = message.activity || [];
  const text = cleanText(message.text || "");

  return (
    <div className="flex w-full max-w-4xl flex-col items-start gap-space-sm self-start">
      {activity.length > 0 && (
        <PipelineBlock activity={activity} open={open} onToggle={onToggle} running={busy} />
      )}

      <div className="flex items-center gap-space-sm pl-1">
        <div className="flex h-7 w-7 items-center justify-center rounded-full bg-surface-container-high">
          <span className="material-symbols-outlined text-[16px] text-primary-container">token</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-headline-sm text-headline-sm text-on-surface">J.A.R.V.I.S.</span>
          <span className="font-label-sm text-label-sm text-outline">
            {clockOf(message.createdAt)}
            {message.latencyMs != null ? ` • ${fmtDuration(message.latencyMs)} latency` : ""}
          </span>
        </div>
      </div>

      <div className="flex w-full flex-col gap-space-md rounded-lg border border-outline-variant/20 bg-surface-container-low/50 p-space-md">
        {text ? (
          <Markdown text={text} />
        ) : (
          <span className="font-label-sm text-label-sm uppercase text-on-surface-variant">
            {busy ? "processing…" : "(нет ответа)"}
          </span>
        )}

        {text && (
          <div className="flex flex-wrap items-center justify-between gap-space-sm pt-space-xs">
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={onRegenerate}
                title="Regenerate this reply"
                className="flex items-center gap-1 rounded bg-surface-container-high px-space-sm py-1 font-label-sm text-label-sm text-on-surface-variant transition-colors hover:bg-surface-container-highest hover:text-primary"
              >
                <span className="material-symbols-outlined text-[15px]">difference</span>
                View Git Diff
              </button>
              <button
                type="button"
                onClick={() => navigator.clipboard?.writeText(text)}
                title="Copy the full reply"
                className="flex items-center gap-1 rounded bg-surface-container-high px-space-sm py-1 font-label-sm text-label-sm text-on-surface-variant transition-colors hover:bg-surface-container-highest hover:text-primary"
              >
                <span className="material-symbols-outlined text-[15px]">content_copy</span>
                Copy Reply
              </button>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onHelpful}
                title="Helpful"
                className={`flex h-7 w-7 items-center justify-center rounded transition-colors ${
                  helpful
                    ? "bg-surface-container-highest text-primary"
                    : "bg-surface-container-high text-on-surface-variant hover:text-primary"
                }`}
              >
                <span className="material-symbols-outlined text-[15px]">thumb_up</span>
              </button>
              <button
                type="button"
                onClick={onRegenerate}
                title="Regenerate"
                className="flex h-7 w-7 items-center justify-center rounded bg-surface-container-high text-on-surface-variant transition-colors hover:text-primary"
              >
                <span className="material-symbols-outlined text-[15px]">refresh</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// PROMPT DOCK
// ============================================================

export function PromptDock({
  value,
  onChange,
  onSubmit,
  loading,
  voiceMode,
  onToggleVoice,
  workdir,
  onChooseWorkdir,
  canStop,
  onStop,
  onAttachFile,
  hasAttachments,
}: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  loading: boolean;
  voiceMode: boolean;
  onToggleVoice: () => void;
  workdir: string;
  onChooseWorkdir: () => void;
  canStop?: boolean;
  onStop?: () => void;
  onAttachFile?: () => void;
  hasAttachments?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  return (
    <div className="z-30 w-full shrink-0 pt-space-sm">
      <div className="flex w-full flex-col gap-space-sm rounded-xl border border-outline-variant/20 bg-surface-container-low/70 p-space-sm shadow-lg transition-colors focus-within:border-primary-container/40">
        <div className="no-scrollbar flex items-center gap-space-xs overflow-x-auto pb-1">
          <button
            type="button"
            id="workdirBtn"
            onClick={onChooseWorkdir}
            title={workdir ? `Workspace: ${workdir}` : "Choose workspace folder"}
            className="group flex max-w-[240px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full bg-surface-container-high/80 px-2.5 py-1 text-on-surface-variant transition-all hover:bg-surface-container-highest hover:text-primary"
          >
            <span className="material-symbols-outlined text-[14px] text-on-surface-variant">folder_open</span>
            <span className="truncate font-label-sm text-label-sm">
              {workdir ? baseName(workdir) : "workspace"}
            </span>
          </button>

          <div className="mx-1 h-4 w-px bg-surface-container-highest" />

          <button
            type="button"
            id="voiceModeBtn"
            onClick={onToggleVoice}
            className="group flex shrink-0 items-center gap-2 whitespace-nowrap rounded-full bg-surface-container-high px-3 py-1 text-on-surface transition-all hover:text-primary"
          >
            <span
              className={`h-2 w-2 rounded-full ${
                voiceMode ? "animate-pulse bg-error" : "bg-primary-container"
              }`}
            />
            <span
              className={`font-label-sm text-label-sm ${
                voiceMode ? "text-primary-container" : ""
              }`}
            >
              {voiceMode ? "Слушаю..." : "Voice • Ready"}
            </span>
            <div className="flex h-3 items-center gap-0.5">
              <span className="h-1.5 w-0.5 rounded-full bg-primary-container" />
              <span className="h-3 w-0.5 rounded-full bg-primary-container" />
              <span className="h-2 w-0.5 rounded-full bg-primary-container" />
            </div>
          </button>
        </div>

        <div className="flex items-end gap-space-sm px-space-xs">
          <div className="flex items-center gap-1 pb-1 text-on-surface-variant">
            <button
              type="button"
              title="Attach workspace file"
              onClick={onAttachFile}
              className="flex h-8 w-8 items-center justify-center rounded-lg transition-colors hover:bg-surface-container-high hover:text-primary"
            >
              <span className="material-symbols-outlined text-[20px]">attach_file</span>
            </button>
            <button
              type="button"
              title="Deep search in vector index"
              className="flex h-8 w-8 items-center justify-center rounded-lg transition-colors hover:bg-surface-container-high hover:text-primary"
            >
              <span className="material-symbols-outlined text-[20px]">manage_search</span>
            </button>
          </div>

          <div className="min-w-0 flex-1">
            <textarea
              ref={ref}
              rows={1}
              value={value}
              onChange={(e) => onChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  onSubmit();
                }
              }}
              placeholder="Directive for J.A.R.V.I.S... (Shift+Enter for new line)"
              className="max-h-36 w-full resize-none overflow-y-auto border-0 bg-transparent py-1.5 font-body-md text-body-md text-on-surface placeholder:text-outline focus:outline-none focus:ring-0"
            />
          </div>

          <button
            type="button"
            id="sendActionBtn"
            onClick={canStop ? onStop : onSubmit}
            disabled={canStop ? false : loading || (!value.trim() && !hasAttachments)}
            className={`flex h-10 items-center justify-center gap-1.5 rounded-lg px-4 font-label-lg text-label-lg transition-colors ${
              canStop
                ? "bg-error/90 text-on-error hover:brightness-110 active:scale-95"
                : "bg-primary-container text-on-primary-container hover:brightness-110 active:scale-95 disabled:opacity-40"
            }`}
          >
            <span>{canStop ? "Stop" : "Send"}</span>
            <span className="material-symbols-outlined text-[17px]">{canStop ? "stop_circle" : "send"}</span>
          </button>
        </div>
      </div>
    </div>
  );
}

// ============================================================
// SIDE TELEMETRY DOCK
// ============================================================

export function SideDock({
  plan,
  isExecuting,
  tools,
  disabledTools,
  onToggleTool,
  onRestoreTools,
}: {
  plan: ExecutionPlan | null;
  isExecuting: boolean;
  tools: { name: string; description: string; group: string }[];
  disabledTools: string[];
  onToggleTool: (name: string, enabled: boolean) => void;
  onRestoreTools: () => void;
}) {
  const tasks = plan?.tasks || [];
  const doneCount = tasks.filter((t) => t.status === "completed").length;
  const failedCount = tasks.filter((t) => t.status === "failed").length;
  const percent = tasks.length
    ? Math.round(((doneCount + failedCount) / tasks.length) * 100)
    : 0;

  return (
    <aside className="flex max-h-[45vh] flex-col gap-space-md overflow-y-auto dash:col-span-4 dash:min-h-0 dash:max-h-none dash:overflow-y-auto">
      <div className="flex w-full flex-col gap-space-md rounded-lg border border-outline-variant/20 bg-surface-container-low/60 p-space-md">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-[18px] text-primary-container">fact_check</span>
            <span className="font-headline-sm text-headline-sm tracking-tight text-primary">
              Active Plan Execution
            </span>
          </div>
          <span className="rounded-full bg-surface-container-high px-2 py-0.5 font-label-sm text-label-sm text-primary-fixed-dim">
            {percent}%
          </span>
        </div>

        <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-container-highest">
          <div
            className="h-full rounded-full bg-gradient-to-r from-primary-container to-secondary transition-all duration-300"
            style={{ width: `${percent}%` }}
          />
        </div>

        <div className="flex flex-col gap-space-xs">
          {tasks.length === 0 && (
            <span className="font-label-sm text-label-sm uppercase text-on-surface-variant">
              {isExecuting ? "planning…" : "no active mission"}
            </span>
          )}
          {tasks.map((t) => {
            const isDone = t.status === "completed";
            const isFailed = t.status === "failed";
            return (
              <label
                key={t.id}
                className="flex cursor-pointer items-start gap-space-sm rounded-lg p-space-xs transition-colors hover:bg-surface-container-high/40"
              >
                <input
                  type="checkbox"
                  checked={isDone}
                  readOnly
                  className="mt-1 h-4 w-4 rounded border-0 bg-surface-container-high text-primary-container focus:ring-0 focus:ring-offset-0"
                />
                <div className="flex min-w-0 flex-1 flex-col">
                  <span
                    className={`font-body-sm text-body-sm text-on-surface ${
                      isDone ? "line-through opacity-70" : isFailed ? "text-error" : ""
                    }`}
                  >
                    {t.title}
                  </span>
                  <span className="font-label-sm text-label-sm text-on-surface-variant">
                    {t.error
                      ? t.error
                      : t.description ||
                        (isDone ? "completed" : t.status === "in_progress" ? "running" : "queued")}
                  </span>
                </div>
              </label>
            );
          })}
        </div>
      </div>

      <div className="flex w-full flex-col gap-space-sm rounded-lg border border-outline-variant/20 bg-surface-container-low/60 p-space-md">
        <div className="flex items-center justify-between">
          <span className="font-label-md text-label-md text-on-surface-variant">
            Tools
          </span>
          <button
            type="button"
            onClick={() => onRestoreTools()}
            disabled={disabledTools.length === 0}
            title="Включить все инструменты"
            className="rounded px-1.5 py-0.5 font-label-sm text-label-sm uppercase text-primary transition-colors hover:bg-surface-container-high disabled:opacity-30"
          >
            Restore all
          </button>
        </div>
        <div className="flex max-h-[320px] flex-col gap-2 overflow-y-auto pr-1">
          {tools.length === 0 && (
            <span className="font-label-sm text-label-sm uppercase text-on-surface-variant">
              no tools detected
            </span>
          )}
          {tools.map((tool) => {
            const enabled = !disabledTools.includes(tool.name);
            return (
              <ToolToggle
                key={tool.name}
                tool={tool}
                enabled={enabled}
                core={CORE_TOOLS.has(tool.name)}
                onToggle={() => onToggleTool(tool.name, !enabled)}
              />
            );
          })}
        </div>
      </div>
    </aside>
  );
}

// ============================================================
// DASHBOARD
// ============================================================

export default function Dashboard({ isListening, isSpeaking, setIsSpeaking }: DashboardProps) {
  const chat = useChat(setIsSpeaking, "code");
  const [telemetryOpen, setTelemetryOpen] = useState<Record<number, boolean>>({});
  const [dockVisible, setDockVisible] = useState(true);
  const [voiceMode, setVoiceMode] = useState(false);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [helpful, setHelpful] = useState<Record<number, boolean>>({});
  const [plan, setPlan] = useState<ExecutionPlan | null>(null);
  const [workdir, setWorkdir] = useState("");
  const flowRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLElement>(null);
  const stickToBottomRef = useRef(true);
  const lastUserIdRef = useRef<number | null>(null);
  const modelPickerRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachments, setAttachments] = useState<{ file: FileAttachment; previewUrl: string | null }[]>([]);
  const [dragOver, setDragOver] = useState(false);

  useEffect(() => {
    if (!modelPickerOpen) return;
    function handleClick(e: MouseEvent) {
      const target = e.target as HTMLElement | null;
      if (modelPickerRef.current?.contains(e.target as Node)) return;
      if (target?.closest?.("[data-model-trigger]")) return;
      setModelPickerOpen(false);
    }
    document.addEventListener("click", handleClick);
    return () => document.removeEventListener("click", handleClick);
  }, [modelPickerOpen]);

  useEffect(() => {
    chat.loadConfig();
  }, []);

  useEffect(() => {
    invoke<string>("get_workdir").then((w) => setWorkdir(w)).catch(() => {});
  }, []);

  const chooseWorkdir = useCallback(async () => {
    try {
      const selected = await open({
        directory: true,
        multiple: false,
        title: "Choose workspace folder",
      });
      if (selected && typeof selected === "string") {
        const result = await invoke<string>("set_workdir", { path: selected });
        if (result) setWorkdir(result);
      }
    } catch (e) {
      console.error("Failed to choose workdir:", e);
    }
  }, []);

  // Drag-drop + Ctrl+V paste for file attachments
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const items = Array.from(e.clipboardData?.items || []);
      const files = items
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((f): f is File => f !== null);
      if (files.length > 0) {
        e.preventDefault();
        void handleFiles(files);
      }
    };
    const onDrop = (e: DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      if (e.dataTransfer?.files.length) {
        void handleFiles(Array.from(e.dataTransfer.files));
      }
    };
    const onDragOver = (e: DragEvent) => {
      e.preventDefault();
      setDragOver(true);
    };
    const onDragLeave = () => setDragOver(false);

    const el = document.documentElement;
    el.addEventListener("paste", onPaste);
    el.addEventListener("drop", onDrop);
    el.addEventListener("dragover", onDragOver);
    el.addEventListener("dragleave", onDragLeave);
    return () => {
      el.removeEventListener("paste", onPaste);
      el.removeEventListener("drop", onDrop);
      el.removeEventListener("dragover", onDragOver);
      el.removeEventListener("dragleave", onDragLeave);
    };
  }, []);

  const handleFiles = async (files: File[]) => {
    const valid = files.filter((f) => f.type || f.name.includes("."));
    if (valid.length === 0) return;
    try {
      const uploaded = await uploadFiles(valid);
      setAttachments((prev) => [
        ...prev,
        ...uploaded.map((att, i) => {
          const previewUrl = isImage(att.mime)
            ? URL.createObjectURL(valid[i])
            : null;
          return { file: { ...att, previewUrl }, previewUrl };
        }),
      ]);
    } catch (e) {
      console.error("Failed to upload files:", e);
    }
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => {
      const removed = prev[index];
      if (removed.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      return prev.filter((_, i) => i !== index);
    });
  };

  const clearAttachments = () => {
    attachments.forEach((a) => {
      if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
    });
    setAttachments([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const isImage = (mime?: string) => !!mime && mime.startsWith("image/");

  const handleSend = async () => {
    if (!chat.input.trim() && attachments.length === 0) return;
    if (chat.isExecuting || chat.loading) {
      chat.stop();
      return;
    }
    try {
      await chat.sendWithAttachments(attachments.map((a) => a.file));
    } finally {
      setAttachments([]);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  useEffect(() => {
    let next: ExecutionPlan | null = null;
    for (const ev of chat.taskEvents) {
      if (ev.type !== "plan_updated" && ev.type !== "plan_created") continue;
      next = normalizePlan(ev.steps ?? ev.plan ?? null) || next;
    }
    setPlan(next);
  }, [chat.taskEvents]);

  // Auto-follow the stream, but only while the reader is already at the bottom.
// `scrollIntoView` is deliberately not used: it scrolls every scrollable
// ancestor and jumps the layout on each token, which reads as the whole window
// twitching while the AI writes.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    let lastUserId: number | null = null;
    for (let i = chat.messages.length - 1; i >= 0; i--) {
      if (chat.messages[i].role === "user") {
        lastUserId = chat.messages[i].id;
        break;
      }
    }

    // A brand new user turn: jump to the bottom unconditionally, so the text
    // that was just sent is always on screen even if the user had scrolled up.
    if (lastUserId !== null && lastUserId !== lastUserIdRef.current) {
      lastUserIdRef.current = lastUserId;
      stickToBottomRef.current = true;
      requestAnimationFrame(() => {
        const node = scrollRef.current;
        if (node) node.scrollTop = node.scrollHeight;
      });
      return;
    }
    if (lastUserId === null) lastUserIdRef.current = null;

    if (!stickToBottomRef.current) return;

    const target = el.scrollHeight - el.clientHeight;
    const distance = target - el.scrollTop;
    if (distance <= 1) return;

    el.scrollTo({ top: target, behavior: distance > 240 ? "smooth" : "auto" });
  }, [chat.messages, chat.isExecuting, chat.taskEvents, chat.input]);

  const handleFlowScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 64;
  }, []);

  const lastAssistant = useMemo(() => {
    for (let i = chat.messages.length - 1; i >= 0; i--) {
      if (chat.messages[i].role === "assistant") return chat.messages[i];
    }
    return null;
  }, [chat.messages]);

  const modelLabel =
    (chat.config.selected_primary
      ? `${chat.config.selected_primary.model}`
      : "auto model") || "auto model";
  const providerLabel = chat.config.selected_primary
    ? PROVIDER_LABELS[chat.config.selected_primary.provider as ProviderId] || ""
    : "";
  const modelText = providerLabel ? `${modelLabel} / ${providerLabel}` : modelLabel;

  const latencyMs = lastAssistant?.latencyMs ?? null;
  const contextChars = chat.messages.reduce((acc, m) => acc + (m.text?.length || 0), 0);
  const tps = Math.max(1, Math.round(contextChars / 900));

  const regenerateLast = () => {
    const lastUser = [...chat.messages].reverse().find((m) => m.role === "user");
    if (lastUser) chat.send(lastUser.text);
  };

  return (
    <div
      className="jd-root relative flex h-full min-h-0 bg-background font-body-md text-on-surface antialiased selection:bg-primary-container selection:text-on-primary-container"
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        if (e.dataTransfer?.files.length) {
          void handleFiles(Array.from(e.dataTransfer.files));
        }
      }}
    >
      {dragOver && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: "rgba(0, 240, 255, 0.15)",
            border: "2px dashed rgba(0, 240, 255, 0.5)",
            zIndex: 50,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#00f0ff",
            fontWeight: "bold",
            fontSize: "14px",
            backdropFilter: "blur(4px)",
            pointerEvents: "none",
          }}
        >
          Перетащите файлы сюда
        </div>
      )}
      <button
        type="button"
        data-no-drag
        onClick={() => invoke("restore_desktop").catch(() => {})}
        title="Вернуть основное окно"
        className="absolute right-0 top-1/2 z-40 flex h-20 w-9 -translate-y-1/2 items-center justify-center rounded-l-xl border border-r-0 border-outline-variant/40 bg-surface-container/70 text-on-surface-variant backdrop-blur-xl transition-all hover:bg-surface-container-high hover:text-primary"
      >
        <span className="material-symbols-outlined text-[22px]">keyboard_double_arrow_right</span>
      </button>
      <Sidebar
        onOpenHistory={() => {
          setHistoryOpen(true);
          chat.refreshHistory();
        }}
        onOpenSettings={() => invoke("show_settings").catch(() => {})}
      />

      <HistoryDrawer
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        items={chat.history}
        loading={chat.historyLoading}
        activeId={activeConversationId}
        onSelect={async (id) => {
          setActiveConversationId(id);
          await chat.loadConversation(id);
          setHistoryOpen(false);
        }}
        onDelete={(id) => {
          chat.removeConversation(id);
          if (activeConversationId === id) setActiveConversationId(null);
        }}
        onNew={() => {
          chat.reset();
          setActiveConversationId(null);
          setHistoryOpen(false);
        }}
      />

      <div className="flex min-w-0 flex-1 flex-col pl-20">
        <Header
          modelLabel={chat.configLoaded ? modelLabel : "loading…"}
          isListening={isListening}
          isSpeaking={isSpeaking}
          latencyMs={latencyMs}
          contextChars={contextChars}
          tps={tps}
        />

        <div className="relative z-30 mx-auto w-full max-w-[1580px] px-margin md:px-gutter">
              <HudStrip
                modelLabel={chat.configLoaded ? modelText : "…"}
                isListening={isListening}
                isSpeaking={isSpeaking}
                isExecuting={chat.isExecuting}
                latencyMs={latencyMs}
                contextChars={contextChars}
                tps={tps}
                dockVisible={dockVisible}
                onToggleDock={() => setDockVisible((v) => !v)}
                onClearContext={chat.reset}
                onToggleModels={() => setModelPickerOpen((v) => !v)}
              />
              {modelPickerOpen && (
                <div className="absolute right-0 top-full z-50 mt-2 w-[320px]" ref={modelPickerRef}>
                  <ModelSelector
                    hideTrigger
                    forceOpen
                    onOpenChange={setModelPickerOpen}
                    primaryModel={chat.config.selected_primary}
                    onPrimaryChange={chat.handlePrimaryChange}
                    apiKeys={chat.config.api_keys}
                    onApiKeyChange={chat.updateApiKey}
                  />
                </div>
              )}
            </div>

            <main className="relative min-h-0 flex-1 overflow-hidden bg-background">
              <div className="mx-auto flex h-full w-full max-w-[1580px] flex-col gap-space-md px-margin py-space-md md:px-gutter">
                <div
                  className={`grid min-h-0 flex-1 grid-cols-1 gap-space-md dash:grid-cols-12 dash:grid-rows-1 ${
                    dockVisible ? "grid-rows-[minmax(0,1fr)_auto]" : "grid-rows-1"
                  }`}
                >
                  <section className="flex min-w-0 flex-col gap-space-lg dash:col-span-8 dash:min-h-0">
                    <div
                      ref={scrollRef}
                      onScroll={handleFlowScroll}
                      className="no-scroll-anchor flex min-h-0 flex-1 flex-col gap-space-lg overflow-y-auto"
                    >
                      <WelcomeNode />

                      <div ref={flowRef} id="chatMessageFlow" className="flex shrink-0 flex-col gap-space-lg">
                        {chat.messages.length === 0 && (
                          <div className="rounded-lg border border-outline-variant/20 px-space-md py-space-lg text-center font-body-sm text-body-sm text-on-surface-variant">
                            awaiting directive
                          </div>
                        )}

                        {chat.messages.map((msg) =>
                          msg.role === "user" ? (
                            <UserMessage key={msg.id} text={msg.text} createdAt={msg.createdAt} />
                          ) : (
                            <AssistantMessage
                              key={msg.id}
                              message={msg}
                              busy={chat.isExecuting && msg.id === lastAssistant?.id && !msg.text}
                              open={telemetryOpen[msg.id] ?? true}
                              onToggle={() =>
                                setTelemetryOpen((prev) => ({ ...prev, [msg.id]: !(prev[msg.id] ?? true) }))
                              }
                              helpful={!!helpful[msg.id]}
                              onHelpful={() => setHelpful((prev) => ({ ...prev, [msg.id]: !prev[msg.id] }))}
                              onRegenerate={regenerateLast}
                            />
                          ),
                        )}
                      </div>
                    </div>

                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  onChange={(e) => {
                    if (e.target?.files?.length) {
                      void handleFiles(Array.from(e.target.files));
                    }
                  }}
                  style={{ display: "none" }}
                />

                {attachments.length > 0 && (
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      gap: "6px",
                      marginBottom: "8px",
                      padding: "0 4px",
                      alignItems: "center",
                    }}
                  >
                    {attachments.map(({ file: att, previewUrl }, idx) => {
                      const img = isImage(att.mime);
                      return (
                        <div
                          key={idx}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: "6px",
                            padding: "6px 10px",
                            borderRadius: "8px",
                            background: img
                              ? "rgba(0, 240, 255, 0.12)"
                              : "rgba(255, 255, 255, 0.06)",
                            border: `1px solid ${
                              img
                                ? "rgba(0, 240, 255, 0.35)"
                                : "rgba(255, 255, 255, 0.12)"
                            }`,
                            fontSize: "11px",
                            color: "var(--on-surface-variant)",
                            maxWidth: "200px",
                          }}
                        >
                          {img && previewUrl ? (
                            <img
                              src={previewUrl}
                              alt={att.name}
                              style={{
                                width: "32px",
                                height: "32px",
                                borderRadius: "4px",
                                objectFit: "cover",
                                border: "1px solid rgba(255,255,255,0.2)",
                              }}
                            />
                          ) : img ? (
                            <ImageIcon
                              size={12}
                              style={{ color: "rgba(0, 240, 255, 0.8)" }}
                            />
                          ) : null}
                          <span title={att.name} style={{ maxWidth: "120px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {att.name}
                          </span>
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              removeAttachment(idx);
                            }}
                            style={{
                              background: "none",
                              border: "none",
                              cursor: "pointer",
                              padding: "2px",
                              color: "var(--on-surface-variant)",
                              opacity: 0.7,
                            }}
                            title="Удалить вложение"
                          >
                            ✕
                          </button>
                        </div>
                      );
                    })}
                    <button
                      type="button"
                      onClick={clearAttachments}
                      style={{
                        background: "none",
                        border: "none",
                        cursor: "pointer",
                        padding: "4px",
                        color: "var(--on-surface-variant)",
                        opacity: 0.6,
                        fontSize: "10px",
                        textTransform: "uppercase",
                      }}
                    >
                      Очистить всё
                    </button>
                  </div>
                )}

                <PromptDock
                  value={chat.input}
                  onChange={chat.setInput}
                  onSubmit={handleSend}
                  loading={chat.loading}
                  voiceMode={voiceMode}
                  onToggleVoice={() => setVoiceMode((v) => !v)}
                  workdir={workdir}
                  onChooseWorkdir={chooseWorkdir}
                  canStop={!!(chat.isExecuting || chat.loading)}
                  onStop={() => chat.stop()}
                  onAttachFile={() => fileInputRef.current?.click()}
                  hasAttachments={attachments.length > 0}
                />
                  </section>

              {dockVisible && (
                <SideDock
                  plan={plan}
                  isExecuting={chat.isExecuting}
                  tools={chat.tools}
                  disabledTools={chat.config.disabled_tools || []}
                  onToggleTool={chat.setToolEnabled}
                  onRestoreTools={chat.restoreAllTools}
                />
              )}
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}