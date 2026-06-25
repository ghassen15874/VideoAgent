"""
Agent 2 — Reverse Engineering Agent
=====================================
Analyzes the raw data bundle from Agent 1 and builds a structured
Signal Map describing how the target site delivers video.

Pipeline:
  Step 1 — Network Pattern Detection
  Step 2 — JavaScript Static Analysis (+ entropy/obfuscation detection)
  Step 3 — API Endpoint Reconstruction
  Step 4 — Blob URL Tracing
"""

import json
import logging
import math
import re
from collections import defaultdict
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Network Pattern Detection
# ═══════════════════════════════════════════════════════════════════════════════

NETWORK_PATTERNS = {
    "m3u8":       re.compile(r"\.m3u8(\?|$|#|/)", re.I),
    "mpd":        re.compile(r"\.mpd(\?|$|#|/)", re.I),
    "mp4_api":    re.compile(r"(video|media|stream|content)[^?]*\.mp4", re.I),
    "ts_segment": re.compile(r"\.(ts|aac|vtt)(\?|$)", re.I),
    "hls_api":    re.compile(r"(hls|manifest|playlist)(\?|/|$)", re.I),
    "dash_api":   re.compile(r"(dash|segment|init\.mp4)", re.I),
    "token_api":  re.compile(r"(token|auth|sign|key|ticket)=", re.I),
    "cdn_url":    re.compile(r"(cdn\.|akamai|cloudfront|fastly|bunnycdn|b-cdn)", re.I),
    "blob_url":   re.compile(r"^blob:", re.I),
    # Expanded: also matches /ajax/get_sources, /embed/, /player/, /sources/, /stream/
    "api_video":  re.compile(
        r"(/api/.*?(video|stream|media|lesson|course|content)|"
        r"/ajax/(get_sources|sources|video|stream|player)|"
        r"/(embed|player|sources|stream|manifest|get_video|video_info|get_stream))",
        re.I
    ),
}

# Path keywords that strongly indicate a video-source API endpoint
# Used for heuristic HIGH-priority classification when no response body is available
_VIDEO_API_PATH_KEYWORDS = re.compile(
    r"/(get_sources?|sources?|get_video|video_url|stream_url|play_url|"
    r"embed|player_token|get_stream|get_manifest|get_link|get_url|"
    r"hls_url|dash_url|mp4_url|jwplayer|video_info|fetch_video|download_url)",
    re.I
)

# Response body keys that strongly suggest a video URL is inside
VIDEO_BODY_KEYS = {"url", "src", "stream", "manifest", "hls", "dash",
                   "playback", "source", "file", "path", "link", "uri",
                   "videoUrl", "streamUrl", "hlsUrl", "dashUrl", "mp4Url"}

# Known player library fingerprints
PLAYER_SIGNATURES = {
    "video.js":      re.compile(r"video\.?js|VideoJS|vjs-", re.I),
    "hls.js":        re.compile(r"Hls\.js|new Hls\(|hls\.loadSource", re.I),
    "dash.js":       re.compile(r"dashjs|MediaPlayerFactory|createPlayer", re.I),
    "jwplayer":      re.compile(r"jwplayer|jwplatform", re.I),
    "plyr":          re.compile(r"new Plyr|plyr\.io", re.I),
    "shaka":         re.compile(r"shaka\.Player|shaka-player", re.I),
    "flowplayer":    re.compile(r"flowplayer", re.I),
    "brightcove":    re.compile(r"brightcove|bcplayer|bc-player", re.I),
    "vimeo":         re.compile(r"player\.vimeo\.com|vimeo-player", re.I),
    "youtube":       re.compile(r"youtube\.com/embed|ytplayer|YT\.Player", re.I),
    "wistia":        re.compile(r"wistia\.com|wistia_embed", re.I),
    "native_html5":  re.compile(r"<video\s", re.I),
}

# Auth header patterns
AUTH_HEADER_PATTERNS = re.compile(
    r"^(authorization|x-auth|x-token|bearer|access-token|api-key)$", re.I
)


# Minimum size (bytes) for a file to be considered a real video, not a preview/ad clip.
# Preview clips are typically < 5 MB. Real videos are almost always > 5 MB.
_MIN_VIDEO_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

