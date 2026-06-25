"""
Agent 4 — Downloader Generator
================================
Receives a Strategy JSON and generates a ready-to-run Python
download script using type-specific templates.

Strategies handled:
  direct_url    → requests + tqdm
  m3u8_ffmpeg   → ffmpeg subprocess
  m3u8_ytdlp   → yt-dlp with headers
  mpd_ytdlp    → yt-dlp DASH
  api_fetch     → requests API call → extract URL → download
  blob_capture  → browser console snippet + yt-dlp fallback
  ytdlp_generic → yt-dlp auto-detect
  custom        → LLM-generated code (placeholder)
"""

import ast
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

OUTPUT_DIR  = "output/scripts"
DOWNLOAD_DIR = "downloads"

# ═══════════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════════

_HEADER = '''#!/usr/bin/env python3
"""
VideoHunter AI — Auto-generated downloader
Strategy : {strategy}
Domain   : {domain}
Generated: {timestamp}
"""
import os
import sys

# Ensure local venv binaries (like ffmpeg) are prioritized in PATH
os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

os.makedirs("{download_dir}", exist_ok=True)
'''

TEMPLATE_YTDLP_GENERIC = _HEADER + '''
import yt_dlp
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _impersonate = ImpersonateTarget("chrome")
except Exception:
    _impersonate = None

URL        = "{target_url}"
FORMAT     = "{ydl_format}"
OUTDIR     = "{download_dir}"
HEADERS    = {headers}
COOKIEFILE = {cookiefile_repr}

ydl_opts = {{
    "format":                    FORMAT,
    "outtmpl":                   os.path.join(OUTDIR, "%(title)s.%(ext)s"),
    "http_headers":              HEADERS,
    # Use playlist_items instead of noplaylist=True:
    # noplaylist skips the extractor resolution step for playlist entries,
    # meaning yt-dlp may try to download a stale/obfuscated URL directly → 404.
    # playlist_items="1" still resolves the full extractor chain but only downloads #1.
    "playlist_items":            "1",
    "retries":                   8,
    "fragment_retries":          15,
    "extractor_retries":         3,
    "concurrent_fragment_downloads": 4,
    "sleep_interval":            1,
    "max_sleep_interval":        5,
    "postprocessors":            {postprocessors},
}}
if _impersonate:
    ydl_opts["impersonate"] = _impersonate
if COOKIEFILE and os.path.exists(COOKIEFILE):
    ydl_opts["cookiefile"] = COOKIEFILE
    print(f"[*] Using cookies from {{COOKIEFILE}}")

print(f"[*] Downloading: {{URL}}")
try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(URL, download=True)
        if info:
            # Handle playlists gracefully
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                if entries:
                    info = entries[0]
            filename = ydl.prepare_filename(info)
            print(f"[+] Saved: {{filename}}")
        else:
            print("[-] No info extracted — download may have failed")
except yt_dlp.utils.DownloadError as e:
    err_str = str(e)
    if "404" in err_str:
        print(f"[-] HTTP 404 on extracted URL — video may use signed/expiring CDN tokens.")
        print(f"[-] Re-running with --force-generic-extractor and fresh cookies...")
        ydl_opts["force_generic_extractor"] = True
        ydl_opts.pop("playlist_items", None)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
            info2 = ydl2.extract_info(URL, download=True)
            if info2:
                print(f"[+] Saved: {{ydl2.prepare_filename(info2)}}")
    else:
        raise
'''

