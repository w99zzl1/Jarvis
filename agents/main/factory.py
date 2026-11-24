import logging
import os

from .agent_refactored import Agent
from shared.llm_client import LLMClient
from shared.llm_client.api_key_manager import APIKeyManager
from .tools.registry import ToolRegistry, GetToolsTool
from .tools.shell import RunPowerShellTool
from .tools.clipboard import ClipboardTool
from .tools.read import ReadTool
from .tools.write import WriteTool
from .tools.edit import EditTool
from .tools.web_search import WebSearchTool
from .tools.monitor import MonitorTool
from .tools.memory import MemoryTool
from .tools.task_status import TaskStatusTool
from .tools.search_files import SearchFilesTool
from .tools.grep import GrepTool
from agents.code.tools.active.disk_tree import DiskTreeTool

import config.main as config_main
from shared.config_loader import get_api_keys
from shared.execution_manager import (
    ExecutionManager,
    create_execution_plan_from_llm,
    ExecutionEvent,
    EventType,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("llm").setLevel(logging.INFO)
logging.getLogger("agent").setLevel(logging.INFO)
logging.getLogger("registry").setLevel(logging.INFO)
logging.getLogger("history").setLevel(logging.INFO)

logger = logging.getLogger("agent.factory")

PROVIDER_DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "google": "gemini-1.5-flash",
    "nim": "nvidia/nemotron-3-ultra",
    "openai": "gpt-4o-mini",
    # Empty -> factory fetches the first live chat model from OpenRouter.
    "openrouter": "",
}


def _resolve_key_for_provider(
    provider: str,
    api_keys,
    api_key: str | None,
    all_keys_from_loader: dict,
) -> str | None:
    """Ищет ключ для указанного провайдера в разных источниках."""
    if not provider:
        provider = "groq"

    if isinstance(api_keys, dict) and provider in api_keys:
        entry = api_keys[provider]
        if isinstance(entry, dict):
            k = entry.get("key")
            if k:
                return k
        elif isinstance(entry, str) and entry:
            return entry

    if isinstance(api_keys, list) and provider == "groq":
        for k in api_keys:
            if isinstance(k, str) and k:
                return k

    if isinstance(all_keys_from_loader, dict) and provider in all_keys_from_loader:
        entry = all_keys_from_loader[provider]
        if isinstance(entry, dict):
            k = entry.get("key")
            if k:
                return k
        elif isinstance(entry, str) and entry:
            return entry

    if isinstance(api_key, str) and api_key:
        if provider == "groq":
            return api_key
        return api_key

    fallback_env = {
        "groq": config_main.GROQ_API_KEY or config_main.LLM_API_KEY,
        "google": config_main.GOOGLE_API_KEY,
        "nim": getattr(config_main, "NIM_API_KEY", None) or os.getenv("NIM_API_KEY"),
        "openai": getattr(config_main, "OPENAI_API_KEY", None) or os.getenv("OPENAI_API_KEY"),
        "openrouter": getattr(config_main, "OPENROUTER_API_KEY", None) or os.getenv("OPENROUTER_API_KEY"),
    }
    return fallback_env.get(provider)


def _fetch_live_default_model(provider: str, key: str, base_url: str | None = None) -> str | None:
    try:
        from shared.llm_client.model_fetcher import fetch_provider_models
        live_models = fetch_provider_models(provider, key, base_url)
        if live_models:
            sorted_models = sorted(live_models)
            return sorted_models[0]
    except Exception as e:
        logger.warning(f"[FACTORY] Failed to fetch live models for {provider}: {e}")
    return None


def _make_llm_for_model(
    model: str,
    provider: str,
    api_keys,
    api_key: str | None,
    all_keys: dict,
    base_url: str | None = None,
) -> LLMClient | None:
    """
    Создаёт LLMClient с ЯВНЫМИ model и provider.
    Никакого авто-детекта — всё приходит явно из UI/server/main.py.
    """
    if not provider:
        raise ValueError("Provider must be passed explicitly (auto-detect removed).")
    
    key = _resolve_key_for_provider(provider, api_keys, api_key, all_keys)
    if not key:
        raise ValueError(f"No API key found for provider '{provider}' (model='{model}').")

    if not model:
        live_default = _fetch_live_default_model(provider, key, base_url)
        if live_default:
            model = live_default
            logger.info("[FACTORY] Model not specified, dynamically fetched and selected '%s' for provider '%s'", model, provider)
        else:
            model = "default"
            logger.warning("[FACTORY] Model not specified and live fetch failed (offline?), using fallback 'default'")

    manager = APIKeyManager([key])
    return LLMClient(key_manager=manager, model=model, base_url=base_url, provider=provider)


def _resolve_model_provider_from_config(
    config,
    fallback_model: str,
    fallback_provider: str,
):
    """Разбирает legacy config.selected_model = {provider, code} если передан."""
    if not config or not isinstance(config, dict):
        return fallback_model, fallback_provider
    sm = config.get("selected_model")
    if isinstance(sm, dict):
        p = sm.get("provider") or fallback_provider
        m = sm.get("code") or sm.get("model") or fallback_model
        return m, p
    sp = config.get("selected_provider")
    if sp:
        return fallback_model, sp
    return fallback_model, fallback_provider


