import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Libraries whose errors must never reach the console. Their failures are
# infrastructure noise rather than something the user typed a request about,
# and printing them mid-prompt corrupts the interactive line the user is
# still editing. They are still written to the session log file in full.
_CONSOLE_SILENCED_PREFIXES = ("opentelemetry",)


def _console_filter(record: logging.LogRecord) -> bool:
    """Keep third-party infrastructure errors off the interactive console.

    Args:
        record: The log record about to be emitted.

    Returns:
        False when the record comes from a silenced library, True otherwise.
    """
    return not record.name.startswith(_CONSOLE_SILENCED_PREFIXES)


def generate_session_id() -> str:
    """Generate a timestamp+uuid id shared by the log file and chat file.

    The timestamp is UTC, matching the UTC timestamps on every log line.

    Returns:
        A string like "20260723_140211_a1b2c3d4".
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short_id = uuid.uuid4().hex[:8]
    return f"{timestamp}_{short_id}"


def init_logger(log_directory: str, session_id: str) -> logging.Logger:
    """Configure the root logger with a per-session file handler and an error-level stream handler.

    Args:
        log_directory: Directory where log files are stored.
        session_id: Shared timestamp+uuid id used to name the log file.

    Returns:
        The configured root logger.
    """
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"log_{session_id}.log"

    formatter = logging.Formatter(
        f"%(asctime)s.%(msecs)03dZ {session_id} %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime

    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.ERROR)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(_console_filter)

    logger = logging.getLogger()
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    for noisy_logger in ("httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    return logger


@contextmanager
def measure_duration() -> Iterator[Callable[[], int]]:
    """Measure the wall-clock duration of a block.

    Yields:
        A callable returning the elapsed time in whole milliseconds.
    """
    start = time.perf_counter()
    yield lambda: int((time.perf_counter() - start) * 1000)
