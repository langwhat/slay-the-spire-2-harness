"""Resident gameplay startup, offline configuration, and recorded evidence inspection."""

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from types import FrameType
from typing import NoReturn

from pydantic import ValidationError

from sts2_harness.client.models import DecisionOwner
from sts2_harness.config import Configuration, ConfigurationError, Driver, load_configuration
from sts2_harness.entry_materials import EntryMaterials
from sts2_harness.external_http import run_external
from sts2_harness.game import ControlWriteError, OperationKind
from sts2_harness.headless import HeadlessError, HeadlessOptions, StandardRunRecipe, run_headless
from sts2_harness.interactive import InteractiveOptions, run_interactive
from sts2_harness.journal import ArchiveError, DependencyKind, Journal, StandardCharacter, evidence
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelUnavailable
from sts2_harness.user_input import UserInputQueue, serving_user_input


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's normal error includes arbitrary command-line values.
        raise ConfigurationError("Invalid command line; use sts2 --help.")


@asynccontextmanager
async def _user_input(port: int | None) -> AsyncGenerator[UserInputQueue | None]:
    if port is None:
        yield None
        return
    async with serving_user_input(port) as queue:
        yield queue


@contextmanager
def _stop_on_signal() -> Generator[asyncio.Event]:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop.set)

    with ExitStack() as signals:
        for name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(name, stop.set)
            except (AttributeError, NotImplementedError):
                previous = signal.signal(name, request_stop)
                if previous is not None:
                    signals.callback(signal.signal, name, previous)
            else:
                signals.callback(loop.remove_signal_handler, name)
        yield stop


async def _start(
    options: InteractiveOptions,
    configuration: Configuration,
    tool_port: int,
    materials: EntryMaterials | None,
    input_port: int | None,
) -> None:
    with _stop_on_signal() as stop:
        if configuration.driver is Driver.EXTERNAL:
            await run_external(options, configuration, tool_port=tool_port, stop_requested=stop)
        else:
            async with _user_input(input_port) as user_input:
                await run_interactive(
                    options,
                    configuration,
                    stop_requested=stop,
                    materials=materials,
                    user_input=user_input,
                )


async def _headless(
    options: HeadlessOptions,
    configuration: Configuration,
    materials: EntryMaterials | None,
    input_port: int | None,
) -> None:
    with _stop_on_signal() as stop:
        async with _user_input(input_port) as user_input:
            await run_headless(
                options,
                configuration,
                stop_requested=stop,
                materials=materials,
                user_input=user_input,
            )


def _build_parser() -> _Parser:
    parser = _Parser(
        description="Attach a resident backend or inspect configuration and evidence safely."
    )
    parser.add_argument("command", choices=["start", "headless", "check-config", "journal"])
    parser.add_argument("--env-file", default=".env", metavar="PATH")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--mod-url", default="http://127.0.0.1:17877", metavar="URL")
    parser.add_argument("--tool-port", type=int, default=17878, metavar="PORT")
    parser.add_argument("--input-port", type=int, default=17879, metavar="PORT")
    parser.add_argument("--output", type=Path, default=Path("runs"), metavar="DIRECTORY")
    parser.add_argument("--game", type=Path, metavar="EXECUTABLE")
    parser.add_argument("--template", type=Path, metavar="MANIFEST")
    parser.add_argument("--instance", type=Path, metavar="NEW_DIRECTORY")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--character", choices=[item.value for item in StandardCharacter], default="SILENT"
    )
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--seed")
    parser.add_argument(
        "--minimal", action="store_true", help="Play with current decision context and fewer tools."
    )
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--view", choices=["timeline", "operation", "model", "request"], default="timeline"
    )
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--around", type=int)
    parser.add_argument("--window", type=int, default=2)
    parser.add_argument(
        "--kind", choices=[kind.value for kind in (*OperationKind, *DependencyKind)]
    )
    parser.add_argument("--role", choices=[role.value for role in DecisionOwner])
    parser.add_argument("--node")
    parser.add_argument("--errors-only", action="store_true")
    parser.add_argument("--expand", action="store_true")
    parser.add_argument("--export", type=Path)
    return parser


