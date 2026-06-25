"""
VideoHunter AI — LangGraph Pipeline
=====================================
Full graph definition with all 5 agent nodes, conditional edges,
retry logic, and memory-bypass shortcuts.

Graph Flow:
  START
    │
    ▼
  memory_read ──── HIT ────────────────────────────┐
    │ MISS                                          │
    ▼                                               │
  site_analyzer ── ERROR ── END(fail)               │
    │                                               │
    ▼                                               │
  reverse_engineer                                  │
    │                          ◄────────────────────┘
    ▼
  strategy_builder
    │
    ▼
  downloader_generator
    │
    ├── SUCCESS ──► memory_write ──► END(success)
    │
    └── FAIL ──── retry_count < 3? ──YES──► strategy_builder (retry+1)
                                    NO───► END(fail)
"""

import logging
from langgraph.graph import StateGraph, END

from core.state import VideoHunterState
from nodes.node_site_analyzer       import node_site_analyzer
from nodes.node_reverse_engineer    import node_reverse_engineer
from nodes.node_strategy_builder    import node_strategy_builder
from nodes.node_downloader_generator import node_downloader_generator
from nodes.node_memory              import node_memory_read, node_memory_write
from nodes.node_script_executor import node_script_executor

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


# ── Conditional edge functions ────────────────────────────────────────────────

def route_after_memory_read(state: VideoHunterState) -> str:
    """If memory hit AND strategy doesn't need dynamic manifests → jump straight to downloader; else → analyze site."""
    if state.get("memory_hit") and state.get("strategy"):
        strat_name = state.get("strategy", {}).get("strategy", "")
        if strat_name in {"ytdlp_generic", "blob_capture"}:
            logger.info(f"[Router] Memory HIT ({strat_name}) — skipping analysis")
            return "downloader_generator"
        else:
            logger.info(f"[Router] Memory HIT ({strat_name}) — needs dynamic manifests, analyzing site")
            return "site_analyzer"
    return "site_analyzer"


def route_after_site_analyzer(state: VideoHunterState) -> str:
    """If analyzer completely failed → end; else → reverse engineer."""
    if state.get("error") and not state.get("raw_bundle"):
        logger.error("[Router] Site Analyzer failed fatally — ending")
        return END
    return "reverse_engineer"


def route_after_downloader(state: VideoHunterState) -> str:
    """If script generated OK → execute it; else → retry or end."""
    if state.get("success") and state.get("script_path"):
        return "script_executor"
        
    retry = state.get("retry_count", 0)
    if retry < MAX_RETRIES:
        logger.warning(f"[Router] Script generation failed (retry {retry+1}/{MAX_RETRIES}) — rebuilding strategy")
        return "strategy_builder"

    logger.error("[Router] Max retries reached — ending with failure")
    return END

def route_after_executor(state: VideoHunterState) -> str:
    """If execution successful → save memory; on failure → re-analyze (404) or retry strategy."""
    if state.get("success"):
        logger.info("[Router] Download script executed ✓ — saving memory")
        return "memory_write"

    retry = state.get("retry_count", 0)
    if retry < MAX_RETRIES:
        # ── 404 = stale/signed CDN URL — need fresh site analysis, not just new strategy
        exec_logs = state.get("execution_logs", "") or ""
        if "404" in exec_logs:
            logger.warning(
                f"[Router] 404 on script execution (retry {retry+1}/{MAX_RETRIES}) "
                "— re-analyzing site for fresh tokens"
            )
            return "reanalyze"
        logger.warning(f"[Router] Script execution failed (retry {retry+1}/{MAX_RETRIES}) — rebuilding strategy")
        return "strategy_builder"

    logger.error("[Router] Max retries reached — ending with failure")
    return END


def increment_retry(state: VideoHunterState) -> VideoHunterState:
    """Middleware: bump retry counter before going back to strategy builder."""
    return {**state, "retry_count": state.get("retry_count", 0) + 1}


# ── Build the graph ───────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(VideoHunterState)

    # Register all nodes
    graph.add_node("memory_read",           node_memory_read)
    graph.add_node("site_analyzer",         node_site_analyzer)
    graph.add_node("reverse_engineer",      node_reverse_engineer)
    graph.add_node("strategy_builder",      node_strategy_builder)
    graph.add_node("downloader_generator",  node_downloader_generator)
    graph.add_node("script_executor",       node_script_executor)
    graph.add_node("memory_write",          node_memory_write)
    graph.add_node("increment_retry",       increment_retry)

    # Entry point
    graph.set_entry_point("memory_read")

    # Edges
    graph.add_conditional_edges(
        "memory_read",
        route_after_memory_read,
        {
            "site_analyzer":        "site_analyzer",
            "downloader_generator": "downloader_generator",
        }
    )

    graph.add_conditional_edges(
        "site_analyzer",
        route_after_site_analyzer,
        {
            "reverse_engineer": "reverse_engineer",
            END:                END,
        }
    )

    graph.add_edge("reverse_engineer",  "strategy_builder")
    graph.add_edge("strategy_builder",  "downloader_generator")

    graph.add_conditional_edges(
        "downloader_generator",
        route_after_downloader,
        {
            "script_executor":  "script_executor",
            "strategy_builder": "increment_retry",
            END:                END,
        }
    )

    graph.add_conditional_edges(
        "script_executor",
        route_after_executor,
        {
            "memory_write":     "memory_write",
            "strategy_builder": "increment_retry",
            "reanalyze":        "increment_retry",  # goes through increment_retry → site_analyzer
            END:                END,
        }
    )

    # After incrementing retry:
    #   - If last failure was a 404 → go back to site_analyzer for fresh tokens
    #   - Otherwise → go to strategy_builder
    def route_after_increment(state: VideoHunterState) -> str:
        exec_logs = state.get("execution_logs", "") or ""
        if "404" in exec_logs and state.get("retry_count", 0) <= MAX_RETRIES:
            logger.info("[Router] 404 detected — routing to site_analyzer for fresh page capture")
            return "site_analyzer"
        return "strategy_builder"

    graph.add_conditional_edges(
        "increment_retry",
        route_after_increment,
        {
            "site_analyzer":   "site_analyzer",
            "strategy_builder": "strategy_builder",
        }
    )

    # Memory write always ends
    graph.add_edge("memory_write", END)

    return graph.compile()


# ── Public runner ─────────────────────────────────────────────────────────────

def run_pipeline(url: str, fmt: str = "mp4", quality: str = "best",
                 cookies_path: str = None, job_id: str = None) -> VideoHunterState:
    """Run the full VideoHunter AI pipeline and return final state."""
    app = build_graph()

    initial_state: VideoHunterState = {
        "url":          url,
        "job_id":       job_id,
        "format":       fmt,
        "quality":      quality,
        "cookies_path": cookies_path,
        "raw_bundle":   None,
        "signal_map":   None,
        "strategy":     None,
        "script":       None,
        "script_path":  None,
        "memory_hit":   False,
        "memory_saved": False,
        "retry_count":  0,
        "error":        None,
        "current_node": None,
        "success":      False,
        "download_url": None,
    }

    logger.info(f"[Pipeline] Starting for URL: {url}")
    final_state = app.invoke(initial_state)
    logger.info(f"[Pipeline] Done — success={final_state.get('success')}")
    return final_state


# ── CLI quick test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, json, logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    result = run_pipeline(url)
    print("\n=== FINAL STATE ===")
    print(json.dumps({
        "success":     result.get("success"),
        "strategy":    result.get("strategy"),
        "script_path": result.get("script_path"),
        "error":       result.get("error"),
        "retry_count": result.get("retry_count"),
    }, indent=2))
