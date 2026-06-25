# VideoHunter AI

VideoHunter AI is a Flask web app and LangGraph pipeline for analyzing video pages, choosing an extraction strategy, generating a downloader script, and executing it.

## What It Does

- Accepts single URLs or batches through a web UI.
- Captures page, media, and network signals with browser automation.
- Builds extraction strategies with rule-based logic plus an optional LLM.
- Generates downloader scripts for direct media URLs, HLS manifests, `yt-dlp`, and related flows.
- Stores successful site strategies in local memory for faster future runs.

## Project Layout

```text
agents/      Core analysis, reverse engineering, strategy, and script generation logic
core/        Shared state, LLM client, and retrieval/memory helpers
nodes/       LangGraph node wrappers
templates/   Flask UI templates
app.py       Flask application and API routes
graph.py     LangGraph pipeline definition and CLI runner
```

Runtime artifacts such as downloaded media, cookies, logs, local memory indexes, `.env`, and the virtual environment are ignored by Git.

## Requirements

- Python 3.10+
- `ffmpeg` available on your system path
- Chromium browser dependencies for Playwright

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Create a `.env` file if you want LLM-assisted strategy generation:

```bash
DEEPSEEK_API_KEY=your_key_here
# or
OPENAI_API_KEY=your_key_here
# or
GOOGLE_API_KEY=your_key_here
# or
OLLAMA_MODEL=llama3
```

The LLM priority is DeepSeek, then OpenAI, then Gemini, then local Ollama. If no provider is configured, the app falls back to rule-based strategy selection.

## Run

```bash
source venv/bin/activate
python app.py
```

Then open:

```text
http://localhost:5000
```

You can also run a quick pipeline test from the command line:

```bash
python graph.py "https://example.com"
```

## 📦 Strategy Store & Ready-to-Use Sites

VideoHunter AI learns as it goes. When it successfully downloads a video from a new domain, it saves the extraction strategy, cookies, headers, and API patterns into its local FAISS + SQLite memory. 

You can **Export** these strategies as `.vhunter` files directly from the UI and share them with friends. When a friend imports your `.vhunter` file, their agent will skip the slow analysis phase and instantly use your proven strategy.

### Pre-cracked Sites (`supported_sites/`)
We have included several pre-cracked strategies in the `supported_sites/` directory. You can import these `.vhunter` files into your web UI to immediately unlock fast downloading for these sites:
- Just open the UI, click **📥 Import .vhunter**, and select a file from `supported_sites/`.

## API

Start a single analysis job:

```bash
curl -X POST http://localhost:5000/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/video","format":"mp4","quality":"best"}'
```

Check job status:

```bash
curl http://localhost:5000/api/status/<job_id>
```

Start a batch:

```bash
curl -X POST http://localhost:5000/api/batch \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://example.com/a","https://example.com/b"],"format":"mp4","quality":"best"}'
```

## Notes

Use this project only with content you have the right to access and download. Some sites use expiring URLs, signed manifests, or anti-bot protections, so retries may re-analyze the page to refresh tokens and media signals.
