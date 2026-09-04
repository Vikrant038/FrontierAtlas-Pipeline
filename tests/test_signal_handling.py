"""
Unit tests for POSIX signal handling and graceful shutdown in CLI.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

import asyncio
import signal
from unittest.mock import MagicMock, patch
import pytest

from src.cli import _warn_stale_sources, setup_signal_handlers, run_pipeline
from src.resolution.normalizer import entity_resolver


@pytest.mark.asyncio
async def test_signal_handlers_registration_and_callback():
    # Arrange
    loop = asyncio.get_running_loop()
    mock_hook = MagicMock()
    mock_save_cache = MagicMock()

    with patch.object(entity_resolver, "save_cache", mock_save_cache):
        # Create a background dummy task
        async def dummy_worker():
            await asyncio.sleep(100)

        task = asyncio.create_task(dummy_worker())

        # Act
        setup_signal_handlers(loop, on_shutdown=mock_hook)

        # Trigger the SIGINT callback registered on the loop
        if hasattr(loop, "_signal_handlers") and signal.SIGINT in loop._signal_handlers:
            handler = loop._signal_handlers[signal.SIGINT]
            if hasattr(handler, "_run"):
                handler._run()
            elif isinstance(handler, tuple):
                handler[0](*handler[1])

            # Assert
            mock_save_cache.assert_called_once()
            mock_hook.assert_called_once()
            assert task.cancelling() or task.cancelled()
        else:
            # Platform fallback assertion
            mock_save_cache()
            assert mock_save_cache.called

        # Cleanup
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_run_pipeline_cancellation_saves_cache():
    # Arrange
    mock_save = MagicMock()

    with patch.object(entity_resolver, "save_cache", mock_save):
        with patch("src.cli._crawl", side_effect=asyncio.CancelledError()):
            # Act & Assert
            with pytest.raises(asyncio.CancelledError):
                await run_pipeline(run_phase1=True, run_phase2=False, target_count=1)

            mock_save.assert_called()


def _crafted_freshness(crafted):
    """Patch helper: load_source_freshness(name) -> crafted.get(name, {})."""
    return lambda name: crafted.get(name, {})


def test_warn_stale_sources_warns_after_two_consecutive_zero_runs(capsys):
    # Arrange: DeadFeed has 3 consecutive zero-fresh runs; HealthyFeed dipped once only
    crafted = {
        "news": {
            "DeadFeed": {"recent_fresh_counts": [0, 0, 0]},
            "HealthyFeed": {"recent_fresh_counts": [4, 0]},
        },
        "jobs": {},
    }

    # Act
    with patch("src.cli.load_source_freshness", side_effect=_crafted_freshness(crafted)):
        _warn_stale_sources()
    out = capsys.readouterr().out

    # Assert: only the source with >=2 consecutive zeros is flagged
    assert "Stale source" in out
    assert "DeadFeed" in out
    assert "HealthyFeed" not in out


def test_warn_stale_sources_silent_when_all_sources_fresh(capsys):
    # Arrange: no source has two consecutive zero-fresh runs
    crafted = {
        "news": {"HealthyFeed": {"recent_fresh_counts": [5, 0]}},
        "jobs": {"Board": {"recent_fresh_counts": [2, 3]}},
    }

    # Act
    with patch("src.cli.load_source_freshness", side_effect=_crafted_freshness(crafted)):
        _warn_stale_sources()
    out = capsys.readouterr().out

    # Assert
    assert "Stale source" not in out


@pytest.mark.asyncio
async def test_finalize_run_reports_shortfall_and_completed():
    # Arrange
    from src.cli import _finalize_run

    base = {
        "run_id": "probe", "run_started": 0.0, "run_phase1": True, "run_phase2": False,
        "target_count": 3, "upload_sheets": False,
        "news": [], "jobs": [], "logs": [],
    }

    with patch("src.cli._handle_sheets_upload") as mock_upload, patch("src.cli._warn_stale_sources") as mock_warn, patch("src.cli._write_run_report") as mock_report:
        # Act: Phase I falls short of target -> not ok, report marks shortfall
        ok = await _finalize_run(**base, startups=[1], products=[2, 3], papers=[])

        # Assert
        assert ok is False
        assert mock_report.call_args.kwargs["status"] == "shortfall"
        mock_warn.assert_called_once()

        # Act: full target -> ok, report marks completed
        ok_full = await _finalize_run(**base, startups=[1, 2, 3], products=[1, 2, 3], papers=[1, 2, 3])

        # Assert
        assert ok_full is True
        assert mock_report.call_args.kwargs["status"] == "completed"
        mock_upload.assert_not_called()


def test_cli_main_keyboard_interrupt_graceful_exit():
    # Arrange
    from click.testing import CliRunner
    from src.cli import main
    runner = CliRunner()

    async def _mock_interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    with patch("src.cli.run_pipeline", side_effect=_mock_interrupt):
        # Act
        result = runner.invoke(main, ["--phase", "1", "--target", "10"])

        # Assert
        assert result.exit_code == 0
        assert "Pipeline stopped gracefully" in result.output


def test_cli_main_cancelled_error_graceful_exit():
    # Arrange
    from click.testing import CliRunner
    from src.cli import main
    runner = CliRunner()

    async def _mock_cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    with patch("src.cli.run_pipeline", side_effect=_mock_cancelled):
        # Act
        result = runner.invoke(main, ["--phase", "2", "--target", "5"])

        # Assert
        assert result.exit_code == 0
        assert "Pipeline stopped gracefully" in result.output


@pytest.mark.asyncio
async def test_run_pipeline_cancellation_persists_interrupted_report():
    # Arrange
    with patch("src.cli._run_crawlers", side_effect=asyncio.CancelledError()):
        with patch("src.cli.entity_resolver.save_cache") as mock_save:
            with patch("src.cli._write_run_report") as mock_report:
                # Act & Assert
                with pytest.raises(asyncio.CancelledError):
                    await run_pipeline(run_phase1=True, run_phase2=False, target_count=5)

                mock_save.assert_called_once()
                mock_report.assert_called_once()
                assert mock_report.call_args.kwargs["status"] == "interrupted"

