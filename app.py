"""
VideoHunter AI — Flask Application
===================================
Web UI: user submits a URL, chooses format (mp4/mp3) and quality,
then the Site Analyzer agent runs and returns the raw data bundle.
"""

# Load .env FIRST before any other imports that read env vars
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # python-dotenv not installed — env vars must be set manually

import json
import logging
import os
import threading
import uuid

from flask import Flask, jsonify, render_template, request

from graph import run_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)

# In-memory job store (replace with Redis/DB for production)
jobs: dict = {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Start an analysis job asynchronously."""
    data = request.get_json(force=True)
    url     = (data.get("url") or "").strip()
    fmt     = data.get("format", "mp4")        # mp4 | mp3
    quality = data.get("quality", "best")      # best | 1080p | 720p | 480p | 360p
    cookies = data.get("cookies_path", None)

    if not url or not url.startswith("http"):
        return jsonify({"error": "Invalid URL"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "url": url, "format": fmt, "quality": quality, "result": None, "error": None}
    TIMEOUT_SECONDS = 3600

    def run():
        try:
            final  = run_pipeline(url, fmt=fmt, quality=quality, cookies_path=cookies, job_id=job_id)
            bundle = final.get("raw_bundle") or {}
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = {
                "success":      final.get("success"),
                "strategy":     final.get("strategy"),
                "script":       final.get("script"),
                "script_path":  final.get("script_path"),
                "download_url": final.get("download_url"),
                "execution_logs": final.get("execution_logs"),
                "memory_hit":   final.get("memory_hit"),
                "retry_count":  final.get("retry_count", 0),
                "signal_map":   final.get("signal_map"),
                "meta":         bundle.get("meta", {}),
                "analyzer":     bundle.get("analyzer"),
                "video_tags":   bundle.get("video_tags", []),
                "blob_urls":    bundle.get("blob_urls", []),
                "media_requests": [
                    r for r in bundle.get("network_requests", [])
                    if r.get("type") in {"media", "fetch", "xhr"}
                    or any(ext in r.get("url", "")
                           for ext in [".m3u8", ".mpd", ".mp4", ".ts", "blob:"])
                ][:50],
                "format":  fmt,
                "quality": quality,
            }
        except Exception as exc:
            logger.exception("Pipeline job failed")
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"]  = str(exc)

    def watchdog():
        import time as _t
        _t.sleep(TIMEOUT_SECONDS)
        if jobs.get(job_id, {}).get("status") == "running":
            logger.error(f"[Watchdog] Job {job_id} timed out after {TIMEOUT_SECONDS}s")
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"]  = (
                f"Timeout after {TIMEOUT_SECONDS}s — "
                "site may be blocking the browser or network is too slow"
            )

    threading.Thread(target=run,      daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/batch", methods=["POST"])
def api_batch():
    """Start a sequential batch of URLs. Returns a list of job_ids in order."""
    data    = request.get_json(force=True)
    urls    = [u.strip() for u in (data.get("urls") or []) if u.strip().startswith("http")]
    fmt     = data.get("format",  "mp4")
    quality = data.get("quality", "best")
    cookies = data.get("cookies_path", None)

    if not urls:
        return jsonify({"error": "No valid URLs provided"}), 400

    # Create all job slots immediately so the UI can track them
    batch_ids = []
    for url in urls:
        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "queued",
            "url": url,
            "format": fmt,
            "quality": quality,
            "result": None,
            "error": None,
        }
        batch_ids.append(job_id)

    def run_sequential(ids):
        """Run each job one at a time, waiting for each to finish."""
        import time as _t
        for jid in ids:
            url = jobs[jid]["url"]
            jobs[jid]["status"] = "running"
            try:
                final  = run_pipeline(url, fmt=fmt, quality=quality, cookies_path=cookies, job_id=jid)
                bundle = final.get("raw_bundle") or {}
                jobs[jid]["status"] = "done"
                jobs[jid]["result"] = {
                    "success":        final.get("success"),
                    "strategy":       final.get("strategy"),
                    "script":         final.get("script"),
                    "script_path":    final.get("script_path"),
                    "download_url":   final.get("download_url"),
                    "execution_logs": final.get("execution_logs"),
                    "memory_hit":     final.get("memory_hit"),
                    "retry_count":    final.get("retry_count", 0),
                    "meta":           bundle.get("meta", {}),
                    "analyzer":       bundle.get("analyzer"),
                    "media_requests": [
                        r for r in bundle.get("network_requests", [])
                        if r.get("type") in {"media", "fetch", "xhr"}
                        or any(ext in r.get("url", "")
                               for ext in [".m3u8", ".mpd", ".mp4", ".ts", "blob:"])
                    ][:50],
                    "format":  fmt,
                    "quality": quality,
                }
            except Exception as exc:
                logger.exception(f"[Batch] Job {jid} failed")
                jobs[jid]["status"] = "error"
                jobs[jid]["error"]  = str(exc)
            # Small pause between downloads to avoid rate-limiting
            _t.sleep(2)

    threading.Thread(target=run_sequential, args=(batch_ids,), daemon=True).start()
    return jsonify({"batch_ids": batch_ids}), 202




@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
        
    # Inject live logs if still running
    if job["status"] == "running":
        log_path = f"output/logs/{job_id}.log"
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    job["live_logs"] = f.read()[-3000:] # Last 3000 chars
            except:
                pass

    return jsonify(job)


@app.route("/api/store", methods=["GET"])
def api_store():
    import sqlite3
    db_path = "memory/sites.db"
    if not os.path.exists(db_path):
        return jsonify([])
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT domain, success_rate, use_count, last_used FROM site_memory ORDER BY last_used DESC").fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/export/<domain>", methods=["GET"])
def api_export(domain):
    import sqlite3
    db_path = "memory/sites.db"
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT domain, strategy, signal_map FROM site_memory WHERE domain=?", (domain,)).fetchone()
        if not row:
            return jsonify({"error": "Domain not found"}), 404
        return jsonify({
            "vhunter_version": 1,
            "domain": row["domain"],
            "strategy": json.loads(row["strategy"]),
            "signal_map": json.loads(row["signal_map"])
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/import", methods=["POST"])
def api_import():
    import sqlite3, time
    from core.rag import add_to_index
    data = request.get_json(force=True)
    domain = data.get("domain")
    strategy = data.get("strategy")
    signal_map = data.get("signal_map")
    if not domain or not strategy:
        return jsonify({"error": "Invalid .vhunter format"}), 400
    
    db_path = "memory/sites.db"
    os.makedirs("memory", exist_ok=True)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS site_memory (
                domain TEXT PRIMARY KEY,
                strategy TEXT,
                signal_map TEXT,
                success_rate REAL,
                use_count INTEGER,
                last_used REAL
            )"""
        )
        existing = conn.execute("SELECT use_count FROM site_memory WHERE domain=?", (domain,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE site_memory SET strategy=?, signal_map=?, last_used=? WHERE domain=?",
                (json.dumps(strategy), json.dumps(signal_map or {}), time.time(), domain)
            )
        else:
            conn.execute(
                "INSERT INTO site_memory (domain, strategy, signal_map, success_rate, use_count, last_used) VALUES (?,?,?,?,?,?)",
                (domain, json.dumps(strategy), json.dumps(signal_map or {}), 1.0, 1, time.time())
            )
        conn.commit()
        # Also add to FAISS if signal_map exists
        if signal_map:
            add_to_index(domain, signal_map, strategy)
            
        return jsonify({"success": True, "domain": domain})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/jobs")
def api_jobs():
    summary = [
        {"job_id": jid, "url": j["url"], "status": j["status"],
         "format": j["format"], "quality": j["quality"]}
        for jid, j in jobs.items()
    ]
    return jsonify(summary)


if __name__ == "__main__":
    os.makedirs("output/raw", exist_ok=True)
    # IMPORTANT: use_reloader=False prevents Werkzeug from spawning a child
    # process that conflicts with asyncio event loops inside threads.
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)