TEMPLATE_M3U8_FFMPEG = _HEADER + '''
import subprocess, sys, os

MANIFEST   = "{manifest_url}"
OUTPUT     = os.path.join("{download_dir}", "{output_filename}")
HEADERS    = {ffmpeg_headers}
TARGET_URL = "{target_url}"

# Auto-inject Referer/Origin from the target page to bypass hotlink stubs
from urllib.parse import urlparse as _up
_ref_base = TARGET_URL or MANIFEST
_parsed   = _up(_ref_base)
_base     = f"{{_parsed.scheme}}://{{_parsed.netloc}}"
HEADERS.setdefault("Referer",  _base + "/")
HEADERS.setdefault("Origin",   _base)
HEADERS.setdefault("User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

cmd = ["ffmpeg", "-y"]
for key, val in HEADERS.items():
    cmd += ["-headers", f"{{key}}: {{val}}\\r\\n"]
cmd += ["-i", MANIFEST, "-c", "copy", "-bsf:a", "aac_adtstoasc", OUTPUT]

print(f"[*] Running ffmpeg for HLS manifest...")
print(f"[*] Using Referer: {{HEADERS.get('Referer', 'none')}}")
result = subprocess.run(cmd, capture_output=False)

def _validate_output():
    if not os.path.exists(OUTPUT) or os.path.getsize(OUTPUT) == 0:
        return False
    try:
        import subprocess as _sp
        _r = _sp.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=codec_name,width",
                      "-of", "csv=p=0", OUTPUT], capture_output=True, text=True, timeout=15)
        _out = _r.stdout.strip()
        if "png" in _out or ",1" in _out or _out == "":
            return False
        return True
    except Exception:
        return os.path.getsize(OUTPUT) > 5*1024*1024

if result.returncode == 0 and _validate_output():
    print(f"[+] Saved to: {{OUTPUT}}")
else:
    print("[-] ffmpeg failed or produced invalid stub — trying yt-dlp fallback...")
    if os.path.exists(OUTPUT): os.remove(OUTPUT)
    import yt_dlp
    ydl_opts = {{
        "outtmpl": OUTPUT,
        "http_headers": HEADERS,
        "external_downloader": "native",
        "hls_use_mpegts": True,
        "concurrent_fragment_downloads": 4,
        "retries": 10,
        "fragment_retries": 20
    }}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([MANIFEST])
            if _validate_output():
                print(f"[+] Saved to: {{OUTPUT}}")
            else:
                print("[-] yt-dlp fallback also produced invalid stub")
                raise SystemExit(1)
        except Exception as e:
            print(f"[-] yt-dlp fallback failed: {{e}}")
            raise SystemExit(1)
'''

TEMPLATE_M3U8_YTDLP = _HEADER + '''
import yt_dlp
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _impersonate = ImpersonateTarget("chrome")
except Exception:
    _impersonate = None

MANIFEST   = "{manifest_url}"
OUTPUT     = os.path.join("{download_dir}", "{output_filename}")
HEADERS    = {headers}
FORMAT     = "{ydl_format}"
COOKIEFILE = {cookiefile_repr}
TARGET_URL = "{target_url}"

# Auto-inject Referer/Origin from the target page
# Many CDN proxy servers return 1x1 PNG stubs when Referer is missing/wrong
from urllib.parse import urlparse as _up
_ref_base = TARGET_URL or MANIFEST
_parsed   = _up(_ref_base)
_base     = f"{{_parsed.scheme}}://{{_parsed.netloc}}"
HEADERS.setdefault("Referer",  _base + "/")
HEADERS.setdefault("Origin",   _base)
HEADERS.setdefault("User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ydl_opts = {{
    "format":     FORMAT,
    "outtmpl":    OUTPUT,
    "http_headers": HEADERS,
    "retries":    10,
    "fragment_retries": 20,
    "postprocessors": {postprocessors},
    # Force internal Python downloader — avoids ffmpeg SIGSEGV (-11) on HLS
    "external_downloader": "native",
    "hls_use_mpegts": True,
    "concurrent_fragment_downloads": 4,
}}
if _impersonate:
    ydl_opts["impersonate"] = _impersonate
if COOKIEFILE and os.path.exists(COOKIEFILE):
    ydl_opts["cookiefile"] = COOKIEFILE
    print(f"[*] Using cookies from {{COOKIEFILE}}")

print(f"[*] Downloading HLS via yt-dlp: {{MANIFEST}}")
print(f"[*] Using Referer: {{HEADERS.get('Referer', 'none')}}")
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([MANIFEST])

# Post-download: validate file is real video, not a hotlink stub (1x1 PNG)
import subprocess as _sp
if os.path.exists(OUTPUT) and os.path.getsize(OUTPUT) > 0:
    try:
        _r = _sp.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width",
             "-of", "csv=p=0", OUTPUT],
            capture_output=True, text=True, timeout=20
        )
        _out = _r.stdout.strip()
        _bad = "png" in _out or ",1" in _out or _out == ""
        if not _bad and any(c in _out for c in ["h264","hevc","vp9","av1","avc"]):
            print(f"[+] Saved to: {{OUTPUT}}")
        elif _bad:
            print(f"[-] ERROR: Output contains invalid/stub content ({{_out!r}})")
            print(f"[-] The CDN rejected segment requests — Referer/cookies may be insufficient")
            raise SystemExit(1)
        else:
            print(f"[+] Saved to: {{OUTPUT}}")
    except FileNotFoundError:
        # ffprobe not found, assume OK if > 5MB
        if os.path.getsize(OUTPUT) > 5*1024*1024:
            print(f"[+] Saved to: {{OUTPUT}}")
        else:
            print(f"[-] File too small, possibly a stub")
else:
    print(f"[-] Output file missing or empty")
    raise SystemExit(1)
'''