def _inspect_evidence(arguments: argparse.Namespace) -> int:
    archive: Path | None = arguments.archive
    view: str = arguments.view
    sequence: int | None = arguments.sequence
    around: int | None = arguments.around
    window: int = arguments.window
    kind: str | None = arguments.kind
    expand: bool = arguments.expand
    role: str | None = arguments.role
    node_id: str | None = arguments.node
    errors_only: bool = arguments.errors_only
    destination: Path | None = arguments.export
    if archive is None:
        raise ArchiveError("Evidence inspection requires --archive PATH.")
    if view == "operation" and sequence is None:
        raise ArchiveError("Operation inspection requires --sequence.")
    reader = Journal.open(archive)
    if view in ("model", "request"):
        selected_kind = DependencyKind.MODEL
    elif view == "operation":
        assert sequence is not None
        selected_kind = reader.operation(sequence).request.kind
    elif kind in {item.value for item in DependencyKind}:
        selected_kind = DependencyKind(kind)
    else:
        selected_kind = OperationKind(kind) if kind is not None else None
    output = evidence(
        reader,
        expand=expand,
        kind=selected_kind,
        around=around,
        window=window,
        sequence=sequence,
        role=DecisionOwner(role) if role is not None else None,
        node_id=node_id,
        errors_only=errors_only,
    )
    text = output.model_dump_json(indent=2)
    if destination is not None:
        try:
            destination.write_text(text + "\n", encoding="utf-8")
        except OSError:
            raise ArchiveError("Cannot write the selected evidence export.") from None
    print(text)
    return 0


def _overrides(settings: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for setting in settings:
        name, separator, value = setting.partition("=")
        if not separator:
            raise ConfigurationError("Startup overrides must use NAME=VALUE.")
        overrides[name] = value
    return overrides


def _headless_options(arguments: argparse.Namespace) -> HeadlessOptions:
    game: Path | None = arguments.game
    template: Path | None = arguments.template
    if game is None or template is None:
        raise ConfigurationError("Headless startup requires --game and --template.")
    instance: Path | None = arguments.instance
    port: int | None = arguments.port
    character: str = arguments.character
    ascension: int = arguments.ascension
    seed: str | None = arguments.seed
    try:
        recipe = StandardRunRecipe(
            character=StandardCharacter(character), ascension=ascension, seed=seed
        )
    except ValidationError:
        raise ConfigurationError("Invalid standard headless run recipe.") from None
    headless_output: Path = arguments.output
    return HeadlessOptions(game, template, headless_output, instance, port, recipe)


def _print_configuration_summary(config: Configuration) -> None:
    model = config.model
    # Free-form model IDs, URLs, header names and values can all carry credentials.
    summary = {
        "driver": config.driver.value,
        "model_configured": model is not None,
        "minimal": config.minimal,
    }
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


def main(argv: Sequence[str] | None = None, *, materials: EntryMaterials | None = None) -> int:
    parser = _build_parser()
    try:
        arguments = parser.parse_args(argv)
        command: str = arguments.command
        minimal: bool = arguments.minimal
        if minimal and command not in ("start", "headless"):
            raise ConfigurationError("Minimal mode requires start or headless.")
        if command == "journal":
            return _inspect_evidence(arguments)
        # argparse is confined to this input boundary; extract concrete values immediately.
        env_file: str = arguments.env_file
        settings: list[str] = arguments.set
        overrides = _overrides(settings)
        if minimal:
            overrides["STS2_MINIMAL"] = "true"
        config = load_configuration(Path(env_file), os.environ, overrides)
        input_port: int | None = arguments.input_port if config.user_input else None
        if command == "headless":
            asyncio.run(_headless(_headless_options(arguments), config, materials, input_port))
            return 0
        if command == "start":
            if materials is not None and config.driver is Driver.EXTERNAL:
                raise ConfigurationError("Entry materials require builtin driving.")
            mod_url: str = arguments.mod_url
            output_directory: Path = arguments.output
            tool_port: int = arguments.tool_port
            asyncio.run(
                _start(
                    InteractiveOptions(mod_url, output_directory),
                    config,
                    tool_port,
                    materials,
                    input_port,
                )
            )
            return 0
        if command == "check-config":
            if config.model is not None:
                try:
                    Llm.validate_configuration(config.model)
                except ModelUnavailable as error:
                    raise ConfigurationError(str(error)) from None
            _print_configuration_summary(config)
        return 0
    except HeadlessError as error:
        print(f"Headless error: {error}", file=sys.stderr)
        return 2
    except ControlWriteError:
        print(
            "Control error: The Mod did not confirm Pause; the backend stopped.",
            file=sys.stderr,
        )
        return 2
    except ArchiveError as error:
        print(f"Archive error: {error}", file=sys.stderr)
        return 2
    except ConfigurationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
