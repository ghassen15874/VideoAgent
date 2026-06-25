import os
import json
import time
import sqlite3
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allow local VideoAgent clients to connect

DB_PATH = "data/store.db"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "vhunter_admin123")  # Change in production

# Initialize DB
os.makedirs("data", exist_ok=True)
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

with get_db() as conn:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            strategy TEXT NOT NULL,
            signal_map TEXT,
            status TEXT DEFAULT 'pending',
            created_at REAL
        )
    """)
    conn.commit()

# --- PUBLIC API FOR VIDEOAGENT CLIENTS ---

@app.route("/api/strategies", methods=["GET"])
def get_approved_strategies():
    search = request.args.get("search", "").lower()
    with get_db() as conn:
        if search:
            query = "SELECT id, domain, strategy, signal_map FROM strategies WHERE status='approved' AND domain LIKE ?"
            rows = conn.execute(query, (f"%{search}%",)).fetchall()
        else:
            query = "SELECT id, domain, strategy, signal_map FROM strategies WHERE status='approved' ORDER BY created_at DESC"
            rows = conn.execute(query).fetchall()
            
    results = []
    for r in rows:
        results.append({
            "vhunter_version": 1,
            "domain": r["domain"],
            "strategy": json.loads(r["strategy"]),
            "signal_map": json.loads(r["signal_map"]) if r["signal_map"] else {}
        })
    return jsonify(results)

@app.route("/api/strategies/submit", methods=["POST"])
def submit_strategy():
    data = request.get_json(force=True)
    domain = data.get("domain")
    strategy = data.get("strategy")
    signal_map = data.get("signal_map", {})
    
    if not domain or not strategy:
        return jsonify({"error": "Missing domain or strategy"}), 400
        
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (domain, strategy, signal_map, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (domain, json.dumps(strategy), json.dumps(signal_map), time.time())
        )
        conn.commit()
    return jsonify({"success": True, "message": "Strategy submitted for approval!"})


# --- ADMIN PANEL ---

app.secret_key = os.environ.get("SECRET_KEY", "super-secret-vhunter-key")

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>VideoHunter Admin Login</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #0a0a0f; --card: rgba(26, 29, 45, 0.8); --accent: #7c3aed; --accent2: #06b6d4; }
        * { box-sizing: border-box; font-family: 'Inter', sans-serif; }
        body { background: var(--bg); color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin:0; position: relative; overflow: hidden; }
        .bg-orb { position: absolute; border-radius: 50%; filter: blur(80px); opacity: 0.15; z-index: 0; }
        .orb1 { width: 500px; height: 500px; background: var(--accent); top: -100px; left: -100px; }
        .orb2 { width: 400px; height: 400px; background: var(--accent2); bottom: -100px; right: -100px; }
        .card { background: var(--card); padding: 40px; border-radius: 16px; text-align: center; box-shadow: 0 10px 40px rgba(0,0,0,0.5); backdrop-filter: blur(12px); border: 1px solid rgba(255,255,255,0.05); z-index: 1; width: 350px; }
        h2 { margin-top: 0; font-weight: 800; background: linear-gradient(135deg, #a78bfa, #67e8f9); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        input { padding: 14px; margin: 20px 0; width: 100%; border-radius: 8px; border: 1px solid #2a2a3d; background: rgba(13, 13, 24, 0.8); color: #fff; outline: none; transition: all 0.3s; }
        input:focus { border-color: var(--accent2); box-shadow: 0 0 10px rgba(6,182,212,0.3); }
        button { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: white; border: none; padding: 14px; width: 100%; border-radius: 8px; cursor: pointer; font-weight: bold; transition: transform 0.2s, box-shadow 0.2s; }
        button:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(124,58,237,0.4); }
    </style>
</head>
<body>
    <div class="bg-orb orb1"></div>
    <div class="bg-orb orb2"></div>
    <div class="card">
        <h2>🔒 Admin Portal</h2>
        {% if error %}<p style="color: #ef4444; font-size: 13px;">{{ error }}</p>{% endif %}
        <form method="POST">
            <input type="password" name="password" placeholder="Enter Admin Password" required autofocus>
            <button type="submit">Authenticate</button>
        </form>
    </div>
</body>
</html>
"""

ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>VideoHunter Admin</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #0a0a0f; --card: rgba(26, 29, 45, 0.6); --accent: #7c3aed; --accent2: #06b6d4; --border: rgba(255,255,255,0.05); }
        * { box-sizing: border-box; font-family: 'Inter', sans-serif; }
        body { background: var(--bg); color: #fff; margin:0; padding: 40px; }
        .bg-orb { position: fixed; border-radius: 50%; filter: blur(100px); opacity: 0.1; z-index: 0; pointer-events: none; }
        .orb1 { width: 600px; height: 600px; background: var(--accent); top: -200px; left: -200px; }
        .orb2 { width: 500px; height: 500px; background: var(--accent2); bottom: -100px; right: -100px; }
        .container { position: relative; z-index: 1; max-width: 900px; margin: 0 auto; }
        .card { background: var(--card); padding: 24px; border-radius: 16px; margin-bottom: 24px; backdrop-filter: blur(12px); border: 1px solid var(--border); box-shadow: 0 8px 30px rgba(0,0,0,0.3); }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }
        h1 { margin: 0; font-weight: 800; font-size: 24px; background: linear-gradient(135deg, #a78bfa, #67e8f9); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        h2 { margin-top: 0; font-size: 16px; color: #e2e8f0; border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 16px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 14px 10px; border-bottom: 1px solid var(--border); font-size: 14px; }
        th { color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em; }
        tr:hover { background: rgba(255,255,255,0.02); }
        button { border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 12px; transition: all 0.2s; }
        .btn-reject { background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); }
        .btn-reject:hover { background: rgba(239, 68, 68, 0.25); transform: translateY(-1px); }
        .btn-approve { background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); }
        .btn-approve:hover { background: rgba(16, 185, 129, 0.25); transform: translateY(-1px); }
        .btn-primary { background: linear-gradient(135deg, var(--accent2), #3b82f6); color: #000; box-shadow: 0 4px 15px rgba(6,182,212,0.3); }
        .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(6,182,212,0.4); }
    </style>
</head>
<body>
    <div class="bg-orb orb1"></div>
    <div class="bg-orb orb2"></div>
    <div class="container">
        <div class="header">
            <h1>📦 Community Store Admin</h1>
            <form method="POST" action="/admin/logout" style="margin:0;"><button class="btn-reject">Logout</button></form>
        </div>
        
        <div class="card">
            <h2>📤 Upload Manually (Auto-Approve)</h2>
            <form method="POST" action="/admin/upload" enctype="multipart/form-data" style="display:flex; gap:10px; align-items:center;">
                <input type="file" name="file" accept=".vhunter" required style="font-size:13px; color:#94a3b8; background:rgba(0,0,0,0.2); padding:10px; border-radius:8px; border:1px solid var(--border);">
                <button type="submit" class="btn-primary">Direct Upload</button>
            </form>
        </div>

        <div class="card">
            <h2>⏳ Pending Submissions ({{ pending|length }})</h2>
            {% if pending|length == 0 %}<p style="color:#64748b; font-size:13px;">No pending strategies.</p>{% else %}
            <table>
                <tr><th>ID</th><th>Domain</th><th>Date</th><th style="text-align:right">Action</th></tr>
                {% for p in pending %}
                <tr>
                    <td style="color:#64748b;">#{{ p.id }}</td>
                    <td><strong style="color:#f8fafc;">{{ p.domain }}</strong></td>
                    <td style="color:#94a3b8; font-size:12px;">{{ p.created_at }}</td>
                    <td style="text-align:right;">
                        <form method="POST" action="/admin/approve/{{ p.id }}" style="display:inline;">
                            <button type="submit" class="btn-approve">Approve</button>
                        </form>
                        <form method="POST" action="/admin/reject/{{ p.id }}" style="display:inline; margin-left:8px;">
                            <button type="submit" class="btn-reject">Reject</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </table>
            {% endif %}
        </div>

        <div class="card">
            <h2>✅ Approved Strategies ({{ approved|length }})</h2>
            {% if approved|length == 0 %}<p style="color:#64748b; font-size:13px;">No approved strategies.</p>{% else %}
            <table>
                <tr><th>ID</th><th>Domain</th><th style="text-align:right">Action</th></tr>
                {% for a in approved %}
                <tr>
                    <td style="color:#64748b;">#{{ a.id }}</td>
                    <td><strong style="color:#f8fafc;">{{ a.domain }}</strong></td>
                    <td style="text-align:right;">
                        <form method="POST" action="/admin/reject/{{ a.id }}" style="display:inline;">
                            <button type="submit" class="btn-reject">Remove</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </table>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

from flask import session, redirect, url_for

def is_admin():
    return session.get("admin_logged_in") == True

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASS:
            session["admin_logged_in"] = True
            return redirect("/admin")
        else:
            error = "Invalid password!"
    return render_template_string(LOGIN_TEMPLATE, error=error)

@app.route("/admin/logout", methods=["POST", "GET"])
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect("/admin/login")

@app.route("/admin", methods=["GET"])
def admin_page():
    if not is_admin(): return redirect("/admin/login")
    with get_db() as conn:
        pending = conn.execute("SELECT id, domain, created_at FROM strategies WHERE status='pending' ORDER BY created_at ASC").fetchall()
        approved = conn.execute("SELECT id, domain, created_at FROM strategies WHERE status='approved' ORDER BY created_at DESC").fetchall()
    return render_template_string(ADMIN_TEMPLATE, pending=pending, approved=approved)

@app.route("/admin/approve/<int:strat_id>", methods=["POST"])
def approve_strategy(strat_id):
    if not is_admin(): return redirect("/admin/login")
    with get_db() as conn:
        conn.execute("UPDATE strategies SET status='approved' WHERE id=?", (strat_id,))
        conn.commit()
    return redirect("/admin")

@app.route("/admin/reject/<int:strat_id>", methods=["POST"])
def reject_strategy(strat_id):
    if not is_admin(): return redirect("/admin/login")
    with get_db() as conn:
        conn.execute("DELETE FROM strategies WHERE id=?", (strat_id,))
        conn.commit()
    return redirect("/admin")

@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    if not is_admin(): return redirect("/admin/login")
    f = request.files.get("file")
    if not f: return "No file", 400
    try:
        data = json.loads(f.read())
        with get_db() as conn:
            conn.execute(
                "INSERT INTO strategies (domain, strategy, signal_map, status, created_at) VALUES (?, ?, ?, 'approved', ?)",
                (data["domain"], json.dumps(data["strategy"]), json.dumps(data.get("signal_map",{})), time.time())
            )
            conn.commit()
        return redirect("/admin")
    except Exception as e:
        return f"Error: {e}", 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
