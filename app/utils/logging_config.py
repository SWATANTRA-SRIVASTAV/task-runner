"""
Structured logging setup.

Uses JSON format in production (easier to ingest into log aggregators)
and plain text in development (easier to read in a terminal).

The LOG_LEVEL env var controls verbosity. Set to DEBUG during development
to see Docker SDK calls and retry decisions.
"""

import logging
import os
import sys


def configure_logging(level: str = "INFO") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Use plain text format locally, JSON-ish in container
    is_container = os.path.exists("/.dockerenv")
    fmt = (
        '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
        if is_container
        else "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    )

    logging.basicConfig(
        level=numeric_level,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    # Silence noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("docker").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
