"""
Unit tests for AsyncBaseCrawler / TargetedCrawler core internals not exercised
through crawler-level tests: GitHub quota classification, fetch_tls status paths,
close()/WAL edge cases, escalation failure branches, and add/recover semantics.
"""

import json
from types import SimpleNamespace

import pytest

from src.crawlers import base
from src.crawlers.base import (
    AsyncBaseCrawler,
    BotBlockedError,
    TargetedCrawler,
    TransientNetworkError,
    github_headers,
    is_github_quota_error,
)


class _ConcreteCrawler(AsyncBaseCrawler):
    async def crawl(self):
        return []


# ---------------------------------------------------------------------------
# 1. Module-level helpers
# ---------------------------------------------------------------------------


def test_github_headers_with_and_without_token():
    assert "Authorization" not in github_headers(None)
    assert github_headers("gh_token")["Authorization"] == "Bearer gh_token"


@pytest.mark.parametrize(
    "message, expected",
    [
        ("HTTP 429 Too Many Requests", True),
        ("403 API rate limit exceeded", True),
        ("403 quota exhausted", True),
        ("403 Repository access blocked", False),   # plain 403 must NOT disable enrichment
        ("500 internal error", False),
    ],
)
def test_is_github_quota_error_messages(message, expected):
    assert is_github_quota_error(Exception(message)) is expected


# ---------------------------------------------------------------------------
# 2. Escalation failure branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_no_tls_fallback_re_raises(monkeypatch):
    crawler = _ConcreteCrawler()

    async def blocked(*args, **kwargs):
        raise BotBlockedError("403")

    monkeypatch.setattr(crawler, "_request", blocked)
    with pytest.raises(BotBlockedError):
        await crawler.fetch("https://example.com", allow_tls_fallback=False)
    await crawler.close()


