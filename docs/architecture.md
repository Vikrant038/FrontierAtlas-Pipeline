# FrontierAtlas / GraphOne AI Intelligence Pipeline: Architectural Blueprint

**System:** FrontierAtlas / GraphOne AI Data Intelligence Engine  
**SSoT Reference:** `GEMINI.md`, `GUARDRAILS.md`, `CODING_STANDARDS.md` | **Risk Tier:** Commercial / Production  
**Runtime:** Python 3.11+ Async Native (`asyncio`, `httpx`, `curl-cffi`, `openpyxl`, `networkx`, `pydantic v2`)

---

## 1. System Architecture & Uni-Directional Pipeline Flow

The pipeline executes a strict uni-directional flow from discovery through resilient extraction to multi-target export:

```text
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   FRONTIERATLAS ARCHITECTURE                                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 [ Discovery / Sources ]
     │  ArXiv API + CDN RSS / HF Daily Papers / OpenAlex Mirror / GitHub API (stars)
     │  Y Combinator AI Directory / Hugging Face Orgs / GitHub AI Org Search
     │  Curated Product Directories (awesome-generative-ai, awesome-ai-tools, etc.)
     │  5 AI News Feeds (TechCrunch, VentureBeat, MIT Tech Review, Verge, HN)
     │  5 AI Job Boards (RemoteOK, Himalayas, Arbeitnow, WeWorkRemotely, HN Hiring)
     ▼
 [ Tiered Network Transport ] ──> [ Anti-Bot Circuit Breaker (src/crawlers/base.py) ]
     │  Level 1: Async HTTP/2 via httpx (Pooled keep-alive connections)
     │  Level 2: Socket-level Chrome124 TLS Impersonation via curl-cffi
     │  Level 3: Hardened Headless Browser via Camoufox (Turnstile/JS fallback)
     ▼
 [ Pre-Flight Protection & Multi-Tier Extraction (src/llm/fallback_chain.py) ]
     │  Token Budgeting (tiktoken cl100k_base <= 3,500 tokens; src/llm/chunker.py)
     │  Sliding-Window Rate Limiter (Gemini: 15, Groq: 30, Gateway: 60 RPM; src/llm/rate_limiter.py)
     │  Tier 1: Google Gemini (`gemini-3.5-flash-lite`)
     │  Tier 2: Groq LLaMA-3.3-70B (`llama-3.3-70b-versatile`)
     │  Tier 3: OpenAI-Compatible Gateway (`deepseek-chat` / `glm-5.3-flash`)
     │  Tier 4: Zero-API Deterministic Heuristics & Parsers (100% Zero-Dropped Guarantee)
     ▼
 [ 3-Tier Entity Resolution & Audit Log (src/resolution/normalizer.py) ]
     │  Tier 1: Canonical Alias & Exact Normalization (NFKD Unicode, suffix removal)
     │  Tier 2: RapidFuzz Token Sort Ratio (Match score >= 90)
     │  Tier 3: LLM Disambiguation (Score 70-89; temperature 0.0 JSON classification)
     ▼
 [ Freshness & Quality Gate (src/utils/date_normalizer.py) ]
     │  Strict 24-Hour Freshness Boundary (Reject all news/jobs > 24h old; ISO-8601 UTC)
     ▼
 [ Multi-Target Exporters & Knowledge Graph (src/exporters/) ]
     │  Formatted 6-Tab Excel Workbook (`openpyxl`; src/exporters/excel_exporter.py)
     │  Structured CSV Files (`src/exporters/csv_exporter.py`)
     │  Live Google Sheets Integration (`gspread` / Google Drive API; src/exporters/sheets_exporter.py)
     │  In-Memory Knowledge Graph (4,700+ nodes, 1,700+ edges; src/exporters/graph_builder.py)
```

---

## 2. Q1: Scaling to 500,000 Entities Without Manual Intervention

### 2.1 The Honest Supply-Ceiling Reality
Scaling to 500,000 records is bounded by **upstream domain supply**, not infrastructure limits:

