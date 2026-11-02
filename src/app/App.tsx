import React, { useState, useCallback, useEffect, useRef } from "react";
import { Settings, Bot } from "lucide-react";
import { motion } from "motion/react";
import JarvisCore from "./components/JarvisCore";
import ChatPanel from "./components/ChatPanel";
import Dashboard from "./components/Dashboard";
import VoiceButton from "./components/VoiceButton";
import { sendVoiceChat, playAudioFromBase64, getBackendStatus } from "./api";
import type { BackendStatus } from "./api";

import {
  emit,
  listen,
} from "@tauri-apps/api/event";

import {
  invoke,
} from "@tauri-apps/api/core";

const CYAN = "#00D9FF";
const VIOLET = "#8B5CFF";
const MONO = "'Space Mono', monospace";

// Controls that must never drag the window.
const DRAG_EXCLUDE =
  "button, input, textarea, select, a, [role='button'], [data-no-drag], [data-model-option], [data-model-selector]";

const DRAG_ICONS = "svg, img, i.material-symbols-outlined, .material-symbols-outlined";

/**
 * Empty space and panel backgrounds drag the window; anything the user can
 * read or click does not. Text nodes are checked directly (rather than relying
 * on a wrapper attribute) so every label, paragraph and number is protected
 * without having to annotate the whole tree.
 */
const shouldBlockDrag = (el: Element | null): boolean => {
  let node: Element | null = el;
  while (node && node !== document.body) {
    if (node.matches?.(DRAG_EXCLUDE) || node.closest?.(DRAG_EXCLUDE)) return true;
    if (node.closest?.(DRAG_ICONS)) return true;
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE && child.textContent?.trim()) return true;
    }
    node = node.parentElement;
  }
  return false;
};

// Global drag state to prevent focus-sync interference
let isGloballyDragging = false;

// ============================================================
// WINDOW GEOMETRY
// ============================================================

const MAIN_W = 427;
const WINDOW_H = 759;

// ============================================================
// GLASS BUTTON
// ============================================================

function GlassButton({
  icon,
  label,
  active,
  onClick,
  accentColor,
}: {
  icon: React.ReactNode;
  label: string;
  active?: boolean;
  onClick?: () => void;
  accentColor: string;
}) {
  const rgb =
    accentColor === CYAN
      ? "0, 217, 255"
      : "139, 92, 255";

  return (
    <button
      onClick={onClick}
      style={{
        background: "none",
        border: "none",
        cursor: "pointer",
        padding: 0,
      }}
      className="flex flex-col items-center gap-2.5"
    >
      <motion.div
        whileTap={{ scale: 0.93 }}
        style={{
          width: 54,
          height: 54,
          borderRadius: "50%",
          background: active
            ? `rgba(${rgb}, 0.14)`
            : "rgba(255, 255, 255, 0.04)",
          border: `1px solid ${
            active
              ? `rgba(${rgb}, 0.55)`
              : "rgba(255,255,255,0.09)"
          }`,
          backdropFilter: "blur(20px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: active
            ? accentColor
            : "rgba(255,255,255,0.42)",
          boxShadow: active
            ? `0 0 24px rgba(${rgb}, 0.35),
               inset 0 0 16px rgba(${rgb}, 0.08)`
            : "none",
          transition: "all 0.3s ease",
        }}
      >
        {icon}
      </motion.div>

      <span
        style={{
          color: "rgba(255,255,255,0.22)",
          fontSize: "9px",
          letterSpacing: "0.22em",
          textTransform: "uppercase",
          fontFamily: MONO,
        }}
      >
        {label}
      </span>
    </button>
  );
}


// ============================================================
// WINDOW HELPERS
// ============================================================

async function safeCloseWindow(win: any) {
  if (!win) return;

  try {
    await win.close();
  } catch {
    try {
      await win.destroy();
    } catch {
      // already gone
    }
  }
}

// ============================================================
// CROP MARKS (viewfinder / corner brackets)
// ============================================================

