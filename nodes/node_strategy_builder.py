"""
Node 3 — Strategy Builder (LangGraph wrapper)
==============================================
Reads:  state["signal_map"], state["memory_hit"], state["retry_count"]
Writes: state["strategy"], state["error"]
"""

import logging
from core.state import VideoHunterState

logger = logging.getLogger(__name__)


def node_strategy_builder(state: VideoHunterState) -> VideoHunterState:
    """Run Agent 3 Strategy Builder and store strategy in state."""
    logger.info("[Node3] Strategy Builder starting…")

    # If we reached this node even on a memory_hit, it means the strategy 
    # required dynamic URLs (like m3u8 manifests) that need the LLM to extract 
    # from the fresh signal map. So we do NOT skip the LLM.

    signal_map = state.get("signal_map")
    if not signal_map:
        return {
            **state,
            "current_node": "strategy_builder",
            "strategy":     None,
            "error":        "No signal map from Node 2",
        }

    from agents.strategy_builder import StrategyBuilder

    try:
        # Inject previous error logs into signal_map so LLM can read it
        if state.get("retry_count", 0) > 0 and state.get("execution_logs"):
            signal_map["previous_error_logs"] = state.get("execution_logs")

        agent    = StrategyBuilder()
        strategy = agent.build(
            signal_map=signal_map,
            retry_count=state.get("retry_count", 0),
        )

        # Inject format/quality into strategy params
        strategy["params"]["format"]  = state.get("format", "mp4")
        strategy["params"]["quality"] = state.get("quality", "best")
        
        # Inject previous error logs if this is a retry loop
        if state.get("retry_count", 0) > 0 and state.get("execution_logs"):
            strategy["params"]["previous_error_logs"] = state.get("execution_logs")

        strategy["params"]["target_url"]    = state.get("url", "")
        strategy["params"]["cookie_file"]   = state.get("cookie_file") or \
                                              state.get("raw_bundle", {}).get("cookie_file")
        if state.get("execution_logs"):
            strategy["params"]["previous_error_logs"] = state.get("execution_logs")

        return {
            **state,
            "current_node": "strategy_builder",
            "strategy":     strategy,
            "error":        None,
        }

    except Exception as e:
        logger.exception("[Node3] Strategy Builder failed")
        return {
            **state,
            "current_node": "strategy_builder",
            "strategy":     None,
            "error":        str(e),
        }