| Entity Domain | Current Scraped Sources | Real Supply Ceiling | Scale-to-500k Bottleneck | Required Source Adapters for 500k |
| :--- | :--- | :--- | :--- | :--- |
| **Startups** | Y Combinator Directory | ~5,000 – 8,000 active | YC directory exhausts at ~5k AI startups | OpenCorporates, Crunchbase API, SEC EDGAR Form D |
| **Products** | Curated awesome-* GitHub directories | ~1,500 – 2,500 tools | Curated lists exhaust quickly | GitHub AI Topics, Toolify, Futurepedia, G2 AI |
| **Papers** | ArXiv (cs.AI, cs.LG, cs.CV, cs.CL, cs.NE) + CDN RSS + HF Daily + OpenAlex | ~30,000 – 60,000 papers (filtered to code-linked) | CS preprints grow by ~120/day | OpenAlex (250M works), Semantic Scholar, CrossRef |
| **Jobs** | 5 AI Job Boards | ~300 – 800 fresh / 24h | Strict 24-hour freshness ceiling | Greenhouse/Lever scrapers, LinkedIn Jobs API |
| **News** | 5 AI News RSS Feeds | ~50 – 150 fresh / 24h | Real global editorial output is finite | NewsAPI, GDELT Global Intelligence, Twitter/X Lists |

### 2.2 What Scales in the Codebase Today
1. **Async Coroutine Concurrency**: `AsyncBaseCrawler` (`src/crawlers/base.py:121`) governs concurrent network tasks through an internal `asyncio.Semaphore(15)`.
2. **Client Connection Pooling**: HTTP/2 persistent connections via `httpx.Limits(max_keepalive_connections=20)` (`src/crawlers/base.py:131`) and reusable `CurlAsyncSession` instances (`src/crawlers/base.py:143`) eliminate TCP/TLS handshake latency.
3. **WAL Checkpointing**: Persistent Write-Ahead Logs (`src/cli.py:470`, `src/utils/run_state.py:20`) guarantee resumption across crashes without duplicate work.
4. **Multi-Key Load Balancing**: `MultiTierLLMEngine._acquire_tier_slot` (`src/llm/fallback_chain.py:138`) dynamically balances requests across comma-separated key pools (`src/config.py:64-84`), scaling throughput $N \times$ by aggregating RPM quotas.
5. **Per-Process Vertical Sharding**: The pipeline supports multi-process partitioning by partitioning search queries or year ranges and merging WAL files at completion (`src/crawlers/papers_crawler.py:270-295`).

### 2.3 Measured Subsystem Throughput
| Pipeline Subsystem | Measured / Theoretical Rate | Operational Bottleneck | Time to Ingest 500,000 Entities |
| :--- | :--- | :--- | :--- |
| **ArXiv Ingestion** | ~560,000 papers / hour | 3.2s interval (`papers_crawler.py:34`) with 500 records/batch | ~55 minutes |
| **GitHub Stars** | 1,714 lookups / hour / token | 5,000 req/hr authenticated rate limit | ~29 hours (single token) / ~3.0 hours (10-token pool) |
| **Products Crawl** | ~3,600 products / hour | HTML parsing & DOM extraction | ~5.8 days (single IP; requires proxy pool) |
| **LLM Extraction** | ~3,600 extractions / hour | Tier 2 Groq RPM (30 RPM/key $\times$ pool) | ~5.7 days (or 0s via Tier 4 Deterministic Engine) |

### 2.4 Target Production Architecture for 500k Scale
To scale horizontally across distributed clusters:
- **Redis Streams Distributed URL Queue**: Ingestion workers produce discovery messages into partitioned consumer groups (`papers-stream`, `startups-stream`).
- **Per-Domain Rate Limiter**: Redis token buckets enforce domain-specific pacing (e.g., 3.0s for ArXiv, 1.0s for RemoteOK) across all distributed worker pods.
- **Stateless Crawler Nodes**: Kubernetes worker pods running `AsyncBaseCrawler` consuming from Redis Streams, persisting raw payloads to S3/MinIO, and publishing normalized records to PostgreSQL.

---

## 3. Q2: Preventing HTTP 413 & 429 Failures Across Thousands of Extractions

### 3.1 Implemented Defenses (Current Codebase)
The pipeline implements proactive pre-flight budgeting and multi-tiered failover:

1. **Pre-Flight Token Budgeting (HTTP 413 Prevention)**: `chunk_to_budget()` in `src/llm/chunker.py:35` measures token lengths using `tiktoken` (`cl100k_base`). Prompts exceeding 3,500 tokens are truncated following semantic priority (`Title > Lead > Metadata > Body`), preventing HTTP 413 `Payload Too Large` before dispatch.
2. **Provider Sliding-Window Rate Limiter**: `ProviderRateLimiter` in `src/llm/rate_limiter.py:20` tracks sub-minute request timestamps using sliding locks (`gemini`: 15 RPM, `groq`: 30 RPM, `custom`: 60 RPM). Requests exceeding window limits are delayed or escalated before triggering provider 429s.
3. **Multi-Tier Cascade & Non-Retryable Error Mapping**: `MultiTierLLMEngine` (`src/llm/fallback_chain.py:103`) executes four extraction tiers:
   $$\text{Gemini Flash} \longrightarrow \text{Groq LLaMA 3.3} \longrightarrow \text{OpenAI Gateway} \longrightarrow \text{Deterministic Selectors}$$
   Errors are classified via `_classify_provider_error()` (`src/llm/fallback_chain.py:90`). Non-retryable HTTP 413 errors raise `LLMPayloadError` immediately with zero retry delay.
