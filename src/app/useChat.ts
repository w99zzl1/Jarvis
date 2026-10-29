import { useCallback, useRef, useState } from "react";
import {
  playAudioFromBase64,
  sendCodeChatStreaming,
  sendMainChatStreaming,
  setConfig,
  getConfig,
  fetchCodeTools,
  fetchConversations,
  fetchConversation,
  deleteConversation,
  stopAgent,
  type AppConfig,
  type CodeToolInfo,
  type ConversationSummary,
  type ConversationEvent,
  type StreamEvent,
  type FileAttachment,
  uploadFiles,
} from "./api";
import type { StreamEvent as TaskStreamEvent } from "./components/TaskList";
export type { FileAttachment } from "./api";

export interface ActivityEntry {
  id: number;
  kind: "say" | "tool";
  text: string;
  status: "running" | "done" | "failed";
  /** ms — filled in when a tool call finishes */
  duration?: number;
  /** Live progress of a long-running tool, e.g. the disk scanner. */
  progress?: ToolProgress;
  /** Backend id of the tool call, when it sent one. */
  callId?: string;
}

export type ToolProgressPhase = "pending" | "live" | "done";

export interface ToolProgress {
  percent: number;
  nodes: number;
  bytes: number;
  queued: number;
  elapsed: number;
  /**
   * `pending` = we know the tool is running but have not heard from it yet.
   * The scanner needs 2-3s before its first sample, and a 29s scan used to look
   * like a frozen UI; the bar is drawn at 0% the moment the tool starts.
   */
  phase: ToolProgressPhase;
}

/** Tools whose first progress sample can take seconds, not milliseconds. */
const SLOW_TOOLS = new Set(["disk_tree"]);

const CONVERSATION_KEY = "jarvis.conversation_id";

/** The id is persisted so an ongoing chat keeps its context across restarts. */
function loadStoredConversationId(): string {
  try {
    return localStorage.getItem(CONVERSATION_KEY) || "";
  } catch {
    return "";
  }
}

function storeConversationId(id: string): void {
  try {
    localStorage.setItem(CONVERSATION_KEY, id);
  } catch {
    // storage unavailable; the in-memory ref still holds it for this session
  }
}