TEMPLATE_MPD_YTDLP = _HEADER + '''
import yt_dlp
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _impersonate = ImpersonateTarget("chrome")
except Exception:
    _impersonate = None

MPD_URL    = "{manifest_url}"
OUTPUT     = os.path.join("{download_dir}", "{output_filename}")
HEADERS    = {headers}
FORMAT     = "{ydl_format}"
COOKIEFILE = {cookiefile_repr}

ydl_opts = {{
    "format":     FORMAT,
    "outtmpl":    OUTPUT,
    "http_headers": HEADERS,
    "retries":    10,
    "fragment_retries": 20,
    "postprocessors": {postprocessors},
    # Force internal Python downloader — avoids ffmpeg SIGSEGV (-11)
    "external_downloader": "native",
    "concurrent_fragment_downloads": 4,
}}
if _impersonate:
    ydl_opts["impersonate"] = _impersonate
if COOKIEFILE and os.path.exists(COOKIEFILE):
    ydl_opts["cookiefile"] = COOKIEFILE
    print(f"[*] Using cookies from {{COOKIEFILE}}")

print(f"[*] Downloading DASH/MPD via yt-dlp: {{MPD_URL}}")
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([MPD_URL])
print("[+] Done!")
'''

TEMPLATE_DIRECT_URL = _HEADER + '''
import requests
from tqdm import tqdm

URL     = "{video_url}"
OUTPUT  = os.path.join("{download_dir}", "{output_filename}")
HEADERS = {headers}
HEADERS.setdefault("User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

print(f"[*] Downloading direct URL: {{URL}}")
session = requests.Session()
session.headers.update(HEADERS)
r = session.get(URL, stream=True, timeout=60)
r.raise_for_status()

total = int(r.headers.get("content-length", 0))
with open(OUTPUT, "wb") as f, tqdm(
    total=total, unit="B", unit_scale=True, desc="Downloading"
) as bar:
    for chunk in r.iter_content(chunk_size=8192):
        f.write(chunk)
        bar.update(len(chunk))

print(f"[+] Saved to: {{OUTPUT}}")
'''

