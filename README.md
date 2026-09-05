# FrontierAtlas Intelligence Pipeline

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/Tests-350%20passing-brightgreen?logo=pytest&logoColor=white)
![Coverage](https://img.shields.io/badge/Coverage-80%25%20gate-yellow?logo=githubactions&logoColor=white)
![Async](https://img.shields.io/badge/asyncio-native-464646?logo=python&logoColor=white)
![Pydantic](https://img.shields.io/badge/pydantic-v2-E92063?logo=pydantic&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-Gemini%20%7C%20Groq%20%7C%20DeepSeek-8E75B2?logo=googlebard&logoColor=white)
![Export](https://img.shields.io/badge/Export-XLSX%20%7C%20CSV%20%7C%20Sheets-1D6F42?logo=microsoftexcel&logoColor=white)
![Docs](https://img.shields.io/badge/Architecture-PDF-blue?logo=readme&logoColor=white)

> **Pipeline:** `Discovery → Tiered Fetch (httpx → curl-cffi → Camoufox) → LLM Extraction (4-tier failover) → Entity Resolution (3-tier + LLM disambiguation) → 24h Freshness Gate → 6-Tab Export + Knowledge Graph`

| Capability | Scale Achieved |
|---|---|
| 🏢 **Startups** | 1,000+ (YC API + HF Orgs + GitHub Org Search, verified `teamSize`) |
| 🛠️ **Products** | 1,000+ (curated awesome-* directories, LLM-assisted pricing) |
| 📄 **Research Papers** | 1,000+ (ArXiv 4-tier failover, live GitHub star telemetry) |
| 📰 **Fresh News** | 5 AI publishers, strict <24h freshness gate |
| 💼 **Fresh Jobs** | 5 AI job boards, AI-title word-boundary filter |
| 🧠 **Entity Resolution** | 3-tier + LLM disambiguation, full audit log |
| 🕸️ **Knowledge Graph** | 4,700+ nodes / 1,700+ edges (NetworkX) |
| 🛡️ **Anti-Bot** | TLS impersonation (curl-cffi) + Camoufox fallback + circuit breaker |

An enterprise-grade, asynchronous, fault-tolerant AI data intelligence and knowledge graph pipeline.

FrontierAtlas / GraphOne maps startups, products, research papers, job listings, and real-time market signals across the global AI industry into an entity-resolution-backed knowledge graph.

## Features

- **Target-driven bulk acquisition** — concurrent ingestion of startups, products, and research papers (arXiv API + RSS category feeds, Hugging Face daily, and the OpenAlex arXiv mirror) with live GitHub star metrics. Targets are configurable (`--target`); at ≥1,000 the run is **WAL-checkpointed and resumable** — an interrupted run recovers partial records on restart and truncates on completion.
- **24-hour signal ingestion** — configurable AI news sources and job boards with a strict freshness gate, enforced by 4-tier date resolution, novelty stamping, and per-source staleness warnings (≥2 consecutive empty runs are flagged).
- **Multi-tier LLM extraction** — fallback chain (Gemini → Groq → DeepSeek → deterministic parsing) with per-key RPM windows, exponential backoff + jitter (429), Retry-After honored incl. RFC 7231 dates, non-retryable 413/context-length classification, and pre-flight token budgeting.
- **3-tier entity resolution** — Unicode NFKD canonical normalization → RapidFuzz token matching → LLM disambiguation, backed by a 50+ canonical seed list and a full audit log.
- **Anti-bot escalation** — tiered transport (`httpx` → `curl-cffi` Chrome TLS impersonation → local `camoufox` fallback) guarded by a per-host circuit breaker, challenge-page detection, time-reset quota backoffs, and **key pools** (`GITHUB_TOKENS`, per-tier LLM key lists) with per-token pacing that multiplies throughput, not just quota.
- **Multi-format export** — 6-tab formatted Excel workbook, per-vertical CSVs, an in-memory NetworkX relationship graph, and optional Google Sheets upload — all files written atomically (`.tmp` + `os.replace`).
- **Operability** — live per-vertical progress monitor with ETA, machine-readable `exports/run_report.json` (`completed` / `shortfall` / `interrupted` + LLM-tier and anti-bot telemetry), graceful SIGINT handling, redacted logging.

## Repository layout

```
src/
├── cli.py                  # Click entry point: orchestration, run report, progress monitor
├── config.py               # pydantic-settings (59 env vars — see .env.example)
├── crawlers/
│   ├── base.py             # AsyncBaseCrawler/TargetedCrawler: transport, retry, WAL, GitHub token pool
│   ├── anti_bot.py         # Per-host circuit breaker + challenge-page detection
│   ├── papers_crawler.py   # arXiv API + RSS, Hugging Face daily, OpenAlex mirror
│   ├── startups_crawler.py # YC batches + GitHub search
│   ├── products_crawler.py # Markdown directories (config-driven source list)
│   ├── news_crawler.py     # RSS feeds with 24h freshness gate
│   └── jobs_crawler.py     # RemoteOK, Arbeitnow, Himalayas, WeWorkRemotely, YC-HN
├── llm/                    # fallback_chain, rate_limiter, chunker, prompts, rules
├── resolution/             # normalizer (3-tier resolver), seed_data
├── schemas/entities.py     # Pydantic record/content schemas
├── exporters/              # excel, csv, graph_builder, sheets
└── utils/                  # date_normalizer, logger (redaction), run_state, security (SSRF guard)
exports/                    # Deliverables: *.xlsx, *.csv, run_report.json, run_state.json, wal/
docs/anti_bot_strategy.md   # Anti-bot architecture (implemented tiers vs. planned)
```

## Tech stack

- **Runtime**: Python 3.11+ (`asyncio`, `typing`)
- **HTTP & scraping**: `httpx`, `curl-cffi`, `camoufox`, `feedparser`, `trafilatura`
- **Validation**: `pydantic>=2.8.0`, `pydantic-settings`
- **LLM providers**: `google-generativeai`, `groq`, `openai`, `tiktoken`
- **Resolution & graph**: `rapidfuzz`, `networkx`, `openpyxl`
- **Testing**: `pytest`, `pytest-asyncio`, `respx`, `pytest-mock`, `freezegun`, `pytest-cov`

## Setup

```bash
git clone <repo-url>
cd Demo_project

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then supply your API keys
```

### Configuration

All knobs live in `src/config.py` and are documented in `.env.example` (59 settings, fully synced). Key ones:

| Group | Variables |
|---|---|
| GitHub enrichment | `GITHUB_TOKEN` or `GITHUB_TOKENS` (comma-separated pool), `GITHUB_INTERVAL_SECONDS`, `GITHUB_SEARCH_PAGES` |
| LLM tiers | `GEMINI_API_KEYS`, `GROQ_API_KEYS`, `DEEPSEEK_API_KEYS`, `CUSTOM_LLM_API_KEYS` (pools; single `*_API_KEY` also accepted) |
| Papers supply | `ARXIV_SEARCH_QUERY`, `ARXIV_CDN_CATEGORIES`, `ARXIV_INTERVAL_SECONDS`, `PAPERS_BATCH_SIZE`, `OPENALEX_PER_PAGE`, `OPENALEX_EMAIL` (polite pool), `HF_DAILY_LIMIT` |
| Sources | `PRODUCT_SOURCES_JSON`, `NEWS_SOURCES_JSON`, per-board job URLs |
| Concurrency | `MAX_CONCURRENT_REQUESTS`, `PRODUCTS_CONCURRENCY`, `MAX_CONCURRENT_LLM_REQUESTS`, `YC_PARALLEL_PAGES` |
| Reliability | `ENABLE_WAL`, `WAL_DIR`, `GITHUB_ANONYMOUS_LOOKUP_BUDGET`, `DEFAULT_REQUEST_TIMEOUT_SECONDS` |

## Usage

```bash
# Phase 1 only (startups + products + papers) — 1,000-item target
python -m src.cli --phase 1 --target 1000

# Everything, including the 24h news/jobs ingestion
python -m src.cli --phase all --target 1000

# Custom deliverable path + live progress every 60s (0 disables)
python -m src.cli --phase all --target 1000 --output exports/run.xlsx --progress-interval 60

# Upload deliverables to Google Sheets (requires GOOGLE_SERVICE_ACCOUNT_PATH)
python -m src.cli --phase all --target 1000 --sheets
```

Notes:
- Defaults: `--phase all`, `--target 1000`, output `exports/FrontierAtlas_Intelligence.xlsx` (`exports/phase1_test.xlsx` for `--phase 1`).
- Exit code is `0` on completion, `1` on Phase-I shortfall (target not reached). `exports/run_report.json` carries the same signal (`status`) plus telemetry for automation/alerting.
- Targets ≥1,000 auto-enable WAL checkpointing; an interrupted run prints guidance and resumes on the next invocation.
- `--progress-interval N` prints a live per-vertical table (collected/target/ETA) every N seconds.

## Deliverables & Submission Links

- **Google Sheets Output (Public Link)**: [https://docs.google.com/spreadsheets/d/1PzZqRtd5n40a5qlfycsrYd_9RVnHXcDhq5MRMhc1xzQ](https://docs.google.com/spreadsheets/d/1PzZqRtd5n40a5qlfycsrYd_9RVnHXcDhq5MRMhc1xzQ) (6 tabs: Startups, Products, Research_Papers, Jobs_24h, News_24h, Entity Mapping Log)
- **Architecture Specification (PDF)**: [`architecture.pdf`](./architecture.pdf) (also in [`docs/architecture.pdf`](docs/architecture.pdf) / [`docs/architecture.md`](docs/architecture.md) — 3 pages, addressing Q1–Q4)
- **Local Deliverables (`exports/`)**:
  - `FrontierAtlas_Intelligence.xlsx` — 6 tabs styled Excel workbook
  - `{startups,products,research_papers,jobs,news}.csv` + `entity_mapping_log.csv`
  - `run_report.json` — status, counts, LLM tier usage, and anti-bot telemetry
  - `run_state.json` — cross-run novelty state & per-source freshness history
- **Submission Form**: [https://forms.gle/8bnrg78Ki4E25RAk8](https://forms.gle/8bnrg78Ki4E25RAk8)

## Testing & coverage

The suite is fully **offline and hermetic** (no live network; HTTP is mocked via `respx`/fixtures, LLMs via injected fakes or `httpx.MockTransport`).

```bash
# Full suite
pytest tests/ -q

# Branch-coverage report + the CI gate (fails under 80%)
pytest --cov=src --cov-report=term-missing:skip-covered --cov-fail-under=80

# Coverage via .coveragerc (src-only, branch-aware, __init__ omitted)
coverage run --branch -m pytest tests/ -q && coverage report
```

**Current baseline (verified):** 350 tests passing; **96% statements / 90% branches** on `src/` (75 partial branches). CI (`.github/workflows/ci.yml`) enforces `--cov-fail-under=80` (currently at ~95%) on every push/PR to `main`/`develop`.

## Docs

- `docs/anti_bot_strategy.md` — the 4-tier anti-bot target architecture with an implemented-vs-planned status annotation.
- `ARCHITECTURE_REVIEW.md` (local, gitignored) — full multi-phase review record, deferred milestones, and the 500k supply-adapter plan.
