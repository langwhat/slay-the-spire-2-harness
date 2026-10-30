"""Stage instance-owned audio resources before native gameplay starts.

FMOD owns a native file thread independent of Godot's Dummy audio driver.
"""

import json
import shutil
from pathlib import Path


def configure_audio(engine: Path) -> str:
    directory = engine / "audio"
    shutil.copytree(Path(__file__).with_name("native_audio"), directory)
    manager = json.dumps("*" + (directory / "manager.gd").as_posix())
    return f"\n[Fmod]\nGeneral/auto_initialize=false\n\n[autoload]\nFmodManager={manager}\n"
