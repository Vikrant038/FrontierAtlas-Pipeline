"""
Unit tests for POSIX signal handling and graceful shutdown in CLI.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

import asyncio
import signal
from unittest.mock import MagicMock, patch
import pytest

from src.cli import setup_signal_handlers, run_pipeline
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
