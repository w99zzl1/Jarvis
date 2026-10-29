import React, { useEffect, useRef, useState } from "react";
import { Send, Square, History, Paperclip, Image as ImageIcon } from "lucide-react";
import { motion, AnimatePresence } from "motion/react";
import ModelSelector from "./ModelSelector";
import Markdown from "./Markdown";
import TaskList from "./TaskList";
import {
  cleanText,
  useChat,
  type ActivityEntry,
} from "../useChat";
import { type FileAttachment, uploadFiles } from "../api";


interface ChatPanelProps {
  isListening: boolean;
  isSpeaking: boolean;
  setIsSpeaking: (v: boolean) => void;
  fill?: boolean;
}

function activityColor(a: ActivityEntry) {
  if (a.status === "failed") return "#ffb4ab";
  if (a.status === "running") return "#00dbe9";
  if (a.kind === "say") return "rgba(225,226,234,0.55)";
  return "#cfbdff";
}

function fmtAgo(updatedSec: number): string {
  const secs = Math.max(0, Date.now() / 1000 - updatedSec);
  if (secs < 60) return "только что";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} м назад`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.floor(hours / 24);
  return `${days} дн назад`;
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

export default function ChatPanel({ isListening, setIsSpeaking, fill }: ChatPanelProps) {
  const chat = useChat(setIsSpeaking, "main");
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<{ file: FileAttachment; previewUrl: string | null }[]>([]);
  const [dragOver, setDragOver] = useState(false);

  useEffect(() => {
    chat.loadConfig();
    chat.refreshHistory();
  }, []);

  // Global paste handler (Ctrl+V / Cmd+V) — works when the panel is focused.
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const items = Array.from(e.clipboardData?.items || []);
      const files = items
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((f): f is File => f !== null);
      if (files.length > 0) {
        e.preventDefault();
        handleFiles(files);
      }
    };
    const onDrop = (e: DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      if (e.dataTransfer?.files.length) {
        handleFiles(Array.from(e.dataTransfer.files));
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
    // Filter out folders / invalid
    const valid = files.filter((f) => f.type || f.name.includes("."));
    if (valid.length === 0) return;

    try {
      const uploaded = await uploadFiles(valid);
      const now = Date.now();
      setAttachments((prev) => [
        ...prev,
        ...uploaded.map((att, i) => {
          // Create a local preview URL for images (frontend-only, not sent to server)
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

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const isImage = (mime?: string) => !!mime && mime.startsWith("image/");

  return (
    <div
      style={{
        width: "100%",
        height: fill ? "100%" : undefined,
        display: "flex",
        flexDirection: "column",
        gap: "8px",
        fontFamily: "'Inter', sans-serif",
        position: "relative",
      }}
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

      <TaskList events={chat.taskEvents} isExecuting={chat.isExecuting} />

      {attachments.length > 0 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "6px",
            padding: "0 4px",
            alignItems: "center",
          }}
        >
          {attachments.map(({ file, previewUrl }, idx) => {
            const img = isImage(file.mime);
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
                    alt={file.name}
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
                <span title={file.name} style={{ maxWidth: "120px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {file.name}
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

      <div
        ref={scrollRef}
        style={{
          ...(fill ? { flex: 1, minHeight: 0 } : { height: "200px" }),
          overflowY: "auto",
          padding: "2px",
          display: "flex",
          flexDirection: "column",
          gap: "10px",
          userSelect: "text",
          WebkitUserSelect: "text",
        }}
      >
        <AnimatePresence>
          {chat.messages.map((msg) => (
            <motion.div
              key={msg.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2 }}
              style={{
                display: "flex",
                justifyContent: msg.role === "user" ? "flex-end" : "flex-start",
              }}
            >
              <div
                onMouseDown={(e) => e.stopPropagation()}
                style={{
                  maxWidth: "88%",
                  padding: "10px 14px",
                  borderRadius: msg.role === "user" ? "12px 12px 4px 12px" : "12px 12px 12px 4px",
                  background:
                    msg.role === "user"
                      ? "rgba(39, 42, 48, 0.6)"
                      : "rgba(25, 28, 33, 0.7)",
                  border: `1px solid ${
                    msg.role === "user" ? "rgba(255,255,255,0.08)" : "rgba(255,255,255,0.06)"
                  }`,
                  backdropFilter: "blur(16px)",
                  color: "var(--on-surface)",
                  fontSize: "13px",
                  lineHeight: "1.55",
                  cursor: "text",
                }}
              >
                {msg.role === "assistant" && (msg.activity?.length ?? 0) > 0 && (
                  <div
                    style={{
                      marginBottom: msg.text ? "8px" : 0,
                      paddingBottom: msg.text ? "8px" : 0,
                      borderBottom: msg.text ? "1px solid rgba(0, 240, 255, 0.15)" : "none",
                      display: "flex",
                      flexDirection: "column",
                      gap: "3px",
                    }}
                  >
                    {msg.activity!.map((a) => (
                      <div
                        key={a.id}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "6px",
                          fontSize: "10px",
                          lineHeight: "1.45",
                          fontFamily: "'Space Mono', monospace",
                          letterSpacing: "0.06em",
                          color: activityColor(a),
                          fontStyle: a.kind === "say" ? "italic" : "normal",
                        }}
                      >
                        <span style={{ flexShrink: 0, opacity: 0.8 }}>
                          {a.kind === "say"
                            ? "›"
                            : a.status === "running"
                            ? "▸"
                            : a.status === "failed"
                            ? "✕"
                            : "•"}
                        </span>
                        <span style={{ wordBreak: "break-word" }}>{a.text}</span>
                      </div>
                    ))}
                  </div>
                )}

                {msg.attachments && msg.attachments.length > 0 && (
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      gap: "6px",
                      marginTop: "8px",
                    }}
                  >
                    {msg.attachments.map((att, idx) => {
                      const img = isImage(att.mime);
                      return (
                        <div
                          key={idx}
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            alignItems: "center",
                            gap: "4px",
                          }}
                        >
                          {img && att.previewUrl ? (
                            <img
                              src={att.previewUrl}
                              alt={att.name}
                              style={{
                                width: "120px",
                                height: "120px",
                                borderRadius: "8px",
                                objectFit: "cover",
                                border: "1px solid rgba(255,255,255,0.15)",
                              }}
                            />
                          ) : img ? (
                            <ImageIcon
                              size={16}
                              style={{ color: "rgba(0, 240, 255, 0.8)" }}
                            />
                          ) : null}
                          {!img && (
                            <div style={{ fontSize: "10px", opacity: 0.6, textAlign: "center" }}>
                              {att.name}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}

                {msg.text ? (
                  <Markdown text={cleanText(msg.text)} />
                ) : chat.isExecuting && msg.id === chat.messages[chat.messages.length - 1]?.id ? (
                  <span
                    style={{
                      opacity: 0.5,
                      fontFamily: "'Space Mono', monospace",
                      fontSize: "10px",
                      letterSpacing: "0.1em",
                      textTransform: "uppercase",
                    }}
                  >
                    processing…
                  </span>
                ) : null}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>

      <div style={{ padding: "0 4px" }} data-no-drag>
        <ModelSelector
          primaryModel={chat.config.selected_primary}
          onPrimaryChange={chat.handlePrimaryChange}
          apiKeys={chat.config.api_keys}
          onApiKeyChange={chat.updateApiKey}
        />
      </div>

      <div
        data-no-drag
        style={{
          display: "flex",
          alignItems: "center",
          gap: "8px",
          padding: "0 4px",
          marginTop: "2px",
          marginBottom: "2px",
        }}
      >
        <button
          type="button"
          onClick={() => {
            setHistoryOpen(true);
            chat.refreshHistory();
          }}
          className="flex h-8 w-8 items-center justify-center rounded-lg text-on-surface-variant transition-colors hover:bg-surface-container-high hover:text-on-surface"
          title="История чата"
        >
          <History size={16} />
        </button>
        <button
          type="button"
          onClick={() => {
            chat.reset();
            setActiveConversationId(null);
            setHistoryOpen(false);
          }}
          className="flex h-8 w-8 items-center justify-center rounded-lg text-on-surface-variant transition-colors hover:bg-surface-container-high hover:text-on-surface"
          title="Новый чат (контекст сброшен)"
        >
          <span className="material-symbols-outlined text-[14px]">add</span>
        </button>
      </div>

      <div
        data-no-drag
        style={{ display: "flex", gap: "8px", padding: "0 4px", marginBottom: "4px" }}
      >
        <input
          ref={inputRef}
          type="text"
          value={chat.input}
          onChange={(e) => chat.setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isListening ? "Слушаю..." : "Директива для JARVIS..."}
          disabled={chat.loading}
          style={{
            flex: 1,
            padding: "10px 14px",
            borderRadius: "12px",
            border: "1px solid rgba(255,255,255,0.08)",
            background: "rgba(11, 14, 19, 0.6)",
            color: "var(--on-surface)",
            fontSize: "12px",
            fontFamily: "'Inter', sans-serif",
            outline: "none",
            transition: "border-color 0.2s",
          }}
          onFocus={(e) => (e.target.style.borderColor = "rgba(0, 240, 255, 0.4)")}
          onBlur={(e) => (e.target.style.borderColor = "rgba(255,255,255,0.08)")}
        />
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
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          className="flex h-8 w-8 items-center justify-center rounded-lg text-on-surface-variant transition-colors hover:bg-surface-container-high hover:text-on-surface"
          title="Прикрепить файл или изображение"
          disabled={chat.isExecuting || chat.loading}
        >
          <Paperclip size={16} />
        </button>
        <button
          type="button"
          onClick={handleSend}
          disabled={(chat.isExecuting || chat.loading) ? false : !chat.input.trim() && attachments.length === 0}
          style={{
            width: "40px",
            height: "40px",
            borderRadius: "12px",
            border: "none",
            background:
              (chat.isExecuting || chat.loading)
                ? "rgba(255,180,171,0.9)"
                : (chat.input.trim() || attachments.length > 0)
                ? "linear-gradient(135deg, #00f0ff, #00dbe9)"
                : "rgba(255,255,255,0.03)",
            color: (chat.isExecuting || chat.loading) ? "#690005" : (chat.input.trim() || attachments.length > 0) ? "#002022" : "rgba(255,255,255,0.2)",
            cursor: (chat.isExecuting || chat.loading) || chat.input.trim() || attachments.length > 0 ? "pointer" : "default",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
            boxShadow: (chat.isExecuting || chat.loading) ? "0 0 12px rgba(255,180,171,0.25)" : (chat.input.trim() || attachments.length > 0) ? "0 0 16px rgba(0,240,255,0.35)" : "none",
            transition: "all 0.2s",
          }}
          title={(chat.isExecuting || chat.loading) ? "Stop agent" : attachments.length > 0 ? `Отправить с ${attachments.length} вложением(ями)` : "Send"}
        >
          {(chat.isExecuting || chat.loading) ? <Square size={15} /> : <Send size={15} />}
        </button>
      </div>

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
    </div>
  );
}