import asyncio
import base64
import io
import logging
import os
import threading
import time
import uuid
import soundfile as sf
from fastapi.responses import StreamingResponse

from server.event_bus import DONE, EventBus, sse_stream

from shared.llm_client import LLMClient
from shared.llm_client.api_key_manager import APIKeyManager
from agents.main.factory import create_agent, create_agent_with_planning
from agents.main.agent import AgentResult
from shared.execution_plan import ExecutionEvent, EventType
from server.conversations import store as conv_store

import config.main as config_main

log = logging.getLogger("server")

# Lazy TTS engine - Silero TTS (local neural voice, Russian + English,
# works offline after first model download, no API key).
_tts_model = None


def _get_tts_model():
    global _tts_model
    if _tts_model is not None:
        return _tts_model
    try:
        from silero_tts.silero_tts import SileroTTS
        model = SileroTTS(model_id="v4_ru", language="ru", speaker="aidar",
                          sample_rate=48000, device="cpu")
        _tts_model = model
        log.info("[TTS] Silero v4_ru loaded")
        return model
    except Exception as e:
        log.warning(f"[TTS] silero-tts unavailable, audio will be silent: {e}")
        _tts_model = False
        return None


def _synthesize_tts(text: str) -> str | None:
    """Generate speech in memory and return base64-encoded WAV audio.

    Silero model produces audio directly into RAM - no temporary files on disk.
    """
    if not text:
        log.warning("[TTS] empty text, skipping synthesis")
        return None
    model = _get_tts_model()
    if model is None:
        log.warning("[TTS] model unavailable, audio will be silent")
        return None
    try:
        import numpy as np

        # Generate audio directly in memory - no temporary file on disk
        audio_tensor = model.tts_model.apply_tts(
            text=text,
            speaker=model.speaker,
            sample_rate=model.sample_rate,
            put_accent=model.put_accent,
            put_yo=model.put_yo,
        )
        audio_np = (
            audio_tensor.numpy()
            if hasattr(audio_tensor, "numpy")
            else np.asarray(audio_tensor)
        )

        buffer = io.BytesIO()
        sf.write(buffer, audio_np, model.sample_rate, format="WAV")
        data = buffer.getvalue()
        if data:
            b64 = base64.b64encode(data).decode("ascii")
            log.info(f"[TTS] synthesized {len(data)} bytes ({len(text)} chars)")
            return b64
        log.warning("[TTS] empty audio output")
    except Exception as e:
        log.warning(f"[TTS] synthesis failed: {e}")
    return None


def _map_to_sse_event(internal_event: dict) -> dict | None:
    etype = internal_event.get("type")
    
    # Internal events (retry instructions, etc.) are needed for LLM context
    # but must never reach the UI as visible messages.
    if internal_event.get("internal"):
        return None
    
    # New Execution Plan Events
    if etype == "plan_created":
        return {
            "type": "plan_created",
            "plan": internal_event.get("plan"),
        }
    if etype == "plan_updated":
        return {
            "type": "plan_updated",
            "plan": internal_event.get("plan"),
        }
    if etype == "task_started":
        return {
            "type": "task_started",
            "task": internal_event.get("task"),
            "plan": internal_event.get("plan"),
        }
    if etype == "task_completed":
        return {
            "type": "task_completed",
            "task": internal_event.get("task"),
            "plan": internal_event.get("plan"),
        }
    if etype == "task_failed":
        return {
            "type": "task_failed",
            "task": internal_event.get("task"),
            "plan": internal_event.get("plan"),
            "error": internal_event.get("error"),
        }
    if etype == "tool_progress":
        # Streamed from a long-running tool's own threads (the native disk
        # scanner), so the UI keeps moving while the tool is still working.
        return {
            "type": "tool_progress",
            "tool": internal_event.get("tool_name") or internal_event.get("tool"),
            "call_id": internal_event.get("call_id"),
            "percent": internal_event.get("percent"),
            "nodes": internal_event.get("nodes"),
            "bytes": internal_event.get("bytes"),
            "queued": internal_event.get("queued"),
            "elapsed": internal_event.get("elapsed"),
            "done": internal_event.get("done"),
        }
    if etype == "tool_started":
        return {
            "type": "tool_started",
            "tool": internal_event.get("tool") or internal_event.get("tool_name"),
            "args": internal_event.get("args") or internal_event.get("tool_args"),
            "task_id": internal_event.get("task_id"),
            "call_id": internal_event.get("call_id"),
        }
    if etype == "tool_finished":
        # The agent emits `tool_finished`; the UI keys off `tool_completed`.
        # Without this translation the activity row stayed "running"
        # forever, because nothing ever closed it.
        return {
            "type": "tool_completed",
            "tool": internal_event.get("tool") or internal_event.get("tool_name"),
            "status": internal_event.get("status"),
            "duration": internal_event.get("duration"),
            "result": internal_event.get("result") or internal_event.get("tool_result"),
            "task_id": internal_event.get("task_id"),
            "call_id": internal_event.get("call_id"),
        }
    if etype == "execution_error":
        return {
            "type": "execution_error",
            "error": internal_event.get("error"),
            "task_id": internal_event.get("task_id"),
        }
    if etype == "execution_completed":
        return {
            "type": "execution_completed",
            "message": internal_event.get("message"),
            "plan": internal_event.get("plan"),
        }
    if etype == "agent_message":
        return {
            "type": "agent_message",
            "message": internal_event.get("message"),
        }
    if etype == "stream":
        return {
            "type": "stream",
            "text": internal_event.get("text", ""),
            "iteration": internal_event.get("iteration", 0),
        }
    
    # Legacy events from Agent
    if etype == "llm_responded":
        preview = internal_event.get("thought_preview", "")
        if preview:
            return {"type": "thought", "preview": preview}
        return None
    if etype == "attempt_started":
        return {
            "type": "progress",
            "phase": "attempt",
            "detail": f"Attempt {internal_event.get('attempt')}",
        }
    return None


