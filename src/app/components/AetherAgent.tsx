import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import AetherModelModal from "./AetherModelModal";
import Markdown from "./Markdown";
import {
  cleanText,
  formatBytes,
  useChat,
  type ActivityEntry,
  type FileAttachment,
  type Message,
  type ToolProgress,
} from "../useChat";
import { uploadFiles } from "../api";
import { startAudioCapture, stopAudioCapture } from "../audioCapture";
import { normalizePlan, type ExecutionPlan } from "./TaskList";
import { PROVIDER_LABELS, type ProviderId } from "../models";

interface AetherAgentProps {
  isSpeaking: boolean;
  setIsSpeaking: (v: boolean) => void;
}

// ============================================================
// HELPERS
// ============================================================

function clockOf(ts: number) {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtDuration(ms?: number) {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function baseName(p: string) {
  if (!p) return "";
  const parts = p.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

function fmtAgo(updatedSec: number) {
  if (!updatedSec) return "";
  const diff = Math.max(0, Date.now() / 1000 - updatedSec);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

const STATUS_DOT: Record<ActivityEntry["status"], string> = {
  running: "bg-amber-400 animate-pulse",
  done: "bg-emerald-400",
  failed: "bg-rose-400",
};

// ============================================================
// TOOL RENDERING (Cursor-style inline rows)
// ============================================================

/** Material-symbol glyph per backend tool id, so each action reads at a glance. */
const TOOL_ICONS: Record<string, string> = {
  read_file: "description",
  read: "description",
  write_file: "note_add",
  edit_file: "edit_document",
  update_plan: "checklist",
  shell: "terminal",
  run_cmd: "terminal",
  run_powershell: "terminal",
  web_search: "travel_explore",
  search_web: "travel_explore",
  grep: "search",
  search_content: "search",
  search_files: "folder_open",
  find_file: "folder_open",
  git_status: "commit",
  git_commit: "commit",
  get_tools: "handyman",
  get_tools_main: "handyman",
  disk_tree: "account_tree",
};

function toolIcon(tool?: string) {
  return (tool && TOOL_ICONS[tool]) || "bolt";
}

/** A CSS ring, not a glyph — immune to the icon-font sizing quirks. */
function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-amber-300/25 border-t-amber-300 ${className}`}
    />
  );
}

function ToolStatusIcon({ status }: { status: ActivityEntry["status"] }) {
  if (status === "running") return <Spinner />;
  return (
    <span
      className={`material-symbols-outlined text-[16px] ${
        status === "failed" ? "text-rose-400" : "text-emerald-400"
      }`}
    >
      {status === "failed" ? "error" : "check_circle"}
    </span>
  );
}

function ProgressBar({ p }: { p: ToolProgress }) {
  const pct = Math.max(0, Math.min(100, Math.round(p.percent)));
  const stats: string[] = [`${pct}%`];
  if (p.nodes > 0) stats.push(`${p.nodes.toLocaleString("en-US")} nodes`);
  if (p.bytes > 0) stats.push(formatBytes(p.bytes));
  if (p.queued > 0) stats.push(`${p.queued} queued`);
  stats.push(`${p.elapsed.toFixed(1)}s`);
  return (
    <div className="mt-2 flex flex-col gap-1.5">
      <div className="h-1 w-full overflow-hidden rounded-full bg-[#1B1E24]">
        <div
          className="h-full rounded-full bg-amber-400 transition-[width] duration-300 ease-out"
          style={{ width: `${Math.max(2, pct)}%` }}
        />
      </div>
      <div className="flex flex-wrap items-center gap-x-2 font-mono text-[10px] text-[#6E7480]">
        {stats.map((s) => (
          <span key={s}>{s}</span>
        ))}
      </div>
    </div>
  );
}

/**
 * One tool call in the timeline. Mirrors Cursor's inline tool rows: a glyph,
 * the action, a live status, and an expandable body with the command/output.
 * The body auto-opens while the tool runs and folds away once it settles,
 * unless the reader has toggled it by hand.
 */
function ToolRow({ entry }: { entry: ActivityEntry }) {
  const [manual, setManual] = useState<boolean | null>(null);
  const hasDetail = Boolean(entry.command || entry.output);
  const open = manual ?? entry.status === "running";

  return (
    <div className="overflow-hidden rounded-lg border border-[#22252C] bg-[#0E1013]">
      <button
        type="button"
        onClick={() => hasDetail && setManual(!open)}
        className={`flex w-full items-center gap-2.5 px-3 py-2 text-left ${
          hasDetail ? "transition-colors hover:bg-[#14161B]" : "cursor-default"
        }`}
      >
        <span className="material-symbols-outlined text-[16px] text-[#8A8F98]">
          {toolIcon(entry.tool)}
        </span>
        <span
          className={`min-w-0 flex-1 truncate font-mono text-[12px] ${
            entry.status === "failed" ? "text-rose-300" : "text-[#ECEEF2]"
          }`}
        >
          {entry.text}
        </span>
        {entry.duration != null && (
          <span className="shrink-0 font-mono text-[10px] text-[#555B66]">
            {fmtDuration(entry.duration)}
          </span>
        )}
        <ToolStatusIcon status={entry.status} />
        {hasDetail && (
          <span className="material-symbols-outlined text-[15px] text-[#555B66]">
            {open ? "expand_less" : "expand_more"}
          </span>
        )}
      </button>

      {entry.progress && (
        <div className="px-3 pb-2.5">
          <ProgressBar p={entry.progress} />
        </div>
      )}

      {open && hasDetail && (
        <div className="border-t border-[#22252C] px-3 py-2">
          {entry.command && (
            <pre className="whitespace-pre-wrap break-words font-mono text-[11px] text-emerald-300">
              $ {entry.command}
            </pre>
          )}
          {entry.output && (
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-[#8A8F98]">
              {entry.output}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================
// MESSAGES
// ============================================================

function UserBubble({ message }: { message: Message }) {
  return (
    <div className="rounded-lg border border-[#22252C] bg-[#14161B] p-3.5 text-[13.5px] leading-relaxed">
      <div className="mb-1.5 flex items-center justify-between font-mono text-[11px] text-[#6E7480]">
        <span className="font-semibold text-[#8A8F98]">YOU</span>
        <span>{clockOf(message.createdAt)}</span>
      </div>
      <p className="whitespace-pre-wrap break-words text-[#ECEEF2]">
        {message.text}
      </p>
      {message.attachments && message.attachments.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {message.attachments.map((a, i) => (
            <span
              key={`${a.path}-${i}`}
              className="rounded border border-[#22252C] bg-[#0E1013] px-1.5 py-0.5 font-mono text-[10px] text-[#8A8F98]"
            >
              {a.name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function SayRow({ text }: { text: string }) {
  const clean = cleanText(text);
  if (!clean.trim()) return null;
  return (
    <div className="text-[13.5px] leading-relaxed text-[#ECEEF2]">
      <Markdown text={clean} />
    </div>
  );
}

/** Bottom-of-turn liveness indicator — the chat is never silently "finished". */
function Working({ label = "Working" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 font-mono text-[12px] text-[#8A8F98]">
      <Spinner />
      <span>{label}</span>
      <span className="animate-pulse">…</span>
    </div>
  );
}

type TimelineItem =
  | { kind: "say"; key: string; text: string }
  | { kind: "tool"; key: string; entry: ActivityEntry };

/**
 * Rebuild the true order of an agent turn: `text → tools → text → tools …`.
 *
 * The backend streams prose per iteration (1-based) and every activity entry is
 * tagged with the iteration it happened in, so grouping both by iteration
 * restores the order the agent actually worked in instead of dumping all text
 * first and all tools after.
 */
function buildTimeline(message: Message): TimelineItem[] {
  const parts =
    message.parts && message.parts.length
      ? message.parts
      : message.text
        ? [message.text]
        : [];
  const activity = message.activity || [];
  const maxIter = Math.max(
    parts.length - 1,
    ...activity.map((a) => a.iteration ?? 0),
  );

  const items: TimelineItem[] = [];
  for (let i = 0; i <= maxIter; i++) {
    const p = parts[i];
    if (p && p.trim()) items.push({ kind: "say", key: `p-${i}`, text: p });
    for (const a of activity) {
      if ((a.iteration ?? 0) !== i) continue;
      items.push(
        a.kind === "say"
          ? { kind: "say", key: `a-${a.id}`, text: a.text }
          : { kind: "tool", key: `a-${a.id}`, entry: a },
      );
    }
  }
  return items;
}

function AgentTurn({ message, busy }: { message: Message; busy: boolean }) {
  const items = useMemo(
    () => buildTimeline(message),
    [message.parts, message.activity, message.text],
  );
  const text = cleanText(message.text || "");
  const hasContent = items.length > 0 || Boolean(message.error);

  return (
    <div className="flex flex-col gap-3">
      {items.map((it) =>
        it.kind === "say" ? (
          <SayRow key={it.key} text={it.text} />
        ) : (
          <ToolRow key={it.key} entry={it.entry} />
        ),
      )}

      {message.error && (
        <div className="whitespace-pre-wrap break-words rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 font-mono text-[12px] text-rose-300">
          {message.error}
        </div>
      )}

      {busy && <Working />}

      {!busy && !hasContent && (
        <span className="font-mono text-[11px] uppercase tracking-wide text-[#6E7480]">
          (no output)
        </span>
      )}

      {!busy && text && (
        <div className="flex items-center gap-3 font-mono text-[10px] text-[#555B66]">
          <span>{clockOf(message.createdAt)}</span>
          {message.latencyMs != null && <span>{fmtDuration(message.latencyMs)}</span>}
          <button
            type="button"
            onClick={() => navigator.clipboard?.writeText(text)}
            className="ml-auto flex items-center gap-1 text-[#6E7480] transition-colors hover:text-[#ECEEF2]"
          >
            <span className="material-symbols-outlined text-[13px]">content_copy</span>
            Copy
          </button>
        </div>
      )}
    </div>
  );
}

// ============================================================
// AETHER AGENT
// ============================================================

export default function AetherAgent({
  isSpeaking,
  setIsSpeaking,
}: AetherAgentProps) {
  const chat = useChat(setIsSpeaking, "code");
  // useChat returns a fresh object each render; keep the voice callbacks bound
  // to the latest instance without re-subscribing on every keystroke.
  const chatRef = useRef(chat);
  chatRef.current = chat;

  const [modelOpen, setModelOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [tab, setTab] = useState<"activity" | "terminal" | "plan">("activity");
  const [workdir, setWorkdir] = useState("");
  const [voiceActive, setVoiceActive] = useState(false);
  const [voiceError, setVoiceError] = useState("");
  const [attachments, setAttachments] = useState<FileAttachment[]>([]);
  const [dragOver, setDragOver] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ---- init ---------------------------------------------------------------
  useEffect(() => {
    chat.loadConfig();
    chat.refreshHistory();
    if (chat.conversationId) void chat.loadConversation(chat.conversationId);
    invoke<string>("get_workdir")
      .then((w) => setWorkdir(w))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- new thread shortcut (Ctrl/Cmd+K) -----------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        chat.reset();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [chat.reset]);

  // ---- auto-scroll --------------------------------------------------------
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [chat.messages, chat.isExecuting]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 64;
  }, []);

  // ---- workdir ------------------------------------------------------------
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

  // ---- attachments --------------------------------------------------------
  const addFiles = useCallback(async (files: File[]) => {
    const valid = files.filter((f) => f.type || f.name.includes("."));
    if (valid.length === 0) return;
    try {
      const uploaded = await uploadFiles(valid);
      setAttachments((prev) => [...prev, ...uploaded]);
    } catch (e) {
      console.error("Failed to upload files:", e);
    }
  }, []);

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = Array.from(e.clipboardData?.items || [])
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((f): f is File => f !== null);
      if (files.length > 0) {
        e.preventDefault();
        void addFiles(files);
      }
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  }, [addFiles]);

  // ---- send / stop --------------------------------------------------------
  const handleSend = useCallback(async () => {
    if (chat.loading) {
      chat.stop();
      return;
    }
    if (!chat.input.trim() && attachments.length === 0) return;
    const atts = attachments;
    setAttachments([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
    await chat.sendWithAttachments(atts);
  }, [chat, attachments]);

  // ---- voice (capture only; recognition and endpointing are backend-owned) --
  const voiceFinalizedRef = useRef(false);

  /** One authoritative final transcript per utterance, manual or VAD-triggered. */
  const handleVoiceFinal = useCallback((text: string) => {
    if (voiceFinalizedRef.current) return;
    voiceFinalizedRef.current = true;
    setVoiceActive(false);
    const t = (text || "").trim();
    if (!t) {
      setVoiceError("Речь не распознана — попробуйте ещё раз");
      return;
    }
    setVoiceError("");
    chatRef.current.setInput((prev) => (prev ? `${prev} ${t}` : t));
  }, []);

  const toggleVoice = useCallback(async () => {
    try {
      if (!voiceActive) {
        setVoiceError("");
        voiceFinalizedRef.current = false;
        await startAudioCapture({
          onFinal: (text) => handleVoiceFinal(text),
          onError: (message) => {
            if (voiceFinalizedRef.current) return;
            voiceFinalizedRef.current = true;
            setVoiceActive(false);
            setVoiceError(message);
          },
        });
        setVoiceActive(true);
      } else {
        // Requesting stop is enough: the backend replies with `final`, which
        // handleVoiceFinal turns into input text exactly once.
        await stopAudioCapture();
      }
    } catch (e: any) {
      if (voiceFinalizedRef.current) return;
      voiceFinalizedRef.current = true;
      setVoiceActive(false);
      const msg = e?.message || String(e);
      setVoiceError(msg);
      console.error("[AetherAgent] voice error:", e);
    }
  }, [voiceActive, handleVoiceFinal]);

  // ---- derived ------------------------------------------------------------
  const modelLabel = chat.config.selected_primary?.model || "—";
  const providerLabel = chat.config.selected_primary
    ? PROVIDER_LABELS[chat.config.selected_primary.provider as ProviderId] || ""
    : "";

  const activityRows = useMemo(() => {
    const rows: { key: string; entry: ActivityEntry }[] = [];
    for (const m of chat.messages) {
      for (const a of m.activity || []) rows.push({ key: `${m.id}-${a.id}`, entry: a });
    }
    return rows;
  }, [chat.messages]);

  const toolCount = activityRows.filter((r) => r.entry.kind === "tool").length;
  const failedCount = activityRows.filter((r) => r.entry.status === "failed").length;

  const terminalLines = useMemo(() => {
    const lines: { key: string; kind: "cmd" | "out" | "err"; text: string }[] = [];
    for (const m of chat.messages) {
      for (const a of m.activity || []) {
        if (a.command) lines.push({ key: `${m.id}-${a.id}-c`, kind: "cmd", text: a.command });
        if (a.output) {
          lines.push({
            key: `${m.id}-${a.id}-o`,
            kind: a.status === "failed" ? "err" : "out",
            text: a.output,
          });
        }
      }
    }
    return lines;
  }, [chat.messages]);

  const plan: ExecutionPlan | null = useMemo(() => {
    let next: ExecutionPlan | null = null;
    for (const ev of chat.taskEvents) {
      if (ev.type !== "plan_updated" && ev.type !== "plan_created") continue;
      next = normalizePlan(ev.steps ?? ev.plan ?? null) || next;
    }
    return next;
  }, [chat.taskEvents]);

  const statusLabel = isSpeaking
    ? "SPEAKING"
    : voiceActive
      ? "LISTENING"
      : chat.isExecuting
        ? "RUNNING"
        : chat.configLoaded
          ? "READY"
          : "…";

  // =========================================================================

  return (
    <div
      data-aether
      className="flex h-screen w-screen flex-col overflow-hidden bg-[#090A0C] font-body-md text-[13.5px] text-[#ECEEF2] antialiased selection:bg-blue-500/30 selection:text-blue-200"
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        if (e.dataTransfer?.files.length) void addFiles(Array.from(e.dataTransfer.files));
      }}
    >
      {/* ============================ TOP BAR ============================ */}
      <header className="flex h-11 shrink-0 select-none items-center justify-between border-b border-[#22252C] bg-[#0E1013] px-4 text-xs">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 font-mono font-medium text-[#ECEEF2]">
            <span className="h-2 w-2 rounded-sm bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]" />
            <span className="tracking-wider">AETHER</span>
          </div>
          <span className="text-[#22252C]">/</span>
          <div className="flex items-center gap-1.5 text-[#8A8F98]">
            <span className="max-w-[220px] truncate font-mono text-[#ECEEF2]">
              {workdir ? baseName(workdir) : "no-workspace"}
            </span>
            <span
              className={`rounded border border-[#22252C] bg-[#14161B] px-1.5 py-0.5 font-mono text-[10px] ${
                chat.isExecuting ? "text-amber-300" : "text-[#6E7480]"
              }`}
            >
              {statusLabel}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setModelOpen(true)}
            title={providerLabel ? `${modelLabel} · ${providerLabel}` : modelLabel}
            className="group flex items-center gap-2 rounded border border-[#22252C] bg-[#14161B] px-2.5 py-1 font-mono text-[11px] text-[#ECEEF2] transition hover:bg-[#1B1E24]"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-purple-400" />
            <span className="max-w-[220px] truncate font-medium">{modelLabel}</span>
            <span className="text-[10px] text-[#6E7480] transition-colors group-hover:text-[#ECEEF2]">
              ▾
            </span>
          </button>

          <div className="hidden items-center gap-2 rounded border border-[#22252C] bg-[#14161B] px-2.5 py-1 font-mono text-[11px] text-[#8A8F98] sm:flex">
            <span>MSGS</span>
            <span className="text-[#ECEEF2]">{chat.messages.length}</span>
            <span className="text-[#22252C]">·</span>
            <span>TOOLS</span>
            <span className="text-[#ECEEF2]">{toolCount}</span>
          </div>

          <button
            type="button"
            onClick={() => setInspectorOpen((v) => !v)}
            className="flex items-center gap-1.5 rounded border border-[#22252C] bg-[#14161B] px-2.5 py-1 font-mono text-[11px] text-[#ECEEF2] transition hover:bg-[#1B1E24]"
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                inspectorOpen ? "bg-emerald-400" : "bg-[#6E7480]"
              }`}
            />
            <span>Inspector: {inspectorOpen ? "ON" : "OFF"}</span>
          </button>
        </div>
      </header>

      {/* ========================== WORKSPACE =========================== */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* ------------------------- LEFT SIDEBAR ------------------------- */}
        <aside className="hidden w-60 shrink-0 select-none flex-col justify-between border-r border-[#22252C] bg-[#0E1013] p-2.5 md:flex">
          <div className="flex min-h-0 flex-col gap-4">
            <button
              type="button"
              onClick={() => chat.reset()}
              className="flex w-full items-center justify-between rounded-md border border-[#22252C] bg-[#14161B] px-2.5 py-1.5 text-xs font-medium text-[#ECEEF2] transition hover:bg-[#1B1E24]"
            >
              <span className="flex items-center gap-1.5">
                <span className="material-symbols-outlined text-[15px] text-[#8A8F98]">add</span>
                New Thread
              </span>
              <kbd className="rounded border border-[#22252C] bg-[#090A0C] px-1.5 py-0.5 text-[10px] text-[#8A8F98]">
                ⌘K
              </kbd>
            </button>

            <div className="flex min-h-0 flex-col">
              <div className="mb-1.5 px-2 font-mono text-[10px] uppercase tracking-wider text-[#6E7480]">
                Active Sessions
              </div>
              <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto text-xs">
                {chat.history.length === 0 && (
                  <span className="px-2.5 py-1.5 text-[#555B66]">
                    {chat.historyLoading ? "loading…" : "no saved threads"}
                  </span>
                )}
                {chat.history.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => void chat.loadConversation(c.id)}
                    className="truncate rounded-md px-2.5 py-1.5 text-left text-[#8A8F98] transition hover:bg-[#14161B]/60 hover:text-[#ECEEF2]"
                  >
                    <span className="block truncate">{c.title || "Untitled"}</span>
                    <span className="font-mono text-[10px] text-[#555B66]">
                      {c.turns} turn{c.turns === 1 ? "" : "s"} · {fmtAgo(c.updated)}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={chooseWorkdir}
            title={workdir ? `Workspace: ${workdir}` : "Choose workspace folder"}
            className="space-y-1 rounded-md border border-[#22252C] bg-[#090A0C] p-2.5 text-left font-mono text-[11px] transition hover:border-[#4A505E]"
          >
            <div className="flex items-center justify-between text-[#6E7480]">
              <span>WORKSPACE</span>
              <span className="font-semibold text-emerald-400">{statusLabel}</span>
            </div>
            <span className="block truncate font-medium text-[#ECEEF2]">
              {workdir || "choose folder…"}
            </span>
            <div className="text-[10px] text-[#6E7480]">
              {workdir ? baseName(workdir) : "code agent"}
            </div>
          </button>
        </aside>

        {/* ------------------------ CENTER CHAT -------------------------- */}
        <section className="flex min-w-0 flex-1 flex-col bg-[#090A0C]">
          <div
            ref={scrollRef}
            onScroll={onScroll}
            className="no-scroll-anchor flex-1 overflow-y-auto px-6 py-6"
          >
            <div className="mx-auto flex max-w-2xl flex-col gap-6">
              {chat.messages.length === 0 && (
                <div className="mt-16 flex flex-col items-center gap-2 text-center">
                  <span className="material-symbols-outlined text-[28px] text-[#22252C]">
                    terminal
                  </span>
                  <p className="font-mono text-[12px] text-[#6E7480]">
                    Ask the agent to edit code, run shell commands, or run tests.
                  </p>
                </div>
              )}
              {chat.messages.map((m) =>
                m.role === "user" ? (
                  <UserBubble key={m.id} message={m} />
                ) : (
                  <AgentTurn
                    key={m.id}
                    message={m}
                    busy={chat.loading && m.id === chat.messages[chat.messages.length - 1]?.id}
                  />
                ),
              )}
            </div>
          </div>

          {/* ------------------------- INPUT DOCK ------------------------- */}
          <div className="bg-[#090A0C] p-4 pt-2">
            <div className="mx-auto max-w-2xl rounded-xl border border-[#22252C] bg-[#14161B] shadow-2xl transition focus-within:border-[#4A505E]">
              {attachments.length > 0 && (
                <div className="flex flex-wrap gap-1.5 border-b border-[#22252C]/80 px-3 py-2">
                  {attachments.map((a, i) => (
                    <span
                      key={`${a.path}-${i}`}
                      className="flex items-center gap-1 rounded border border-[#22252C] bg-[#0E1013] px-1.5 py-0.5 font-mono text-[10px] text-[#8A8F98]"
                    >
                      {a.name}
                      <button
                        type="button"
                        onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                        className="text-[#6E7480] transition hover:text-rose-300"
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}

              {voiceError && (
                <div className="mb-2 break-words font-mono text-[10px] text-rose-300">
                  {voiceError}
                </div>
              )}

              <textarea
                value={chat.input}
                onChange={(e) => chat.setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void handleSend();
                  }
                }}
                rows={2}
                placeholder="Ask agent to edit code, execute shell commands, or run tests…"
                className="w-full resize-none bg-transparent px-3.5 pb-2 pt-3 text-[13.5px] text-[#ECEEF2] placeholder-[#555B66] focus:outline-none"
              />

              <div className="flex items-center justify-between border-t border-[#22252C]/80 px-3 py-2 font-mono text-[11px] text-[#8A8F98]">
                <div className="flex flex-wrap items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => setInspectorOpen((v) => !v)}
                    className="flex items-center gap-1.5 rounded border border-[#22252C] bg-[#0E1013] px-2.5 py-1 text-[11.5px] text-[#ECEEF2] transition hover:bg-[#14161B]"
                  >
                    <span className="material-symbols-outlined text-[14px] text-[#8A8F98]">
                      add
                    </span>
                    <span className="font-body-md font-medium">Inspector</span>
                  </button>

                  <button
                    type="button"
                    onClick={() => setModelOpen(true)}
                    className="flex items-center gap-1 rounded-md border border-[#22252C] bg-[#0E1013] px-2.5 py-1 text-[11.5px] text-[#ECEEF2] transition hover:bg-[#14161B]"
                  >
                    <span className="font-body-md text-[#8A8F98]">Model</span>
                    <span className="text-[#6E7480]">/</span>
                    <span className="max-w-[180px] truncate font-body-md font-medium">
                      {modelLabel}
                    </span>
                  </button>

                  <button
                    type="button"
                    onClick={toggleVoice}
                    className={`flex items-center gap-1.5 rounded border px-2.5 py-1 text-[11.5px] transition ${
                      voiceActive
                        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                        : "border-[#22252C] bg-[#0E1013] text-[#8A8F98] hover:text-[#ECEEF2]"
                    }`}
                  >
                    <span className="font-body-md">{voiceActive ? "Listening…" : "Voice"}</span>
                    <span className="material-symbols-outlined text-[13px]">
                      {voiceActive ? "mic" : "keyboard_arrow_down"}
                    </span>
                  </button>
                </div>

                <div className="flex items-center gap-2">
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    hidden
                    onChange={(e) => {
                      if (e.target.files?.length) void addFiles(Array.from(e.target.files));
                    }}
                  />
                  <button
                    type="button"
                    title="Attach file"
                    onClick={() => fileInputRef.current?.click()}
                    className="flex h-8 w-8 items-center justify-center rounded-lg text-[#8A8F98] transition hover:bg-[#0E1013] hover:text-[#ECEEF2]"
                  >
                    <span className="material-symbols-outlined text-[18px]">attach_file</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleSend()}
                    disabled={
                      !chat.loading && !chat.input.trim() && attachments.length === 0
                    }
                    className={`flex h-8 items-center gap-1.5 rounded-lg px-3.5 font-body-md text-[12px] font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${
                      chat.loading
                        ? "bg-rose-500/90 text-white hover:brightness-110"
                        : "bg-[#ECEEF2] text-[#090A0C] hover:bg-white"
                    }`}
                  >
                    {chat.loading ? "Stop" : "Run"}
                    <span className="material-symbols-outlined text-[16px]">
                      {chat.loading ? "stop_circle" : "send"}
                    </span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* --------------------- RIGHT INSPECTOR ------------------------- */}
        {inspectorOpen && (
          <aside className="hidden w-[44%] max-w-2xl shrink-0 flex-col border-l border-[#22252C] bg-[#0E1013] lg:flex">
            <div className="flex h-10 select-none items-center justify-between border-b border-[#22252C] px-3 text-xs font-mono">
              <div className="flex items-center gap-1">
                {(
                  [
                    ["activity", "Activity"],
                    ["terminal", "Terminal"],
                    ["plan", "Plan"],
                  ] as const
                ).map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setTab(id)}
                    className={`rounded px-2.5 py-1 transition ${
                      tab === id
                        ? "border border-[#22252C] bg-[#14161B] text-[#ECEEF2]"
                        : "text-[#8A8F98] hover:text-[#ECEEF2]"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={() => {
                  const el = document.querySelector<HTMLElement>("[data-aether-inspector]");
                  navigator.clipboard?.writeText(el?.innerText || "");
                }}
                className="rounded px-2 py-1 text-[11px] text-[#8A8F98] transition hover:bg-[#14161B] hover:text-[#ECEEF2]"
              >
                Copy
              </button>
            </div>

            <div className="flex select-none items-center justify-between border-b border-[#22252C] bg-[#090A0C] px-3.5 py-1.5 font-mono text-[11px] text-[#6E7480]">
              <span className="truncate text-[#ECEEF2]">
                {workdir || "no workspace"}
              </span>
              <span>
                {toolCount} tool{toolCount === 1 ? "" : "s"} · UTF-8
              </span>
            </div>

            <div
              data-aether-inspector
              className="flex-1 select-text overflow-auto py-2 font-mono text-[12px] leading-6"
            >
              {/* Activity */}
              {tab === "activity" &&
                (activityRows.length === 0 ? (
                  <EmptyInspector label="No tool activity yet." />
                ) : (
                  <div className="flex flex-col">
                    {activityRows.map(({ key, entry }) => (
                      <div key={key} className="flex items-start gap-2.5 px-3.5 py-1">
                        <span
                          className={`mt-2 h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_DOT[entry.status]}`}
                        />
                        <div className="flex min-w-0 flex-1 flex-col">
                          <span
                            className={
                              entry.status === "failed" ? "text-rose-300" : "text-[#ECEEF2]"
                            }
                          >
                            {entry.text}
                          </span>
                          {entry.command && (
                            <span className="truncate text-[#6E7480]">$ {entry.command}</span>
                          )}
                        </div>
                        <span className="shrink-0 text-[#555B66]">
                          {entry.status === "running" ? "running" : fmtDuration(entry.duration)}
                        </span>
                      </div>
                    ))}
                  </div>
                ))}

              {/* Terminal */}
              {tab === "terminal" &&
                (terminalLines.length === 0 ? (
                  <EmptyInspector label="No shell output yet." />
                ) : (
                  <div className="flex flex-col px-3.5">
                    {terminalLines.map((l) => (
                      <pre
                        key={l.key}
                        className={`whitespace-pre-wrap break-words ${
                          l.kind === "cmd"
                            ? "text-emerald-300"
                            : l.kind === "err"
                              ? "text-rose-300"
                              : "text-[#8A8F98]"
                        }`}
                      >
                        {l.kind === "cmd" ? `$ ${l.text}` : l.text}
                      </pre>
                    ))}
                  </div>
                ))}

              {/* Plan */}
              {tab === "plan" &&
                (!plan || plan.tasks.length === 0 ? (
                  <EmptyInspector label="No active plan." />
                ) : (
                  <div className="flex flex-col gap-1 px-3.5 py-1">
                    {plan.tasks.map((t) => {
                      const tone =
                        t.status === "completed"
                          ? "text-emerald-400"
                          : t.status === "failed"
                            ? "text-rose-400"
                            : t.status === "in_progress"
                              ? "text-amber-300"
                              : "text-[#6E7480]";
                      const mark =
                        t.status === "completed"
                          ? "✓"
                          : t.status === "failed"
                            ? "✕"
                            : t.status === "in_progress"
                              ? "▸"
                              : "•";
                      return (
                        <div key={t.id} className="flex items-start gap-2.5">
                          <span className={`w-3 shrink-0 text-center ${tone}`}>{mark}</span>
                          <span
                            className={`${tone} ${
                              t.status === "completed" ? "line-through opacity-70" : ""
                            }`}
                          >
                            {t.title}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                ))}
            </div>

            <div className="flex h-11 select-none items-center justify-between border-t border-[#22252C] bg-[#0E1013] px-4 font-mono text-xs">
              <span className="flex items-center gap-1.5 text-[#6E7480]">
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    failedCount > 0 ? "bg-rose-400" : "bg-emerald-400"
                  }`}
                />
                <span>
                  {failedCount} error{failedCount === 1 ? "" : "s"} · {toolCount} call
                  {toolCount === 1 ? "" : "s"}
                </span>
              </span>
              <button
                type="button"
                onClick={() => chat.stop()}
                disabled={!chat.loading}
                className="rounded bg-[#ECEEF2] px-3 py-1 font-medium text-[#090A0C] shadow-sm transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-30"
              >
                Stop Agent
              </button>
            </div>
          </aside>
        )}
      </div>

      {/* ==================== RETURN TO MAIN ==================== */}
      <button
        type="button"
        data-no-drag
        onClick={() => invoke("restore_desktop").catch(() => {})}
        title="Return to the main window"
        className="fixed bottom-6 right-6 z-50 flex items-center gap-2 rounded-lg border border-[#22252C] bg-[#14161B] px-4 py-2 font-mono text-xs text-[#ECEEF2] shadow-xl transition hover:bg-[#1B1E24]"
      >
        <span className="material-symbols-outlined text-[16px]">keyboard_return</span>
        <span>Main</span>
      </button>

      {/* ====================== MODEL MODAL ===================== */}
      {modelOpen && (
        <AetherModelModal
          primaryModel={chat.config.selected_primary}
          onPrimaryChange={chat.handlePrimaryChange}
          apiKeys={chat.config.api_keys}
          onConnectProvider={chat.updateApiKey}
          onClose={() => setModelOpen(false)}
        />
      )}

      {/* ======================= DRAG OVERLAY ======================= */}
      {dragOver && (
        <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center border-2 border-dashed border-blue-400/50 bg-blue-500/10 text-sm font-medium text-blue-200 backdrop-blur-sm">
          Drop files to attach
        </div>
      )}
    </div>
  );
}

function EmptyInspector({ label }: { label: string }) {
  return (
    <div className="flex h-full items-center justify-center px-6 text-center font-mono text-[12px] text-[#555B66]">
      {label}
    </div>
  );
}