TEMPLATE_API_FETCH = _HEADER + '''
import requests
import yt_dlp
import http.cookiejar

API_URL    = "{api_endpoint}"
TARGET     = "{target_url}"
OUTPUT     = os.path.join("{download_dir}", "{output_filename}")
HEADERS    = {headers}
COOKIEFILE = {cookiefile_repr}
HEADERS.setdefault("User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS.setdefault("Referer", "{target_url}")
HEADERS.setdefault("X-Requested-With", "XMLHttpRequest")

# Step 1: Build a session with cookies
session = requests.Session()
session.headers.update(HEADERS)
if COOKIEFILE and os.path.exists(COOKIEFILE):
    cj = http.cookiejar.MozillaCookieJar(COOKIEFILE)
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(cj)
        print(f"[*] Loaded cookies from {{COOKIEFILE}}")
    except Exception as ce:
        print(f"[!] Cookie load failed: {{ce}}")

# Step 2: Hit the API to get the real video URL
print(f"[*] Fetching video sources from API: {{API_URL}}")
try:
    resp = session.get(API_URL, timeout=30)
    resp.raise_for_status()
    raw_text = resp.text
    print(f"[*] API status: {{resp.status_code}}, content-type: {{resp.headers.get('content-type', 'unknown')}}")
    print(f"[*] API response preview: {{raw_text[:300]}}")
    try:
        data = resp.json()
    except Exception:
        import re as _re
        # Try to extract any JSON-like array or object from the response
        m = _re.search(r'(\\[.*?\\]|\\{{.*?\\}})', raw_text, _re.S)
        data = __import__("json").loads(m.group(1)) if m else {{}}
except Exception as e:
    print(f"[-] API call failed: {{e}} — falling back to yt-dlp on original URL")
    data = {{}}

# Step 3: Recursively extract all candidate video URLs from the response
VIDEO_KEYS = {{"url", "src", "stream", "hls", "manifest", "playback",
               "videoUrl", "streamUrl", "hlsUrl", "mp4Url", "source",
               "file", "path", "link", "uri", "dashUrl", "dash"}}

def _extract_video_urls(obj, depth=0):
    """Recursively pull all HTTP URLs from any JSON structure."""
    if depth > 6:
        return []
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in VIDEO_KEYS and isinstance(v, str) and v.startswith("http"):
                found.append((k, v))
            else:
                found.extend(_extract_video_urls(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_extract_video_urls(item, depth + 1))
    return found

candidates = _extract_video_urls(data)
print(f"[*] Found {{len(candidates)}} video URL candidate(s): {{[c[1][:60] for c in candidates[:5]]}}")

# Pick best quality: prefer non-.m3u8 first, then fall back to HLS
VIDEO_URL = None
hls_url   = None
for key, url in candidates:
    if ".m3u8" in url or ".mpd" in url:
        if not hls_url:
            hls_url = url
    else:
        VIDEO_URL = url
        break
if not VIDEO_URL:
    VIDEO_URL = hls_url   # fall back to HLS if no direct MP4

# Step 4: Download
if VIDEO_URL:
    print(f"[+] Using video URL: {{VIDEO_URL[:100]}}")
    if ".m3u8" in VIDEO_URL or ".mpd" in VIDEO_URL:
        print("[*] Stream URL detected — using yt-dlp...")
        ydl_opts = {{
            "outtmpl":          OUTPUT,
            "http_headers":     HEADERS,
            "playlist_items":   "1",
            "retries":          8,
            "fragment_retries": 15,
        }}
        if COOKIEFILE and os.path.exists(COOKIEFILE):
            ydl_opts["cookiefile"] = COOKIEFILE
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([VIDEO_URL])
        print(f"[+] Saved to: {{OUTPUT}}")
    else:
        from tqdm import tqdm
        r = session.get(VIDEO_URL, stream=True, timeout=120)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(OUTPUT, "wb") as f, tqdm(total=total, unit="B", unit_scale=True) as bar:
            for chunk in r.iter_content(8192):
                f.write(chunk)
                bar.update(len(chunk))
        print(f"[+] Saved to: {{OUTPUT}}")
else:
    print("[-] No video URL found in API response — trying yt-dlp on original URL")
    ydl_opts = {{
        "outtmpl":        OUTPUT,
        "http_headers":   HEADERS,
        "playlist_items": "1",
        "retries":        8,
    }}
    if COOKIEFILE and os.path.exists(COOKIEFILE):
        ydl_opts["cookiefile"] = COOKIEFILE
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(TARGET, download=True)
        if info:
            print(f"[+] Saved: {{ydl.prepare_filename(info)}}")
'''