async def chat_stream(user_input: str, api_keys=None,
                          api_key: str | None = None, system_prompt: str = "",
                          primary_model: str = "openai/gpt-oss-120b",
                          primary_provider: str = "groq",
                          vision_model: str | None = None,
                          config=None,
                           use_planning: bool = False,
                          primary_spec=None,
                         conversation_id=None,
                         attachments=None):
    """POST /chat/stream — streaming SSE endpoint for Main Jarvis (WITH EXPLICIT PROVIDERS)."""
    if primary_spec:
        from server.main import resolve_stream_models
        primary_model, primary_provider = resolve_stream_models(
            primary_spec=primary_spec,
            primary_model=primary_model,
            primary_provider=primary_provider,
            api_keys=api_keys,
        )
    # Bounded and lossy for progress ticks: the native scanner's threads push
    # through here while holding the GIL, so this must never block or grow.
    bus = EventBus()

    def run_agent():
        try:
            cid = conversation_id or str(uuid.uuid4())
            history = conv_store.load(cid)

            # Build user message with attachment context
            # Images are embedded as base64 data URLs so Gemini Vision can see them natively.
            user_content: list[dict] = [{"type": "text", "text": user_input}]
            if attachments:
                att_lines = []
                for a in attachments:
                    path = a.get("path", "")
                    name = a.get("name", "file")
                    mime = a.get("mime", "application/octet-stream")
                    size = a.get("size", 0)
                    is_image = mime.startswith("image/")
                    if is_image and path and os.path.exists(path):
                        try:
                            import base64
                            with open(path, "rb") as f:
                                b64 = base64.b64encode(f.read()).decode("ascii")
                            data_url = f"data:{mime};base64,{b64}"
                            user_content.append(
                                {"type": "image_url", "image_url": {"url": data_url}}
                            )
                            att_lines.append(
                                f"- {name} (изображение, {size} байт) — прикреплено как изображение"
                            )
                        except Exception as e:
                            log.warning(f"[CHAT] Failed to embed image {path}: {e}")
                            att_lines.append(
                                f"- {name} ({mime}, {size} байт) — не удалось встроить: {e}"
                            )
                    else:
                        att_lines.append(
                            f"- {name} ({mime}, {size} байт) по пути {path}"
                        )
                if att_lines:
                    att_context = "\n".join(att_lines)
                    user_content[0]["text"] = (
                        f"{user_input}\n\n--- Прикреплённые файлы ---\n{att_context}\n"
                        "---_END_ATTACHMENTS---\n"
                    )

            # Persist the user turn immediately so it survives a crash
            conv_store.save(cid, history + [{"role": "user", "content": user_content}])

            last_save = [0.0]
            _HIGH_FREQ = {"stream", "tool_progress"}

            def saving_sink(event):
                etype = event.get("type")
                now = time.time()
                if etype in _HIGH_FREQ and (now - last_save[0]) < 0.5:
                    bus.push(event)
                    return
                try:
                    conv_store.save(cid, agent.log.events)
                except Exception:
                    pass
                last_save[0] = now
                bus.push(event)

            if use_planning:
                agent, execution_manager = create_agent_with_planning(
                    api_keys=api_keys,
                    api_key=api_key,
                    system_prompt=system_prompt or config_main.SYSTEM_PROMPT,
                    primary_model=primary_model,
                    primary_provider=primary_provider,
                    vision_model=vision_model,
                    config=config,
                    event_sink=bus.push,
                    base_url=None,
                )
                agent.set_event_sink(saving_sink)
                result: AgentResult = agent.run(user_content)
                conv_store.save(cid, agent.log.events)
                _tts_audio = _synthesize_tts(result.output or "")
                log.info(f"[CHAT] final audio={'present' if _tts_audio else 'silent'} for reply len={len(result.output or '')}")
                bus.push({
                    "type": "final",
                    "reply": result.output,
                    "audio": _tts_audio,
                    "success": result.success,
                    "state": result.state,
                    "iterations": result.iterations,
                    "tool_calls": result.tool_calls_total,
                    "trace_id": result.trace_id,
                    "model_used": result.model_used,
                    "attempts": result.attempts,
                    "verification_status": result.verification_status,
                    "verification_reason": result.verification_reason,
                    "error": result.error,
                    "conversation_id": cid,
                })
            else:
                agent = create_agent(
                    api_keys=api_keys,
                    api_key=api_key,
                    system_prompt=system_prompt or config_main.SYSTEM_PROMPT,
                    primary_model=primary_model,
                    primary_provider=primary_provider,
                    vision_model=vision_model,
                    config=config,
                    event_sink=bus.push,
                )
                agent.set_event_sink(saving_sink)
                agent.load_history(history)
                result: AgentResult = agent.run(user_content)
                conv_store.save(cid, agent.log.events)
                _tts_audio = _synthesize_tts(result.output or "")
                log.info(f"[CHAT] final audio={'present' if _tts_audio else 'silent'} for reply len={len(result.output or '')}")
                bus.push({
                    "type": "final",
                    "reply": result.output,
                    "audio": _tts_audio,
                    "success": result.success,
                    "state": result.state,
                    "iterations": result.iterations,
                    "tool_calls": result.tool_calls_total,
                    "trace_id": result.trace_id,
                    "model_used": result.model_used,
                    "attempts": result.attempts,
                    "verification_status": result.verification_status,
                    "verification_reason": result.verification_reason,
                    "error": result.error,
                    "conversation_id": cid,
                })
        except Exception as e:
            log.exception("[STREAM] Chat error")
            bus.push({"type": "final", "error": str(e), "reply": None,
                      "success": False, "state": "failed", "iterations": 0,
                      "tool_calls": 0, "trace_id": None, "model_used": "unknown",
                      "verification_status": None, "verification_reason": None,
                      "conversation_id": conversation_id})
        finally:
            bus.push({"type": DONE})

    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    return StreamingResponse(sse_stream(bus, _map_to_sse_event),
                             media_type="text/event-stream")