4. **Exponential Backoff with Jitter**: In `src/llm/fallback_chain.py:73`, `tenacity` retries transient errors using `wait_exponential_jitter(initial=0.5, max=3.0, jitter=0.5)`, preventing thundering herd spikes.
5. **RFC 7231 & Integer Retry-After Compliance**: `_sleep_provider_retry_after()` (`src/llm/fallback_chain.py:60`) parses HTTP 429 `Retry-After` headers in integer seconds or RFC 7231 HTTP dates (`src/utils/date_normalizer.py:214`), honoring upstream backoff before failover.
6. **Crawler Leaky Bucket**: `papers_crawler.py:34` enforces a strict 3.2-second delay between batch queries, respecting ArXiv terms of service.

### 3.2 Production Evidence: Real Quota-Degradation Incident
During live testing, the primary Gemini API key exhausted its daily free quota. The multi-tier engine intercepted HTTP 429s, logged the backoff window, and cascaded extraction to Groq, DeepSeek, and deterministic heuristics without dropping a single entity:
```text
Recorded Telemetry: {'gemini': 0, 'groq': 2, 'deepseek': 6, 'deterministic': 785}
Result: 100% of pipeline records extracted successfully; zero pipeline crashes.
```

### 3.3 Production Distributed Enhancement
For multi-node deployments: Replace the local memory limiter with a **Redis Token Bucket** using Lua scripts for atomic token consumption across worker pods.

---

## 4. Q3: Deduplication & Idempotent Processing Across Distributed Nodes

### 4.1 Current Implementation (Single-Node)
- Local run state persisted to `exports/run_state.json` via atomic write-replace (`src/utils/run_state.py:20-65`).
- In-memory `seen_urls` set filters duplicate URLs within the crawl run (`src/crawlers/base.py:170-195`).

### 4.2 Production Distributed Idempotency
Relying strictly on URL strings fails in real-world scraping due to:
1. **URL Parameter Mutation**: Marketing tags (`?utm_source=...`, `?ref=...`) and session tokens generate distinct URLs for identical content.
2. **Content Syndication**: Identical press releases or wire articles appear simultaneously across TechCrunch, VentureBeat, and Yahoo Finance with entirely different domains.

### 4.3 Target Solution: Content Fingerprinting with Redis Sets
Each worker computes a normalized SHA-256 content fingerprint before extraction:
$$\text{Fingerprint} = \text{SHA-256}\Big(\text{NFKD\_Normalize}(\text{Title}) \;\parallel\; \text{Extract\_Domain}(\text{URL})\Big)$$
- **Atomic Pre-Check**: Worker issues `SADD dedup:global:fingerprints <fingerprint>`.
  - If returns `1`: Record is novel; worker proceeds with extraction.
  - If returns `0`: Record already ingested by another worker; execution aborted immediately.
- **TTL Expiration**: For ephemeral domains (jobs, news), Redis keys carry a 30-day TTL to allow periodic re-indexing while preventing duplicate signals within active windows.

---

## 5. Q4: Primary Database, Vector Indexing, and Graph Storage

### 5.1 Current Architecture
The current pipeline generates:
- Validated Pydantic models (`src/schemas/entities.py`) ensuring strict runtime schema compliance.
- Multi-Tab `.xlsx` workbook via `openpyxl` (`src/exporters/excel_exporter.py`).
- Flat CSV exports for data pipelines (`src/exporters/csv_exporter.py`).
- In-memory NetworkX directed graph (`src/exporters/graph_builder.py`) mapping 4,700+ nodes and 1,700+ edges across entities.

### 5.2 Target Production Storage Architecture

```text
┌──────────────────────────────────────────────────────────────────────────────────┐
│                           PRODUCTION STORAGE TOPOLOGY                            │
├───────────────────┬───────────────────┬───────────────────┬──────────────────────┤
│    PostgreSQL     │       Neo4j       │     pgvector      │     Redis Cluster    │
│  Relational Core  │  Property Graph   │ Vector Similarity │ Queue & Fast Dedup   │
└───────────────────┴───────────────────┴───────────────────┴──────────────────────┘
```

