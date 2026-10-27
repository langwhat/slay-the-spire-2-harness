"""Offline configuration inspection entry point."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from sts2_harness.config import ConfigurationError, load_configuration


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's normal error includes arbitrary command-line values.
        raise ConfigurationError("Invalid command line; use sts2 --help.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description="Validate startup configuration offline without exposing secrets.")
    parser.add_argument("command", choices=["check-config"])
    parser.add_argument("--env-file", default=".env", metavar="PATH")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    try:
        arguments = parser.parse_args(argv)
        # argparse is confined to this input boundary; extract concrete values immediately.
        env_file: str = arguments.env_file
        settings: list[str] = arguments.set
        overrides: dict[str, str] = {}
        for setting in settings:
            name, separator, value = setting.partition("=")
            if not separator:
                raise ConfigurationError("Startup overrides must use NAME=VALUE.")
            overrides[name] = value
        config = load_configuration(Path(env_file), os.environ, overrides)
    except ConfigurationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    model = config.model
    # Free-form model IDs, URLs, header names and values can all carry credentials.
    summary = {"driver": config.driver.value, "model_configured": model is not None}
    print(json.dumps(summary))
    if model is not None:
        print(
            json.dumps(
                {
                    "protocol": model.protocol.value,
                    "authentication": model.authentication.kind.value,
                    "endpoint": "[configured; hidden]",
                    "model": "[configured; hidden]",
                    "reasoning_effort_configured": bool(model.reasoning_effort),
                    "service_tier_configured": bool(model.service_tier),
                    "custom_header_count": len(model.custom_headers),
                    "timeout_seconds": model.timeout_seconds,
                    "extra_retries": model.extra_retries,
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
