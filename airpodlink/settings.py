"""Persisted user settings (%LOCALAPPDATA%\\airpodlink\\settings.json), changed from the tray."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib

PATH = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "airpodlink" / "settings.json"


@dataclasses.dataclass
class Settings:
    codec: str = "sbc"           # sbc (default) | eld (needs optional FDK AAC) | aac
    mic_gain_db: float = 0.0     # fixed gain on the decoded AirPods mic
    connect: bool = True         # False = released: don't page, refuse incoming (iPhone can have them)
    volume_sync: bool = True     # Windows "AirPods Pro" slider/mute <-> AirPods absolute volume

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(PATH.read_text(encoding="utf-8"))
            fields = {f.name for f in dataclasses.fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in fields})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self), indent=2), encoding="utf-8")
        tmp.replace(PATH)
