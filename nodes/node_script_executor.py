"""
Node 4.5 — Script Executor
================================
Runs the generated downloader script using subprocess.
If it fails, it returns success=False so the Router can retry.
"""

import logging
import subprocess
import os

from core.state import VideoHunterState

logger = logging.getLogger(__name__)


def node_script_executor(state: VideoHunterState) -> VideoHunterState:
    """Run the generated download script."""
    logger.info("[Node4.5] Script Executor starting…")

    script_path = state.get("script_path")
    if not script_path or not os.path.exists(script_path):
        return {
            **state,
            "current_node": "script_executor",
            "success": False,
            "error": "No script found to execute",
        }

    import sys
    
    job_id = state.get("job_id")
    log_path = f"output/logs/{job_id}.log" if job_id else f"{script_path}.log"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    
    try:
        logger.info(f"[Node4.5] Executing {script_path} (Logs: {log_path})")
        with open(log_path, "w", encoding="utf-8") as f:
            result = subprocess.run(
                [sys.executable, script_path],
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=300
            )

        with open(log_path, "r", encoding="utf-8") as f:
            full_logs = f.read()

        # ── Smart success detection ──────────────────────────────────────────
        # 1) Parse "[+] Saved: <path>" from script output (most reliable)
        saved_file = None
        for line in full_logs.splitlines():
            line = line.strip()
            if line.startswith("[+] Saved:") or line.startswith("[+] Saved to:"):
                # e.g.  "[+] Saved: downloads/video.mp4"
                saved_file = line.split(":", 1)[-1].strip()
                break

        # 2) Also accept yt-dlp's own "[download] X has already been downloaded"
        #    or "100% of" lines — check downloads/ dir for newest file
        if not saved_file and result.returncode == 0:
            dl_dir = "downloads"
            if os.path.isdir(dl_dir):
                files = [
                    os.path.join(dl_dir, f) for f in os.listdir(dl_dir)
                    if os.path.isfile(os.path.join(dl_dir, f))
                ]
                if files:
                    # Pick the most recently modified file
                    saved_file = max(files, key=os.path.getmtime)

        file_ok = (
            saved_file
            and os.path.exists(saved_file)
            and os.path.getsize(saved_file) > 0
        )

        if result.returncode == 0 and file_ok:
            logger.info(f"[Node4.5] ✅ Script executed successfully! Output: {saved_file}")
            return {
                **state,
                "current_node": "script_executor",
                "success": True,
                "download_url": saved_file,
                "error": None,
                "execution_logs": full_logs,
            }
        else:
            logger.error(
                f"[Node4.5] Script failed or no file produced "
                f"(returncode={result.returncode}, file={saved_file!r})"
            )
            return {
                **state,
                "current_node": "script_executor",
                "success": False,
                "error": "Execution failed or file empty",
                "execution_logs": full_logs,
            }

    except subprocess.TimeoutExpired:
        logger.error("[Node4.5] Script execution timed out.")
        return {
            **state,
            "current_node": "script_executor",
            "success": False,
            "error": "Download timed out (exceeded 5 minutes)",
        }
    except Exception as e:
        logger.exception("[Node4.5] Executor failed")
        return {
            **state,
            "current_node": "script_executor",
            "success": False,
            "error": str(e),
        }
