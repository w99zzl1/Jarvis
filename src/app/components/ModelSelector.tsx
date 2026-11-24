import React, { useState, useEffect, useMemo, useCallback } from "react";
import { createPortal } from "react-dom";
import {
  ModelOption,
  ProviderId,
  PROVIDER_LABELS,
  PROVIDER_ENV_HINT,
  modelKey,
  classifyModel,
  TIER_LABELS,
} from "../models";
import { fetchLiveModels } from "../api";

interface ModelSelectorProps {
  primaryModel: { provider: ProviderId; model: string } | null;
  onPrimaryChange: (spec: { provider: ProviderId; model: string }) => void;
  apiKeys: Record<string, string>;
  onApiKeyChange: (provider: string, key: string) => void;
  /** The owner renders its own trigger (HUD pill / modal) — hide the built-in one. */
  hideTrigger?: boolean;
  /** Controlled open state (used together with hideTrigger). */
  forceOpen?: boolean;
  /** Notifies the owner when the panel wants to open/close (controlled mode). */
  onOpenChange?: (open: boolean) => void;
}

const PROVIDER_COLORS: Record<string, string> = {
  Groq: "var(--jarvis-accent)",
  Google: "#4285F4",
  OpenAI: "#10A37F",
  OpenRouter: "#A78BFA",
  "NVIDIA NIM": "#76B900",
  "Self-hosted": "#9AA6B8",
};

const KEY_ORDER: ProviderId[] = ["groq", "google", "openai", "openrouter", "nim"];

type FilterId = "all" | "fast" | "smart" | "preview";

const FILTERS: { id: FilterId; label: string }[] = [
  { id: "all", label: "All" },
  { id: "fast", label: "Fast" },
  { id: "smart", label: "Reasoning" },
  { id: "preview", label: "Preview" },
];

const TIER_ICON: Record<string, string> = {
  fast: "bolt",
  smart: "neurology",
  preview: "science",
};

function tierBadge(tier: string | undefined): string {
  if (tier === "fast") return "bg-primary-container/15 text-primary-container";
  if (tier === "smart") return "bg-secondary/20 text-secondary";
  return "bg-surface-container text-outline";
}

