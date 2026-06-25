"""
VideoHunter AI — Shared State
==============================
Single TypedDict that flows through every node in the LangGraph.
Every agent reads from and writes to this state object.
"""

from typing import Any, Optional
from typing_extensions import TypedDict


class VideoHunterState(TypedDict, total=False):
    # ── Input ────────────────────────────────────────────────────────────────
    url:          str               # Target URL submitted by user
    job_id:       Optional[str]
    format:       str               # "mp4" | "mp3"
    quality:      str               # "best" | "1080p" | "720p" | "480p" | "360p"
    cookies_path: Optional[str]     # Optional path to cookies JSON file

    # ── Agent 1 output ───────────────────────────────────────────────────────
    raw_bundle:   Optional[dict]    # Full Raw Data Bundle from Site Analyzer
    cookie_file:  Optional[str]     # Netscape cookie file saved by Playwright

    # ── Agent 2 output ───────────────────────────────────────────────────────
    signal_map:   Optional[dict]    # Structured signal map from Reverse Engineer

    # ── Agent 3 output ───────────────────────────────────────────────────────
    strategy:     Optional[dict]    # Chosen strategy from Strategy Builder

    # ── Agent 4 output ───────────────────────────────────────────────────────
    script:       Optional[str]     # Generated Python downloader script
    script_path:  Optional[str]
    download_url: Optional[str]     # Final output path (e.g. downloads/video.mp4)
    execution_logs: Optional[str]   # Output from running the script
    success:      bool
    error:        Optional[str]
    current_node: Optional[str]     # For logging/UI progress tracking

    # ── Agent 5 output ───────────────────────────────────────────────────────
    memory_hit:   bool              # True if strategy was loaded from cache
    memory_saved: bool              # True if strategy was saved after success

    # ── Control flow ─────────────────────────────────────────────────────────
    retry_count:  int               # How many times strategy has been retried
    error:        Optional[str]     # Last error message (any agent)
    current_node: Optional[str]     # For logging/UI progress tracking

    # ── Final result ─────────────────────────────────────────────────────────
    success:      bool              # True if extraction completed successfully
    download_url: Optional[str]     # Final direct download URL (if found)
