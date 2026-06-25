"""
Core — LLM Client
==================
Unified wrapper for DeepSeek / OpenAI / Gemini / Ollama.
Agent 3 uses this to reason about extraction strategy.

Priority: DeepSeek → OpenAI → Gemini → Ollama → None
"""

import json
import logging
import os
import re
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# DeepSeek API base URL (OpenAI-compatible)
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"   # DeepSeek-V3 (latest)


# ── Provider callers ──────────────────────────────────────────────────────────

def _call_deepseek(prompt: str, model: str) -> str:
    """DeepSeek uses OpenAI-compatible API — just override base_url."""
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model=model,
        temperature=0,
        openai_api_key=os.getenv("DEEPSEEK_API_KEY"),
        openai_api_base=DEEPSEEK_BASE_URL,
    )
    resp = llm.invoke(prompt)
    return resp.content


def _call_openai(prompt: str, model: str) -> str:
    from langchain_openai import ChatOpenAI
    llm  = ChatOpenAI(model=model, temperature=0)
    resp = llm.invoke(prompt)
    return resp.content


def _call_gemini(prompt: str, model: str) -> str:
    from langchain_google_genai import ChatGoogleGenerativeAI
    llm  = ChatGoogleGenerativeAI(model=model, temperature=0)
    resp = llm.invoke(prompt)
    return resp.content


def _call_ollama(prompt: str, model: str) -> str:
    from langchain_ollama import ChatOllama
    llm  = ChatOllama(model=model, temperature=0)
    resp = llm.invoke(prompt)
    return resp.content


# ── Main dispatcher ───────────────────────────────────────────────────────────

def call_llm(prompt: str) -> Optional[str]:
    """
    Call whichever LLM is configured via environment variables.

    Priority: DeepSeek → OpenAI → Gemini → Ollama → None

    Set env vars:
      DEEPSEEK_API_KEY   → uses deepseek-chat (DeepSeek-V3)  ← DEFAULT
      OPENAI_API_KEY     → uses gpt-4o-mini
      GOOGLE_API_KEY     → uses gemini-1.5-flash
      OLLAMA_MODEL       → uses local Ollama (e.g. llama3)

    Optional overrides:
      DEEPSEEK_MODEL     → change DeepSeek model (e.g. deepseek-reasoner)
      OPENAI_MODEL       → change OpenAI model
      GEMINI_MODEL       → change Gemini model
    """

    # 1. DeepSeek (highest priority — key is configured)
    if os.getenv("DEEPSEEK_API_KEY"):
        model = os.getenv("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL)
        logger.info(f"[LLM] Using DeepSeek: {model}")
        return _call_deepseek(prompt, model)

    # 2. OpenAI
    if os.getenv("OPENAI_API_KEY"):
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        logger.info(f"[LLM] Using OpenAI: {model}")
        return _call_openai(prompt, model)

    # 3. Gemini
    if os.getenv("GOOGLE_API_KEY"):
        model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        logger.info(f"[LLM] Using Gemini: {model}")
        return _call_gemini(prompt, model)

    # 4. Ollama (local)
    ollama_model = os.getenv("OLLAMA_MODEL")
    if ollama_model:
        logger.info(f"[LLM] Using Ollama: {ollama_model}")
        return _call_ollama(prompt, ollama_model)

    logger.warning("[LLM] No LLM configured — falling back to rule-based strategy")
    return None


# ── JSON parser ───────────────────────────────────────────────────────────────

def extract_json_from_response(text: str) -> Optional[dict]:
    """Parse JSON from LLM response (handles markdown code fences)."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None
