import logging

import pytest


@pytest.fixture(autouse=True)
def _detach_cli_log_handlers():
    """cli.main() attaches handlers bound to pytest's per-test stderr; drop them afterwards."""
    yield
    log = logging.getLogger("tubeaudio")
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