@pytest.mark.asyncio
async def test_escalate_camoufox_import_error_path(monkeypatch):
    import builtins

    crawler = _ConcreteCrawler()
    real_import = builtins.__import__

    def no_camoufox(name, *args, **kwargs):
        if name == "camoufox" or name.startswith("camoufox."):
            raise ImportError("camoufox is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_camoufox)
    with pytest.raises(BotBlockedError, match="not installed"):
        await crawler._escalate_camoufox("https://example.com")
    await crawler.close()


@pytest.mark.asyncio
async def test_escalate_camoufox_success_json_and_challenge(monkeypatch):
    # Inject a fake camoufox module: browser -> page -> content.
    class FakePage:
        async def goto(self, url, timeout=None):
            pass

        async def content(self):
            return self._content

    class FakeBrowser:
        def __init__(self, content):
            self._content = content

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def new_page(self):
            return FakePage()

    class FakeAsyncCamoufox:
        def __init__(self, *args, **kwargs):
            self._content = kwargs.pop("_content", "<html>ok</html>")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def new_page(self):
            page = FakePage()
            page._content = self._content
            return page

    fake_mod = SimpleNamespace(async_api=SimpleNamespace(AsyncCamoufox=FakeAsyncCamoufox))
    monkeypatch.setitem(__import__("sys").modules, "camoufox", SimpleNamespace())
    monkeypatch.setitem(__import__("sys").modules, "camoufox.async_api", fake_mod.async_api)

    crawler = _ConcreteCrawler()
    before = AsyncBaseCrawler.escalation_successes

    # Plain content -> text returned, counter incremented
    out = await crawler._escalate_camoufox("https://example.com")
    assert "<html>ok</html>" in out
    assert AsyncBaseCrawler.escalation_successes == before + 1

    # JSON content requested -> fail fast without a browser launch (the browser
    # tier renders HTML; JSON parsing of a page can never succeed).
    crawler2 = _ConcreteCrawler()
    with pytest.raises(BotBlockedError, match="browser tier"):
        await crawler2._escalate_camoufox("https://example.com", as_json=True)

    # Challenge page -> BotBlockedError (not counted as a success)
    crawler3 = _ConcreteCrawler()
    fake_mod.async_api.AsyncCamoufox = lambda *a, **k: FakeAsyncCamoufox(
        _content="<html><title>Attention Required</title><body>cf-challenge</body></html>"
    )
    with pytest.raises(BotBlockedError, match="challenge"):
        await crawler3._escalate_camoufox("https://example.com")
    await crawler.close()
    await crawler2.close()
    await crawler3.close()


# ---------------------------------------------------------------------------
# 3. fetch_tls status paths (fake curl session, no network)
# ---------------------------------------------------------------------------


class _FakeCurlResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeCurlSession:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self._closed = False

    async def get(self, url, params=None, headers=None, timeout=None):
        if self._error is not None:
            raise self._error
        return self._result

    async def close(self):
        self._closed = True


@pytest.mark.asyncio
async def test_fetch_tls_status_paths(monkeypatch):
    sleeps = []
    monkeypatch.setattr(base.asyncio, "sleep", lambda s: _noop_sleep(sleeps, s))
    crawler = _ConcreteCrawler()

    # Success
    async def session_ok():
        return _FakeCurlSession(_FakeCurlResponse(200, "ok"))

    monkeypatch.setattr(crawler, "get_curl_session", session_ok)
    assert await crawler.fetch_tls("https://example.com") == "ok"

    # 403 -> BotBlockedError (never retried)
    async def session_403():
        return _FakeCurlSession(_FakeCurlResponse(403))

    monkeypatch.setattr(crawler, "get_curl_session", session_403)
    with pytest.raises(BotBlockedError):
        await crawler.fetch_tls("https://example.com")

    # 500 -> TransientNetworkError (retry sleeps suppressed for test speed)
    async def session_500():
        return _FakeCurlSession(_FakeCurlResponse(500))

    monkeypatch.setattr(crawler, "get_curl_session", session_500)
    with pytest.raises(TransientNetworkError):
        await crawler.fetch_tls("https://example.com")

    # Generic transport exception -> wrapped as TransientNetworkError
    async def session_boom():
        return _FakeCurlSession(error=RuntimeError("boom"))

    monkeypatch.setattr(crawler, "get_curl_session", session_boom)
    with pytest.raises(TransientNetworkError):
        await crawler.fetch_tls("https://example.com")
    await crawler.close()


async def _noop_sleep(sleeps, seconds):
    sleeps.append(seconds)


# ---------------------------------------------------------------------------
# 4. close() error paths and context-manager protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_handles_curl_session_failure(monkeypatch):
    crawler = _ConcreteCrawler()

    class _BoomSession:
        async def close(self):
            raise RuntimeError("close failed")

    crawler._curl_session = _BoomSession()
    await crawler.close()  # must not raise
    assert crawler._curl_session is None


@pytest.mark.asyncio
async def test_context_manager_protocol():
    crawler = _ConcreteCrawler()
    async with crawler as entered:
        assert entered is crawler
    # __aexit__ invoked close(); double close is a no-op
    await crawler.close()


# ---------------------------------------------------------------------------
# 5. TargetedCrawler WAL / add / recover edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_targeted_default_crawl_truncates_to_target(tmp_path):
    crawler = TargetedCrawler(target_count=2)
    crawler.collected = ["a", "b", "c"]
    assert await crawler.crawl() == ["a", "b"]


def test_add_seen_duplicate_and_full():
    crawler = TargetedCrawler(target_count=2)
    assert crawler.add("Key", "item-1") is False
    assert crawler.add("KEY", "item-1-dup") is False  # duplicate key (case-insensitive)
    assert crawler.collected == ["item-1"]
    assert crawler.add("Key2", "item-2") is True  # reached quota
    assert crawler.add("Key3", "item-3") is True  # full: no append


def test_add_wal_disabled_writes_nothing(tmp_path, monkeypatch):
    crawler = TargetedCrawler(target_count=5, wal_enabled=False)
    wrote = []

    def spy(key, item):
        wrote.append(key)

    monkeypatch.setattr(crawler, "_write_wal", spy)
    crawler.add("k1", "v1")
    assert wrote == []


def test_pick_github_token_multi_key_and_exhaustion(tmp_path, monkeypatch):
    crawler = TargetedCrawler(target_count=5)
    monkeypatch.setattr(crawler, "github_tokens", ["t1", "t2", "t3"])
    picks = {crawler._pick_github_token(f"k{i}") for i in range(20)}
    assert picks == {"t1", "t2", "t3"}  # all tokens reachable via hash rotation

    crawler._exhausted_github_tokens = {"t1", "t2", "t3"}
    assert crawler._pick_github_token("x") is None  # all exhausted -> anonymous

    crawler._exhausted_github_tokens = {"t1", "t3"}
    assert crawler._pick_github_token("x") == "t2"  # single survivor shortcut


def test_wal_write_serialization_variants(tmp_path):
    # pydantic model_dump path + old-style .dict() + plain object fallback
    wal = str(tmp_path / "wal.jsonl")

    class _DictItem:
        def dict(self):
            return {"kind": "dict-item"}

    c1 = TargetedCrawler(target_count=10, wal_path=wal)
    from src.schemas.entities import SourceMetadata

    c1.add("p", SourceMetadata(name="n", url="u"))
    c2 = TargetedCrawler(target_count=10, wal_path=wal)
    c2.add("d", _DictItem())
    c3 = TargetedCrawler(target_count=10, wal_path=wal)
    c3.add("r", {"plain": [1, 2]})

    lines = [json.loads(l) for l in open(wal).read().splitlines()]
    assert lines[0]["data"]["name"] == "n"
    assert lines[1]["data"] == {"kind": "dict-item"}
    assert lines[2]["data"] == {"plain": [1, 2]}
    c1.close_wal()
    c2.close_wal()
    c3.close_wal()


def test_write_wal_error_is_swallowed(tmp_path):
    wal = str(tmp_path / "sub" / "wal.jsonl")
    from pathlib import Path

    Path(wal).parent.mkdir(parents=True, exist_ok=True)
    crawler = TargetedCrawler(target_count=5, wal_path=wal)
    # Force the file handle to a closed object so the write raises
    crawler._wal_file = open(wal, "a")
    crawler._wal_file.close()
    crawler.add("k", {"x": 1})  # must not raise
    crawler.close_wal()


def test_reset_wal_if_complete_truncates(tmp_path):
    wal = str(tmp_path / "wal.jsonl")
    open(wal, "w").write("line\n")
    crawler = TargetedCrawler(target_count=1, wal_path=wal)
    crawler.collected = ["one"]
    crawler.reset_wal_if_complete()
    assert open(wal).read() == ""
    crawler.close_wal()


def test_reset_wal_oserror_swallowed(tmp_path):
    # wal_path pointing at a directory makes truncation raise OSError -> warning
    crawler = TargetedCrawler(target_count=1, wal_path=str(tmp_path))
    crawler.collected = ["one"]
    crawler.reset_wal_if_complete()  # must not raise
    crawler.close_wal()


def test_recover_from_wal_edge_lines(tmp_path):
    wal = str(tmp_path / "wal.jsonl")
    # Blank line, corrupt JSON line, duplicate of an already-seen key, valid pydantic
    # entry, and an entry whose model validation fails (falls back to raw data).
    from src.schemas.entities import SourceMetadata

    valid = SourceMetadata(name="n", url="u").model_dump(mode="json")
    open(wal, "w").write(
        "\n"
        "{corrupt-not-json}\n"
        + json.dumps({"key": "k1", "data": {"x": 1}}) + "\n"
        + json.dumps({"key": "k2", "data": valid}) + "\n"
        + json.dumps({"key": "k3", "data": {"bad": "payload"}}) + "\n"
    )

    class _StrictModel:
        @staticmethod
        def model_validate(data):
            if "name" not in data:
                raise ValueError("missing name")
            return SourceMetadata(**data)

    crawler = TargetedCrawler(target_count=10, wal_path=wal)
    crawler.seen_keys.add("k1")  # already-seen -> skipped
    count = crawler.recover_from_wal(model_cls=_StrictModel)
    assert count == 2  # k2 validated, k3 fell back to raw dict
    assert isinstance(crawler.collected[0], SourceMetadata)
    assert crawler.collected[1] == {"bad": "payload"}
    crawler.close_wal()


def test_recover_from_wal_read_error_swallowed(tmp_path):
    # wal_path is a directory -> open() for reading raises OSError -> logged, 0 recovered
    crawler = TargetedCrawler(target_count=5, wal_path=str(tmp_path))
    assert crawler.recover_from_wal() == 0
    crawler.close_wal()


def test_recover_from_wal_skips_when_full(tmp_path):
    wal = str(tmp_path / "wal.jsonl")
    open(wal, "w").write(json.dumps({"key": "a", "data": 1}) + "\n" + json.dumps({"key": "b", "data": 2}) + "\n")
    crawler = TargetedCrawler(target_count=1, wal_path=wal)
    crawler.collected = ["already-full"]
    assert crawler.recover_from_wal() == 0  # is_full short-circuits before reading
    crawler.close_wal()


def test_rewrite_wal_compaction_and_oserror(tmp_path):
    good_wal = str(tmp_path / "good.jsonl")
    crawler = TargetedCrawler(target_count=5, wal_path=good_wal)
    crawler._rewrite_wal([{"key": "a", "data": 1}, {"key": "b", "data": 2}])
    assert len(open(good_wal).read().splitlines()) == 2
    crawler.close_wal()

    # Directory as wal_path -> OSError -> warning, no raise
    bad = TargetedCrawler(target_count=5, wal_path=str(tmp_path))
    bad._rewrite_wal([{"key": "a", "data": 1}])
    bad.close_wal()
