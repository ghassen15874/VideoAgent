"""
Core — RAG Engine (FAISS + SQLite)
====================================
Embeds signal maps into vectors and retrieves similar past domains.
Used by Agent 3 Strategy Builder to find analogous sites.
"""

import json
import logging
import os
import sqlite3
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

DB_PATH    = "memory/sites.db"
INDEX_PATH = "memory/faiss.index"
DIM        = 384   # sentence-transformers/all-MiniLM-L6-v2 dimension


# ── Lazy-load heavy deps ──────────────────────────────────────────────────────

_embed_model = None
_faiss_index = None
_index_domain_map: list = []   # maps FAISS index position → domain string


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("[RAG] Loaded embedding model: all-MiniLM-L6-v2")
        except ImportError:
            logger.warning("[RAG] sentence-transformers not installed — RAG disabled")
            _embed_model = None
    return _embed_model


def _get_faiss_index():
    global _faiss_index, _index_domain_map
    if _faiss_index is not None:
        return _faiss_index

    try:
        import faiss
    except ImportError:
        logger.warning("[RAG] faiss-cpu not installed — RAG disabled")
        return None

    os.makedirs("memory", exist_ok=True)

    if os.path.exists(INDEX_PATH) and os.path.exists(INDEX_PATH + ".map"):
        _faiss_index = faiss.read_index(INDEX_PATH)
        with open(INDEX_PATH + ".map") as f:
            _index_domain_map = json.load(f)
        logger.info(f"[RAG] Loaded FAISS index — {_faiss_index.ntotal} vectors")
    else:
        _faiss_index = faiss.IndexFlatL2(DIM)
        _index_domain_map = []
        logger.info("[RAG] Created new FAISS index")

    return _faiss_index


def _save_index():
    try:
        import faiss
        faiss.write_index(_faiss_index, INDEX_PATH)
        with open(INDEX_PATH + ".map", "w") as f:
            json.dump(_index_domain_map, f)
    except Exception as e:
        logger.warning(f"[RAG] Failed to save index: {e}")


def _signal_map_to_text(signal_map: dict) -> str:
    """Convert signal map to a searchable text representation."""
    sigs = signal_map.get("signals", {})
    parts = [
        f"domain:{signal_map.get('domain', '')}",
        f"m3u8:{sigs.get('m3u8_found', False)}",
        f"mpd:{sigs.get('mpd_found', False)}",
        f"blob:{sigs.get('blob_detected', False)}",
        f"direct:{sigs.get('direct_video_tag', False)}",
        f"auth:{sigs.get('auth_required', False)}",
        f"player:{sigs.get('player_library', 'none')}",
        f"hls_js:{sigs.get('hls_loader_in_js', False)}",
        f"cdn:{sigs.get('cdn_detected', False)}",
        f"obfuscated:{sigs.get('js_obfuscated', False)}",
    ]
    return " ".join(parts)


def embed_signal_map(signal_map: dict) -> Optional[np.ndarray]:
    """Return a numpy embedding vector for the given signal map."""
    model = _get_embed_model()
    if model is None:
        return None
    text = _signal_map_to_text(signal_map)
    vec  = model.encode([text], convert_to_numpy=True)
    return vec.astype(np.float32)


def add_to_index(domain: str, signal_map: dict, strategy: dict) -> bool:
    """Add a new site to the FAISS index and SQLite store."""
    vec   = embed_signal_map(signal_map)
    index = _get_faiss_index()
    if vec is None or index is None:
        return False

    try:
        import faiss
        index.add(vec)
        _index_domain_map.append(domain)
        _save_index()
        logger.info(f"[RAG] Added {domain} to FAISS index (total: {index.ntotal})")
        return True
    except Exception as e:
        logger.warning(f"[RAG] Failed to add to index: {e}")
        return False


def query_similar(signal_map: dict, top_k: int = 3) -> list:
    """
    Find top-k most similar past domains.
    Returns list of dicts: {domain, strategy, distance}
    """
    vec   = embed_signal_map(signal_map)
    index = _get_faiss_index()

    if vec is None or index is None or index.ntotal == 0:
        return []

    try:
        k = min(top_k, index.ntotal)
        distances, indices = index.search(vec, k)

        results = []
        conn = sqlite3.connect(DB_PATH)
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(_index_domain_map):
                continue
            domain = _index_domain_map[idx]
            row = conn.execute(
                "SELECT strategy, success_rate FROM site_memory WHERE domain=?",
                (domain,)
            ).fetchone()
            if row:
                results.append({
                    "domain":       domain,
                    "strategy":     json.loads(row[0]),
                    "success_rate": row[1],
                    "distance":     float(dist),
                })
        conn.close()
        return results

    except Exception as e:
        logger.warning(f"[RAG] Query failed: {e}")
        return []
