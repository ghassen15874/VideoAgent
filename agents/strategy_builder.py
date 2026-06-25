"""
Agent 3 — Strategy Builder
============================
Decides the best video extraction strategy using:
  1. Memory cache check  (fast, zero cost)
  2. FAISS RAG retrieval (find similar past domains)
  3. LLM reasoning       (OpenAI / Gemini / Ollama)
  4. Rule-based fallback (if no LLM configured)
  5. Strategy validation (dry-run sanity check)
"""

import json
import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

VALID_STRATEGIES = {
    "direct_url",
    "m3u8_ffmpeg",
    "m3u8_ytdlp",
    "mpd_ytdlp",
    "api_fetch",
    "blob_capture",
    "ytdlp_generic",
    "custom",
}

# ── LLM Prompt Template ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert in video extraction and reverse engineering web video delivery systems.
Your job is to analyze a Signal Map from a website and choose the optimal extraction strategy.
Always respond with ONLY valid JSON — no markdown, no explanation outside the JSON."""

USER_PROMPT_TEMPLATE = """
## Signal Map from target site:
{signal_map}

## Similar sites and their successful strategies (from memory):
{rag_results}

## Choose the best strategy from these options:
- "direct_url"     → <video> tag src is a public URL, just download it
- "m3u8_ffmpeg"   → HLS stream found, use ffmpeg to mux segments
- "m3u8_ytdlp"    → HLS stream found, use yt-dlp (easier, handles tokens)
- "mpd_ytdlp"     → DASH MPD manifest found, use yt-dlp
- "api_fetch"     → Hit the API endpoint to get the real video URL first
- "blob_capture"  → MediaSource/Blob stream — capture segments via browser
- "ytdlp_generic" → Let yt-dlp auto-detect (good fallback for known platforms)
- "custom"        → None of the above match, generate custom Python code

## Rules:
- If m3u8 URL found AND auth token is IN the URL → use "m3u8_ffmpeg" (token already embedded)
- If m3u8 found AND auth is in headers → use "m3u8_ytdlp" (yt-dlp handles headers better)
- If DASH MPD found → prefer "mpd_ytdlp"
- If direct video tag src exists → "direct_url" (simplest)
- If API endpoint returns video URL in JSON → "api_fetch"
- If blob/MediaSource detected → "blob_capture"
- If obfuscated JS and nothing else → "ytdlp_generic"
- **IMPORTANT**: If `api_video_endpoint` is set OR `same_domain_apis` contains a URL with
  path segments like `/ajax/get_sources`, `/sources`, `/get_video`, `/player`, `/embed` →
  prefer "api_fetch" with that URL as `api_endpoint`... UNLESS a direct CDN URL (`.mp4` or `.m3u8`) 
  is already found in `direct_video_srcs` or `m3u8_urls`. If a CDN URL is already found, 
  always prefer `direct_url` or `m3u8_ytdlp` because single-use API tokens will fail if replayed.
- **CRITICAL — Ad Avoidance**: For major, well-known video hosting platforms, ALWAYS prefer
  "ytdlp_generic" over "direct_url". Large platforms frequently inject ad clips (or short previews) 
  into their `<video>` tags. Rely on `ytdlp_generic` for these sites, as yt-dlp's native 
  extractors safely bypass the ads and fetch the real, full-length media.

