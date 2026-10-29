import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from agents.main.agent import Agent
from agents.main.factory import create_agent, create_agent_with_planning
from agents.code.factory import create_code_agent

DEFAULT_TTL_SECONDS = 3600


@dataclass
class Session:
    agent: Agent
    agent_type: str
    created_at: float
    last_used_at: float
    metadata: Dict[str, Any]


class AgentSessionStore:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def _cleanup_if_needed(self):
        now = time.time()
        expired = [cid for cid, s in self._sessions.items() if now - s.last_used_at > self._ttl]
        for cid in expired:
            self._sessions.pop(cid, None)

    def _reusable(self, conversation_id: Optional[str], agent_type: str, signature) -> Optional[Agent]:
        """Return the stored agent when it matches the request, else None.

        `signature` identifies the model/provider a turn was built for. A stored
        session whose signature differs (the user switched model) is dropped:
        its history would be replayed to a different model.
        """
        if not conversation_id or conversation_id not in self._sessions:
            return None
        s = self._sessions[conversation_id]
        if s.agent_type == agent_type and s.metadata.get("signature") == signature:
            s.last_used_at = time.time()
            return s.agent
        self._sessions.pop(conversation_id, None)
        return None

    def get_or_create_code(self, conversation_id: Optional[str], project_path: str,
                           signature=None, **kwargs) -> Tuple[str, Agent]:
        with self._lock:
            self._cleanup_if_needed()
            existing = self._reusable(conversation_id, 'code', signature)
            if existing is not None:
                return conversation_id, existing
            cid = conversation_id or str(uuid.uuid4())
            agent = create_code_agent(project_path=project_path, **kwargs)
            self._sessions[cid] = Session(agent=agent, agent_type='code', created_at=time.time(), last_used_at=time.time(), metadata={"signature": signature})
            return cid, agent

    def get_or_create_main(self, conversation_id: Optional[str],
                           signature=None, **kwargs) -> Tuple[str, Agent]:
        with self._lock:
            self._cleanup_if_needed()
            existing = self._reusable(conversation_id, 'main', signature)
            if existing is not None:
                return conversation_id, existing
            cid = conversation_id or str(uuid.uuid4())
            agent = create_agent(**kwargs)
            self._sessions[cid] = Session(agent=agent, agent_type='main', created_at=time.time(), last_used_at=time.time(), metadata={"signature": signature})
            return cid, agent

    def touch(self, conversation_id: str):
        with self._lock:
            if conversation_id in self._sessions:
                self._sessions[conversation_id].last_used_at = time.time()

    def delete(self, conversation_id: str):
        with self._lock:
            self._sessions.pop(conversation_id, None)


store = AgentSessionStore()