# Known video embed/iframe hosting services — their URLs should be tried with yt-dlp
EMBED_VIDEO_HOSTS = re.compile(
    r"(mydaddy\.cc|doodstream\.(co|com)|streamtape\.com|mixdrop\.(co|ag)|"
    r"voe\.sx|upstream\.to|filemoon\.(sx|to)|lulustream\.com|"
    r"streamhg\.com|streamlare\.com|wolfstream\.tv|embedgram\.com|"
    r"vidhide\.(com|net)|vidcloud\.(co|org)|streamsb\.net|sbplay\.(org|one))",
    re.I
)


def _probe_sizes(urls: list, timeout: int = 6) -> list:
    """
    HEAD-request each URL to get Content-Length.
    Returns the list sorted largest→smallest.
    URLs that fail or return no Content-Length go to the end.
    Skips URLs where size < _MIN_VIDEO_SIZE_BYTES.
    """
    try:
        import requests as _req
    except ImportError:
        return urls

    _UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    sized = []
    for url in urls:
        try:
            r = _req.head(url, headers=_UA, timeout=timeout, allow_redirects=True)
            cl = int(r.headers.get("content-length", 0))
            sized.append((url, cl))
            logger.debug(f"[SizeProbe] {url[:70]} → {cl//1024}KB")
        except Exception:
            sized.append((url, 0))
    # Sort descending by size; filter out tiny files (previews/ads)
    sized.sort(key=lambda x: x[1], reverse=True)
    result = [u for u, sz in sized if sz >= _MIN_VIDEO_SIZE_BYTES or sz == 0]
    if not result:
        # If everything is tiny, return all (better than nothing)
        result = [u for u, _ in sized]
    return result


def _step1_network_patterns(requests: list) -> dict:
    """Scan all network requests for video delivery signals."""
    results = {k: [] for k in NETWORK_PATTERNS}
    auth_headers_found = {}
    high_priority_requests = []

    for req in requests:
        url  = req.get("url", "")
        rtype = req.get("type", "")
        headers = req.get("headers", {})
        resp_headers = req.get("response_headers", {})
        resp_body = req.get("response_body", "") or ""

        # Match URL patterns
        for name, pattern in NETWORK_PATTERNS.items():
            if pattern.search(url):
                results[name].append(url)

        # ALSO match by resource type reported by Playwright (e.g. rtype='mpd', 'hls')
        # This catches manifests like Reddit's DASH that don't have .mpd in the URL path
        if rtype == "mpd" and url not in results["mpd"]:
            results["mpd"].append(url)
        if rtype in ("m3u8", "hls") and url not in results["m3u8"]:
            results["m3u8"].append(url)
        if rtype == "mp4" and url not in results.get("mp4_api", []):
            results.setdefault("mp4_api", []).append(url)

        # Detect auth headers in requests
        for hname, hval in headers.items():
            if AUTH_HEADER_PATTERNS.match(hname):
                auth_headers_found[hname] = hval

        # API endpoint reconstruction — check response body for video keys
        ct = resp_headers.get("content-type", "").lower()
        if resp_body and ("json" in ct or "javascript" in ct or resp_body.strip().startswith("{") or resp_body.strip().startswith("[")):
            # Try to parse it, even if it's buried in text/html
            try:
                # Basic cleanup if it's embedded in some wrapper
                raw = resp_body
                import re as _re
                m = _re.search(r'(\[.*\]|\{.*\})', raw, _re.S)
                if m:
                    body_json = json.loads(m.group(1))
                    found_keys = _find_video_keys(body_json)
                    if found_keys:
                        high_priority_requests.append({
                            "url":        url,
                            "type":       rtype,
                            "video_keys": found_keys,
                        })
                        # NEW: Extract the actual URLs found inside the API response
                        for k, v_url in found_keys.items():
                            if ".m3u8" in v_url and v_url not in results["m3u8"]:
                                results["m3u8"].append(v_url)
                            elif ".mpd" in v_url and v_url not in results["mpd"]:
                                results["mpd"].append(v_url)
                            elif ".mp4" in v_url and v_url not in results["mp4_api"]:
                                results["mp4_api"].append(v_url)
            except (json.JSONDecodeError, TypeError, ImportError):
                pass

    return {
        "matched_urls":           {k: v[:5] for k, v in results.items() if v},
        "auth_headers":           auth_headers_found,
        "high_priority_requests": high_priority_requests[:10],
    }


