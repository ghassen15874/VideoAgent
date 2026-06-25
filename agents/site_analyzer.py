"""
Agent 1 — Site Analyzer
=======================
Opens the target URL using a real browser (Playwright primary,
Selenium/undetected_chromedriver fallback) and collects every
observable artifact before any analysis begins.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JavaScript injected BEFORE page load — hooks Blob URL creation
# ---------------------------------------------------------------------------
BLOB_HOOK_SCRIPT = """
(function () {
    const _orig = URL.createObjectURL.bind(URL);
    window.__capturedBlobURLs = window.__capturedBlobURLs || [];
    URL.createObjectURL = function (obj) {
        const url = _orig(obj);
        window.__capturedBlobURLs.push({
            url: url,
            type: obj instanceof Blob ? obj.type : 'MediaSource',
            size: obj instanceof Blob ? obj.size : null,
            timestamp: Date.now()
        });
        return url;
    };

    // Also hook XHR to reliably capture API response bodies
    // (Bypasses Playwright's "No resource with given identifier" bug)
    window.__capturedXHR = window.__capturedXHR || [];
    const _open = XMLHttpRequest.prototype.open;
    const _send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this._reqUrl = url;
        return _open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        this.addEventListener('load', function() {
            try {
                if (this.responseType === '' || this.responseType === 'text') {
                    window.__capturedXHR.push({
                        url: this._reqUrl,
                        body: this.responseText
                    });
                }
            } catch(e) {}
        });
        return _send.apply(this, arguments);
    };
    
    // And hook fetch
    const _fetch = window.fetch;
    window.fetch = async function() {
        const res = await _fetch.apply(this, arguments);
        const clone = res.clone();
        clone.text().then(text => {
            window.__capturedXHR.push({
                url: typeof arguments[0] === 'string' ? arguments[0] : (arguments[0] && arguments[0].url),
                body: text
            });
        }).catch(e => {});
        return res;
    };
})();
"""

# Media-related MIME types we always want to capture fully
MEDIA_CONTENT_TYPES = {
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "application/dash+xml",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "audio/mpeg",
    "audio/mp4",
    "text/plain",          # some m3u8 served as text/plain
}


# ---------------------------------------------------------------------------
# Helper — synchronous Selenium fallback
# ---------------------------------------------------------------------------
def _selenium_analyze(url: str, cookies_path: Optional[str]) -> dict:
    """Fallback analyzer using Selenium + undetected_chromedriver."""
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        raise RuntimeError(
            "undetected_chromedriver not installed. Run: pip install undetected-chromedriver"
        )

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")

    driver = uc.Chrome(options=options, version_main=None)
    try:
        if cookies_path and os.path.exists(cookies_path):
            driver.get(urlparse(url).scheme + "://" + urlparse(url).netloc)
            with open(cookies_path) as f:
                for cookie in json.load(f):
                    try:
                        driver.add_cookie(cookie)
                    except Exception:
                        pass

        driver.get(url)
        # Wait for page to settle
        time.sleep(5)

        html = driver.page_source
        dom  = driver.execute_script("return document.documentElement.outerHTML;")

        video_tags = driver.execute_script("""
            const tags = [];
            document.querySelectorAll('video, audio').forEach(el => {
                tags.push({
                    tag:    el.tagName,
                    src:    el.src || null,
                    poster: el.poster || null,
                    sources: Array.from(el.querySelectorAll('source')).map(s => ({
                        src: s.src, type: s.type
                    }))
                });
            });
            return tags;
        """)

        local_storage = driver.execute_script("""
            const s = {};
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                s[k] = localStorage.getItem(k);
            }
            return s;
        """)

        cookies = driver.get_cookies()

        scripts = driver.execute_script("""
            return Array.from(document.querySelectorAll('script')).map(s => ({
                src:     s.src || null,
                content: s.src ? null : s.textContent.substring(0, 2000)
            }));
        """)

        return {
            "analyzer": "selenium",
            "url": url,
            "html": html[:50000],
            "dom": dom[:50000],
            "network_requests": [],          # Selenium cannot intercept natively
            "console_logs": [],
            "scripts": scripts,
            "video_tags": video_tags,
            "blob_urls": [],
            "cookies": {c["name"]: c["value"] for c in cookies},
            "local_storage": local_storage,
            "service_workers": [],
            "error": None,
        }
    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Primary async Playwright analyzer
# ---------------------------------------------------------------------------
async def _playwright_analyze(url: str, cookies_path: Optional[str]) -> dict:
    """Primary analyzer using Playwright with full network interception."""
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    network_requests = []
    console_logs     = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            java_script_enabled=True,
            ignore_https_errors=True,
        )

        # Load cookies from file if provided
        if cookies_path and os.path.exists(cookies_path):
            with open(cookies_path) as f:
                raw = json.load(f)
            await context.add_cookies(raw)

        # Inject blob-capture hook before ANY script runs
        await context.add_init_script(BLOB_HOOK_SCRIPT)

        page = await context.new_page()

        # ------------------------------------------------------------------
        # Network interception — capture ALL requests & responses
        # ------------------------------------------------------------------
        async def on_request(request):
            entry = {
                "url":     request.url,
                "method":  request.method,
                "type":    request.resource_type,
                "headers": dict(request.headers),
                "post_data": None,
                "response_body": None,
                "response_status": None,
                "response_headers": {},
            }
            if request.method in ("POST", "PUT", "PATCH"):
                try:
                    entry["post_data"] = request.post_data
                except Exception:
                    pass
            network_requests.append(entry)

        async def on_response(response):
            ct = response.headers.get("content-type", "").lower().split(";")[0].strip()
            # Find the matching request entry and update it
            for entry in reversed(network_requests):
                if entry["url"] == response.url:
                    entry["response_status"]  = response.status
                    entry["response_headers"] = dict(response.headers)
                    # Only read body for relevant content types or if it's an API request
                    is_api = entry["type"] in ("xhr", "fetch")
                    if is_api or ct in MEDIA_CONTENT_TYPES or "json" in ct or "m3u8" in ct or "mpd" in ct:
                        try:
                            body = await response.body()
                            entry["response_body"] = body.decode("utf-8", errors="replace")[:5000]
                        except Exception:
                            pass
                    break

        page.on("request",  on_request)
        page.on("response", on_response)

        # ------------------------------------------------------------------
        # Route interceptor — sniff AJAX API responses with the browser's
        # own valid tokens (e.g. get_sources). We let the request through
        # unchanged but read the response body before it goes to page JS.
        # This is the only reliable way to capture responses where the site's
        # JS generates the correct token (MD5/hash) itself.
        # ------------------------------------------------------------------
        intercepted_api_responses = {}

        async def _route_handler(route):
            url_r = route.request.url
            try:
                response = await route.fetch()
                try:
                    body_bytes = await response.body()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    # Only store non-empty, non-whitespace responses
                    if body_text.strip():
                        intercepted_api_responses[url_r] = body_text
                        logger.info(f"[SiteAnalyzer] Intercepted API response: {url_r[:80]} ({len(body_text)} bytes)")
                        # Also update the matching network_request entry
                        for entry in reversed(network_requests):
                            if entry["url"] == url_r and not entry.get("response_body"):
                                entry["response_body"] = body_text[:5000]
                                break
                except Exception:
                    pass
                await route.fulfill(response=response)
            except Exception as re_err:
                logger.debug(f"[SiteAnalyzer] Route intercept error for {url_r}: {re_err}")
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/ajax/**", _route_handler)
        await page.route("**/api/**", _route_handler)


        # ------------------------------------------------------------------
        # Console logs
        # ------------------------------------------------------------------
        page.on("console", lambda msg: console_logs.append({
            "type": msg.type,
            "text": msg.text,
        }))

        # ── Two-stage navigation ─────────────────────────────────────────────
        # Stage 1: load DOM fast (domcontentloaded ~3-5s)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except PWTimeout:
            logger.warning("domcontentloaded timeout — collecting partial data")
        except Exception as e:
            logger.warning(f"goto error: {e} — continuing")

        # Stage 2: wait for XHR/Fetch to settle (max 10s extra)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass   # Not fatal — we still collect what we have

        # Stage 3: Auto-Clicker for Modals and Play Buttons (18+, Accept Cookies, Play Video)
        try:
            logger.info("[SiteAnalyzer] Running auto-clicker heuristic to bypass modals and trigger video...")
            await page.evaluate("""
                (() => {
                    const keywords = ['agree', 'accept', 'i am 18', 'over 18', 'enter', 'yes', 'play', 'watch', 'continue'];
                    const elements = document.querySelectorAll('button, a, div[role="button"], span');
                    
                    for (let el of elements) {
                        if (!el.innerText) continue;
                        const text = el.innerText.toLowerCase().trim();
                        if (text.length > 30) continue; // Skip large text blocks
                        
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                        
                        for (let kw of keywords) {
                            if (text === kw || text.includes(kw)) {
                                if (el.offsetHeight > 10 && el.offsetWidth > 10) {
                                    try { el.click(); console.log("Clicked:", text); } catch(e) {}
                                    break;
                                }
                            }
                        }
                    }
                    
                    // Look for large play overlays (common in adult/piracy tubes)
                    const playButtons = document.querySelectorAll('[class*="play"], [id*="play"]');
                    for (let btn of playButtons) {
                        const style = window.getComputedStyle(btn);
                        if (style.display !== 'none' && btn.offsetHeight > 30) {
                            try { btn.click(); console.log("Clicked Play Overlay"); } catch(e) {}
                        }
                    }
                })();
            """)
            # Give the network time to catch the newly triggered media requests
            await asyncio.sleep(2.5)
        except Exception as e:
            logger.warning(f"[SiteAnalyzer] Auto-clicker failed: {e}")

        # ------------------------------------------------------------------
        # Collect DOM artifacts
        # ------------------------------------------------------------------
        html = await page.content()
        dom  = await page.evaluate("document.documentElement.outerHTML")

        video_tags = await page.evaluate("""
            () => Array.from(document.querySelectorAll('video, audio')).map(el => ({
                tag:    el.tagName,
                src:    el.src || null,
                poster: el.poster || null,
                currentSrc: el.currentSrc || null,
                sources: Array.from(el.querySelectorAll('source')).map(s => ({
                    src: s.src, type: s.type
                }))
            }))
        """)

        scripts = await page.evaluate("""
            () => Array.from(document.querySelectorAll('script')).map(s => ({
                src:     s.src || null,
                content: s.src ? null : s.textContent.substring(0, 3000)
            }))
        """)

        blob_urls = await page.evaluate(
            "() => window.__capturedBlobURLs || []"
        )

        service_workers = await page.evaluate("""
            async () => {
                if (!navigator.serviceWorker) return [];
                try {
                    const regs = await navigator.serviceWorker.getRegistrations();
                    return regs.map(r => ({ scope: r.scope, scriptURL: r.active?.scriptURL }));
                } catch(e) { return []; }
            }
        """)

        local_storage = await page.evaluate("""
            () => {
                const s = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    s[k] = localStorage.getItem(k);
                }
                return s;
            }
        """)
        cookies = await context.cookies()

        captured_xhr = await page.evaluate("() => window.__capturedXHR || []")
        for cx in captured_xhr:
            for req in network_requests:
                if req["url"] == cx["url"] and not req["response_body"]:
                    req["response_body"] = cx["body"]

        await browser.close()

    # Save cookies as Netscape format (usable by yt-dlp's cookiefile option)
    cookie_file_path = None
    try:
        parsed_host = urlparse(url).netloc
        cookie_dir = "output/cookies"
        os.makedirs(cookie_dir, exist_ok=True)
        safe_host = re.sub(r"[^a-zA-Z0-9_-]", "_", parsed_host)
        cookie_file_path = os.path.join(cookie_dir, f"{safe_host}.txt")
        with open(cookie_file_path, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# Captured by VideoHunter AI (Playwright)\n\n")
            for c in cookies:
                domain  = c.get("domain", "")
                flag    = "TRUE" if domain.startswith(".") else "FALSE"
                path    = c.get("path", "/")
                secure  = "TRUE" if c.get("secure") else "FALSE"
                expires = int(c.get("expires", 0)) if c.get("expires") and c["expires"] > 0 else 0
                name    = c.get("name", "")
                value   = c.get("value", "")
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
        logger.info(f"[SiteAnalyzer] Saved {len(cookies)} cookies → {cookie_file_path}")
    except Exception as e:
        logger.warning(f"[SiteAnalyzer] Cookie file save failed: {e}")

    return {
        "analyzer": "playwright",
        "url": url,
        "html": html[:60000],
        "dom": dom[:60000],
        "network_requests": network_requests,
        "console_logs": console_logs,
        "scripts": scripts,
        "video_tags": video_tags,
        "blob_urls": blob_urls,
        "cookies": {c["name"]: c["value"] for c in cookies},
        "cookie_file": cookie_file_path,
        "local_storage": local_storage,
        "service_workers": service_workers,
        "intercepted_api_responses": intercepted_api_responses,
        "error": None,
    }



# ---------------------------------------------------------------------------
# Public API — SiteAnalyzer class
# ---------------------------------------------------------------------------
class SiteAnalyzer:
    """
    Agent 1 — Site Analyzer.
    Tries Playwright first; falls back to Selenium on failure.
    """

    def __init__(self, output_dir: str = "output/raw"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def analyze(self, url: str, cookies_path: Optional[str] = None) -> dict:
        """
        Synchronous entry point.
        Returns the Raw Data Bundle dict and saves it to output_dir.
        """
        logger.info(f"[SiteAnalyzer] Analyzing: {url}")

        # Fix: asyncio.run() inside Flask threads needs a fresh event loop
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            bundle = loop.run_until_complete(_playwright_analyze(url, cookies_path))
            loop.close()
            logger.info("[SiteAnalyzer] Playwright analysis complete.")
        except Exception as pw_err:
            logger.warning(f"[SiteAnalyzer] Playwright failed ({pw_err}), trying Selenium…")
            try:
                bundle = _selenium_analyze(url, cookies_path)
                logger.info("[SiteAnalyzer] Selenium analysis complete.")
            except Exception as sel_err:
                bundle = {
                    "analyzer": "failed",
                    "url": url,
                    "html": "", "dom": "",
                    "network_requests": [], "console_logs": [],
                    "scripts": [], "video_tags": [], "blob_urls": [],
                    "cookies": {}, "local_storage": {}, "service_workers": [],
                    "error": f"Playwright: {pw_err} | Selenium: {sel_err}",
                }
                logger.error(f"[SiteAnalyzer] Both analyzers failed: {bundle['error']}")

        # Add metadata
        bundle["meta"] = {
            "domain":    urlparse(url).netloc,
            "timestamp": time.time(),
            "request_count":     len(bundle.get("network_requests", [])),
            "media_request_count": sum(
                1 for r in bundle.get("network_requests", [])
                if r.get("type") in {"media", "fetch", "xhr"}
                or any(ext in r.get("url", "") for ext in [".m3u8", ".mpd", ".mp4", ".ts"])
            ),
        }

        # Save raw bundle
        domain_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", urlparse(url).netloc)
        out_path = os.path.join(self.output_dir, f"{domain_slug}_raw.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2, ensure_ascii=False)
        logger.info(f"[SiteAnalyzer] Raw bundle saved → {out_path}")

        bundle["_saved_path"] = out_path
        return bundle


# ---------------------------------------------------------------------------
# CLI quick-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    analyzer = SiteAnalyzer()
    result = analyzer.analyze(target)
    print(json.dumps(result["meta"], indent=2))
    print(f"\nSaved to: {result['_saved_path']}")
