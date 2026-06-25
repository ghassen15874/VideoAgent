"""
Node 5 — Memory Agent (LangGraph wrapper)
==========================================
Two nodes:
  node_memory_read  → called before Strategy Builder (cache lookup)
  node_memory_write → called after success (save + add to FAISS index)
"""

import json
import logging
import os
import sqlite3
import time
from urllib.parse import urlparse

from core.state import VideoHunterState

logger = logging.getLogger(__name__)

DB_PATH = "memory/sites.db"


def _get_conn() -> sqlite3.Connection:
    os.makedirs("memory", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS site_memory (
            id           INTEGER PRIMARY KEY,
            domain       TEXT UNIQUE,
            strategy     TEXT,
            signal_map   TEXT,
            success_rate REAL DEFAULT 1.0,
            use_count    INTEGER DEFAULT 1,
            last_used    REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extraction_log (
            id        INTEGER PRIMARY KEY,
            domain    TEXT,
            url       TEXT,
            strategy  TEXT,
            success   INTEGER,
            error_msg TEXT,
            timestamp REAL
        )
    """)
    conn.commit()
    return conn


# ── Read node ──────────────────────────────────────────────────────────────────

def node_memory_read(state: VideoHunterState) -> VideoHunterState:
    """Check SQLite cache for a known strategy for this domain."""
    logger.info("[Node5-Read] Checking memory cache…")

    domain = urlparse(state.get("url", "")).netloc
    if not domain:
        return {**state, "current_node": "memory_read", "memory_hit": False}

    try:
        conn = _get_conn()
        row  = conn.execute(
            "SELECT strategy, success_rate, use_count FROM site_memory WHERE domain=?",
            (domain,)
        ).fetchone()

        if row and row[1] >= 0.5:
            cached = json.loads(row[0])
            use_count = row[2] + 1
            
            # CRITICAL: Overwrite the target_url with the CURRENT request URL
            # Otherwise we download the old video that was cached!
            if "params" in cached:
                cached["params"]["target_url"] = state.get("url")
            else:
                cached["params"] = {"target_url": state.get("url")}
                
            # Update last_used + use_count
            conn.execute(
                "UPDATE site_memory SET last_used=?, use_count=? WHERE domain=?",
                (time.time(), use_count, domain)
            )
            conn.commit()
            conn.close()

            logger.info(
                f"[Node5-Read] HIT for {domain} → {cached.get('strategy')} "
                f"(success_rate={row[1]:.2f}, used {use_count}x)"
            )
            return {
                **state,
                "current_node": "memory_read",
                "memory_hit": True,
                "strategy": cached,
                "error": None,
            }

        conn.close()
    except Exception as e:
        logger.warning(f"[Node5-Read] DB error: {e}")

    logger.info(f"[Node5-Read] MISS for {domain}")
    return {**state, "current_node": "memory_read", "memory_hit": False}


# ── Write node ─────────────────────────────────────────────────────────────────

def node_memory_write(state: VideoHunterState) -> VideoHunterState:
    """Save strategy + update FAISS index after a successful extraction."""
    logger.info("[Node5-Write] Saving to memory…")

    domain     = urlparse(state.get("url", "")).netloc
    strategy   = state.get("strategy")
    signal_map = state.get("signal_map")
    success    = state.get("success", False)

    if not domain or not strategy:
        return {**state, "current_node": "memory_write", "memory_saved": False}

    try:
        conn = _get_conn()

        # Always log the attempt
        conn.execute(
            """INSERT INTO extraction_log
               (domain, url, strategy, success, error_msg, timestamp)
               VALUES (?,?,?,?,?,?)""",
            (domain, state.get("url"), strategy.get("strategy"),
             int(success), state.get("error"), time.time())
        )

        if success:
            existing = conn.execute(
                "SELECT use_count, success_rate FROM site_memory WHERE domain=?",
                (domain,)
            ).fetchone()

            if existing:
                count    = existing[0] + 1
                new_rate = (existing[1] * existing[0] + 1.0) / count
                conn.execute(
                    """UPDATE site_memory
                       SET strategy=?, signal_map=?, success_rate=?,
                           use_count=?, last_used=?
                       WHERE domain=?""",
                    (json.dumps(strategy), json.dumps(signal_map or {}),
                     new_rate, count, time.time(), domain)
                )
            else:
                conn.execute(
                    """INSERT INTO site_memory
                       (domain, strategy, signal_map, success_rate, use_count, last_used)
                       VALUES (?,?,?,?,?,?)""",
                    (domain, json.dumps(strategy), json.dumps(signal_map or {}),
                     1.0, 1, time.time())
                )

            conn.commit()
            conn.close()

            # ── Add to FAISS index ────────────────────────────────────────────
            if signal_map:
                try:
                    from core.rag import add_to_index
                    added = add_to_index(domain, signal_map, strategy)
                    logger.info(f"[Node5-Write] FAISS index updated: {added}")
                except Exception as re:
                    logger.warning(f"[Node5-Write] FAISS update failed: {re}")

            logger.info(f"[Node5-Write] Saved strategy for {domain}")
            return {**state, "current_node": "memory_write", "memory_saved": True}

        else:
            # Penalize failed strategy
            conn.execute(
                "UPDATE site_memory SET success_rate=MAX(0, success_rate-0.2) WHERE domain=?",
                (domain,)
            )
            conn.commit()
            conn.close()
            logger.info(f"[Node5-Write] Penalized strategy for {domain}")

    except Exception as e:
        logger.warning(f"[Node5-Write] DB error: {e}")

    return {**state, "current_node": "memory_write", "memory_saved": False}
