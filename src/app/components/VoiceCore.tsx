import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import AetherModelModal from "./AetherModelModal";
import JarvisCoreVisualizer from "./JarvisCoreVisualizer";
import Markdown from "./Markdown";
import { HistoryDrawer } from "./ChatPanel";
import {
  cleanText,
  useChat,
  type ActivityEntry,
  type FileAttachment,
  type Message,
} from "../useChat";
import { uploadFiles, type BackendStatus } from "../api";
import { startAudioCapture, stopAudioCapture } from "../audioCapture";
import { PROVIDER_LABELS, type ProviderId } from "../models";

interface VoiceCoreProps {
  isSpeaking: boolean;
  setIsSpeaking: (v: boolean) => void;
  backend: BackendStatus;
}

// ============================================================
// SCOPED STYLES (ported from the AETHER "Voice Core" mockup)
// ============================================================

const VOICE_CORE_CSS = `
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--jarvis-scrollbar); border-radius: 2px; }
::-webkit-scrollbar-thumb:hover { background: var(--jarvis-scrollbar-hover); }

/* One technical typeface for every HUD caption: clock, TEXT INPUT, section
   headers, session tag and corner labels. Prose stays sans. */
#voiceCoreRoot {
  --hud-font: 'JetBrains Mono', 'Fira Code', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace;
}
#voiceCoreRoot .font-mono {
  font-family: var(--hud-font);
}

/* Cinematic canvas core — see JarvisCoreVisualizer.tsx */
.jarvis-core-stack canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
/* Golden mean: keep the two-layer glow and screen blend, but pull back the
   blur/brightness so it lights the arcs instead of washing them out. */
#jarvisCore {
  filter: drop-shadow(0 0 2px rgba(var(--jarvis-core-glow-rgb), 0.7))
          drop-shadow(0 0 5px rgba(var(--jarvis-core-glow-2-rgb), 0.42))
          contrast(1.12) saturate(1.22);
}
#bloomLayer {
  filter: blur(9px) brightness(1.6);
  mix-blend-mode: screen;
  opacity: 0.65;
  will-change: transform;
}
/* Refined Spring Easing for silky transitions */
.smooth-transition {
  transition-property: all;
  transition-duration: 500ms;
  transition-timing-function: cubic-bezier(0.16, 1, 0.3, 1);
}

/* Stage transitions when switching between Voice Core and Voice Mode */
.mode-voice #coreOrbCluster {
  transform: translateY(118px) scale(1.48);
}

/* Chat tab: lift the core and tighten the stage so the log gets more room.
   Scoped to :not(.mode-voice) so voice-mode geometry/animation stay intact. */
#stageContainer:not(.mode-voice) #coreStageTop {
  padding-top: 6px;
  padding-bottom: 6px;
}
#stageContainer:not(.mode-voice) #coreStageInner {
  padding-top: 0;
  padding-bottom: 0;
}
/* Chat-only trim: the canvas is rendered at 480px internally and shown at
   ~245px, so a uniform CSS scale keeps every ring/text crisp (no bitmap
   stretching, no squashing). Matching negative margins collapse the layout
   box by the same amount — no dead space appears around the smaller core.
   The voice-mode transform below is untouched, so that tab renders 1:1. */
#stageContainer:not(.mode-voice) #coreOrbCluster {
  transform: scale(0.92);
  margin: -10px 0;
}

/* Top Voice Core Header Status and Transcription Banner */
.mode-voice #coreStatusHeader {
  opacity: 0;
  transform: translateY(-12px);
  pointer-events: none;
}
.mode-voice #coreTranscriptionBox {
  opacity: 0;
  transform: translateY(-16px) scale(0.96);
  pointer-events: none;
}

/* Voice Mode overlay indicators above the centered orb */
#voiceModeTopStatus {
  opacity: 0;
  transform: translateY(-14px);
  pointer-events: none;
}
.mode-voice #voiceModeTopStatus {
  opacity: 1;
  transform: translateY(0);
  pointer-events: auto;
}

/* Terminal-style HUD frame around the chat log */
#chatLogFrame {
  position: relative;
  display: flex;
  flex: 1 1 0%;
  flex-direction: column;
  min-height: 0;
  margin: 0 8px 8px;
  border: 1px solid rgba(var(--jarvis-hud-rgb), 0.15);
  border-radius: 10px;
  overflow: hidden;
  background-color: var(--jarvis-chat-bg);
  background-image:
    linear-gradient(rgba(var(--jarvis-hud-rgb), 0.028) 1px, transparent 1px),
    linear-gradient(90deg, rgba(var(--jarvis-hud-rgb), 0.028) 1px, transparent 1px);
  background-size: 24px 24px;
  box-shadow: inset 0 0 36px rgba(var(--jarvis-hud-rgb), 0.035), 0 0 16px rgba(var(--jarvis-hud-rgb), 0.05);
}
/* Bracket-only HUD corners — no captions. */
.hud-corner {
  position: absolute;
  z-index: 5;
  width: 9px;
  height: 9px;
  pointer-events: none;
  border: 0 solid rgba(var(--jarvis-core-glow-rgb), 0.45);
}
.hud-corner-tl {
  top: 5px;
  left: 8px;
  border-left-width: 1px;
  border-top-width: 1px;
}
.hud-corner-br {
  bottom: 5px;
  right: 8px;
  border-right-width: 1px;
  border-bottom-width: 1px;
}
/* Messages dissolve into the core instead of being cut off at the edge. */
#chatLogStream {
  -webkit-mask-image: linear-gradient(to bottom, transparent 0%, black 15%);
  mask-image: linear-gradient(to bottom, transparent 0%, black 15%);
}
/* Voice mode: the chat HUD slides away with the log, and the leftover divider
   under the core disappears so the screen has no line across its middle. */
.mode-voice #chatLogFrame {
  opacity: 0;
  transform: translateY(90px) scale(0.97);
  pointer-events: none;
}
.mode-voice #coreStageTop {
  border-bottom-color: transparent;
}
.mode-voice #bottomInputBox {
  opacity: 0;
  transform: translateY(70px);
  pointer-events: none;
}

/* Voice Mode Bottom Action Sheet slides up into place */
#voiceModeBottomControls,
#voiceModeBottomDrawer {
  opacity: 0;
  transform: translateY(105%);
  pointer-events: none;
}
.mode-voice #voiceModeBottomControls,
.mode-voice #voiceModeBottomDrawer {
  opacity: 1;
  transform: translateY(0%);
  pointer-events: auto;
}

.voice-actions-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.voice-action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 12px 12px;
  border-radius: 8px;
  background-color: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: rgba(255, 255, 255, 0.8);
  font-family: 'Inter', sans-serif;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}
.voice-action-btn:hover {
  background-color: rgba(255, 255, 255, 0.09);
  border-color: rgba(255, 255, 255, 0.14);
  color: #ffffff;
}
.voice-action-btn svg {
  width: 14px;
  height: 14px;
}
`;