function CropMarks({
  color = CYAN,
  inset = 10,
  size = 16,
  thickness = 2,
}: {
  color?: string;
  inset?: number;
  size?: number;
  thickness?: number;
}) {
  const common: React.CSSProperties = {
    position: "absolute",
    width: size,
    height: size,
    pointerEvents: "none",
    zIndex: 60,
  };
  const b = `solid ${thickness}px ${color}`;
  return (
    <>
      <span style={{ ...common, top: inset, left: inset, borderTop: b, borderLeft: b }} />
      <span style={{ ...common, top: inset, right: inset, borderTop: b, borderRight: b }} />
      <span style={{ ...common, bottom: inset, left: inset, borderBottom: b, borderLeft: b }} />
      <span style={{ ...common, bottom: inset, right: inset, borderBottom: b, borderRight: b }} />
    </>
  );
}

// ============================================================
// APP
// ============================================================

export default function App() {
  const params = new URLSearchParams(window.location.search);

  const isAssistantView = params.get("view") === "assistant";

  const isSettingsView = params.get("view") === "settings";

  // In the assistant window we ARE the visible assistant, so Ctrl+D there has
  // to mean "go back to the desktop", not "open myself again".
  const [isAssistantVisible, setIsAssistantVisible] = useState(isAssistantView);
  const [isSettingsVisible, setIsSettingsVisible] = useState(false);
  const [opacity, setOpacity] = useState(0.72);

  const [isListening, setIsListening] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);

  // Local Python backend: Rust launches it and watches /health. A packaged
  // build has no console, so a failure has to show up here instead.
  const [backend, setBackend] = useState<BackendStatus>({
    ready: false,
    failed: false,
    message: "",
    mode: "",
  });

  // ==========================================================
  // VIEW
  // ==========================================================

  const assistantVisibleRef = useRef(isAssistantView);
  const settingsVisibleRef = useRef(false);

  // Load persisted opacity on mount
  useEffect(() => {
    invoke("get_opacity").then((o: number) => setOpacity(o)).catch(() => {});
    
    // Listen for opacity changes from Rust
    const unlistenOpacity = listen("opacity-changed", (event: any) => {
      setOpacity(event.payload);
    });

    // The assistant can also be closed from its own window, so the main
    // window has to learn about it instead of trusting its own flag.
    const unlistenAssistant = listen("assistant-visibility", (event: any) => {
      const visible = !!event.payload;
      assistantVisibleRef.current = visible;
      setIsAssistantVisible(visible);
    });

    return () => {
      unlistenOpacity.then(u => u());
      unlistenAssistant.then(u => u());
      unlistenAudioStart.then(u => u());
      unlistenAudioStop.then(u => u());
    };
  }, []);

  // ==========================================================
  // AUDIO CAPTURE (microphone ? Rust ? Python STT)
  // The frontend is only a pipe: Rust commands start/stop capture,
  // chunks flow frontend ? Rust ? Python STT ? text back to frontend.
  // ==========================================================
  const [mediaRecorderRef, setMediaRecorderRef] = useRef<MediaRecorder | null>(null);
  const [audioChunksRef, setAudioChunksRef] = useRef<Blob[]>([]);
  const [sessionIdRef, setSessionIdRef] = useRef<string>("default");

  const sendAudioChunk = useCallback(async (chunk: Blob) => {
    try {
      const arrayBuffer = await chunk.arrayBuffer();
      const base64 = btoa(String.fromCharCode(...new Uint8Array(arrayBuffer)));
      await invoke("audio_chunk", { session_id: sessionIdRef.current, data: base64 });
    } catch (e) {
      console.error("[Voice] Failed to send audio chunk:", e);
    }
  }, []);

  useEffect(() => {
    let unlistenStart: (() => Promise<void>) | undefined;
    let unlistenStop: (() => Promise<void>) | undefined;

    unlistenStart = listen("audio:start-capture", async (event: any) => {
      const payload = event.payload || {};
      const sid = payload.session_id || "default";
      sessionIdRef.current = sid;
      audioChunksRef.current = [];

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const recorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
        mediaRecorderRef.current = recorder;

        recorder.ondataavailable = (event: BlobEvent) => {
          if (event.data && event.data.size > 0) {
            audioChunksRef.current.push(event.data);
            void sendAudioChunk(event.data);
          }
        };

        recorder.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
        };

        recorder.start(100);
      } catch (e) {
        console.error("[Voice] Microphone access error:", e);
      }
    });

    unlistenStop = listen("audio:stop-capture", async () => {
      try {
        if (mediaRecorderRef.current) {
          mediaRecorderRef.current.stop();
        }
      } catch (e) {
        console.error("[Voice] Stop recorder error:", e);
      }
    });

    return () => {
      unlistenStart?.then(u => u());
      unlistenStop?.then(u => u());
    };
  }, [sendAudioChunk]);

  // Backend status: ask once on mount, then follow the Rust watchdog.
  useEffect(() => {
    getBackendStatus().then(setBackend).catch(() => {});

    const unlisten = listen<BackendStatus>("backend-status", (event: any) => {
      setBackend(event.payload);
    });

    return () => {
      unlisten.then(u => u());
    };
  }, []);

  // ==========================================================
  // CREATE / CLOSE WINDOWS
  // ==========================================================

  const toggleChatWindow = useCallback(async () => {
    const next = !isAssistantVisible;
    setIsAssistantVisible(next);
    assistantVisibleRef.current = next;
    await invoke(next ? "show_assistant" : "hide_assistant");
  }, [isAssistantVisible]);

  // The Assistant button is a press-plate, not a lever: it only ever opens the
  // console. Closing happens from the assistant window itself.
  const openAssistant = useCallback(async () => {
    if (assistantVisibleRef.current) return;
    assistantVisibleRef.current = true;
    setIsAssistantVisible(true);
    await invoke("show_assistant");
  }, []);

  const toggleSettingsWindow = useCallback(async () => {
    const next = !isSettingsVisible;
    setIsSettingsVisible(next);
    settingsVisibleRef.current = next;
    await invoke(next ? "show_settings" : "hide_settings");
  }, [isSettingsVisible]);

  // ==========================================================
  // VOICE
  // ==========================================================

  const handleVoiceTranscript =
    useCallback(
      async (text: string) => {
        if (!text) return;

        try {
          setIsSpeaking(true);

          const resp =
            await sendVoiceChat(text);

          if (resp.audio) {
            await playAudioFromBase64(
              resp.audio
            );
          }
        } catch (e) {
          console.error(
            "Voice chat error:",
            e
          );
        } finally {
          setIsSpeaking(false);
        }
      },
      []
    );

  // ==========================================================
  // CONNECTOR VIEW - REMOVED
  // ==========================================================

  // ==========================================================
  // UNIFIED DRAG (rigid body)
  //
  // Either window (main or assistant) can be dragged; the other
  // follows synchronously so they behave as one solid unit.
  // ==========================================================

  useEffect(() => {
    const handler = async (e: MouseEvent) => {
      if (e.button !== 0) return;
      const target = e.target as HTMLElement;

      if (shouldBlockDrag(target)) {
        return;
      }

      e.preventDefault();
      isGloballyDragging = true;
      emit("drag-start").catch(() => {});

      const startX = e.screenX;
      const startY = e.screenY;
      // Last screen position we already sent to Rust, so we send only the
      // INCREMENTAL delta each frame (no drift / no accumulation error).
      let lastSentX = startX;
      let lastSentY = startY;

      let rafId = 0;
      let lastEv: MouseEvent | null = null;

      const applyMove = () => {
        rafId = 0;
        const ev = lastEv;
        if (!ev) return;

        const incDx = ev.screenX - lastSentX;
        const incDy = ev.screenY - lastSentY;
        lastSentX = ev.screenX;
        lastSentY = ev.screenY;

        // One command -> Rust moves main + visible assistant/settings
        // atomically (DeferWindowPos). No per-window async calls.
        invoke("move_group", { dx: incDx, dy: incDy }).catch(() => {});
      };

      const onMove = (ev: MouseEvent) => {
        lastEv = ev;
        if (rafId) return;
        rafId = requestAnimationFrame(applyMove);
      };

      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        if (rafId) cancelAnimationFrame(rafId);
        isGloballyDragging = false;
        emit("drag-end").catch(() => {});
      };

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    };

    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // ==========================================================
  // FOCUS SYNC - REMOVED
  // ==========================================================


  // ==========================================================
  // GLOBAL SHORTCUT: Ctrl/Cmd + D
  //
  // Roll up the chat (assistant) the same way the
  // Assistant button does. Works while the app is focused.
  // ==========================================================

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (
        (e.ctrlKey || e.metaKey) &&
        (e.key === "d" || e.key === "D")
      ) {
        e.preventDefault();
        toggleChatWindow();
      }
    };

    window.addEventListener("keydown", onKey);

    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [toggleChatWindow]);

  // ==========================================================
  // CONNECTOR VIEW - REMOVED
  // ==========================================================

  // ==========================================================
  // ASSISTANT VIEW
  // ==========================================================

  if (isAssistantView) {
    return (
      <>
        <style>{`
        html,
        body,
        #root {
          margin: 0 !important;
          padding: 0 !important;
          width: 100% !important;
          height: 100% !important;
          background: transparent !important;
          overflow: hidden !important;
        }
      `}</style>

        <Dashboard
          isListening={isListening}
          isSpeaking={isSpeaking}
          setIsSpeaking={setIsSpeaking}
        />
      </>
    );
  }

  // ==========================================================
  // SETTINGS VIEW
  // ==========================================================

