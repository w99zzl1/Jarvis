import json
import os
import time
import re
import logging

logger = logging.getLogger("memory")


class MemoryStore:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self):
        if self._loaded:
            return
        self._loaded = True
        self._file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "memory_store.json")
        self._data: list[dict] = []
        self._load()

    def _load(self):
        if os.path.exists(self._file):
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info(f"[MEMORY] Loaded {len(self._data)} items from {self._file}")
            except Exception as e:
                logger.warning(f"[MEMORY] Failed to load: {e}")
                self._data = []

    def _save(self):
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def remember(self, key: str, value: str, tags: list[str] = None) -> str:
        entry = {
            "key": key,
            "value": value,
            "tags": tags or [],
            "created": time.time(),
            "updated": time.time(),
        }

        for i, existing in enumerate(self._data):
            if existing["key"] == key:
                existing["value"] = value
                if tags:
                    existing["tags"] = list(set(existing["tags"] + tags))
                existing["updated"] = time.time()
                self._save()
                logger.info(f"[MEMORY] Updated key '{key}'")
                return f"Updated memory: {key} = {value[:100]}"

        self._data.append(entry)
        self._save()
        logger.info(f"[MEMORY] Saved key '{key}'")
        return f"Saved to memory: {key} = {value[:100]}"

    _SYNONYMS = {
        "напиток": ["drink", "кофе", "чай", "вода", "сок"],
        "цвет": ["color", "colour", "синий", "красный", "зеленый", "желтый"],
        "имя": ["name", "зовут", "зову"],
        "дата": ["date", "день", "рождения"],
        "адрес": ["address", "проживан", "улица"],
        "телефон": ["phone", "номер"],
        "пароль": ["password", "pass"],
        "почт": ["email", "mail"],
        "любим": ["favorite", "fav", "любимый", "любимая"],
    }

    def search(self, query: str, limit: int = 5) -> str:
        query_lower = query.lower()
        query_words = re.split(r'\s+', query_lower)
        results = []

        search_terms = set(query_words)
        for word in query_words:
            for key, synonyms in self._SYNONYMS.items():
                if key in word or word in key:
                    search_terms.update(synonyms)

        for entry in self._data:
            score = 0
            key_lower = entry["key"].lower()
            val_lower = entry["value"].lower()
            combined = f"{key_lower} {val_lower}"

            if query_lower in key_lower:
                score += 5
            if query_lower in val_lower:
                score += 3

            for term in search_terms:
                if len(term) >= 3:
                    if term in key_lower:
                        score += 2
                    if term in val_lower:
                        score += 1

            for field in [key_lower, val_lower]:
                if len(query_lower) >= 3 and len(field) >= 3:
                    common = sum(1 for c in query_lower if c in field)
                    if common / max(len(query_lower), 1) >= 0.8:
                        score += 1

            for tag in entry.get("tags", []):
                if query_lower in tag.lower():
                    score += 1

            if score > 0:
                results.append((score, entry))

        results.sort(key=lambda x: -x[0])
        top = results[:limit]

        if not top:
            return f"No memories found for: {query}"

        lines = ["Found memories:"]
        for score, entry in top:
            value_preview = entry["value"][:200]
            tags_str = f" [{', '.join(entry['tags'])}]" if entry.get("tags") else ""
            lines.append(f"  - {entry['key']}: {value_preview}{tags_str} (relevance: {score})")

        return "\n".join(lines)

    def forget(self, key: str) -> str:
        for i, entry in enumerate(self._data):
            if entry["key"] == key:
                self._data.pop(i)
                self._save()
                logger.info(f"[MEMORY] Deleted key '{key}'")
                return f"Deleted memory: {key}"
        return f"No memory found with key: {key}"

    def list_keys(self, tag: str = None) -> str:
        if tag:
            items = [e for e in self._data if tag in e["tags"]]
        else:
            items = self._data
        if not items:
            return "No memories stored."
        keys = "\n".join(f"  - {e['key']}" for e in items)
        return f"Stored memories ({len(items)}):\n{keys}"

    def to_context_string(self, limit: int = 10) -> str:
        if not self._data:
            return ""
        recent = sorted(self._data, key=lambda x: -x["updated"])[:limit]
        lines = ["Relevant memories:"]
        for entry in recent:
            lines.append(f"  {entry['key']}: {entry['value'][:150]}")
        return "\n".join(lines)