| Engine | Storage Role | Justification vs Alternative |
| :--- | :--- | :--- |
| **PostgreSQL** | Primary relational records, transactional WAL, entity audit logs | **vs MongoDB**: ACID compliance and relational foreign keys prevent orphaned entity records. |
| **Neo4j** | Graph database for multi-hop relationship mapping (`Founder` $\leftrightarrow$ `Startup` $\leftrightarrow$ `Product` $\leftrightarrow$ `Paper` $\leftrightarrow$ `Job`) | **vs SQL Recursive CTEs**: Index-free adjacency graph traversals scale at $O(k)$ depth without recursive join explosions. |
| **pgvector** | Entity name embeddings, semantic article deduplication | **vs Pinecone**: Co-locating vector embeddings directly inside PostgreSQL eliminates dual-write drift and sync lag. |
| **Redis** | Distributed URL queues (Streams), token-bucket rate limits, dedup sets | **vs AWS SQS**: Sub-millisecond latency and atomic Lua script rate-limiting avoid cloud polling overhead. |

---

## 6. Per-Phase Component Mapping

| Phase | Subsystem | Module File | Primary Libraries | Operational Function |
| :--- | :--- | :--- | :--- | :--- |
| **Phase I** | Papers Crawler | `src/crawlers/papers_crawler.py` | `httpx`, `feedparser` | Batch queries ArXiv with 4-tier failover (API → CDN RSS → HF Daily → OpenAlex); fetches live GitHub stars from author-declared repo links. |
| **Phase I** | Products Crawler | `src/crawlers/products_crawler.py` | `httpx`, `regex` | Parses curated product directories (awesome-* GitHub READMEs); LLM-assisted pricing classification with keyword fallback. |
| **Phase I** | Startups Crawler | `src/crawlers/startups_crawler.py` | `httpx` | Ingests Y Combinator API (with teamSize), Hugging Face orgs, GitHub org search. |
| **Phase II** | News Crawler | `src/crawlers/news_crawler.py` | `feedparser`, `trafilatura` | Ingests 5 AI news feeds; enforces strict 24-hour freshness boundary. |
| **Phase II** | Jobs Crawler | `src/crawlers/jobs_crawler.py` | `httpx`, `curl-cffi`, `dateparser` | Ingests 5 job boards; normalizes remote status and role families. |
| **Phase III**| LLM Fallback Engine | `src/llm/fallback_chain.py` | `google-genai`, `openai`, `tenacity` | 4-tier fallback: Gemini $\to$ Groq $\to$ Gateway $\to$ Deterministic. |
| **Phase III**| Token Budgeter | `src/llm/chunker.py` | `tiktoken` | Pre-flight prompt budgeting preventing HTTP 413 context overflows. |
| **Phase III**| Rate Limiter | `src/llm/rate_limiter.py` | `asyncio` | Sliding-window RPM governor protecting provider quotas. |
| **Phase IV** | Entity Resolver | `src/resolution/normalizer.py` | `rapidfuzz`, `unicodedata` | 3-tier matching: Exact $\to$ RapidFuzz token sort $\to$ LLM disambiguation. |
| **Phase V** | Anti-Bot Breaker | `src/crawlers/base.py`, `anti_bot.py` | `curl-cffi`, `camoufox` | TLS fingerprinting & Camoufox browser fallback for Cloudflare-protected sites. |
| **Phase VI** | Multi-Tab Exporter | `src/exporters/excel_exporter.py` | `openpyxl` | Generates 6-tab styled Excel workbook with auto-fitting and header styling. |
| **Phase VI** | Sheets Exporter | `src/exporters/sheets_exporter.py` | `gspread`, `google-auth` | Live upload to Google Sheets with tenacity retry and quota backoff. |
| **Phase VI** | Graph Builder | `src/exporters/graph_builder.py` | `networkx` | Builds in-memory directed knowledge graph; exports metrics and topology. |

---

## 7. Production Roadmap (Documented Design — Planned Not Built)

| Component | Target Architecture | Implementation Blueprint | Status |
| :--- | :--- | :--- | :--- |
| **Distributed Sharding** | Multi-node horizontal workers | Redis Streams message queue with consumer group assignment. | **Planned** (Documented Not Built) |
| **Proxy Rotation Pool** | Residential IP rotation | BrightData / Oxylabs egress proxies rotating on 403 / Cloudflare challenges. | **Planned** (Documented Not Built) |
| **Live Pipeline Watcher** | Real-time SRE telemetry dashboard | Prometheus exporter scraping pipeline gauges; Grafana dashboard for RPM & error rates. | **Planned** (Documented Not Built) |
| **Automated Scheduler** | Continuous incremental indexing | Cron-based Kubernetes CronJob executing hourly delta ingestions with WAL commits. | **Planned** (Documented Not Built) |
