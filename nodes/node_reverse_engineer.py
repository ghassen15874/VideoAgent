"""
Node 2 — Reverse Engineer (LangGraph wrapper)
==============================================
Reads:  state["raw_bundle"]
Writes: state["signal_map"], state["error"]
"""

import json
import logging
import os
from core.state import VideoHunterState

logger = logging.getLogger(__name__)


def node_reverse_engineer(state: VideoHunterState) -> VideoHunterState:
    """Run Agent 2 Reverse Engineer and store signal map in state."""
    logger.info("[Node2] Reverse Engineer starting…")

    raw_bundle = state.get("raw_bundle")
    if not raw_bundle:
        return {**state, "current_node": "reverse_engineer",
                "signal_map": None, "error": "No raw bundle from Node 1"}

    # ── Guard: empty bundle (bot-blocked page) ───────────────────────────────
    total_requests = len(raw_bundle.get("network_requests", []))
    total_scripts  = len(raw_bundle.get("scripts", []))
    if total_requests == 0 and total_scripts == 0 and not raw_bundle.get("html"):
        logger.warning("[Node2] Raw bundle is empty — likely bot-blocked")
        return {
            **state,
            "current_node": "reverse_engineer",
            "signal_map": None,
            "error": "Empty bundle — site may have blocked the browser agent",
        }

    from agents.reverse_engineer import ReverseEngineer

    try:
        agent      = ReverseEngineer()
        signal_map = agent.analyze(raw_bundle)

        # Save signal map to disk for debugging
        os.makedirs("output/signals", exist_ok=True)
        domain_slug = signal_map.get("domain", "unknown").replace(".", "_")
        out_path    = f"output/signals/{domain_slug}_signals.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(signal_map, f, indent=2, ensure_ascii=False)
        logger.info(f"[Node2] Signal map saved → {out_path}")

        return {
            **state,
            "current_node": "reverse_engineer",
            "signal_map":   signal_map,
            "error":        None,
        }

    except Exception as e:
        logger.exception("[Node2] Reverse Engineer failed")
        return {
            **state,
            "current_node": "reverse_engineer",
            "signal_map":   None,
            "error":        str(e),
        }