TEMPLATE_BLOB_CAPTURE = _HEADER + '''
# ── Blob/MediaSource capture ──────────────────────────────────────────────────
# This site uses Blob URLs / MediaSource API to stream video.
# Option A: Browser console snippet (copy-paste into DevTools)
# Option B: Python fallback via yt-dlp

BROWSER_SNIPPET = """
// Run this in browser DevTools console while the video is playing:
const video = document.querySelector('video');
const stream = video.captureStream ? video.captureStream() : video.mozCaptureStream();
const recorder = new MediaRecorder(stream, {{ mimeType: 'video/webm;codecs=vp9' }});
const chunks = [];
recorder.ondataavailable = e => chunks.push(e.data);
recorder.onstop = () => {{
    const blob = new Blob(chunks, {{ type: 'video/webm' }});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'video.webm';
    a.click();
    console.log('[+] Download triggered!');
}};
recorder.start();
console.log('[*] Recording started — press stop after video ends');
// To stop: recorder.stop()
"""

print("[!] Blob/MediaSource stream detected.")
print("[*] Option A — Paste this into browser DevTools console:")
print(BROWSER_SNIPPET)

import yt_dlp

TARGET = "{target_url}"
OUTPUT = os.path.join("{download_dir}", "{output_filename}")

print("[*] Option B — Trying yt-dlp fallback on original URL...")
try:
    with yt_dlp.YoutubeDL({{"outtmpl": OUTPUT, "noplaylist": True}}) as ydl:
        ydl.download([TARGET])
    print(f"[+] yt-dlp succeeded! Saved to: {{OUTPUT}}")
except Exception as e:
    print(f"[-] yt-dlp fallback failed: {{e}}")
    print("[!] Use the browser snippet above to capture manually.")
'''

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ydl_format(quality: str, output_format: str) -> str:
    if output_format == "mp3":
        return "bestaudio/best"
    mapping = {
        "best":  "bestvideo+bestaudio/best",
        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    }
    return mapping.get(quality, "bestvideo+bestaudio/best")


def _postprocessors(output_format: str) -> str:
    if output_format == "mp3":
        return str([{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}])
    return "[]"