// ==========================================================
// SETTINGS VIEW
// ==========================================================

if (isSettingsView) {
    const setOpacityVal = async (val: number) => {
      const v = Math.round(val * 100) / 100;
      await invoke("set_opacity", { opacity: v });
    };
    const resetOpacity = async () => {
      await invoke("set_opacity", { opacity: 1 });
    };

    return (
      <><style>{`
        html,
        body,
        #root {
          margin: 0 !important;
          padding: 0 !important;
          width: 100% !important;
          height: 100% !important;
          background: transparent !important;
          overflow: hidden !important;
        }
      `}</style>
        <div
          style={{
            width: "100%",
            height: "100%",
            position: "fixed",
            top: 0,
            left: 0,
            zIndex: 0,
          }}
        />

          <div
          data-panel-bg
          style={{
            width: "100%",
            height: "100%",
            background: `rgba(1, 4, 8, ${opacity})`,
            display: "flex",
            flexDirection: "column",
            justifyContent: "flex-start",
            padding: "16px",
            boxSizing: "border-box",
            fontFamily: MONO,
            color: "rgba(255,255,255,0.85)",
            position: "relative",
            zIndex: 1,
          }}
        >
          <CropMarks color={VIOLET} />
<div
              style={{
                fontSize: "13px",
                letterSpacing: "0.22em",
                textTransform: "uppercase",
                color: VIOLET,
                marginBottom: "14px",
              }}
            >
            Settings
          </div>

          {/* Existing Settings */}
          <div style={{ marginBottom: "18px" }}>
            <div
              style={{
                fontSize: "10px",
                letterSpacing: "0.15em",
                textTransform: "uppercase",
                color: "rgba(255,255,255,0.5)",
                marginBottom: "8px",
              }}
            >
              Прозрачность: {Math.round(opacity * 100)}%
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              <input
                type="range"
                min={10}
                max={100}
                value={Math.round(opacity * 100)}
                onChange={(e) => setOpacityVal(Number(e.target.value) / 100)}
                style={{ flex: 1, cursor: "pointer" }}
              />
              <button
                onClick={resetOpacity}
                title="Reset to 100%"
                aria-label="Reset opacity"
                style={{
                  background: "none",
                  border: "1px solid rgba(139,92,255,0.6)",
                  borderRadius: "50%",
                  width: 24,
                  height: 24,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                  padding: 0,
                  color: VIOLET,
                  flexShrink: 0,
                }}
              >
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M1 4v6h6" />
                  <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      </>
    );
  }

  // ==========================================================
  // MAIN VIEW
  // ==========================================================

  return (
    <>
      <style>{`
        html,
        body,
        #root {
          margin: 0;
          padding: 0;
          width: 100%;
          height: 100%;
          background: transparent;
          overflow: hidden;
        }

        * {
          box-sizing: border-box;
        }
      `}</style>

      <div
        style={{
          width: "100%",
          height: "100%",
          position: "fixed",
          top: 0,
          left: 0,
          zIndex: 0,
        }}
      />

      <div
        style={{
          width: "100%",
          height: "100%",
          position: "relative",
          zIndex: 1,
        }}
      >
         {/* MAIN AREA — fixed "frame", never resizes */}
         <div
           data-panel-bg
           style={{
             position: "absolute",
             top: 0,
             left: 0,
             width: MAIN_W,
             height: WINDOW_H,

            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "space-between",
            overflow: "hidden",
            fontFamily: MONO,
            paddingTop: "16px",
            paddingBottom: "12px",
            paddingLeft: "24px",
            paddingRight: "24px",
            background: `rgba(1, 4, 8, ${opacity})`,
            borderRadius: 0,
          }}
        >
          <CropMarks color={CYAN} />
          {/* ... MAIN CONTENT ... */}
          <div
            className="flex flex-col items-center gap-1.5 z-10"
            style={{
              marginTop: "8px",
            }}
          >
            <motion.p
              style={{
                color: backend.failed ? "#FF5C7A" : CYAN,
                letterSpacing: "0.46em",
                fontSize: "9.5px",
                opacity: 0.4,
                fontFamily: MONO,
              }}
              animate={{
                opacity: isSpeaking
                  ? [0.4, 0.9, 0.4]
                  : [0.4, 0.7, 0.4],
              }}
              transition={{
                duration:
                  isSpeaking
                    ? 1.0
                    : 3.5,
                repeat: Infinity,
                ease: "easeInOut",
              }}
            >
              {isSpeaking
                ? "SPEAKING"
                : isListening
                ? "LISTENING"
                : backend.failed
                ? "BACKEND OFFLINE"
                : backend.ready
                ? "SYSTEM ONLINE"
                : "STARTING BACKEND"}
            </motion.p>

            <h1
              style={{
                color:
                  "rgba(255,255,255,0.85)",
                letterSpacing:
                  "0.42em",
                fontSize: "19px",
                fontWeight: 400,
                fontFamily: MONO,
                margin: 0,
              }}
            >
              JARVIS
            </h1>
          </div>

          {backend.failed && (
            <div
              data-no-drag
              title={backend.message}
              style={{
                width: "100%",
                margin: "4px 0 6px",
                padding: "6px 8px",
                border: "1px solid rgba(255,92,122,0.5)",
                background: "rgba(255,92,122,0.07)",
                color: "rgba(255,196,206,0.9)",
                fontFamily: MONO,
                fontSize: "9px",
                lineHeight: 1.45,
                textAlign: "center",
                overflow: "hidden",
                display: "-webkit-box",
                WebkitLineClamp: 3,
                WebkitBoxOrient: "vertical",
              }}
            >
              {backend.message || "The local backend is not running."}
            </div>
          )}

          {/* CORE */}

          <div
            className="flex-1 flex items-center justify-center z-10"
            style={{
              width: "100%",
              maxWidth: "95%",
              minHeight: 0,
            }}
          >
            <JarvisCore />
          </div>

          {/* BOTTOM */}

          <div
            className="w-full z-10 flex flex-col gap-0"
            style={{
              marginBottom: "8px",
            }}
          >
            <div
              style={{
                marginBottom: "12px",
              }}
            >
              <ChatPanel
                isListening={isListening}
                isSpeaking={isSpeaking}
                setIsSpeaking={setIsSpeaking}
              />
            </div>

            <div className="flex w-full justify-between items-end">
              <div className="flex items-end gap-4">
                <VoiceButton
                  isListening={isListening}
                  setIsListening={setIsListening}
                  onTranscript={handleVoiceTranscript}
                />

                <GlassButton
                  icon={<Bot size={18} />}
                  label="Assistant"
                  accentColor={CYAN}
                  onClick={openAssistant}
                />
              </div>

              <GlassButton
                icon={<Settings size={18} />}
                label="Settings"
                accentColor={VIOLET}
                onClick={toggleSettingsWindow}
                active={isSettingsVisible}
              />
            </div>
          </div>
        </div>

        {/* ASSISTANT and SETTINGS now live in their own
            WebViewWindows (assistant / settings) */}
      </div>
    </>
  );
}