// ============================================================
// HELPERS
// ============================================================

const pad = (n: number) => String(n).padStart(2, "0");

function clockOf(ts: number) {
  const d = new Date(ts);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function clockSecOf(ts: number) {
  const d = new Date(ts);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtMs(ms?: number) {
  if (ms == null || Number.isNaN(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)} мс`;
  return `${(ms / 1000).toFixed(2)} с`;
}

function isImage(mime?: string) {
  return !!mime && mime.startsWith("image/");
}

type TimelineItem =
  | { kind: "say"; key: string; text: string }
  | { kind: "tool"; key: string; entry: ActivityEntry };

/**
 * Rebuild the true order of an agent turn: `text → tools → text → tools …`.
 * The backend streams prose per iteration and tags every activity entry with
 * the iteration it happened in, so grouping both by iteration restores the
 * order the agent actually worked in.
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
    0,
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

// ============================================================
// MESSAGE RENDERING
// ============================================================

function UserMessage({ message }: { message: Message }) {
  const voice = message.source === "voice";
  return (
    <div className="flex flex-col items-end">
      <div className="mb-1 mr-1 flex items-center gap-1.5">
        <span className="flex items-center gap-1 font-mono text-[11px] text-emerald-400">
          {voice ? (
            <svg
              className="h-2.5 w-2.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              viewBox="0 0 24 24"
            >
              <path
                d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          ) : (
            <span className="material-symbols-outlined text-[12px] leading-none">
              keyboard
            </span>
          )}
          {voice ? "VOICE INPUT" : "TEXT INPUT"}
        </span>
        <span className="font-mono text-[12px] text-[#94A3B8]">{clockOf(message.createdAt)}</span>
      </div>
      <div className="max-w-[88%] rounded-xl rounded-tr-sm border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-[13px] leading-snug text-[#F8FAFC] shadow-sm">
        <p className="whitespace-pre-wrap break-words">{message.text}</p>
        {message.attachments && message.attachments.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {message.attachments.map((a, i) => (
              <span
                key={`${a.path}-${i}`}
                className="rounded border border-white/[0.1] bg-black/20 px-1.5 py-0.5 font-mono text-[12px] text-[#94A3B8]"
              >
                {a.name}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Collapsible terminal block for shell tool calls (command + output). */
function ToolBlock({ entry }: { entry: ActivityEntry }) {
  // Collapsed by default: raw command/output is noise until the reader asks.
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const copy = () => {
    navigator.clipboard?.writeText(entry.command || entry.output || "").then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      },
      () => {},
    );
  };

  const failed = entry.status === "failed";

  if (!entry.command && !entry.output) {
    return (
      <div className="flex items-center gap-2 font-mono text-[12px]">
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${
            entry.status === "running"
              ? "animate-pulse bg-amber-400"
              : failed
                ? "bg-rose-400"
                : "bg-emerald-400"
          }`}
        />
        <span className={`min-w-0 flex-1 truncate ${failed ? "text-rose-300" : "text-[#CBD5E1]"}`}>
          {entry.text}
        </span>
        {entry.duration != null && (
          <span className="shrink-0 text-[12px] text-[#555B66]">{fmtMs(entry.duration)}</span>
        )}
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-md border border-[#202534] bg-[#08090D]">
      <div className="flex items-center justify-between border-b border-[#1B1F2C] bg-[#12151E] px-2.5 py-1.5 font-mono text-[12px] text-[#94A3B8]">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-1.5 font-medium text-[#CBD5E1]"
        >
          <span className="text-[8px] text-amber-400">●</span>
          <span className="truncate">{entry.text}</span>
          <span className="opacity-60">{open ? "▾" : "▸"}</span>
        </button>
        <button
          type="button"
          onClick={copy}
          title="Копировать"
          className="transition-colors hover:text-white"
        >
          {copied ? (
            <span className="font-mono text-[11px] text-emerald-400">скопировано</span>
          ) : (
            <svg
              className="h-2.5 w-2.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              viewBox="0 0 24 24"
            >
              <rect height="13" rx="2" ry="2" width="13" x="9" y="9" />
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
          )}
        </button>
      </div>
      {open && (
        <div className="space-y-1 p-2.5 font-mono text-[12px] leading-relaxed">
          {entry.command && (
            <div className="whitespace-pre-wrap break-words font-medium text-[#CBD5E1]">
              $ {entry.command}
            </div>
          )}
          {entry.output && (
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-[#94A3B8]">
              {entry.output}
            </pre>
          )}
          {entry.status === "running" && <div className="text-[#94A3B8]">…</div>}
          {entry.status === "done" && (
            <div className="flex items-center gap-1 pt-0.5 text-[12px] font-semibold text-emerald-400">
              <span>✓</span> успех
            </div>
          )}
          {failed && (
            <div className="flex items-center gap-1 pt-0.5 text-[12px] font-semibold text-rose-400">
              <span>✕</span> ошибка
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function AgentMessage({ message, busy }: { message: Message; busy: boolean }) {
  const items = useMemo(
    () => buildTimeline(message),
    [message.parts, message.activity, message.text],
  );
  const hasContent = items.length > 0 || Boolean(message.error);

  return (
    <div className="flex flex-col items-start">
      <div className="mb-1 ml-1 flex items-center gap-1.5">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
        <span className="font-mono text-[12px] font-medium text-[#CBD5E1]">AETHER</span>
        <span className="font-mono text-[11px] text-[#94A3B8]">{clockSecOf(message.createdAt)}</span>
      </div>
      <div className="max-w-[94%] space-y-2.5 rounded-xl rounded-tl-sm border border-[#1D212D] bg-[#10131B] p-3 text-[#CBD5E1] shadow-sm">
        {items.map((it) =>
          it.kind === "say" ? (
            <div key={it.key} className="text-[14px] leading-relaxed text-[#F1F5F9]">
              <Markdown text={cleanText(it.text)} />
            </div>
          ) : (
            <ToolBlock key={it.key} entry={it.entry} />
          ),
        )}

        {message.error && (
          <div className="whitespace-pre-wrap break-words rounded-md border border-rose-500/40 bg-rose-500/10 px-2.5 py-2 font-mono text-[12px] text-rose-300">
            {message.error}
          </div>
        )}

        {busy && (
          <div className="flex items-center gap-2 font-mono text-[12px] text-[#94A3B8]">
            <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
            работаю…
          </div>
        )}

        {!busy && !hasContent && (
          <span className="font-mono text-[12px] uppercase tracking-wide text-[#555B66]">
            (пусто)
          </span>
        )}

        {!busy && hasContent && (
          <div className="flex items-center gap-3 border-t border-[#181C26] pt-1 font-mono text-[12px] text-[#555B66]">
            {message.latencyMs != null && <span>{fmtMs(message.latencyMs)}</span>}
            <button
              type="button"
              onClick={() => navigator.clipboard?.writeText(cleanText(message.text || ""))}
              className="ml-auto transition-colors hover:text-[#ECEEF2]"
            >
              копировать
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// VOICE CORE WINDOW
// ============================================================

export default function VoiceCore({ isSpeaking, setIsSpeaking, backend }: VoiceCoreProps) {
  const [mode, setMode] = useState<"core" | "voice">("core");
  const chat = useChat(setIsSpeaking, "main");
  const [modelOpen, setModelOpen] = useState(false);
  const [recording, setRecording] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [micAnalyser, setMicAnalyser] = useState<AnalyserNode | null>(null);
  const [voiceError, setVoiceError] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<FileAttachment[]>([]);

  const switchMode = useCallback((newMode: "core" | "voice") => {
    setMode(newMode);
  }, []);

  const toggleMode = useCallback(() => {
    switchMode(mode === "core" ? "voice" : "core");
  }, [mode, switchMode]);

  // The chat object is recreated on every render; keep a ref so the global
  // keyboard listeners stay subscribed to the latest instance without churn.
  const chatRef = useRef(chat);
  chatRef.current = chat;

  const recordingRef = useRef(false);
  const voiceFinalizedRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ---- init ---------------------------------------------------------------
  useEffect(() => {
    chat.loadConfig();
    chat.refreshHistory();
    if (chat.conversationId) {
      setActiveConversationId(chat.conversationId);
      void chat.loadConversation(chat.conversationId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- auto-scroll (only while pinned to the bottom) ----------------------
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [chat.messages, chat.isExecuting]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  // ---- voice: capture, backend-owned captions + endpointing ---------------
  /** Handle the one authoritative final transcript for the current utterance. */
  const handleFinalText = useCallback(async (text: string) => {
    if (voiceFinalizedRef.current) return;
    voiceFinalizedRef.current = true;
    recordingRef.current = false;
    setRecording(false);

    const t = (text || "").trim();
    if (!t) {
      setVoiceError("Речь не распознана — попробуйте ещё раз");
      setTranscript("");
      return;
    }
    setVoiceError("");
    setTranscript(t);
    try {
      await chatRef.current.send(t, undefined, "voice", true);
    } catch (e: any) {
      const msg = e?.message || String(e);
      setVoiceError(msg);
      console.error("[VoiceCore] voice send failed:", e);
    }
  }, []);

  const startRecording = useCallback(async () => {
    if (recordingRef.current) return;
    try {
      setVoiceError("");
      setTranscript("");
      voiceFinalizedRef.current = false;
      await startAudioCapture({
        onPartial: (_delta, text) => setTranscript(text),
        // The Python backend owns endpointing and sends `final` both when the
        // user stops and when its VAD detects a pause.
        onFinal: (text) => { void handleFinalText(text); },
        onAnalyser: setMicAnalyser,
        onError: (message) => {
          if (voiceFinalizedRef.current) return;
          voiceFinalizedRef.current = true;
          recordingRef.current = false;
          setRecording(false);
          setVoiceError(message);
        },
      });
      recordingRef.current = true;
      setRecording(true);
    } catch (e: any) {
      const msg = e?.message || String(e);
      setVoiceError(msg);
      setTranscript("");
      console.error("[VoiceCore] start recording failed:", e);
    }
  }, [handleFinalText]);

  const stopRecording = useCallback(async () => {
    if (!recordingRef.current) return;
    try {
      // Only request the stop. The backend's `final` event carries the text and
      // is handled once by handleFinalText (manual or VAD endpoint alike).
      const text = await stopAudioCapture();
      if (text && !voiceFinalizedRef.current) await handleFinalText(text);
    } catch (e: any) {
      if (voiceFinalizedRef.current) return;
      voiceFinalizedRef.current = true;
      recordingRef.current = false;
      setRecording(false);
      const msg = e?.message || String(e);
      setVoiceError(msg);
      setTranscript("");
      console.error("[VoiceCore] stop recording failed:", e);
    }
  }, [handleFinalText]);

  const toggleRecording = useCallback(() => {
    if (recordingRef.current) void stopRecording();
    else void startRecording();
  }, [startRecording, stopRecording]);

  // ---- attachments --------------------------------------------------------
  const addFiles = useCallback(async (files: File[]) => {
    const valid = files.filter((f) => f.type || f.name.includes("."));
    if (valid.length === 0) return;
    try {
      const uploaded = await uploadFiles(valid);
      setAttachments((prev) => [...prev, ...uploaded]);
    } catch (e) {
      console.error("[VoiceCore] upload failed:", e);
    }
  }, []);

  // ---- send / stop --------------------------------------------------------
  const handleSend = useCallback(async () => {
    // Stop has to be reachable while a turn runs, when the input is empty.
    if (chat.isExecuting || chat.loading) {
      chat.stop();
      return;
    }
    if (!chat.input.trim() && attachments.length === 0) return;
    const atts = attachments;
    setAttachments([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
    const speak = mode === "voice";
    if (atts.length > 0) await chat.sendWithAttachments(atts, "text", speak);
    else await chat.send(undefined, undefined, "text", speak);
  }, [chat, attachments, mode]);

  const showIde = useCallback(() => {
    invoke("show_assistant").catch(() => {});
  }, []);

  const minimizeWindow = useCallback(() => {
    try {
      void getCurrentWindow().minimize();
    } catch {
      /* not running under Tauri */
    }
  }, []);

  const closeWindow = useCallback(() => {
    invoke("quit_app").catch(() => {
      try {
        void getCurrentWindow().close();
      } catch {
        /* ignore */
      }
    });
  }, []);

  // ---- keyboard: Tab toggles mode, Ctrl+1/2 select, Space = push-to-talk --
  const isTypingTarget = (t: EventTarget | null): boolean => {
    const el = t as HTMLElement | null;
    if (!el) return false;
    return (
      el.tagName === "INPUT" ||
      el.tagName === "TEXTAREA" ||
      el.tagName === "SELECT" ||
      el.tagName === "BUTTON" ||
      el.isContentEditable
    );
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" && !isTypingTarget(e.target)) {
        e.preventDefault();
        toggleMode();
      } else if ((e.ctrlKey || e.metaKey) && e.key === "1") {
        e.preventDefault();
        switchMode("core");
      } else if ((e.ctrlKey || e.metaKey) && e.key === "2") {
        e.preventDefault();
        switchMode("voice");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleMode, switchMode]);

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.code !== "Space" || e.repeat) return;
      if (isTypingTarget(e.target) || recordingRef.current) return;
      e.preventDefault();
      void startRecording();
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== "Space") return;
      if (isTypingTarget(e.target) || !recordingRef.current) return;
      e.preventDefault();
      void stopRecording();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [startRecording, stopRecording]);

  // ---- derived ------------------------------------------------------------
  const providerLabel = chat.config.selected_primary
    ? PROVIDER_LABELS[chat.config.selected_primary.provider as ProviderId] || ""
    : "";
  const modelLabel = chat.config.selected_primary?.model || "модель не выбрана";
  const busy = chat.loading;
  const sessionLabel = (() => {
    const id = chat.conversationId || activeConversationId || "";
    const short = id.replace(/[^a-z0-9]/gi, "").slice(-4).toUpperCase();
    return short || "NEW";
  })();

  const backendOnline = backend.ready && !backend.failed;
  const backendLabel = backend.failed
    ? "BACKEND OFFLINE"
    : backend.ready
      ? "BACKEND ONLINE"
      : "STARTING";
  const transcriptText = transcript;

  return (
    <>
      <style>{VOICE_CORE_CSS}</style>
      <div
        id="voiceCoreRoot"
        className="relative flex h-full w-full flex-col overflow-hidden border border-[#1A1D26] bg-[#0C0D12] text-[#E2E8F0] shadow-2xl"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          if (e.dataTransfer?.files.length) void addFiles(Array.from(e.dataTransfer.files));
        }}
      >
        {/* ===================== TITLE BAR ===================== */}
        <div className="z-30 flex h-9 shrink-0 select-none items-center justify-between border-b border-[#1A1D26] bg-[#0E1017] px-3">
          <div className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]" />
            <span className="font-mono text-[12px] font-semibold uppercase tracking-wider text-[#CBD5E1]">
              AETHER // CORE
            </span>
            <span className="font-mono text-[12px] text-[#94A3B8]">v0.1.0-beta.1</span>
          </div>

          <div className="flex items-center gap-2.5">
            <button
              type="button"
              onClick={showIde}
              title="Открыть окно AI Code Agent"
              className="group flex items-center gap-1.5 rounded border border-[#232734] bg-[#151821] px-2 py-0.5 text-[#94A3B8] transition-all hover:border-emerald-500/50 hover:bg-[#1E222D] hover:text-emerald-400"
            >
              <svg
                className="h-3 w-3 transition-transform group-hover:scale-105"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                viewBox="0 0 24 24"
              >
                <path
                  d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <span className="font-mono text-[12px] font-medium tracking-tight">AETHER IDE</span>
              <svg
                className="h-2.5 w-2.5 opacity-60"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                viewBox="0 0 24 24"
              >
                <path
                  d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>

            <div className="flex items-center gap-1 pl-1">
              <button
                type="button"
                onClick={minimizeWindow}
                title="Свернуть"
                className="flex h-3 w-3 items-center justify-center rounded-full bg-[#1A1D26] text-[#94A3B8] transition-colors hover:bg-[#282D3B] hover:text-white"
              >
                <svg
                  className="h-2 w-2"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="3"
                  viewBox="0 0 24 24"
                >
                  <line x1="4" x2="20" y1="12" y2="12" />
                </svg>
              </button>
              <button
                type="button"
                onClick={closeWindow}
                title="Закрыть"
                className="flex h-3 w-3 items-center justify-center rounded-full bg-[#1A1D26] text-[#94A3B8] transition-colors hover:bg-rose-950 hover:text-rose-400"
              >
                <svg
                  className="h-2 w-2"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  viewBox="0 0 24 24"
                >
                  <line x1="18" x2="6" y1="6" y2="18" />
                  <line x1="6" x2="18" y1="6" y2="18" />
                </svg>
              </button>
            </div>
          </div>
        </div>

        {/* ================ MODE SELECTOR SUBHEADER ================ */}
        <div className="z-30 flex shrink-0 select-none items-center justify-between border-b border-[#1A1D26] bg-[#0C0E14] px-3 py-1.5">
          <div className="flex items-center gap-1 rounded-lg border border-[#1F232F] bg-[#12151D] p-0.5">
            <button
              type="button"
              onClick={() => switchMode("core")}
              title="Вид по умолчанию: Чат, Терминал, Промпт (Ctrl+1 или Tab)"
              className={`flex items-center gap-1.5 rounded-md px-3 py-1 font-mono text-[12px] font-medium transition-all duration-300 ${
                mode === "core"
                  ? "border border-emerald-500/30 bg-[#1A202C] text-emerald-400 shadow-sm"
                  : "border border-transparent text-[#94A3B8] hover:bg-[#161A23] hover:text-white"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full transition-transform duration-300 ${
                  mode === "core" ? "bg-emerald-400 shadow-[0_0_6px_#10B981]" : "bg-[#3A4150]"
                }`}
              />
              <span>Чат</span>
            </button>
            <button
              type="button"
              onClick={() => switchMode("voice")}
              title="Фокусированный режим голосового управления (Ctrl+2 или Tab)"
              className={`flex items-center gap-1.5 rounded-md px-3 py-1 font-mono text-[12px] font-medium transition-all duration-300 ${
                mode === "voice"
                  ? "border border-emerald-500/30 bg-[#1A202C] text-emerald-400 shadow-sm"
                  : "border border-transparent text-[#94A3B8] hover:bg-[#161A23] hover:text-white"
              }`}
            >
              <svg
                className="h-2.5 w-2.5 opacity-80"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                viewBox="0 0 24 24"
              >
                <path
                  d="M12 2v20M17 5v14M7 5v14M2 10v4M22 10v4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <span>Voice Mode</span>
            </button>
          </div>

          <button
            type="button"
            onClick={() => void invoke("show_settings")}
            title={`Настройки · ${backendLabel}`}
            className="flex items-center gap-1.5 rounded-md border border-[#1e292b] bg-[#141a1c] p-1 text-[#94A3B8] transition-colors hover:bg-[#1c2527] hover:text-white"
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${backendOnline ? "bg-emerald-400" : "bg-rose-400"}`}
            />
            <span className="material-symbols-outlined text-[14px]">settings</span>
          </button>
        </div>

        {/* ==================== DYNAMIC STAGE ==================== */}
        <div
          id="stageContainer"
          className={`relative flex flex-1 flex-col overflow-hidden ${mode === "voice" ? "mode-voice" : ""}`}
        >
          {/* One shared error surface — never duplicated between modes. */}
          {voiceError && (
            <div
              role="status"
              className="pointer-events-none absolute inset-x-4 top-3 z-40 mx-auto max-w-md rounded-lg border border-rose-500/30 bg-[#1A1016]/95 px-3 py-2 text-center font-mono text-[13px] leading-snug text-rose-200 shadow-lg backdrop-blur-md"
            >
              {voiceError}
            </div>
          )}

          {/* Upper half: voice core engine & status */}
          <div
            id="coreStageTop"
            className="relative z-10 flex shrink-0 flex-col justify-between overflow-visible border-b border-[#1A1D26] bg-gradient-to-b from-[#0E1118] via-[#0C0E14] to-[#0A0C11] p-3.5"
          >
            <div id="coreStageInner" className="z-20 flex flex-col items-center justify-center py-1">
              <div
                id="coreOrbCluster"
                className="group smooth-transition relative flex h-[245px] w-[245px] origin-center cursor-pointer items-center justify-center transition-transform duration-500"
                onClick={() =>
                  mode === "voice" ? void toggleRecording() : switchMode("voice")
                }
                title={
                  mode === "voice"
                    ? recording
                      ? "Остановить запись"
                      : "Начать запись"
                    : "Перейти в Voice Mode"
                }
              >
                <JarvisCoreVisualizer
                  analyser={micAnalyser}
                  active={recording || isSpeaking}
                />
              </div>

              {transcriptText && (
                <p
                  id="coreTranscriptionBox"
                  className="smooth-transition mt-0 max-w-[520px] text-center text-[15px] leading-snug text-[#F1F5F9] transition-all duration-500"
                >
                  {transcriptText}
                </p>
              )}

            </div>
          </div>

          {/* Lower half: chat log & interaction stream */}
          <div id="chatLogFrame" className="smooth-transition">
            <span className="hud-corner hud-corner-tl" aria-hidden="true" />
            <div
              id="chatLogStream"
              ref={scrollRef}
              onScroll={onScroll}
              className="smooth-transition flex-1 origin-bottom space-y-3 overflow-y-auto bg-transparent px-3 pb-3 pt-3 transition-all duration-500"
              style={{ userSelect: "text", WebkitUserSelect: "text" }}
            >
              <div className="my-0.5 flex items-center justify-center">
                <span className="rounded border border-[#1A1D27] bg-[#11141C] px-2.5 py-0.5 font-mono text-[11px] uppercase tracking-widest text-[#94A3B8]">
                  Сегодня // Сессия #{sessionLabel}
                </span>
              </div>

              {chat.messages.length === 0 && (
                <div className="mt-10 flex flex-col items-center gap-2 px-6 text-center">
                  <span className="material-symbols-outlined text-[26px] text-[#22252C]">
                    graphic_eq
                  </span>
                  <p className="font-mono text-[12px] text-[#555B66]">
                    Скажите команду голосом или введите текст — AETHER выполнит её в вашем рабочем
                    пространстве.
                  </p>
                </div>
              )}

              {chat.messages.map((m) =>
                m.role === "user" ? (
                  <UserMessage key={m.id} message={m} />
                ) : (
                  <AgentMessage
                    key={m.id}
                    message={m}
                    busy={busy && m.id === chat.messages[chat.messages.length - 1]?.id}
                  />
                ),
              )}
            </div>
            <span className="hud-corner hud-corner-br" aria-hidden="true" />
          </div>

          {/* Bottom: input & model selector */}
          <div
            id="bottomInputBox"
            className="smooth-transition z-10 shrink-0 border-t border-[#1A1D26] bg-[#0D1017] px-3 pb-2 pt-3 transition-all duration-500"
          >
            {chat.rateLimitState && chat.rateLimitState.phase && (
              <div className="mb-2 flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 font-mono text-[12px] text-amber-300">
                <span className="material-symbols-outlined text-[14px]">hourglass_empty</span>
                <span className="flex-1">
                  {chat.rateLimitState.phase === "waiting" && (
                    <>
                      ⏳ Лимит на <b>{chat.rateLimitState.provider}</b> — ждём{" "}
                      <b>{chat.rateLimitState.wait_seconds}с</b>…
                    </>
                  )}
                  {chat.rateLimitState.phase === "fallback" && (
                    <>
                      🔄 Переход на <b>{chat.rateLimitState.fallback_provider}</b>…
                    </>
                  )}
                  {chat.rateLimitState.phase === "exhausted" && (
                    <>
                      ⚠️ Лимит исчерпан на <b>{chat.rateLimitState.provider}</b>
                    </>
                  )}
                </span>
                <button
                  type="button"
                  onClick={() => chat.stop()}
                  className="rounded border border-amber-500/40 bg-amber-500/20 px-1.5 py-0.5 transition-colors hover:bg-amber-500/30"
                >
                  Отмена
                </button>
              </div>
            )}

            <div className="rounded-xl border border-[#202534] bg-[#12151E] p-2.5 transition-colors focus-within:border-emerald-500/60">
              {attachments.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1.5 border-b border-[#1A1E29] pb-2">
                  {attachments.map((a, i) => (
                    <span
                      key={`${a.path}-${i}`}
                      className="flex items-center gap-1 rounded border border-[#222736] bg-[#171A24] px-1.5 py-0.5 font-mono text-[12px] text-[#CBD5E1]"
                    >
                      {isImage(a.mime) && (
                        <span className="material-symbols-outlined text-[13px] text-emerald-400">
                          image
                        </span>
                      )}
                      <span className="max-w-[140px] truncate">{a.name}</span>
                      <button
                        type="button"
                        onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                        className="text-[#6E7480] transition-colors hover:text-rose-300"
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}

              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={chat.input}
                  onChange={(e) => chat.setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void handleSend();
                    }
                  }}
                  placeholder={recording ? "Слушаю…" : "Сказать команду или ввести текст…"}
                  className="w-full bg-transparent text-sm text-[#F1F5F9] placeholder-[#94A3B8] focus:outline-none"
                />
                <button
                  type="button"
                  onClick={toggleRecording}
                  title={recording ? "Остановить запись" : "Включить запись голоса"}
                  className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-lg border transition-all hover:scale-105 ${
                    recording
                      ? "border-emerald-400/60 bg-emerald-500/25 text-emerald-300"
                      : "border-emerald-500/30 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20"
                  }`}
                >
                  <svg
                    className="h-3.5 w-3.5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    viewBox="0 0 24 24"
                  >
                    <path
                      d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </div>

              <div className="mt-2 flex items-center justify-between border-t border-[#1A1E29] pt-2">
                <div className="flex items-center gap-1.5 overflow-x-hidden">
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
                    onClick={() => fileInputRef.current?.click()}
                    title="Прикрепить файл или изображение"
                    className="flex shrink-0 items-center gap-1 rounded border border-[#222736] bg-[#171A24] px-1.5 py-0.5 font-mono text-[12px] text-[#CBD5E1] transition-colors hover:bg-[#1E2332] hover:text-white"
                  >
                    <span className="text-sm font-light leading-none">+</span>
                    <span>Файл</span>
                  </button>

                  <button
                    type="button"
                    onClick={() => setModelOpen(true)}
                    title={providerLabel ? `${modelLabel} · ${providerLabel}` : modelLabel}
                    className="flex max-w-[170px] shrink-0 items-center gap-1 truncate rounded border border-[#222736] bg-[#171A24] px-1.5 py-0.5 font-mono text-[12px] text-[#CBD5E1] transition-colors hover:bg-[#1E2332] hover:text-white"
                  >
                    {providerLabel && <span className="truncate text-[#94A3B8]">{providerLabel} /</span>}
                    <span className="truncate font-medium text-[#E2E8F0]">{modelLabel}</span>
                    <svg
                      className="h-2.5 w-2.5 shrink-0 opacity-70"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      viewBox="0 0 24 24"
                    >
                      <path d="M19 9l-7 7-7-7" />
                    </svg>
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      setHistoryOpen(true);
                      chat.refreshHistory();
                    }}
                    title="История чата"
                    className="flex shrink-0 items-center gap-1 rounded border border-[#222736] bg-[#171A24] px-1.5 py-0.5 font-mono text-[12px] text-[#CBD5E1] transition-colors hover:bg-[#1E2332] hover:text-white"
                  >
                    <span className="material-symbols-outlined text-[13px]">history</span>
                    <span>История</span>
                  </button>
                </div>

                <button
                  type="button"
                  onClick={handleSend}
                  disabled={!busy && !chat.input.trim() && attachments.length === 0}
                  title={busy ? "Остановить агента" : "Отправить"}
                  className={`ml-1 flex shrink-0 items-center gap-1 rounded px-2.5 py-0.5 font-mono text-[12px] font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                    busy
                      ? "bg-rose-500/90 text-white hover:brightness-110"
                      : "bg-emerald-500 text-[#07130D] hover:bg-emerald-400"
                  }`}
                >
                  <span>{busy ? "Стоп" : "Send"}</span>
                  <span className="text-[11px] opacity-75">{busy ? "■" : "↵"}</span>
                </button>
              </div>
            </div>
          </div>

          {/* =============== VOICE MODE BOTTOM CONTROLS =============== */}
          <div
            id="voiceModeBottomControls"
            className="smooth-transition absolute inset-x-0 bottom-0 z-[25] p-3 transition-all duration-500"
          >
              <div className="voice-actions-grid">
                <button
                  type="button"
                  className="voice-action-btn"
                  onClick={toggleRecording}
                  title={recording ? "Остановить запись" : "Включить микрофон"}
                >
                  <svg
                    style={{ color: "var(--jarvis-voice-mic)" }}
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    viewBox="0 0 24 24"
                  >
                    <path
                      d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
                      strokeLinecap="round"
                    />
                  </svg>
                  <span>{recording ? "Слушаю…" : "Микрофон"}</span>
                </button>
                <button
                  type="button"
                  className="voice-action-btn"
                  onClick={() => chat.stop()}
                  title="Остановить агента"
                >
                  <svg
                    style={{ color: "var(--jarvis-voice-stop)", fill: "currentColor" }}
                    viewBox="0 0 24 24"
                  >
                    <rect height="12" rx="1.5" width="12" x="6" y="6" />
                  </svg>
                  <span>Стоп</span>
                </button>
                <button
                  type="button"
                  className="voice-action-btn"
                  onClick={() => switchMode("core")}
                  title="Перейти в чат"
                >
                  <svg
                    style={{ color: "var(--jarvis-voice-muted)" }}
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    viewBox="0 0 24 24"
                  >
                    <path
                      d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
                      strokeLinecap="round"
                    />
                  </svg>
                  <span>Чат</span>
                </button>
              </div>
          </div>
        </div>
      </div>

      {modelOpen && (
        <AetherModelModal
          primaryModel={chat.config.selected_primary}
          onPrimaryChange={chat.handlePrimaryChange}
          apiKeys={chat.config.api_keys}
          onConnectProvider={chat.updateApiKey}
          onClose={() => setModelOpen(false)}
        />
      )}

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
    </>
  );
}
