"""Offline configuration and recorded game evidence inspection."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from sts2_harness.config import ConfigurationError, load_configuration
from sts2_harness.game import OperationKind
from sts2_harness.journal import ArchiveError, Journal, evidence


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's normal error includes arbitrary command-line values.
        raise ConfigurationError("Invalid command line; use sts2 --help.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description="Validate startup configuration offline without exposing secrets.")
    parser.add_argument("command", choices=["check-config", "journal"])
    parser.add_argument("--env-file", default=".env", metavar="PATH")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--view", choices=["timeline", "operation", "model", "request"], default="timeline"
    )
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--around", type=int)
    parser.add_argument("--window", type=int, default=2)
    parser.add_argument("--kind", choices=[kind.value for kind in OperationKind])
    parser.add_argument("--expand", action="store_true")
    parser.add_argument("--export", type=Path)
    try:
        arguments = parser.parse_args(argv)
        command: str = arguments.command
        if command == "journal":
            archive: Path | None = arguments.archive
            view: str = arguments.view
            sequence: int | None = arguments.sequence
            around: int | None = arguments.around
            window: int = arguments.window
            kind: str | None = arguments.kind
            expand: bool = arguments.expand
            destination: Path | None = arguments.export
            if view in ("model", "request"):
                raise ArchiveError(
                    "Model request evidence is unsupported by game/control archives."
                )
            if archive is None:
                raise ArchiveError("Evidence inspection requires --archive PATH.")
            if view == "operation" and sequence is None:
                raise ArchiveError("Operation inspection requires --sequence.")
            output = evidence(
                Journal.open(archive),
                expand=expand,
                kind=OperationKind(kind) if kind is not None else None,
                around=around,
                window=window,
                sequence=sequence,
            )
            text = output.model_dump_json(indent=2)
            if destination is not None:
                try:
                    destination.write_text(text + "\n", encoding="utf-8")
                except OSError:
                    raise ArchiveError("Cannot write the selected evidence export.") from None
            print(text)
            return 0
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
    except ArchiveError as error:
        print(f"Archive error: {error}", file=sys.stderr)
        return 2
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
