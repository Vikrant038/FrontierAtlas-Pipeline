# FrontierAtlas Intelligence Pipeline

An enterprise-grade, asynchronous, fault-tolerant AI data intelligence and knowledge graph pipeline.

## Overview

FrontierAtlas / GraphOne maps founders, startups, products, research papers, job listings, and real-time market signals across the global AI industry.

This repository implements the production-grade data acquisition and intelligence engine:
- **Massive Distributed Acquisition**: Concurrent ingestion of startups, products, and research papers (arXiv API + RSS category feeds, Hugging Face daily, and the OpenAlex arXiv mirror) with live GitHub star metrics. Targets are configurable (`--target`); at ≥1,000 the run is **WAL-checkpointed and resumable** — an interrupted run recovers partial records on restart and truncates on completion.
- **High-Fidelity Signal Ingestion**: Real-time monitoring across configurable AI news sources and job boards with a strict 24-hour freshness guarantee, enforced by 4-tier date resolution and per-source staleness warnings.
- **Multi-Tier LLM Extraction Engine**: Resilient fallback chain (Gemini 2.0 Flash → Groq Llama 3.3 70B → DeepSeek Chat → Deterministic parsing) with per-key RPM windows, exponential backoff + jitter (429 defense, Retry-After honored incl. RFC 7231 dates), non-retryable 413/context-length classification, and pre-flight token budgeting.
- **3-Tier Deterministic Entity Resolution**: Unicode NFKD canonical normalization, RapidFuzz token matching, and LLM disambiguation backed by a 50+ canonical seed list and full audit logging.
- **Scalable Anti-Bot Architecture**: Tiered transport using `httpx`, `curl-cffi` (Chrome TLS fingerprint impersonation), and `camoufox` fallback, guarded by a per-host circuit breaker, challenge-page detection, and time-reset quota backoffs (`GITHUB_TOKENS` pools with per-token pacing).
- **Key Pools**: `GITHUB_TOKENS` plus per-tier LLM key lists multiply throughput, not just quota.
- **Multi-Format Export**: 6-tab formatted Excel workbook (`.xlsx`), individual CSVs, and an in-memory NetworkX relationship graph — written atomically (`.tmp` + `os.replace`).
- **Operability**: live per-vertical progress monitor with ETA, machine-readable `exports/run_report.json` (completed/shortfall/interrupted + telemetry), graceful signal handling, redacted logging.

## Tech Stack

- **Runtime**: Python 3.11+ (`asyncio`, `typing`)
- **HTTP & Scraping**: `httpx`, `curl-cffi`, `playwright`, `camoufox`, `feedparser`, `trafilatura`
- **Validation**: `pydantic>=2.8.0`, `pydantic-settings`
- **LLM Providers**: `google-generativeai`, `groq`, `openai`, `tiktoken`
- **Entity Resolution**: `rapidfuzz`, `unicodedata`
- **Graph & Export**: `networkx`, `openpyxl`
- **Testing**: `pytest`, `pytest-asyncio`, `respx`, `pytest-mock`, `freezegun`

## Setup & Installation

```bash
# 1. Clone repository
git clone <repo-url>
cd Demo_project

# 2. Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Edit .env and supply your API keys (59 documented settings; key pools
# accept comma-separated lists: GITHUB_TOKENS, GEMINI_API_KEYS, GROQ_API_KEYS,
# DEEPSEEK_API_KEYS, CUSTOM_LLM_API_KEYS)
```

## Usage

```bash
# Phase 1 (startups + products + papers) with a 1,000-item target
python -m src.cli --phase 1 --target 1000

# Everything, including 24h news/jobs ingestion
python -m src.cli --phase all --target 1000

# Custom deliverable path + live progress every 60s (0 disables)
python -m src.cli --phase all --target 1000 --output exports/run.xlsx --progress-interval 60

# Upload deliverables to Google Sheets (requires GOOGLE_SERVICE_ACCOUNT_PATH)
python -m src.cli --phase all --target 1000 --sheets
```

Notes:
- Defaults: `--phase all`, `--target 1000`, output `exports/FrontierAtlas_Intelligence.xlsx` (`exports/phase1_test.xlsx` for `--phase 1`).
- Exit code is `1` on a Phase-I shortfall (target not reached), `0` on completion — the `run_report.json` `status` field carries the same signal for automation.
- Targets ≥1,000 automatically enable WAL checkpointing; an interrupted run prints guidance and resumes on the next invocation.
- All source URLs, pacing, batch sizes, and limits are env-configurable (see `.env.example`).

## Running Tests & Coverage Gate

```bash
# Full test suite (352 tests; 100% offline, hermetic)
pytest tests/ -q

# Run with CI branch-coverage report & 80% threshold enforcement
pytest --cov=src --cov-report=term-missing:skip-covered --cov-fail-under=80
```

Current baseline: **352 tests passing; 95% statements / 91% branches** on `src/` (CI gate enforces `--cov-fail-under=80`).
