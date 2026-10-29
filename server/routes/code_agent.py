import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi.responses import StreamingResponse

from agents.code.factory import create_code_agent
from agents.main.factory import create_agent_with_planning
from agents.main.agent import AgentResult
from server.event_bus import DONE, EventBus, sse_stream
from shared.execution_plan import ExecutionEvent, EventType
from server.conversations import store as conv_store
from shared import agent_job

log = logging.getLogger("server")


def _looks_like_progress(event: dict) -> bool:
    """A bare scanner sample: counters, no envelope.

    `percent`/`nodes` together are specific enough that no other internal event
    can be mistaken for one.
    """
    return ("percent" in event and "nodes" in event
            and event.get("type") in (None, ""))


def _map_to_sse_event(internal_event: dict) -> dict | None:
    etype = internal_event.get("type")

    if etype in ("plan_created", "plan_updated"):
        plan = internal_event.get("plan")
        steps = None
        if isinstance(plan, dict):
            tasks = plan.get("tasks")
            if isinstance(tasks, list):
                steps = [
                    {
                        "id": t.get("id"),
                        "text": t.get("title") or t.get("text"),
                        "status": t.get("status"),
                    }
                    for t in tasks
                    if isinstance(t, dict)
                ]
        elif isinstance(plan, list):
            steps = [
                {
                    "id": s.get("id"),
                    "text": s.get("text"),
                    "status": s.get("status"),
                }
                for s in plan
                if isinstance(s, dict) and s.get("id") is not None
            ]
        return {"type": "plan_updated", "steps": steps}

    if etype == "task_started":
        return {"type": "task_started", "task": internal_event.get("task")}
    if etype == "task_completed":
        return {"type": "task_completed", "task": internal_event.get("task")}
    if etype == "task_failed":
        return {
            "type": "task_failed",
            "task": internal_event.get("task"),
            "error": internal_event.get("error"),
        }

    if etype == "tool_started":
        return {
            "type": "tool_started",
            "tool": internal_event.get("tool_name") or internal_event.get("tool"),
            "args": internal_event.get("tool_args") or internal_event.get("args"),
            "task_id": internal_event.get("task_id"),
            "call_id": internal_event.get("call_id"),
        }

    if etype == "tool_progress" or _looks_like_progress(internal_event):
        # Streamed from the native scanner's worker threads while a long scan
        # runs. The UI shows it as a progress bar; nothing here waits on it.
        #
        # The shape is also recognised without a `type`, so a raw sample straight
        # from Rust still streams - which is what the planned move to native
        # Tauri events will send.
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

    if etype in ("tool_finished", "tool_completed"):
        return {
            "type": "tool_completed",
            "tool": internal_event.get("tool_name") or internal_event.get("tool"),
            "status": internal_event.get("status"),
            "duration": internal_event.get("duration"),
            "result": internal_event.get("tool_result"),
            "task_id": internal_event.get("task_id"),
            "call_id": internal_event.get("call_id"),
        }

    if etype == "execution_error":
        return {
            "type": "execution_error",
            "error": internal_event.get("error"),
        }
    if etype == "execution_completed":
        return {
            "type": "execution_completed",
            "message": internal_event.get("message"),
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

    if etype == "verification_decision":
        return {
            "type": "progress",
            "phase": "verify",
            "detail": internal_event.get("reason", ""),
            "status": internal_event.get("decision"),
        }

    return None


def _project_root() -> str:
    from shared import paths

    return str(paths.workspace_dir())


def list_code_tools() -> list[dict]:
    """Full code-agent tool catalog for the UI toggles (enabled or not)."""
    from agents.code.registry import CodeToolRegistry
    return CodeToolRegistry(project_path=_project_root()).list_all()


async def code_chat_stream(
    user_input: str,
    api_keys=None,
    api_key: str | None = None,
    system_prompt: str = "",
    primary_model: str = "openai/gpt-oss-120b",
    primary_provider: str = "groq",
    config=None,
    use_planning: bool = False,
    primary_spec=None,
    conversation_id=None,
    disabled_tools=None,
    attachments=None,
):
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
            agent_job.set_current_cid(cid)
            try:
                agent_job.ensure_job(cid)
            except Exception:
                pass
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
                            log.warning(f"[CODE CHAT] Failed to embed image {path}: {e}")
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
                    system_prompt=system_prompt,
                    primary_model=primary_model,
                    primary_provider=primary_provider,
                    config=config,
                    event_sink=bus.push,
                    base_url=None,
                )
                agent.set_event_sink(saving_sink)
                result: AgentResult = agent.run(user_content)
                conv_store.save(cid, agent.log.events)
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
                    "conversation_id": cid,
                })
            else:
                agent = create_code_agent(
                    project_path=_project_root(),
                    api_keys=api_keys,
                    api_key=api_key,
                    system_prompt=system_prompt,
                    primary_model=primary_model,
                    primary_provider=primary_provider,
                    config=config,
                    event_sink=bus.push,
                    disabled_tools=disabled_tools,
                )
                agent.set_event_sink(saving_sink)
                agent.load_history(history)
                result: AgentResult = agent.run(user_content)
                conv_store.save(cid, agent.log.events)
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
                    "conversation_id": cid,
                })
        except Exception as e:
            log.exception("[STREAM] Code agent error")
            bus.push({
                "type": "final",
                "error": str(e),
                "reply": None,
                "success": False,
                "state": "failed",
                "iterations": 0,
                "tool_calls": 0,
                "trace_id": None,
                "model_used": f"{primary_provider}/{primary_model}",
                "attempts": 0,
                "verification_status": None,
                "verification_reason": None,
                "conversation_id": conversation_id,
            })
        finally:
            # Wake the consumer even if the agent died without a final event.
            bus.push({"type": DONE})

    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    return StreamingResponse(sse_stream(bus, _map_to_sse_event),
                             media_type="text/event-stream")


def code_chat_route(
    user_input: str,
    api_keys=None,
    api_key: str = None,
    system_prompt: str = "",
    primary_model: str = "openai/gpt-oss-120b",
    primary_provider: str = "groq",
    config=None,
    disabled_tools=None,
    attachments=None,
) -> dict:
    log.info(f"[CODE AGENT] route called - primary={primary_provider}:{primary_model}")
    try:
        agent = create_code_agent(
            project_path=_project_root(),
            api_keys=api_keys,
            api_key=api_key,
            system_prompt=system_prompt,
            primary_model=primary_model,
            primary_provider=primary_provider,
            config=config,
            disabled_tools=disabled_tools,
        )
    except ValueError as e:
        log.warning(f"[CODE AGENT] Failed to create agent: {e}")
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
                    log.warning(f"[CODE ROUTE] Failed to embed image {path}: {e}")
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

    result: AgentResult = agent.run(user_content)
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