def _safe_filename(url: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", url)[:50]
    return slug


def _build_ffmpeg_headers(headers: dict) -> str:
    """Convert headers dict to Python repr for template."""
    return repr(headers)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API — DownloaderGenerator class
# ═══════════════════════════════════════════════════════════════════════════════

class DownloaderGenerator:
    """
    Agent 4 — Downloader Generator.
    Selects the right template, fills variables, validates syntax, saves file.
    """

    def _generate_custom_script(self, strategy: dict, common: dict) -> str:
        from core.llm_client import call_llm
        import json
        error_context = ""
        prev_logs = strategy.get("params", {}).get("previous_error_logs", "")
        if prev_logs:
            # Classify the error type to give the LLM targeted guidance
            error_type_hint = ""
            if "404" in prev_logs:
                error_type_hint = (
                    "\n## ERROR CLASSIFICATION: HTTP 404\n"
                    "The API returned empty body or a stale URL — wrong token/SALT was used.\n"
                    "Find the correct SALT in the live JS below and recompute the hash.\n"
                )
            elif "403" in prev_logs:
                error_type_hint = (
                    "\n## ERROR CLASSIFICATION: HTTP 403 Forbidden\n"
                    "The server rejected headers/cookies. Pass correct Referer + cookies.\n"
                )
            elif "ExtractorError" in prev_logs or "Unsupported URL" in prev_logs:
                error_type_hint = (
                    "\n## ERROR CLASSIFICATION: No extractor found\n"
                    "Use Playwright to intercept network requests and find the real video URL.\n"
                )

        # This is the key to being truly dynamic: we don't rely on cached/truncated
        # script content. We fetch the actual source files the site uses for token
        # generation and give the full raw JS to the LLM to reverse-engineer.
        domain_safe = _safe_filename(urlparse(common['target_url']).netloc)
        raw_bundle_path = os.path.join("output", "raw", f"{domain_safe}_raw.json")
        js_context = ""

        # Keywords that flag a JS file as potentially containing token/hash/auth logic
        _SUSPICIOUS_JS = re.compile(
            r"(md5|hash|token|fix|source|player|movies|ssl|auth|secret|crypto|encode|sign)",
            re.I
        )

        if os.path.exists(raw_bundle_path):
            try:
                import requests as _req_mod
                with open(raw_bundle_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)

                target_host = urlparse(common['target_url']).netloc

                # Collect suspicious external JS URLs from the same domain
                suspicious_urls = []
                for script in raw_data.get("scripts", []):
                    src = script.get("src") or ""
                    if src and target_host in src and _SUSPICIOUS_JS.search(src):
                        suspicious_urls.append(src)

                # Also scan network requests for same-domain JS
                for req in raw_data.get("network_requests", []):
                    url = req.get("url", "")
                    if target_host in url and url.endswith(".js") and _SUSPICIOUS_JS.search(url):
                        if url not in suspicious_urls:
                            suspicious_urls.append(url)

                # Also add any inline scripts with hash/token patterns
                inline_snippets = []
                for script in raw_data.get("scripts", []):
                    content = script.get("content") or ""
                    if content and _SUSPICIOUS_JS.search(content) and len(content) > 100:
                        inline_snippets.append(content[:3000])

                # Fetch the suspicious JS files live
                fetched_js = {}
                _headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                for js_url in suspicious_urls[:8]:
                    try:
                        r = _req_mod.get(js_url, headers=_headers, timeout=10)
                        if r.status_code == 200 and len(r.text) > 50:
                            fetched_js[js_url] = r.text[:6000]
                            logger.info(f"[CustomGen] Fetched JS: {js_url} ({len(r.text)} bytes)")
                    except Exception as fe:
                        logger.debug(f"[CustomGen] Failed to fetch {js_url}: {fe}")

                # Build context block
                js_context += f"\n## Live-Fetched JS Files from {target_host}:\n"
                for js_url, js_code in fetched_js.items():
                    js_context += f"\n### {js_url}\n```javascript\n{js_code}\n```\n"

                if inline_snippets:
                    js_context += "\n## Inline Scripts with token/hash patterns:\n"
                    for snip in inline_snippets[:3]:
                        js_context += f"```javascript\n{snip}\n```\n"

                # Relevant network calls
                api_reqs = [r for r in raw_data.get("network_requests", [])
                            if "?" in r.get("url", "") or "ajax" in r.get("url", "")]
                js_context += "\n## Relevant Network API Calls (from Playwright session):\n"
                for r in api_reqs[:12]:
                    js_context += f"- {r.get('method','GET')} {r.get('url','')} (Status: {r.get('response_status')})\n"

            except Exception as e:
                logger.warning(f"[CustomGen] Failed to build JS context: {e}")

        prompt = f"""
You are an expert Python developer and cybersecurity reverse-engineer.
This is an AI self-healing fallback mechanism because the standard extraction failed.
{error_context}

## Site Reverse Engineering Data
The following JS files were fetched LIVE from {common['target_url']} right now.
Search them for the token generation algorithm (SALT strings, MD5/SHA hash inputs, magic cookie patterns).
Replicate the algorithm exactly in Python to forge a valid API token.
{js_context}

## Strategy Context:
{json.dumps(strategy, indent=2)}

## Variables you MUST use exactly as shown:
URL to download/inspect: {common['target_url']}
Output file path: {common['download_dir']}/{common['output_filename']}
Headers dict: {common['headers']}
Cookie file: {common.get('cookiefile_repr', 'None')}

## Rules:
1. Return ONLY valid, executable Python 3 code. No markdown formatting (no ```python).
2. Start the script with `#!/usr/bin/env python3`.
3. REVERSE ENGINEER the token algorithm from the live JS above — extract the SALT, determine the hash input formula, forge the cookie. Do NOT hardcode values from other sites.
4. If the JS is too obfuscated to decode, use `playwright.sync_api` to intercept the real network request the browser makes (with correct cookies + hash) and replay it.
5. ffmpeg and Playwright Chromium are already installed.
6. YOU MUST PRINT `[+] Saved to: <output_path>` when the file is successfully downloaded.

Write the complete Python script now:
"""

        response = call_llm(prompt)
        if response:
            code = response.strip()
            if code.startswith("```python"): code = code[9:]
            elif code.startswith("```"): code = code[3:]
            if code.endswith("```"): code = code[:-3]
            code = code.strip()

            # Auto-extract bypass constants from LLM-generated code and save to registry
            try:
                import re as _re
                from urllib.parse import urlparse as _up
                _domain = _up(common.get("target_url", "")).netloc

                # Look for a SALT constant pattern: e.g. salt = "abc123..."  or SALT = "..."
                _salt_m = _re.search(r'(?:salt|SALT|_salt)\s*=\s*["\']([a-zA-Z0-9]{10,})["\']', code)
                # Look for an MD5 or sha256 hash pattern
                _algo_m = _re.search(r'hashlib\.(md5|sha1|sha256|sha512)\(', code)
                # Look for a magic/cookie string
                _magic_m = _re.search(r'(?:magic|_magic|MAGIC)\s*=\s*["\']([a-zA-Z0-9]{40,})["\']', code)
                # Look for cookie name formula (contains episode_id)
                _ep_regex_m = _re.search(r're\.search\(r["\']([^"\']+)["\'].*?(?:api_url|API_URL|url)', code, _re.I | _re.S)

                if _salt_m and _domain and _algo_m:
                    from core.bypass_registry import register_bypass
                    register_bypass(_domain, {
                        "type":             "md5_cookie_handshake",
                        "api_path_pattern": "get_sources",
                        "episode_id_regex": _ep_regex_m.group(1) if _ep_regex_m else r"([A-Z0-9]{8,})",
                        "hash_algorithm":   _algo_m.group(1),
                        "hash_input":       "{episode_id}{random_b}{salt}",
                        "salt":             _salt_m.group(1),
                        "random_b_len":     6,
                        "random_b_charset": "0123456789abcdefghijklmnopqrstuvwxyz",
                        "cookie_domain":    f".{_domain}",
                        "cookie_name_formula": ("magic[13:37] + episode_id + magic[40:64]"
                                                if _magic_m else "magic[:24] + episode_id"),
                        "magic_string":     _magic_m.group(1) if _magic_m else "",
                        "url_hash_slot":    2,
                        "added":            _time.strftime("%Y-%m-%d"),
                        "notes":            "Auto-extracted by LLM self-healing loop.",
                    })
                    logger.info(f"[DownloaderGen] Auto-registered bypass for {_domain}")
            except Exception as _reg_err:
                logger.debug(f"[DownloaderGen] Bypass auto-register skipped: {_reg_err}")

            return code
        else:
            return TEMPLATE_YTDLP_GENERIC.format(**common)


    def generate(self, strategy: dict, output_format: str = "mp4",
                 quality: str = "best") -> dict:
        import time as _time

        s_name   = strategy.get("strategy", "ytdlp_generic")
        params   = strategy.get("params", {})
        domain   = strategy.get("domain", "unknown")
        fmt      = output_format
        ydl_fmt  = _ydl_format(quality, fmt)
        ext      = "mp3" if fmt == "mp3" else "mp4"
        import hashlib
        url_hash = hashlib.md5(params.get("target_url", "").encode()).hexdigest()[:6]
        filename = f"{_safe_filename(domain)}_{url_hash}_video.{ext}"
        headers  = params.get("headers") or {}
        ts       = _time.strftime("%Y-%m-%d %H:%M:%S")

        target_url = params.get("target_url", "")
        # Clean chunking parameters from direct URLs so the full video downloads
        target_url = re.sub(r'&(?:bytestart|byteend|range|start|end)=[\d\-]+', '', target_url)
        target_url = re.sub(r'\?(?:bytestart|byteend|range|start|end)=[\d\-]+&', '?', target_url)
        target_url = re.sub(r'\?(?:bytestart|byteend|range|start|end)=[\d\-]+$', '', target_url)

        cookie_file = params.get("cookie_file")
        common = dict(
            strategy=s_name, domain=domain, timestamp=ts,
            download_dir=DOWNLOAD_DIR, output_filename=filename,
            headers=repr(headers), ydl_format=ydl_fmt,
            postprocessors=_postprocessors(fmt),
            target_url=target_url,
            cookiefile_repr=repr(cookie_file),
        )

        # ── Select template ───────────────────────────────────────────────────
        if s_name == "ytdlp_generic":
            script = TEMPLATE_YTDLP_GENERIC.format(**common)

        elif s_name == "m3u8_ffmpeg":
            script = TEMPLATE_M3U8_FFMPEG.format(
                **common,
                manifest_url=params.get("manifest_url", ""),
                ffmpeg_headers=repr(headers),
            )

        elif s_name == "m3u8_ytdlp":
            script = TEMPLATE_M3U8_YTDLP.format(
                **common,
                manifest_url=params.get("manifest_url", ""),
            )

        elif s_name == "mpd_ytdlp":
            script = TEMPLATE_MPD_YTDLP.format(
                **common,
                manifest_url=params.get("manifest_url", ""),
            )

        elif s_name == "direct_url":
            video_url = params.get("video_url", "")
            video_url = re.sub(r'&(?:bytestart|byteend|range|start|end)=[\d\-]+', '', video_url)
            video_url = re.sub(r'\?(?:bytestart|byteend|range|start|end)=[\d\-]+&', '?', video_url)
            video_url = re.sub(r'\?(?:bytestart|byteend|range|start|end)=[\d\-]+$', '', video_url)
            script = TEMPLATE_DIRECT_URL.format(
                **common,
                video_url=video_url,
            )

        elif s_name == "api_fetch":
            script = TEMPLATE_API_FETCH.format(
                **common,
                api_endpoint=params.get("api_endpoint", ""),
            )

        elif s_name == "blob_capture":
            script = TEMPLATE_BLOB_CAPTURE.format(**common)

        else:
            # custom or unknown → AI Fallback! Generate script using LLM
            logger.info("[DownloaderGen] Strategy is custom/unknown. Using LLM to dynamically generate Python script...")
            script = self._generate_custom_script(strategy, common)

        # ── Syntax validation ─────────────────────────────────────────────────
        syntax_ok = True
        syntax_err = None
        try:
            ast.parse(script)
        except SyntaxError as e:
            syntax_ok  = False
            syntax_err = str(e)
            logger.error(f"[DownloaderGen] Syntax error: {e}")

        # ── Save to disk ──────────────────────────────────────────────────────
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        script_name = f"{_safe_filename(domain)}_{s_name}_downloader.py"
        script_path = os.path.join(OUTPUT_DIR, script_name)

        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        logger.info(f"[DownloaderGen] Saved → {script_path} (valid={syntax_ok})")

        return {
            "script":       script,
            "script_path":  script_path,
            "syntax_ok":    syntax_ok,
            "syntax_error": syntax_err,
            "strategy":     s_name,
            "output_file":  os.path.join(DOWNLOAD_DIR, filename),
        }
