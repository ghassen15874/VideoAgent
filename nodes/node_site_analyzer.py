"""
Node 1 — Site Analyzer
=======================
LangGraph node wrapper around agents/site_analyzer.py.
Reads:  state["url"], state["cookies_path"]
Writes: state["raw_bundle"], state["error"], state["current_node"]
"""

import logging
from core.state import VideoHunterState

logger = logging.getLogger(__name__)


def node_site_analyzer(state: VideoHunterState) -> VideoHunterState:
    """Run Site Analyzer and store raw bundle in state."""
    logger.info("[Node1] Site Analyzer starting…")

    from agents.site_analyzer import SiteAnalyzer

    try:
        analyzer = SiteAnalyzer()
        bundle = analyzer.analyze(
            url=state["url"],
            cookies_path=state.get("cookies_path"),
        )
        if bundle.get("error"):
            logger.error(f"[Node1] Analyzer error: {bundle['error']}")
            return {
                **state,
                "current_node": "site_analyzer",
                "raw_bundle": bundle,
                "error": bundle["error"],
            }

        logger.info(f"[Node1] Done — {bundle['meta']['request_count']} requests captured")
        return {
            **state,
            "current_node": "site_analyzer",
            "raw_bundle": bundle,
            "error": None,
        }

    except Exception as e:
        logger.exception("[Node1] Unexpected failure")
        return {
            **state,
            "current_node": "site_analyzer",
            "raw_bundle": None,
            "error": str(e),
        }