def _find_video_keys(obj, depth=0) -> dict:
    """Recursively find keys in JSON that likely contain video URLs."""
    if depth > 5:
        return {}
    found = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in VIDEO_BODY_KEYS and isinstance(v, str) and v.startswith("http"):
                found[k] = v
            else:
                found.update(_find_video_keys(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj[:5]:
            found.update(_find_video_keys(item, depth + 1))
    return found


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — JavaScript Static Analysis
# ═══════════════════════════════════════════════════════════════════════════════

JS_PATTERNS = {
    "fetch_call":        re.compile(r'fetch\(\s*["\']([^"\']+)["\']', re.I),
    "axios_call":        re.compile(r'axios\.(get|post|put)\(\s*["\']([^"\']+)["\']', re.I),
    "jquery_ajax":       re.compile(r'\$\.ajax\(\s*\{[^}]*url\s*:\s*["\']([^"\']+)["\']', re.I | re.S),
    "xhr_open":          re.compile(r'\.open\(\s*["\'][A-Z]+["\']\s*,\s*["\']([^"\']+)["\']', re.I),
    "video_src_assign":  re.compile(r'(?:video|player|media)\.src\s*=\s*["\']([^"\']+)["\']', re.I),
    "player_src_call":   re.compile(r'\.src\(\s*\{[^}]*src\s*:\s*["\']([^"\']+)["\']', re.I | re.S),
    "m3u8_literal":      re.compile(r'["\']([^"\']*\.m3u8[^"\']*)["\']', re.I),
    "mpd_literal":       re.compile(r'["\']([^"\']*\.mpd[^"\']*)["\']', re.I),
    "blob_usage":        re.compile(r'URL\.createObjectURL|new\s+MediaSource|addSourceBuffer', re.I),
    "hls_loader":        re.compile(r'new\s+Hls\(\)|hls\.loadSource\(', re.I),
    "source_buffer":     re.compile(r'addSourceBuffer\s*\(\s*["\']([^"\']+)["\']', re.I),
}

# Entropy threshold for obfuscation detection
OBFUSCATION_ENTROPY_THRESHOLD = 4.8
# Minimum length for entropy check (short strings can have high entropy naturally)
OBFUSCATION_MIN_LENGTH = 200


def _shannon_entropy(text: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not text:
        return 0.0
    freq = defaultdict(int)
    for ch in text:
        freq[ch] += 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _is_obfuscated(script_content: str) -> bool:
    """
    Detect obfuscation using:
    1. Shannon entropy > threshold
    2. High ratio of special chars (;,=,{,},|,!)
    3. Very long variable-less strings
    """
    if len(script_content) < OBFUSCATION_MIN_LENGTH:
        return False

    entropy = _shannon_entropy(script_content)
    if entropy > OBFUSCATION_ENTROPY_THRESHOLD:
        return True

    # Check special character density
    special = sum(1 for c in script_content if c in r";={|}!^~")
    ratio = special / max(len(script_content), 1)
    if ratio > 0.15:
        return True

    # Check for eval / atob patterns
    if re.search(r'\beval\s*\(|\batob\s*\(|String\.fromCharCode', script_content):
        return True

    return False


def _step2_js_analysis(scripts: list) -> dict:
    """Scan all script tags for video delivery patterns."""
    all_urls_found    = defaultdict(list)
    obfuscated_count  = 0
    player_lib        = None
    blob_usage        = False
    hls_loader        = False

    # Combine all script content for player detection
    full_text = ""

    for script in scripts:
        content = script.get("content") or ""
        src     = script.get("src") or ""
        full_text += content + " "

        if not content:
            continue

        is_obf = _is_obfuscated(content)
        if is_obf:
            obfuscated_count += 1
            continue   # Can't reliably parse obfuscated code with regex

        # Pattern matching
        for pname, pattern in JS_PATTERNS.items():
            matches = pattern.findall(content)
            if matches:
                # flatten tuples from groups
                flat = [m if isinstance(m, str) else m[-1] for m in matches]
                all_urls_found[pname].extend(flat[:5])

        if JS_PATTERNS["blob_usage"].search(content):
            blob_usage = True
        if JS_PATTERNS["hls_loader"].search(content):
            hls_loader = True

    # Detect player library
    for lib_name, sig in PLAYER_SIGNATURES.items():
        if sig.search(full_text):
            player_lib = lib_name
            break

    return {
        "urls_found":        {k: list(set(v))[:5] for k, v in all_urls_found.items() if v},
        "obfuscated_scripts": obfuscated_count,
        "blob_usage_in_js":  blob_usage,
        "hls_loader_found":  hls_loader,
        "player_library":    player_lib,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — API Endpoint Reconstruction
# ═══════════════════════════════════════════════════════════════════════════════

def _step3_api_reconstruction(requests: list, base_url: str) -> dict:
    """
    For each XHR/Fetch request, extract URL template and check
    if response body contains video-related keys.
    Also uses URL path heuristics to identify high-priority source APIs
    even when the response body was not captured.
    """
    endpoints = []
    seen = set()
    base_domain = urlparse(base_url).netloc
    same_domain_apis = []  # first-party XHR/fetch calls only

    for req in requests:
        rtype = req.get("type", "")
        if rtype not in ("xhr", "fetch"):
            continue

        url         = req.get("url", "")
        method      = req.get("method", "GET")
        resp_body   = req.get("response_body", "") or ""
        resp_status = req.get("response_status")
        resp_headers = req.get("response_headers", {}) or {}

        if not url or url in seen:
            continue
        seen.add(url)

        # Build URL template (replace IDs with {id})
        parsed   = urlparse(url)
        template = re.sub(r'/\d+', '/{id}', parsed.path)
        template = re.sub(r'[a-f0-9]{8,}', '{hash}', template)

        entry = {
            "url":      url,
            "template": parsed.scheme + "://" + parsed.netloc + template,
            "method":   method,
            "status":   resp_status,
            "priority": "normal",
            "video_keys_in_response": {},
            "same_domain": parsed.netloc == base_domain,
        }

        ct = resp_headers.get("content-type", "").lower()

        # ── Priority bump 1: response body contains video URL keys ───────────
        if resp_body and ("json" in ct or resp_body.strip().startswith("{")):
            try:
                body_json = json.loads(resp_body)
                vkeys = _find_video_keys(body_json)
                if vkeys:
                    entry["video_keys_in_response"] = vkeys
                    entry["priority"] = "HIGH"
            except Exception:
                pass

        # ── Priority bump 2: binary video content-type in response ───────────
        if any(vt in ct for vt in ["video/", "audio/", "application/x-mpegurl",
                                    "application/dash+xml"]):
            entry["priority"] = "HIGH"
            entry["content_type"] = ct

        # ── Priority bump 3: path heuristic (no response body needed) ────────
        # Many sites serve the get_sources endpoint with text/html or no Content-Type,
        # so the body never gets parsed. We promote based on path keywords instead.
        if entry["priority"] != "HIGH" and _VIDEO_API_PATH_KEYWORDS.search(parsed.path):
            entry["priority"] = "HIGH"
            entry["heuristic_reason"] = "path_keyword_match"

        # ── Priority bump 4: same-domain XHR that returned 200 ──────────────
        # First-party calls that aren't analytics/tracking are worth investigating
        _TRACKING_SKIP = re.compile(
            r"(google-analytics|googletagmanager|facebook\.net|yandex\.ru"
            r"|doubleclick|tsyndicate|hotjar|sentry|cdn-cgi/rum|clarity\.ms"
            r"|segment\.io|mixpanel|amplitude)",
            re.I
        )
        if (parsed.netloc == base_domain
                and resp_status == 200
                and not _TRACKING_SKIP.search(url)):
            same_domain_apis.append(url)
            # Boost: same-domain 200 XHR that we haven't already flagged HIGH
            if entry["priority"] != "HIGH":
                entry["priority"] = "medium"

        endpoints.append(entry)

    # Sort: HIGH → medium → normal
    priority_order = {"HIGH": 0, "medium": 1, "normal": 2}
    endpoints.sort(key=lambda e: priority_order.get(e["priority"], 2))
    return {
        "api_endpoints":    endpoints[:20],
        "same_domain_apis": same_domain_apis[:15],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Blob URL Tracing
# ═══════════════════════════════════════════════════════════════════════════════

SEGMENT_PATTERNS = re.compile(
    r"(chunk[_-]\d+|seg[_-]\d+|segment\d+|frag\d+|part\d+|"
    r"\d+\.ts|\d+\.aac|init\.mp4|init\d*\.m4s|media\d*\.m4s)",
    re.I
)


def _step4_blob_tracing(blob_urls: list, requests: list) -> dict:
    """
    If blob URLs detected, find:
    - Segment fetch patterns (chunk_0.ts, seg-1-v1.ts)
    - Initialization segments (init.mp4, init.m4s)
    - MediaSource MIME types
    """
    if not blob_urls:
        return {"blob_detected": False}

    segments     = []
    init_segments = []
    mime_types   = set()

    # Scan blob_urls for type info
    for b in blob_urls:
        btype = b.get("type", "") if isinstance(b, dict) else ""
        if btype:
            mime_types.add(btype)

    # Scan all network requests for segment patterns
    for req in requests:
        url = req.get("url", "")
        if SEGMENT_PATTERNS.search(url):
            if "init" in url.lower():
                init_segments.append(url)
            else:
                segments.append(url)

    # Reconstruct segment URL template
    segment_template = None
    if segments:
        sample = segments[0]
        segment_template = re.sub(r'\d+(?=\.(ts|aac|m4s|mp4))', '{N}', sample)

    return {
        "blob_detected":     True,
        "blob_count":        len(blob_urls),
        "mime_types":        list(mime_types),
        "segment_urls":      segments[:10],
        "init_segments":     init_segments[:5],
        "segment_template":  segment_template,
        "strategy_hint":     "blob_capture" if not segments else "m3u8_ffmpeg",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Confidence Scorer
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_confidence(signal_map: dict) -> float:
    """Score from 0.0 to 1.0 based on how many actionable signals were found."""
    score = 0.0
    sigs  = signal_map.get("signals", {})

    if sigs.get("m3u8_found"):       score += 0.35
    if sigs.get("mpd_found"):        score += 0.35
    if sigs.get("direct_video_tag"): score += 0.30
    if sigs.get("api_video_endpoint"):score += 0.25
    if sigs.get("blob_detected"):    score += 0.15
    if sigs.get("player_library"):   score += 0.05
    if sigs.get("auth_required"):    score -= 0.05   # auth makes it harder

    return round(min(max(score, 0.05), 1.0), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API — ReverseEngineer class
# ═══════════════════════════════════════════════════════════════════════════════

class ReverseEngineer:
    """
    Agent 2 — Reverse Engineering Agent.
    Takes a Raw Data Bundle and returns a Signal Map.
    """

    def analyze(self, raw_bundle: dict) -> dict:
        url      = raw_bundle.get("url", "")
        domain   = urlparse(url).netloc
        requests = raw_bundle.get("network_requests", [])
        scripts  = raw_bundle.get("scripts", [])
        video_tags = raw_bundle.get("video_tags", [])
        blob_urls  = raw_bundle.get("blob_urls", [])
        intercepted_api_responses = raw_bundle.get("intercepted_api_responses", {})

        logger.info(f"[ReverseEngineer] Analyzing {domain} — "
                    f"{len(requests)} requests, {len(scripts)} scripts, "
                    f"{len(intercepted_api_responses)} intercepted API responses")

        # ── Priority 0: Extract video URLs from intercepted browser API calls ─
        # The Playwright route interceptor captured the browser's own ajax requests
        # (which use the correct token generated by the site's JS).
        # If ANY of those responses contain a real video URL, use it directly.
        _URL_RE = re.compile(r'https?://[^\s\'"<>]+\.(?:m3u8|mp4|ts|mpd)[^\s\'"<>]*', re.I)
        intercepted_video_urls = []
        for api_url, body in intercepted_api_responses.items():
            found = _URL_RE.findall(body)
            if found:
                logger.info(f"[ReverseEngineer] ✓ Intercepted video URL from {api_url[:60]}: {found[0][:80]}")
                intercepted_video_urls.extend(found)
            else:
                # Also try JSON parsing
                try:
                    data = json.loads(body)
                    for val in (json.dumps(data),):
                        found = _URL_RE.findall(val)
                        intercepted_video_urls.extend(found)
                except Exception:
                    pass

        # ── Run all 4 steps ──────────────────────────────────────────────────
        net   = _step1_network_patterns(requests)
        js    = _step2_js_analysis(scripts)
        api   = _step3_api_reconstruction(requests, url)
        blobs = _step4_blob_tracing(blob_urls, requests)


        # ── Collect all m3u8 / mpd URLs from multiple sources ────────────────
        # Also extract from intercepted API responses — these use the browser's live token
        _M3U8_RE = re.compile(r'https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*', re.I)
        _MPD_RE  = re.compile(r'https?://[^\s\'"<>]+\.mpd[^\s\'"<>]*', re.I)
        intercepted_m3u8 = []
        intercepted_mpd  = []
        for api_url, body in intercepted_api_responses.items():
            intercepted_m3u8.extend(_M3U8_RE.findall(body))
            intercepted_mpd.extend(_MPD_RE.findall(body))

        m3u8_urls = (
            intercepted_m3u8 +
            net["matched_urls"].get("m3u8", []) +
            js["urls_found"].get("m3u8_literal", [])
        )
        mpd_urls = (
            intercepted_mpd +
            net["matched_urls"].get("mpd", []) +
            js["urls_found"].get("mpd_literal", [])
        )

        # ── Direct video tag check ────────────────────────────────────────────
        # Known ad/promo CDN hostnames to de-prioritize
        _AD_HOSTS = re.compile(
            r"(pemsrv\.com|efryqqbee\.com|tsyndicate|doubleclick|googlesyndication"
            r"|adnxs\.com|rubiconproject|adsrvr\.org|securepubads|2mdn\.net"
            r"|ima\.googleapis|pubmatic|openx\.net"
            r"|adtng\.com|a\.adtng\.com|ht-cdn.*\.adtng\.com"  # PornHub ad network
            r"|phncdn\.com.*\/plain\/)"  # PornHub thumbnail previews (not real videos)
            ,
            re.I
        )

        raw_tag_srcs = [
            t.get("src") or t.get("currentSrc")
            for t in video_tags
            if t.get("src") or t.get("currentSrc")
        ]
        # Also include media-type network requests (these are often the REAL video)
        media_reqs = [
            r.get("url", "") for r in requests
            if r.get("type") in ("media", "video") and r.get("url", "").startswith("http")
        ]
        # Combine: intercepted (highest confidence) > media requests > video tag srcs
        all_srcs = intercepted_video_urls + media_reqs + raw_tag_srcs + net["matched_urls"].get("mp4_api", [])
        # Filter out ad CDNs, blobs, and deduplicate
        raw_video_srcs = list(dict.fromkeys(
            u for u in all_srcs
            if u and not _AD_HOSTS.search(u) and not u.startswith("blob:")
        ))

        # Size-probe all MP4 candidates: sort largest first, discard < 5MB (preview/ad clips)
        if not intercepted_video_urls and raw_video_srcs:
            logger.info(f"[ReverseEngineer] Probing {len(raw_video_srcs)} MP4 URLs for file size...")
            direct_video_srcs = _probe_sizes(raw_video_srcs)
        else:
            direct_video_srcs = raw_video_srcs

        # Detect embed/iframe video hosts in network requests
        embed_video_urls = []
        _embed_url_re = re.compile(r'https?://[^\s<>]+', re.I)
        for req in requests:
            url_r = req.get("url", "")
            body  = req.get("response_body") or ""
            if EMBED_VIDEO_HOSTS.search(url_r):
                embed_video_urls.append(url_r)
            if body:
                for candidate in _embed_url_re.findall(body):
                    if EMBED_VIDEO_HOSTS.search(candidate):
                        embed_video_urls.append(candidate)
            if "altplayer" in url_r or ("player" in url_r and "?" in url_r):
                from urllib.parse import parse_qs, urlparse as _up2
                qs = parse_qs(_up2(url_r).query)
                for val in qs.get("i", []):
                    clean = val.lstrip("/")
                    if not clean.startswith("http"):
                        clean = "https://" + clean
                    if EMBED_VIDEO_HOSTS.search(clean):
                        embed_video_urls.append(clean)
        embed_video_urls = list(dict.fromkeys(embed_video_urls))
        if embed_video_urls:
            logger.info(f"[ReverseEngineer] Found {len(embed_video_urls)} embed video URL(s)")


        # ── API video endpoint (best candidate) ──────────────────────────────
        api_video_endpoint = None
        api_high_priority  = [ep for ep in api["api_endpoints"] if ep["priority"] == "HIGH"]
        for ep in api_high_priority:
            api_video_endpoint = ep["url"]
            break

        # ── Auth detection ────────────────────────────────────────────────────
        auth_required = bool(net["auth_headers"])
        auth_headers  = net["auth_headers"]

        # ── Build final signal map ────────────────────────────────────────────
        signal_map = {
            "domain": domain,
            "signals": {
                "direct_video_tag":  bool(direct_video_srcs),
                "direct_video_srcs": direct_video_srcs[:3],

                # Embed/iframe video hosts detected (mydaddy.cc, doodstream, etc.)
                # strategy_builder should use yt-dlp on these if direct_video_srcs is empty/tiny
                "embed_video_urls":   embed_video_urls[:5],
                "embed_found":        bool(embed_video_urls),

                # Set when Playwright route-interceptor captured the browser's own API call
                # with the correct token — highest confidence, use direct_url strategy
                "intercepted_api_success": bool(intercepted_video_urls),
                "intercepted_video_urls":  intercepted_video_urls[:3],

                "m3u8_found":  bool(m3u8_urls),
                "m3u8_urls":   list(set(m3u8_urls))[:5],

                "mpd_found":   bool(mpd_urls),
                "mpd_urls":    list(set(mpd_urls))[:5],

                "blob_detected": blobs.get("blob_detected", False),
                "blob_info":     blobs,

                "api_video_endpoint":  api_video_endpoint,
                "high_priority_apis":  api_high_priority[:5],

                # All same-domain XHR/fetch calls that returned 200 (for LLM reasoning)
                "same_domain_apis":    api.get("same_domain_apis", [])[:10],

                "auth_required":   auth_required,
                "auth_headers":    auth_headers,

                "js_obfuscated":       js["obfuscated_scripts"] > 0,
                "obfuscated_count":    js["obfuscated_scripts"],
                "player_library":      js["player_library"],
                "hls_loader_in_js":    js["hls_loader_found"],

                "ts_segments_found":   bool(net["matched_urls"].get("ts_segment")),
                "cdn_detected":        bool(net["matched_urls"].get("cdn_url")),
                "cdn_urls":            net["matched_urls"].get("cdn_url", [])[:3],

                "fetch_urls_in_js":    js["urls_found"].get("fetch_call", [])[:5],
                "xhr_urls_in_js":      js["urls_found"].get("xhr_open", [])[:5],
            },
            "network_summary": {
                "total_requests":         len(requests),
                "high_priority_requests": net["high_priority_requests"],
                "all_api_endpoints":      api["api_endpoints"][:10],
            },
            "confidence": 0.0,   # filled below
        }

        signal_map["confidence"] = _compute_confidence(signal_map)

        logger.info(
            f"[ReverseEngineer] Done — "
            f"m3u8={signal_map['signals']['m3u8_found']} "
            f"mpd={signal_map['signals']['mpd_found']} "
            f"blob={signal_map['signals']['blob_detected']} "
            f"player={signal_map['signals']['player_library']} "
            f"confidence={signal_map['confidence']}"
        )
        return signal_map