def create_agent(
    api_keys: dict | None = None,
    api_key: str | None = None,
    system_prompt: str = "",
    primary_model: str = "",
    primary_provider: str = "",
    vision_model: str | None = None,
    config=None,
    event_sink=None,
    base_url: str | None = None,
) -> "Agent":

    # 1) Resolve primary (provider/model). Явные аргументы приоритетнее config.selected_model
    if not primary_provider or not primary_model:
        fb_m, fb_p = primary_model, primary_provider
        if not fb_p:
            fb_p = "groq"
        if not fb_m:
            fb_m = PROVIDER_DEFAULT_MODELS.get(fb_p, "openai/gpt-oss-120b")
        resolved_primary_m, resolved_primary_p = _resolve_model_provider_from_config(
            config, fb_m, fb_p
        )
        if not primary_model:
            primary_model = resolved_primary_m
        if not primary_provider:
            primary_provider = resolved_primary_p

    all_keys = get_api_keys()

    logger.info(
        "[FACTORY] Creating agent — primary %s:%s",
        primary_provider, primary_model,
    )

    primary_llm = _make_llm_for_model(
        primary_model, primary_provider, api_keys, api_key, all_keys, base_url
    )

    registry = ToolRegistry()

    registry.register(GetToolsTool(registry))
    registry.register(RunPowerShellTool())
    registry.register(ClipboardTool())
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(WebSearchTool())
    registry.register(MonitorTool())
    registry.register(MemoryTool())
    registry.register(TaskStatusTool())
    registry.register(SearchFilesTool())
    registry.register(GrepTool(project_path=""))
    registry.register(DiskTreeTool())

    if not system_prompt:
        system_prompt = config_main.SYSTEM_PROMPT
    # The directory is generated from the live registry, so lazy tools (whose
    # schemas are not sent in `tools=`) are still discoverable by the model.
    system_prompt = system_prompt.replace(
        "{tool_directory}", registry.get_prompt_directory()
    )

    agent = Agent(
        registry=registry,
        primary_llm=primary_llm,
        system_prompt=system_prompt,
        config=config,
        event_sink=event_sink,
    )
    return agent


def create_agent_with_planning(
    api_keys: dict | None = None,
    api_key: str | None = None,
    system_prompt: str = "",
    primary_model: str = "",
    primary_provider: str = "",
    vision_model: str | None = None,
    config=None,
    event_sink=None,
    base_url: str | None = None,
) -> tuple[Agent, ExecutionManager]:
    """
    Creates an Agent wrapped with ExecutionManager for dynamic planning.
    Returns (agent, execution_manager) tuple.
    
    The execution_manager.execute() must be called to run the plan.
    """
    # Create the base agent first
    agent = create_agent(
        api_keys=api_keys,
        api_key=api_key,
        system_prompt=system_prompt,
        primary_model=primary_model,
        primary_provider=primary_provider,
        vision_model=vision_model,
        config=config,
        event_sink=None,
        base_url=base_url,
    )
    
    # Wrap the event_sink to also forward to ExecutionManager
    def combined_event_sink(event: ExecutionEvent):
        if event_sink:
            try:
                event_sink(event.to_dict())
            except Exception:
                pass
    
    # Create ExecutionManager
    # Note: The plan will be generated on first execute() call based on user_input
    # We pass a placeholder plan that will be replaced
    from shared.execution_plan import ExecutionPlan
    placeholder_plan = ExecutionPlan(user_request="")
    
    execution_manager = ExecutionManager(
        plan=placeholder_plan,
        agent=agent,
        event_sink=combined_event_sink,
        tool_registry=agent.registry,
        llm_client=agent.primary_llm,
        user_input="",  # Will be set when execute() is called
    )
    
    # Monkey-patch the agent's run method to use execution manager
    original_run = agent.run
    
    def run_with_planning(user_input: str):
        # Update the execution manager's user_input
        execution_manager.user_input = user_input
        
        # Generate plan using LLM
        available_tools = agent._build_tools_for_llm()
        plan = create_execution_plan_from_llm(
            llm_client=agent.primary_llm,
            user_input=user_input,
            system_prompt=agent.system_prompt,
            available_tools=available_tools,
        )
        
        # Replace the plan in execution manager
        execution_manager.plan = plan
        
        # Execute the plan
        final_plan = execution_manager.execute()
        
        # Return AgentResult-compatible result
        from agents.main.agent import AgentResult, AgentState
        
        success = final_plan.is_complete and all(
            t.status.value == "completed" for t in final_plan.tasks
        )
        
        return AgentResult(
            success=success,
            output=final_plan.tasks[-1].description if final_plan.tasks else "Task completed",
            state=AgentState.FINISHED.value if success else AgentState.VERIFIED_FAILED.value,
            trace_id=final_plan.id,
            model_used=getattr(agent.primary_llm, "model", "unknown"),
            attempts=1,
            verification_status="completed" if success else "failed",
            verification_reason="Plan executed" if success else "Some tasks failed",
            attempt_status="completed" if success else "failed",
        )
    
    agent.run = run_with_planning
    
    return agent, execution_manager
