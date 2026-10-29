import time
import logging

from .registry import Tool, ToolResult, VerificationStatus

log = logging.getLogger("web_search")


class WebSearchTool(Tool):
    name = "search_web"
    description = "Search the internet for information."
    category = "web"
    cost = "medium"
    group = "always"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    }

    def __init__(self):
        self._empty_queries: set[str] = set()

    def reset(self):
        self._empty_queries.clear()

    def execute(self, args: dict) -> ToolResult:
        query = args.get("query", "")
        if not query:
            return ToolResult(status="error", error="Query is empty")

        # Block retry of the same empty-query within one agent.run()
        q_lower = query.strip().lower()
        if q_lower in self._empty_queries:
            return ToolResult(
                status="error",
                error=(
                    f"Search for '{query.strip()}' returned 0 results for this query. "
                    "Reformulate the query or tell the user nothing was found. "
                    "Do NOT call search_web again with the same query."
                ),
            )

        start = time.time()
        max_results = 5

        # --- Provider 1: ddgs (DuckDuckGo API) ---
        results = self._search_ddgs(query, max_results)
        if results:
            elapsed = time.time() - start
            return ToolResult(
                status="success",
                data={"results": results, "total": len(results)},
                metadata={"execution_time": elapsed, "provider": "ddgs"},
            )

        # --- Provider 2: Startpage HTML scraping ---
        results = self._search_startpage(query, max_results)
        if results:
            elapsed = time.time() - start
            return ToolResult(
                status="success",
                data={"results": results, "total": len(results)},
                metadata={"execution_time": elapsed, "provider": "startpage"},
            )

        # --- All providers returned 0 results → remember this query ---
        self._empty_queries.add(q_lower)
        elapsed = time.time() - start
        return ToolResult(
            status="error",
            error="Search services found nothing for the query. Reformulate or inform the user.",
            metadata={"execution_time": elapsed},
        )

    # ----- ddgs provider -----

    def _search_ddgs(self, query: str, max_results: int) -> list[dict]:
        try:
            from ddgs import DDGS

            raw = DDGS().text(query, max_results=max_results)
            if not raw:
                return []
            results = []
            for item in raw:
                title = (item.get("title") or "").strip()
                url = (item.get("href") or "").strip()
                snippet = (item.get("body") or "").strip()[:300]
                if title and url:
                    results.append({"title": title, "url": url, "snippet": snippet})
            log.info("ddgs returned %d results for: %s", len(results), query)
            return results
        except Exception as e:
            log.warning("ddgs failed: %s", e)
            return []

    # ----- Startpage provider -----

    def _search_startpage(self, query: str, max_results: int) -> list[dict]:
        try:
            import requests
            from bs4 import BeautifulSoup

            resp = requests.get(
                "https://www.startpage.com/sp/search",
                params={"query": query, "cat": "web"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                },
                timeout=10,
            )
            if resp.status_code != 200:
                log.warning("startpage HTTP %d", resp.status_code)
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".result")[:max_results]
            results = []
            for item in items:
                title_el = item.select_one(".wgl-site-title")
                title = title_el.get_text(strip=True) if title_el else ""

                url = ""
                link_container = item.select_one(".wgl-title-link-container")
                if link_container:
                    a = link_container.select_one("a")
                    if a:
                        url = a.get("href", "")

                snippet_el = item.select_one(".w-gl__description") or item.select_one(".description")
                snippet = snippet_el.get_text(strip=True)[:300] if snippet_el else ""

                if title and url:
                    results.append({"title": title, "url": url, "snippet": snippet})

            log.info("startpage returned %d results for: %s", len(results), query)
            return results
        except Exception as e:
            log.warning("startpage failed: %s", e)
            return []

    # ----- Verification -----

    def verify(self, args: dict, result: ToolResult) -> VerificationStatus:
        if result.status != "success":
            return VerificationStatus.FAILED
        results = result.data.get("results") if isinstance(result.data, dict) else None
        if not results:
            return VerificationStatus.UNKNOWN
        return VerificationStatus.SUCCESS