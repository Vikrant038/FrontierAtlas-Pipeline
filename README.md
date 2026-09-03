# FrontierAtlas Intelligence Pipeline

An enterprise-grade, asynchronous, fault-tolerant AI data intelligence and knowledge graph pipeline.

## Overview

FrontierAtlas / GraphOne maps founders, startups, products, research papers, job listings, and real-time market signals across the global AI industry.

This repository implements the production-grade data acquisition and intelligence engine:
- **Massive Distributed Acquisition**: Concurrent ingestion of 1,000+ startups, 1,000+ products, and 1,000+ research papers (Arxiv/PapersWithCode) with live GitHub star metrics.
- **High-Fidelity Signal Ingestion**: Real-time monitoring across 5 AI news sources and 5 AI job boards with a strict 24-hour freshness guarantee.
- **Multi-Tier LLM Extraction Engine**: Resilient fallback chain (Gemini 2.0 Flash → Groq Llama 3.3 70B → DeepSeek Chat → Deterministic parsing) featuring exponential backoff jitter (429 defense) and pre-flight token budgeting (413 defense).
- **3-Tier Deterministic Entity Resolution**: Unicode NFKD canonical normalization, RapidFuzz token matching, and LLM disambiguation backed by a 50+ canonical seed list and full audit logging.
- **Scalable Anti-Bot Architecture**: Tiered transport using `httpx`, `curl-cffi` (Chrome TLS fingerprint impersonation), `crawl4ai`, and `camoufox` fallback.
- **Multi-Format Export**: 6-tab formatted Excel workbook (`.xlsx`), individual CSVs, and an in-memory NetworkX relationship graph.

## Tech Stack

- **Runtime**: Python 3.11+ (`asyncio`, `typing`)
- **HTTP & Scraping**: `httpx`, `curl-cffi`, `crawl4ai`, `playwright`, `camoufox`, `feedparser`, `trafilatura`
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
# Edit .env and supply your API keys
```

## Running Tests

```bash
pytest tests/ -v
```