## Respond ONLY with this exact JSON structure:
{{
  "strategy": "<strategy_name>",
  "reason": "<one sentence explanation>",
  "params": {{
    "manifest_url": "<m3u8 or mpd URL if applicable>",
    "video_url": "<direct video URL if applicable>",
    "api_endpoint": "<API URL if applicable>",
    "headers": {{}},
    "auth_in": "query_param|header|cookie|none"
  }},
  "confidence": <0.0 to 1.0>,
  "fallback": "<fallback strategy name>"
}}
"""


# ── Rule-based fallback (no LLM needed) ──────────────────────────────────────

def _rule_based_strategy(signal_map: dict, retry_count: int = 0) -> dict:
    """
    Pure rule-based strategy selection.
    Used when no LLM is configured OR as validation cross-check.
    retry_count shifts to progressively simpler strategies.
    """
    sigs   = signal_map.get("signals", {})
    domain = signal_map.get("domain", "")

    # ── Domains yt-dlp natively supports → always use ytdlp_generic ──────────
    # These sites have dedicated yt-dlp extractors that handle auth, HLS, ads etc.
    # Trying to pick URLs ourselves will always grab ad clips instead.
    YTDLP_NATIVE_DOMAINS = {
        "www.pornhub.com", "pornhub.com",
        "www.xhamster.com", "xhamster.com",
        "www.redtube.com", "redtube.com",
        "www.tube8.com", "tube8.com",
        "www.youporn.com", "youporn.com",
        "www.spankbang.com", "spankbang.com",
        "www.eporner.com", "eporner.com",
        "www.xnxx.com", "xnxx.com",
        "www.xvideos.com", "xvideos.com",
        "www.youtube.com", "youtu.be",
        "www.twitter.com", "x.com", "twitter.com",
        "www.reddit.com", "reddit.com",
        "www.vimeo.com", "vimeo.com",
        "www.dailymotion.com", "dailymotion.com",
        "www.twitch.tv", "twitch.tv",
        "hqporner.com", "www.hqporner.com",
    }
    if domain in YTDLP_NATIVE_DOMAINS:
        return {
            "strategy":   "ytdlp_generic",
            "reason":     f"yt-dlp has a native extractor for {domain} — using it to avoid ad URL selection",
            "params":     {"target_url": signal_map.get("domain", ""), "auth_in": "none"},
            "confidence": 0.95,
            "fallback":   "custom",
            "source":     "rules",
        }

    rules = []

    # Rule 0: Embed/iframe video host detected (mydaddy.cc, doodstream, etc.)
    # Use yt-dlp on the embed URL — much more reliable than picking a preview clip
    if sigs.get("embed_found") and sigs.get("embed_video_urls"):
        rules.append(("ytdlp_generic", 0.87, {
            "target_url": sigs["embed_video_urls"][0],
            "auth_in":    "none",
        }))

    # Rule 1: Direct video tag → simplest (only if size-probed to be real content)
    if sigs.get("direct_video_tag") and sigs.get("direct_video_srcs"):
        rules.append(("direct_url", 0.90, {
            "video_url": sigs["direct_video_srcs"][0],
            "auth_in":   "none",
        }))


    # Rule 2: M3U8 found
    if sigs.get("m3u8_found") and sigs.get("m3u8_urls"):
        best_m3u8 = sigs["m3u8_urls"][0]
        # Token in URL → ffmpeg can carry it
        if "token=" in best_m3u8 or "sign=" in best_m3u8 or "key=" in best_m3u8:
            rules.append(("m3u8_ffmpeg", 0.88, {
                "manifest_url": best_m3u8,
                "auth_in":      "query_param",
                "headers":      {},
            }))
        elif sigs.get("auth_required"):
            rules.append(("m3u8_ytdlp", 0.82, {
                "manifest_url": best_m3u8,
                "auth_in":      "header",
                "headers":      sigs.get("auth_headers", {}),
            }))
        else:
            rules.append(("m3u8_ffmpeg", 0.85, {
                "manifest_url": best_m3u8,
                "auth_in":      "none",
                "headers":      {},
            }))

    # Rule 3: MPD found
    if sigs.get("mpd_found") and sigs.get("mpd_urls"):
        rules.append(("mpd_ytdlp", 0.85, {
            "manifest_url": sigs["mpd_urls"][0],
            "auth_in":      "header" if sigs.get("auth_required") else "none",
            "headers":      sigs.get("auth_headers", {}),
        }))

    # Rule 4: API endpoint detected (either via JSON body keys OR path heuristic)
    # Triggers on api_video_endpoint alone — doesn't require high_priority_apis list
    if sigs.get("api_video_endpoint"):
        rules.append(("api_fetch", 0.82, {
            "api_endpoint": sigs["api_video_endpoint"],
            "headers":      sigs.get("auth_headers", {}),
            "auth_in":      "header" if sigs.get("auth_required") else "cookie",
        }))
    elif sigs.get("high_priority_apis"):
        # Fallback: use first high_priority_api even without video keys in body
        first_api = sigs["high_priority_apis"][0]
        rules.append(("api_fetch", 0.78, {
            "api_endpoint": first_api.get("url", ""),
            "headers":      sigs.get("auth_headers", {}),
            "auth_in":      "header" if sigs.get("auth_required") else "cookie",
        }))

    # Rule 5: Blob/MediaSource
    if sigs.get("blob_detected"):
        rules.append(("blob_capture", 0.60, {
            "auth_in": "none",
        }))

    # Default: yt-dlp generic
    rules.append(("ytdlp_generic", 0.40, {
        "target_url": signal_map.get("domain", ""),
        "auth_in":    "none",
    }))

    # Skip N rules based on retry count
    if retry_count > 0:
        return {
            "strategy":   "custom",
            "reason":     f"Fallback to AI generated python script after {retry_count} failure(s)",
            "params":     {"target_url": signal_map.get("domain", ""), "headers": sigs.get("auth_headers", {})},
            "confidence": 0.5,
            "fallback":   "ytdlp_generic",
            "source":     "rules"
        }

    idx      = min(retry_count, len(rules) - 1)
    strategy_name, confidence, params = rules[idx]

    return {
        "strategy":   strategy_name,
        "reason":     f"Rule-based selection (rule #{idx+1})",
        "params":     params,
        "confidence": confidence,
        "fallback":   rules[min(idx + 1, len(rules) - 1)][0],
        "source":     "rules",
    }


# ── Strategy validation ───────────────────────────────────────────────────────

def _validate_strategy(strategy: dict, signal_map: dict) -> tuple[bool, str]:
    """
    Dry-run sanity checks before returning strategy.
    Returns (is_valid, reason_if_invalid).
    """
    name   = strategy.get("strategy", "")
    params = strategy.get("params", {})

    if name not in VALID_STRATEGIES:
        return False, f"Unknown strategy: {name}"

    if name in ("m3u8_ffmpeg", "m3u8_ytdlp", "mpd_ytdlp"):
        if not params.get("manifest_url"):
            # Try to recover from signal map
            urls = signal_map.get("signals", {}).get("m3u8_urls") or \
                   signal_map.get("signals", {}).get("mpd_urls")
            if urls:
                strategy["params"]["manifest_url"] = urls[0]
            else:
                return False, "Strategy requires manifest_url but none found"

    if name == "direct_url" and not params.get("video_url"):
        srcs = signal_map.get("signals", {}).get("direct_video_srcs")
        if srcs:
            strategy["params"]["video_url"] = srcs[0]
        else:
            return False, "Strategy requires video_url but none found"

    if name == "api_fetch" and not params.get("api_endpoint"):
        ep = signal_map.get("signals", {}).get("api_video_endpoint")
        if ep:
            strategy["params"]["api_endpoint"] = ep
        else:
            return False, "Strategy requires api_endpoint but none found"

    return True, "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# Public API — StrategyBuilder class
# ═══════════════════════════════════════════════════════════════════════════════

class StrategyBuilder:
    """
    Agent 3 — Strategy Builder.
    Decision flow: Cache → RAG → LLM → Rules → Validate
    """

    def build(self, signal_map: dict, retry_count: int = 0) -> dict:
        domain = signal_map.get("domain", "unknown")
        logger.info(f"[StrategyBuilder] Building strategy for {domain} (retry={retry_count})")

        # ── Step 1: Query FAISS RAG ──────────────────────────────────────────
        rag_results = []
        rag_context = "No similar sites found in memory."
        try:
            from core.rag import query_similar
            rag_results = query_similar(signal_map, top_k=3)
            if rag_results:
                rag_context = json.dumps([
                    {"domain": r["domain"],
                     "strategy": r["strategy"].get("strategy"),
                     "success_rate": r["success_rate"]}
                    for r in rag_results
                ], indent=2)
                logger.info(f"[StrategyBuilder] RAG found {len(rag_results)} similar sites")
        except Exception as e:
            logger.warning(f"[StrategyBuilder] RAG query failed: {e}")

        # ── Step 2: Try LLM ──────────────────────────────────────────────────
        strategy = None
        try:
            from core.llm_client import call_llm, extract_json_from_response

            # Trim signal map for prompt (avoid huge payloads)
            prompt_signal_map = {
                "domain":  domain,
                "signals": signal_map.get("signals", {}),
                "confidence": signal_map.get("confidence", 0),
            }
            # Remove large nested structures from prompt
            for k in ("blob_info", "high_priority_apis", "cdn_urls",
                      "fetch_urls_in_js", "xhr_urls_in_js"):
                prompt_signal_map["signals"].pop(k, None)

            prev_logs = signal_map.get("previous_error_logs", "")
            error_hint = ""
            if retry_count > 0 and prev_logs:
                error_hint = f"\n## PREVIOUS EXECUTION FAILED:\n{prev_logs}\n\nSince this is a retry, standard approaches (api_fetch/ytdlp_generic) have already failed. You MUST select 'custom' to trigger the AI self-healing Python generator if this is a 403 or 404 error caused by tokens.\n"
            
            prompt = (
                SYSTEM_PROMPT + "\n\n" +
                USER_PROMPT_TEMPLATE.format(
                    signal_map=json.dumps(prompt_signal_map, indent=2),
                    rag_results=rag_context,
                ) + error_hint
            )

            raw_response = call_llm(prompt)
            if raw_response:
                parsed = extract_json_from_response(raw_response)
                if parsed and parsed.get("strategy") in VALID_STRATEGIES:
                    strategy = parsed
                    strategy["source"] = "llm"
                    logger.info(f"[StrategyBuilder] LLM chose: {strategy['strategy']}")
                else:
                    logger.warning(f"[StrategyBuilder] LLM returned invalid strategy: {parsed}")

        except Exception as e:
            logger.warning(f"[StrategyBuilder] LLM call failed: {e}")

        # ── Step 3: Rule-based fallback ───────────────────────────────────────
        if strategy is None:
            strategy = _rule_based_strategy(signal_map, retry_count)
            logger.info(f"[StrategyBuilder] Rules chose: {strategy['strategy']}")

        # ── Step 4: Validate & auto-repair ───────────────────────────────────
        is_valid, reason = _validate_strategy(strategy, signal_map)
        if not is_valid:
            logger.warning(f"[StrategyBuilder] Validation failed ({reason}) — falling back to rules")
            strategy = _rule_based_strategy(signal_map, retry_count + 1)
            strategy["validation_note"] = reason

        # ── Finalize ──────────────────────────────────────────────────────────
        strategy["domain"]       = domain
        strategy["retry_count"]  = retry_count
        strategy["rag_used"]     = len(rag_results) > 0
        strategy["rag_matches"]  = [r["domain"] for r in rag_results]

        logger.info(
            f"[StrategyBuilder] Final: {strategy['strategy']} "
            f"(conf={strategy.get('confidence', '?')}, "
            f"source={strategy.get('source', '?')})"
        )
        return strategy
