"""
Node 4 — Downloader Generator (LangGraph wrapper)
==================================================
Reads:  state["strategy"], state["format"], state["quality"]
Writes: state["script"], state["script_path"], state["success"]
"""

import logging
from core.state import VideoHunterState

logger = logging.getLogger(__name__)


def node_downloader_generator(state: VideoHunterState) -> VideoHunterState:
    """Generate download script from strategy and store in state."""
    logger.info("[Node4] Downloader Generator starting…")

    strategy = state.get("strategy")
    if not strategy:
        return {**state, "current_node": "downloader_generator",
                "script": None, "success": False,
                "error": "No strategy from Node 3"}

    from agents.downloader_generator import DownloaderGenerator

    try:
        gen    = DownloaderGenerator()
        result = gen.generate(
            strategy=strategy,
            output_format=state.get("format", "mp4"),
            quality=state.get("quality", "best"),
        )

        success = result["syntax_ok"]
        logger.info(
            f"[Node4] Script generated: {result['script_path']} "
            f"(strategy={result['strategy']}, valid={success})"
        )

        return {
            **state,
            "current_node": "downloader_generator",
            "script":       result["script"],
            "script_path":  result["script_path"],
            "download_url": result["output_file"],
            "success":      success,
            "error":        result.get("syntax_error") if not success else None,
        }

    except Exception as e:
        logger.exception("[Node4] Downloader Generator failed")
        return {
            **state,
            "current_node": "downloader_generator",
            "script":       None,
            "script_path":  None,
            "success":      False,
            "error":        str(e),
        }
