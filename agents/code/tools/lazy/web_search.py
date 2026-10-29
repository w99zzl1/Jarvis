import time
from typing import Dict, Any

from ..base import Tool


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the internet for current real-world information: movies and their "
        "ratings, news, people, prices, dates, documentation, anything not "
        "derivable from this project's files. Use this for factual questions "
        "instead of browsing the filesystem."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "num_results": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    }

    def __init__(self, project_path: str):
        self.project_path = project_path
        self.group = "always"

    def execute(self, query: str, num_results: int = 5) -> Dict[str, Any]:
        start = time.time()
        results = self._search_ddgs(query, num_results)
        if not results:
            results = self._search_startpage(query, num_results)

        elapsed = time.time() - start
        if not results:
            return {
                "success": False,
                "error": "No web results found",
                "execution_time": elapsed,
            }

        return {
            "success": True,
            "results": results,
            "total": len(results),
            "execution_time": elapsed,
        }

    def _search_ddgs(self, query: str, max_results: int) -> list[dict]:
        try:
            from ddgs import DDGS

            raw = DDGS().text(query, max_results=max_results)
            results = []
            for item in raw or []:
                title = (item.get("title") or "").strip()
                url = (item.get("href") or "").strip()
                snippet = (item.get("body") or "").strip()[:300]
                if title and url:
                    results.append({"title": title, "url": url, "snippet": snippet})
            return results
        except Exception:
            return []

    def _search_startpage(self, query: str, max_results: int) -> list[dict]:
        try:
            import requests
            from bs4 import BeautifulSoup

            resp = requests.get(
                "https://www.startpage.com/sp/search",
                params={"query": query, "cat": "web"},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                },
                timeout=10,
            )
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".result")[:max_results]
            results = []
            for item in items:
                title_el = item.select_one(".wgl-site-title")
                title = title_el.get_text(strip=True) if title_el else ""
                link_container = item.select_one(".wgl-title-link-container")
                url = ""
                if link_container:
                    a = link_container.select_one("a")
                    if a:
                        url = a.get("href", "")
                snippet_el = item.select_one(".w-gl__description") or item.select_one(".description")
                snippet = snippet_el.get_text(strip=True)[:300] if snippet_el else ""
                if title and url:
                    results.append({"title": title, "url": url, "snippet": snippet})
            return results
        except Exception:
            return []