/** Progress for a tool that has just started and has not reported yet. */
function pendingProgress(tool?: string): ToolProgress | undefined {
  if (!tool || !SLOW_TOOLS.has(tool)) return undefined;
  return {
    percent: 0,
    nodes: 0,
    bytes: 0,
    queued: 0,
    elapsed: 0,
    phase: "pending",
  };
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export interface Message {
  id: number;
  role: "user" | "assistant";
  text: string;
  parts?: string[];
  activity?: ActivityEntry[];
  createdAt: number;
  /** total round-trip in ms, filled when the reply lands */
  latencyMs?: number;
  /** Attachments (images/files) attached to this message */
  attachments?: FileAttachment[];
}

const TOOL_LABELS: Record<string, string> = {
  read_file: "чтение файла",
  write_file: "запись файла",
  edit_file: "правка файла",
  update_plan: "обновление плана",
  shell: "команда",
  web_search: "поиск в интернете",
  search_content: "поиск по коду",
  search_files: "поиск файлов",
  find_file: "поиск файла",
  grep: "поиск по коду",
  git_status: "git status",
  git_commit: "git commit",
  get_tools: "загрузка инструментов",
  read: "чтение",
  disk_tree: "сканирование диска",
  search_web: "поиск в интернете",
  get_tools_main: "загрузка инструментов",
};

export function describeTool(tool?: string, args?: any): string {
  if (!tool) return "инструмент";
  const base = TOOL_LABELS[tool] || tool;
  if (args && typeof args === "object" && !Array.isArray(args)) {
    const target =
      args.path ||
      args.file ||
      args.target ||
      args.query ||
      args.pattern ||
      args.command;
    if (target) return `${base}: ${String(target).slice(0, 80)}`;
  }
  return base;
}

/** Leading <br> soup the agent sometimes emits — never render it. */
export function cleanText(text: string): string {
  return text.replace(/^(?:\s*<br\s*\/?>\s*)+/i, "").replace(/^\n+/, "");
}

/** Id for a fresh conversation; the backend keeps history keyed by this. */
function newConversationId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `conv-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Flatten a persisted event's content (string or multimodal blocks) to text. */
function contentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts: string[] = [];
    for (const block of content) {
      if (typeof block === "string") parts.push(block);
      else if (block && typeof block === "object" && typeof (block as any).text === "string") {
        parts.push((block as any).text);
      }
    }
    return parts.join("\n");
  }
  return "";
}

/** Turn a stored event log back into the message list the UI renders. */
function eventsToMessages(events: ConversationEvent[]): Message[] {
  const out: Message[] = [];
  const now = Date.now();
  let n = 0;
  for (const e of events) {
    const role = e.role;
    if (role !== "user" && role !== "assistant") continue;
    const text = contentToText(e.content).trim();
    if (!text) continue;
    out.push({ id: n, role, text, createdAt: now - (events.length - n) * 1000 });
    n += 1;
  }
  return out;
}

const EMPTY_CONFIG: AppConfig = {
  api_keys: {},
  selected_primary: null,
  selected_model: {},
  disabled_tools: [],
};

export function useChat(onSpeaking?: (v: boolean) => void, agentType: "main" | "code" = "main") {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [taskEvents, setTaskEvents] = useState<TaskStreamEvent[]>([]);
  const [isExecuting, setIsExecuting] = useState(false);
  const [config, setConfigState] = useState<AppConfig>(EMPTY_CONFIG);
  const [configLoaded, setConfigLoaded] = useState(false);
  /** Catalog of code-agent tools, fetched once, for the side-dock toggles. */
  const [tools, setTools] = useState<CodeToolInfo[]>([]);
  /** Saved conversations, newest first, for the chat-history drawer. */
  const [history, setHistory] = useState<ConversationSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [conversationId, setConversationIdState] = useState<string | null>(null);

  const idRef = useRef(0);
  const taskEventsRef = useRef<TaskStreamEvent[]>([]);
  /** Backend conversation id: kept across turns so history is not lost. */
  const conversationIdRef = useRef<string>(loadStoredConversationId());
  /** Aborts the in-flight SSE request when the user presses Stop. */
  const abortRef = useRef<AbortController | null>(null);
  /** Message that is currently streaming, so Stop can mark it immediately. */
  const liveIdRef = useRef<number | null>(null);

  const loadConfig = useCallback(() => {
    getConfig()
      .then((cfg) => {
        if (cfg) {
          setConfigState((prev) => ({
            ...prev,
            api_keys: cfg.api_keys || prev.api_keys,
            selected_primary: cfg.selected_primary ?? prev.selected_primary,
            selected_model: cfg.selected_model || prev.selected_model,
            disabled_tools: cfg.disabled_tools || prev.disabled_tools,
          }));
        }
      })
      .finally(() => setConfigLoaded(true));
    fetchCodeTools().then(setTools).catch(() => {});
  }, []);

  const updateApiKey = useCallback(async (provider: string, key: string) => {
    setConfigState((prev) => {
      const next = {
        ...prev,
        api_keys: { ...prev.api_keys, [provider]: key },
        selected_model: prev.selected_model || {},
      };
      setConfig(next);
      return next;
    });
  }, []);

  const handlePrimaryChange = useCallback(
    async (spec: { provider: any; model: string }) => {
      setConfigState((prev) => {
        const next = {
          ...prev,
          selected_primary: spec,
          selected_model: prev.selected_model || {},
        };
        setConfig(next);
        return next;
      });
    },
    [],
  );

  /** Enable/disable a code-agent tool; persists the disabled set to config. */
  const setToolEnabled = useCallback((name: string, enabled: boolean) => {
    setConfigState((prev) => {
      const disabled = new Set(prev.disabled_tools || []);
      if (enabled) disabled.delete(name);
      else disabled.add(name);
      const next = {
        ...prev,
        disabled_tools: Array.from(disabled),
        selected_model: prev.selected_model || {},
      };
      setConfig(next);
      return next;
    });
  }, []);

  /** One-click "Restore all": clear every disabled tool. */
  const restoreAllTools = useCallback(() => {
    setConfigState((prev) => {
      if (!prev.disabled_tools || prev.disabled_tools.length === 0) return prev;
      const next = {
        ...prev,
        disabled_tools: [],
        selected_model: prev.selected_model || {},
      };
      setConfig(next);
      return next;
    });
  }, []);

  /** Reload the saved-conversation list (called when the drawer opens / a chat ends). */
  const refreshHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      setHistory(await fetchConversations());
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  /** Load a past conversation: replay its log and make it the active one. */
  const loadConversation = useCallback(async (id: string) => {
    const events = await fetchConversation(id);
    const msgs = eventsToMessages(events);
    setMessages(msgs);
    setTaskEvents([]);
    taskEventsRef.current = [];
    idRef.current = msgs.length + 1;
    conversationIdRef.current = id;
    storeConversationId(id);
  }, []);

  const removeConversation = useCallback(async (id: string) => {
    const ok = await deleteConversation(id);
    if (ok) setHistory((h) => h.filter((c) => c.id !== id));
    return ok;
  }, []);

  const reset = useCallback(() => {
    setMessages([]);
    setTaskEvents([]);
    taskEventsRef.current = [];
    // New chat: mint a fresh id so the backend starts from empty context.
    const fresh = newConversationId();
    conversationIdRef.current = fresh;
    storeConversationId(fresh);
  }, []);

  const stop = useCallback(() => {
    const cid = conversationIdRef.current;
    const controller = abortRef.current;
    abortRef.current = null;
    controller?.abort();
    if (cid) stopAgent(cid).catch(() => {});
    setMessages((prev) =>
      prev.map((m) => {
        if (m.id !== liveIdRef.current) return m;
        const activity = (m.activity || []).map((e) =>
          e.status === "running"
            ? { ...e, status: "failed" as const, progress: undefined }
            : e,
        );
        return {
          ...m,
          activity,
          text: m.text || "(остановлено)",
        };
      }),
    );
    liveIdRef.current = null;
    setLoading(false);
    setIsExecuting(false);
  }, []);

  const send = useCallback(
    async (raw?: string, attachments?: FileAttachment[]) => {
      const text = (raw ?? input).trim();
      if (!text && (!attachments || attachments.length === 0)) return;
      if (loading) return;

      const startedAt = performance.now();
      const toolStartTimes = new Map<number, number>();

      // The scanner goes quiet for 2-3s before its first sample and for the whole
      // walk it only samples a few times a second. A local ticker keeps the
      // elapsed counter moving while we are still in `pending`, so the UI is
      // visibly alive before the backend says anything. It stops on its own the
      // moment a real sample arrives.
      let pendingTick: number | null = null;

      const appendActivity = (
        msgId: number,
        update: (prev: ActivityEntry[]) => ActivityEntry[],
      ) => {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === msgId
              ? { ...m, activity: update(m.activity || []) }
              : m,
          ),
        );
      };

      setInput("");
      setTaskEvents([]);
      taskEventsRef.current = [];
      setIsExecuting(true);

      const userMsg: Message = {
        id: idRef.current++,
        role: "user",
        text,
        createdAt: Date.now(),
        ...(attachments && attachments.length > 0 ? { attachments } : {}),
      };
      const liveId = idRef.current++;
      liveIdRef.current = liveId;
      setMessages((prev) => [
        ...prev,
        userMsg,
        { id: liveId, role: "assistant", text: "", createdAt: Date.now() },
      ]);
      setLoading(true);

      const controller = new AbortController();
      abortRef.current = controller;

      // Started only once `liveId` exists: the tick writes into that message.
      pendingTick = setInterval(() => {
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== liveId || !m.activity) return m;
            let touched = false;
            const activity = m.activity.map((e) => {
              if (e.status !== "running" || e.progress?.phase !== "pending") return e;
              touched = true;
              return {
                ...e,
                progress: { ...e.progress!, elapsed: e.progress!.elapsed + 0.25 },
              };
            });
            return touched ? { ...m, activity } : m;
          }),
        );
      }, 250);

      let finalEvent: any = null;
      try {
        const conversationId =
          loadStoredConversationId() || conversationIdRef.current || newConversationId();
        conversationIdRef.current = conversationId;
        storeConversationId(conversationId);
        const sendStreaming = agentType === "code"
          ? sendCodeChatStreaming
          : sendMainChatStreaming;
        finalEvent = await sendStreaming(
          text,
          config.selected_primary || null,
          config.api_keys,
          (ev: StreamEvent) => {
            if (ev.type === "stream") {
              const it = (ev as any).iteration ?? 0;
              setMessages((prev) =>
                prev.map((m) => {
                  if (m.id !== liveId) return m;
                  const parts =
                    m.parts && m.parts.length ? m.parts : m.text ? [m.text] : [];
                  if (parts.length <= it) {
                    for (let i = parts.length; i < it; i++) parts.push("");
                  }
                  parts[it] = ev.text;
                  return { ...m, parts, text: parts.join("\n\n") };
                }),
              );
              return;
            }

            if (ev.type === "plan_updated" || ev.type === "plan_created") {
              taskEventsRef.current = [
                ...taskEventsRef.current,
                ev as TaskStreamEvent,
              ];
              setTaskEvents([...taskEventsRef.current]);
              return;
            }

            if (ev.type === "agent_message") {
              const say = (ev.message || "").trim();
              if (!say) return;
              appendActivity(liveId, (a) => [
                ...a,
                {
                  id: a.length ? a[a.length - 1].id + 1 : 1,
                  kind: "say",
                  text: say,
                  status: "done",
                },
              ]);
              return;
            }

            if (ev.type === "tool_started") {
              const toolName = (ev as any).tool || (ev as any).tool_name;
              const callId = (ev as any).call_id;
              const label = describeTool(toolName, (ev as any).args);
              appendActivity(liveId, (a) => {
                const id = a.length ? a[a.length - 1].id + 1 : 1;
                toolStartTimes.set(id, performance.now());
                return [
                  ...a,
                  {
                    id,
                    kind: "tool",
                    text: label,
                    status: "running",
                    // Drawn immediately: for the scanner the first real sample is
                    // seconds away, and an empty bar reads as a hang.
                    progress: pendingProgress(toolName),
                    callId,
                  },
                ];
              });
              return;
            }

            if (ev.type === "tool_progress") {
              // Comes from the scanner's own threads while it is still walking.
              // The percentage is an estimate; the counters are exact.
              const progress: ToolProgress = {
                percent: Math.max(0, Math.min(100, Number(ev.percent) || 0)),
                nodes: Number(ev.nodes) || 0,
                bytes: Number(ev.bytes) || 0,
                queued: Number(ev.queued) || 0,
                elapsed: Number(ev.elapsed) || 0,
                phase: ev.done ? "done" : "live",
              };
              appendActivity(liveId, (a) => {
                // Match on call_id when the backend sends it; fall back to the
                // newest running tool, which is right for a sequential agent.
                const callId = (ev as any).call_id;
                let realIdx = -1;
                if (callId) {
                  const byId = a.findIndex(
                    (e) => e.kind === "tool" && e.status === "running" && e.callId === callId,
                  );
                  if (byId !== -1) realIdx = byId;
                }
                if (realIdx === -1) {
                  const idx = [...a]
                    .reverse()
                    .findIndex((e) => e.kind === "tool" && e.status === "running");
                  if (idx === -1) return a;
                  realIdx = a.length - 1 - idx;
                }
                const next = [...a];
                next[realIdx] = {
                  ...next[realIdx],
                  progress: ev.done ? { ...progress, percent: 100 } : progress,
                };
                return next;
              });
              return;
            }

            if (ev.type === "tool_completed") {
              const label = describeTool(
                (ev as any).tool || (ev as any).tool_name,
                (ev as any).args,
              );
              const failed =
                (ev as any).status && (ev as any).status !== "success";
              const serverMs = (ev as any).duration;
              appendActivity(liveId, (a) => {
                const idx = [...a]
                  .reverse()
                  .findIndex((e) => e.kind === "tool" && e.status === "running");
                if (idx === -1) {
                  return [
                    ...a,
                    {
                      id: a.length ? a[a.length - 1].id + 1 : 1,
                      kind: "tool",
                      text: label,
                      status: failed ? "failed" : "done",
                      duration: serverMs,
                    },
                  ];
                }
                const realIdx = a.length - 1 - idx;
                const started = toolStartTimes.get(a[realIdx].id);
                const next = [...a];
                next[realIdx] = {
                  ...next[realIdx],
                  status: failed ? "failed" : "done",
                  // Drop the live counters: the finished entry shows duration.
                  progress: undefined,
                  duration:
                    serverMs ?? (started ? performance.now() - started : undefined),
                };
                return next;
              });
              return;
            }

            if (ev.type === "execution_error") {
              appendActivity(liveId, (a) => [
                ...a,
                {
                  id: a.length ? a[a.length - 1].id + 1 : 1,
                  kind: "tool",
                  text: (ev as any).error || (ev as any).message || "ошибка",
                  status: "failed",
                },
              ]);
              return;
            }
          },
          {
            conversation_id: conversationId,
            disabled_tools: config.disabled_tools,
            ...(attachments && attachments.length > 0
              ? { attachments: attachments.map((a) => ({ path: a.path, name: a.name, mime: a.mime, size: a.size })) }
              : {}),
          },
          controller.signal,
        );

        // Adopt the id the backend assigned (it generates one when we send none).
        if (finalEvent.conversation_id) {
          conversationIdRef.current = finalEvent.conversation_id;
          storeConversationId(finalEvent.conversation_id);
        }

        const reply = finalEvent.error
          ? `Ошибка: ${finalEvent.error}`
          : finalEvent.reply || "(нет ответа)";

        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== liveId) return m;
            const parts =
              m.parts && m.parts.length ? [...m.parts] : m.text ? [m.text] : [];
            const last = parts.length ? parts[parts.length - 1] : "";
            const already = last && reply && last.trim() === reply.trim();
            if (!already) parts.push(reply);
            return {
              ...m,
              parts,
              text: parts.join("\n\n"),
              latencyMs: performance.now() - startedAt,
            };
          }),
        );

        if (finalEvent.audio) {
          onSpeaking?.(true);
          try {
            await playAudioFromBase64(finalEvent.audio);
          } catch {}
          onSpeaking?.(false);
        }
      } catch (e: any) {
        if (e?.name !== "AbortError") {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === liveId
                ? {
                    ...m,
                    text: m.text
                      ? m.text + "\n\nОшибка: " + (e?.message || e)
                      : "Ошибка: " + (e?.message || e),
                  }
                : m,
            ),
          );
        }
      } finally {
        if (pendingTick !== null) clearInterval(pendingTick);
        if (abortRef.current === controller) abortRef.current = null;
        if (liveIdRef.current === liveId) {
          liveIdRef.current = null;
          setLoading(false);
          setIsExecuting(false);
        }
        refreshHistory();
      }
    },
    [config, input, loading, onSpeaking, refreshHistory, agentType],
  );

  return {
    messages,
    setMessages,
    input,
    setInput,
    loading,
    taskEvents,
    isExecuting,
    config,
    configLoaded,
    loadConfig,
    updateApiKey,
    handlePrimaryChange,
    tools,
    setToolEnabled,
    restoreAllTools,
    send,
    sendWithAttachments: (atts: FileAttachment[]) => send(undefined, atts),
    stop,
    reset,
    history,
    historyLoading,
    refreshHistory,
    loadConversation,
    removeConversation,
    conversationId: conversationIdRef.current,
  };
}