def chat_route(user_input: str, api_keys=None,
                api_key: str = None, system_prompt: str = "",
                primary_model: str = "openai/gpt-oss-120b",
                primary_provider: str = "groq",
                vision_model: str | None = None,
                config=None,
                conversation_id=None,
                attachments=None) -> dict:
    """Handle /chat request (WITH EXPLICIT PROVIDERS)."""
    cid = conversation_id or str(uuid.uuid4())
    history = conv_store.load(cid)

    # Build user message with attachment context
    # Images are embedded as base64 data URLs so Gemini Vision can see them natively.
    user_content: list[dict] = [{"type": "text", "text": user_input}]
    if attachments:
        att_lines = []
        for a in attachments:
            path = a.get("path", "")
            name = a.get("name", "file")
            mime = a.get("mime", "application/octet-stream")
            size = a.get("size", 0)
            is_image = mime.startswith("image/")
            if is_image and path and os.path.exists(path):
                try:
                    import base64
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                    data_url = f"data:{mime};base64,{b64}"
                    user_content.append(
                        {"type": "image_url", "image_url": {"url": data_url}}
                    )
                    att_lines.append(
                        f"- {name} (изображение, {size} байт) — прикреплено как изображение"
                    )
                except Exception as e:
                    log.warning(f"[CHAT ROUTE] Failed to embed image {path}: {e}")
                    att_lines.append(
                        f"- {name} ({mime}, {size} байт) — не удалось встроить: {e}"
                    )
            else:
                att_lines.append(
                    f"- {name} ({mime}, {size} байт) по пути {path}"
                )
        if att_lines:
            att_context = "\n".join(att_lines)
            user_content[0]["text"] = (
                f"{user_input}\n\n--- Прикреплённые файлы ---\n{att_context}\n"
                "---_END_ATTACHMENTS---\n"
            )

    # Persist the user turn immediately so it survives a crash.
    conv_store.save(cid, history + [{"role": "user", "content": user_content}])

    agent = create_agent(
        api_keys=api_keys,
        api_key=api_key,
        system_prompt=system_prompt or config_main.SYSTEM_PROMPT,
        primary_model=primary_model,
        primary_provider=primary_provider,
        vision_model=vision_model,
        config=config,
    )

    result: AgentResult = agent.run(user_content)
    conv_store.save(cid, agent.log.events)

    return {
        "success": result.success,
        "output": result.output,
        "state": result.state,
        "trace_id": result.trace_id,
        "error": result.error,
        "model_used": result.model_used,
        "attempts": result.attempts,
        "verification_status": result.verification_status,
        "verification_reason": result.verification_reason,
        "attempt_status": result.attempt_status,
        "conversation_id": cid,
    }