export default function ModelSelector({
  primaryModel,
  onPrimaryChange,
  apiKeys,
  onApiKeyChange,
  hideTrigger = false,
  forceOpen,
  onOpenChange,
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"models" | "keys">("models");
  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
  const [liveModels, setLiveModels] = useState<Record<string, string[]>>({});
  const [loadingLive, setLoadingLive] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<FilterId>("all");
  const [pending, setPending] = useState<{ provider: ProviderId; model: string } | null>(primaryModel);

  const open = forceOpen ?? isOpen;

  const closePanel = useCallback(() => {
    setIsOpen(false);
    onOpenChange?.(false);
  }, [onOpenChange]);

  const loadLiveModels = useCallback(async () => {
    setLoadingLive(true);
    const res = await fetchLiveModels(apiKeys);
    setLiveModels(res);
    setLoadingLive(false);
  }, [apiKeys]);

  useEffect(() => {
    loadLiveModels();
  }, [loadLiveModels]);

  useEffect(() => {
    const init: Record<string, string> = {};
    for (const [k, v] of Object.entries(apiKeys || {})) {
      init[k] = typeof v === "string" ? v : (v as { key?: string } | undefined)?.key || "";
    }
    setKeyInputs(init);
  }, [apiKeys]);

  // Stage the current selection whenever the panel (re)opens.
  useEffect(() => {
    if (open) setPending(primaryModel ?? null);
  }, [open, primaryModel]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") closePanel();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closePanel]);

  const dynamicCatalog = useMemo(() => {
    const catalog: ModelOption[] = [];
    for (const [provider, models] of Object.entries(liveModels)) {
      // Порядок приходит с бэкенда: стабильные модели первыми, preview последними.
      for (const m of models) {
        catalog.push({
          provider: provider as ProviderId,
          model: m,
          label: m,
          group: PROVIDER_LABELS[provider as ProviderId] || provider,
          tier: classifyModel(m),
        });
      }
    }
    return catalog;
  }, [liveModels]);

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    const map = new Map<string, ModelOption[]>();
    for (const opt of dynamicCatalog) {
      if (filter !== "all" && opt.tier !== filter) continue;
      if (q && !opt.model.toLowerCase().includes(q) && !opt.group.toLowerCase().includes(q)) continue;
      if (!map.has(opt.group)) map.set(opt.group, []);
      map.get(opt.group)!.push(opt);
    }
    return map;
  }, [dynamicCatalog, query, filter]);

  const selected = pending ?? primaryModel;
  const engineCount = Object.keys(liveModels).length;
  const modelCount = dynamicCatalog.length;

  const engage = () => {
    if (
      selected &&
      (!primaryModel || selected.provider !== primaryModel.provider || selected.model !== primaryModel.model)
    ) {
      onPrimaryChange({ provider: selected.provider, model: selected.model });
    }
    closePanel();
  };

  const openPanel = () => {
    if (open) return;
    setIsOpen(true);
    onOpenChange?.(true);
  };

  const colorFor = (group: string) => PROVIDER_COLORS[group] || "#9AA6B8";

  const tabActive = "bg-primary-container text-on-primary-container font-bold shadow-sm";
  const tabIdle = "text-outline hover:text-on-surface";

  const modal = open
    ? createPortal(
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={closePanel}
          data-no-drag
          data-model-trigger
        >
          <div
            className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-outline-variant/25 bg-surface-container-lowest/98 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            data-model-selector
          >
            {/* Header */}
            <div className="flex items-center justify-between gap-3 border-b border-outline-variant/30 px-6 pb-4 pt-6">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-primary-container/15 text-primary-container">
                  <span className="material-symbols-outlined text-[20px]">hub</span>
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-[16px] font-bold tracking-wide text-on-surface">
                      Neural Engine Dispatcher
                    </h2>
                    <span className="rounded-full border border-primary-container/30 bg-primary-container/15 px-2.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wider text-primary-container">
                      HUD v5.0
                    </span>
                  </div>
                  <p className="text-[12px] text-outline">Arbitration core &amp; dynamic latency matrix</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={loadLiveModels}
                  title="Sync telemetry"
                  className="flex items-center gap-1.5 rounded-full border border-outline-variant/50 bg-surface-container-low px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-outline transition-all hover:bg-surface-container hover:text-primary"
                >
                  <span className={`material-symbols-outlined text-[14px] ${loadingLive ? "animate-spin" : ""}`}>
                    sync
                  </span>
                  <span>Sync</span>
                </button>
                <button
                  type="button"
                  onClick={closePanel}
                  title="Close dialog"
                  className="flex h-8 w-8 items-center justify-center rounded-full border border-outline-variant/50 bg-surface-container-low text-outline transition-all hover:bg-surface-container hover:text-on-surface"
                >
                  <span className="material-symbols-outlined text-[16px]">close</span>
                </button>
              </div>
            </div>

            {/* Tabs */}
            <div className="flex items-center justify-between gap-3 border-b border-outline-variant/20 bg-surface-container-lowest/60 px-6 py-3.5">
              <div className="inline-flex rounded-full border border-outline-variant/40 bg-surface-container-lowest p-1">
                <button
                  type="button"
                  onClick={() => setActiveTab("models")}
                  className={`flex items-center gap-1.5 rounded-full px-4 py-1.5 font-mono text-[11px] uppercase tracking-wider transition-all ${
                    activeTab === "models" ? tabActive : tabIdle
                  }`}
                >
                  <span className="material-symbols-outlined text-[14px]">neurology</span>
                  <span>Neural Registry</span>
                  <span className="ml-1 rounded-full bg-on-primary-container/20 px-1.5 text-[9px]">
                    {modelCount}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("keys")}
                  className={`flex items-center gap-1.5 rounded-full px-4 py-1.5 font-mono text-[11px] uppercase tracking-wider transition-all ${
                    activeTab === "keys" ? tabActive : tabIdle
                  }`}
                >
                  <span className="material-symbols-outlined text-[14px]">key</span>
                  <span>Access Vault</span>
                </button>
              </div>
              <div className="hidden items-center gap-2 font-mono text-[11px] text-outline sm:flex">
                <span className="flex items-center gap-1.5">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary-container" />
                  {engineCount} Engines Synced
                </span>
                <span>•</span>
                <span className="text-tertiary-fixed-dim">Zero Throttle</span>
              </div>
            </div>

            {/* Models catalog */}
            {activeTab === "models" && (
              <div className="flex flex-col gap-5 overflow-y-auto p-6">
                <div className="flex flex-col items-stretch justify-between gap-3 sm:flex-row sm:items-center">
                  <div className="flex flex-1 items-center gap-2.5 rounded-full border border-outline-variant/50 bg-surface-container-low px-3.5 py-2 transition-all focus-within:border-primary-container/40">
                    <span className="material-symbols-outlined text-[16px] text-outline">search</span>
                    <input
                      type="text"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      placeholder="Search architecture, provider, tags..."
                      className="w-full border-none bg-transparent p-0 text-[13px] text-on-surface placeholder:text-outline/70 focus:outline-none"
                    />
                  </div>
                  <div className="flex items-center gap-1.5 overflow-x-auto pb-0.5">
                    {FILTERS.map((f) => (
                      <button
                        key={f.id}
                        type="button"
                        onClick={() => setFilter(f.id)}
                        className={`rounded-full px-3.5 py-1.5 font-mono text-[11px] uppercase transition-all ${
                          filter === f.id
                            ? "bg-primary-container text-on-primary-container shadow-sm"
                            : "border border-outline-variant/50 bg-surface-container-low text-outline hover:text-primary"
                        }`}
                      >
                        {f.label}
                      </button>
                    ))}
                  </div>
                </div>

                {grouped.size === 0 ? (
                  <div className="px-1 py-10 text-center font-mono text-[12px] text-outline">
                    {loadingLive
                      ? "Loading models..."
                      : "No models available — add an API key or check your connection."}
                  </div>
                ) : (
                  Array.from(grouped.entries()).map(([group, models]) => {
                    const color = colorFor(group);
                    return (
                      <div key={group} className="flex flex-col gap-2.5">
                        <div className="flex items-center justify-between px-1">
                          <div className="flex items-center gap-2">
                            <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
                            <span
                              className="font-mono text-[12px] font-semibold uppercase tracking-wider"
                              style={{ color }}
                            >
                              {group}
                            </span>
                          </div>
                          <span className="font-mono text-[10px] text-outline">{models.length} models</span>
                        </div>
                        {models.map((opt) => {
                          const isSelected =
                            selected?.provider === opt.provider && selected?.model === opt.model;
                          return (
                            <button
                              key={modelKey(opt)}
                              type="button"
                              onClick={() => setPending({ provider: opt.provider, model: opt.model })}
                              className={`group flex items-center justify-between rounded-2xl border p-3.5 text-left transition-all ${
                                isSelected
                                  ? "border-primary-container/40 bg-surface-container/90"
                                  : "border-outline-variant/30 bg-surface-container-low/60 hover:border-primary-container/30 hover:bg-surface-container"
                              }`}
                            >
                              <div className="flex min-w-0 items-center gap-3.5">
                                <div
                                  className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${
                                    isSelected
                                      ? "bg-primary-container/20 text-primary-container"
                                      : "bg-surface-container text-outline group-hover:text-primary"
                                  }`}
                                >
                                  <span className="material-symbols-outlined text-[18px]">
                                    {TIER_ICON[opt.tier || "smart"]}
                                  </span>
                                </div>
                                <div className="flex min-w-0 flex-col">
                                  <div className="flex min-w-0 items-center gap-2">
                                    <span
                                      className={`truncate text-[13px] font-semibold ${
                                        isSelected ? "text-primary" : "text-on-surface group-hover:text-primary"
                                      }`}
                                    >
                                      {opt.model}
                                    </span>
                                    {opt.tier && (
                                      <span
                                        className={`shrink-0 rounded-full px-2 py-0.5 font-mono text-[10px] ${tierBadge(
                                          opt.tier,
                                        )}`}
                                      >
                                        {TIER_LABELS[opt.tier]}
                                      </span>
                                    )}
                                  </div>
                                  <span className="truncate text-[11px] text-outline">
                                    {opt.group} · {opt.provider}
                                  </span>
                                </div>
                              </div>
                              <div
                                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full transition-all ${
                                  isSelected
                                    ? "bg-primary-container text-on-primary-container shadow-sm"
                                    : "bg-surface-container-high text-transparent group-hover:bg-primary-container group-hover:text-on-primary-container"
                                }`}
                              >
                                <span className="material-symbols-outlined text-[16px]">check</span>
                              </div>
                            </button>
                          );
                        })}
                      </div>
                    );
                  })
                )}
              </div>
            )}

            {/* Access vault */}
            {activeTab === "keys" && (
              <div className="flex flex-col gap-4 overflow-y-auto p-6">
                <div className="flex items-center gap-3.5 rounded-2xl border border-primary-container/20 bg-surface-container-low/80 p-4">
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-primary-container/15 text-primary-container">
                    <span className="material-symbols-outlined text-[20px]">lock</span>
                  </div>
                  <div>
                    <p className="text-[13px] font-semibold text-primary">Client-Side Key Vault</p>
                    <p className="text-[11px] text-outline">
                      Keys stay on this machine and are only sent to the provider you select.
                    </p>
                  </div>
                </div>
                <div className="flex flex-col gap-2.5">
                  {KEY_ORDER.map((prov) => {
                    const val = keyInputs[prov] || "";
                    const active = !!val;
                    return (
                      <div
                        key={prov}
                        className="flex flex-col justify-between gap-3 rounded-2xl border border-outline-variant/30 bg-surface-container-low/70 p-3.5 sm:flex-row sm:items-center"
                      >
                        <div className="flex items-center gap-2.5">
                          <span
                            className={`h-2 w-2 rounded-full ${
                              active ? "bg-primary-container" : "bg-outline-variant"
                            }`}
                          />
                          <div>
                            <p className="text-[13px] font-semibold text-on-surface">{PROVIDER_LABELS[prov]}</p>
                            <p className="font-mono text-[10px] text-outline">
                              {active ? "Active" : PROVIDER_ENV_HINT[prov]}
                            </p>
                          </div>
                        </div>
                        <input
                          type="text"
                          value={val}
                          placeholder={PROVIDER_ENV_HINT[prov]}
                          onChange={(e) => {
                            const v = e.target.value;
                            setKeyInputs({ ...keyInputs, [prov]: v });
                            onApiKeyChange(prov, val === undefined ? v : v);
                          }}
                          className="w-full rounded-xl border border-outline-variant/40 bg-surface-container-lowest px-3 py-1.5 font-mono text-[11px] text-primary-fixed outline-none transition-all focus:border-primary-container/40 sm:w-64"
                        />
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Footer */}
            <div className="flex flex-col items-center justify-between gap-3 border-t border-outline-variant/30 bg-surface-container-lowest/80 px-6 py-4 sm:flex-row">
              <div className="flex min-w-0 items-center gap-2 font-mono text-[11px]">
                <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-primary-container" />
                <span className="uppercase text-outline">Active:</span>
                <span className="truncate font-bold text-primary-container">{selected?.model || "—"}</span>
              </div>
              <div className="flex w-full items-center gap-2 sm:w-auto">
                <button
                  type="button"
                  onClick={closePanel}
                  className="flex-1 rounded-full bg-surface-container px-4 py-2 font-mono text-[11px] uppercase tracking-wider text-outline transition-all hover:bg-surface-container-high hover:text-on-surface sm:flex-none"
                >
                  Dismiss
                </button>
                <button
                  type="button"
                  onClick={engage}
                  disabled={!selected}
                  className="flex-1 rounded-full bg-primary-container px-6 py-2 font-mono text-[11px] font-bold uppercase tracking-wider text-on-primary-container transition-all hover:bg-primary disabled:cursor-not-allowed disabled:opacity-50 sm:flex-none"
                >
                  Engage Neural Engine
                </button>
              </div>
            </div>
          </div>
        </div>,
        document.body,
      )
    : null;

  return (
    <div className="relative w-full" data-model-selector>
      {!hideTrigger && (
        <button
          type="button"
          onClick={openPanel}
          data-model-selector
          className="group flex w-full items-center justify-between gap-2 rounded-full border border-outline-variant/60 bg-surface-container-low/80 px-3.5 py-2 text-left transition-all hover:border-primary-container/40 hover:bg-surface-container"
        >
          <span className="flex min-w-0 items-center gap-2">
            <span className="material-symbols-outlined text-[15px] text-primary-container">psychology</span>
            <span className="truncate font-mono text-[12px] tracking-wide text-on-surface">
              {selected?.model || "Select engine"}
            </span>
          </span>
          <span className="material-symbols-outlined text-[14px] text-outline transition-transform group-hover:translate-y-0.5">
            expand_more
          </span>
        </button>
      )}
      {modal}
    </div>
  );
}
