import asyncio
import logging
import threading

from fastapi.responses import StreamingResponse

from agents.main.factory import create_agent
from agents.main.agent import AgentResult
from server.event_bus import DONE, EventBus, sse_stream

import config.main as config_main

log = logging.getLogger("server")


def _map_to_sse_event(internal_event: dict) -> dict | None:
    etype = internal_event.get("type")
    if etype == "llm_responded":
        preview = internal_event.get("thought_preview", "")
        if preview:
            return {"type": "thought", "preview": preview}
        return None
    if etype == "tool_started":
        return {
            "type": "progress",
            "phase": "executing",
            "tool": internal_event.get("tool"),
            "detail": internal_event.get("args", {}),
        }
    if etype == "tool_progress":
        # Streamed from a long-running tool's own threads (the native disk
        # scanner). Forwarded verbatim so the UI can move its progress bar.
        return {
            "type": "tool_progress",
            "tool": internal_event.get("tool"),
            "call_id": internal_event.get("call_id"),
            "percent": internal_event.get("percent"),
            "nodes": internal_event.get("nodes"),
            "bytes": internal_event.get("bytes"),
            "queued": internal_event.get("queued"),
            "elapsed": internal_event.get("elapsed"),
            "done": internal_event.get("done"),
        }
    if etype == "tool_finished":
        return {
            "type": "progress",
            "phase": "executed",
            "tool": internal_event.get("tool"),
            "duration": internal_event.get("duration"),
            "status": internal_event.get("status"),
        }
    if etype == "tool_output":
        return {
            "type": "tool_output",
            "tool": internal_event.get("tool"),
            "call_id": internal_event.get("call_id"),
            "output": internal_event.get("output"),
            "done": internal_event.get("done", False),
        }
    if etype == "attempt_started":
        return {
            "type": "progress",
            "phase": "attempt",
            "detail": f"Attempt {internal_event.get('attempt')}",
        }
    return None


async def agent_chat_stream(user_input: str, api_keys=None,
                              api_key: str | None = None, system_prompt: str = "",
                              primary_model: str = "openai/gpt-oss-120b",
                              primary_provider: str = "groq",
                              config=None,
                              primary_spec=None):
    """POST /agent/chat/stream — streaming SSE endpoint for Code Agent (EXPLICIT PROVIDERS)."""
    if primary_spec:
        from server.main import resolve_stream_models
        primary_model, primary_provider = resolve_stream_models(
            primary_spec=primary_spec,
            primary_model=primary_model,
            primary_provider=primary_provider,
            api_keys=api_keys,
        )
    from shared.cancellation import registry as cancel_registry

    # Legacy endpoint: it carries no conversation id, so Stop cannot address it
    # by id. A client disconnect still cancels the turn through the bus.
    import uuid

    cid = str(uuid.uuid4())
    cancel_token = cancel_registry.register(cid)
    # Bounded and lossy for progress ticks: the native scanner's threads push
    # through here while holding the GIL, so this must never block or grow.
    bus = EventBus(on_close=cancel_token.cancel)

    def run_agent():
        try:
            agent = create_agent(
                api_keys=api_keys,
                api_key=api_key or "",
                system_prompt=system_prompt or config_main.SYSTEM_PROMPT,
                primary_model=primary_model,
                primary_provider=primary_provider,
                config=config,
                event_sink=bus.push,
            )
            agent.set_cancel_token(cancel_token)
            result: AgentResult = agent.run(user_input)
            bus.push({
                "type": "final",
                "reply": result.output,
                "audio": None,
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
            })
        except Exception as e:
            log.exception("[STREAM] Agent error")
            bus.push({"type": "final", "error": str(e), "reply": None,
                      "success": False, "state": "failed", "iterations": 0,
                      "tool_calls": 0, "trace_id": None, "model_used": "unknown",
                      "verification_status": None, "verification_reason": None})
        finally:
            cancel_registry.unregister(cid, cancel_token)
            from shared import agent_job

            agent_job.cleanup_job(cid)
            bus.push({"type": DONE})

    # Send handshake event first with schema version
    from server.event_bus import make_handshake_event
    bus.push(make_handshake_event())

    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    return StreamingResponse(sse_stream(bus, _map_to_sse_event),
                             media_type="text/event-stream")


def agent_route(user_input: str, api_keys=None,
                api_key: str = None, system_prompt: str = "",
                primary_model: str = "openai/gpt-oss-120b",
                primary_provider: str = "groq",
                config=None) -> dict:
    """Handle /agent/chat request (EXPLICIT PROVIDERS)."""
    log.info(f"[AGENT] agent_route called - primary={primary_provider}:{primary_model}")

    try:
        agent = create_agent(
            api_keys=api_keys,
            api_key=api_key or "",
            system_prompt=system_prompt or config_main.SYSTEM_PROMPT,
            primary_model=primary_model,
            primary_provider=primary_provider,
            config=config,
        )
    except ValueError as e:
        log.warning(f"[AGENT] Failed to create agent: {e}")
        return {
            "success": False,
            "output": "",
            "state": "failed",
            "error": str(e),
            "model_used": f"{primary_provider}/{primary_model}",
            "attempts": 0,
            "verification_status": None,
            "verification_reason": "Missing API key or invalid provider",
            "attempt_status": "no_keys",
        }

    result: AgentResult = agent.run(user_input)

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
    }
