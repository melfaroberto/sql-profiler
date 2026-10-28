"""Profiles: one folder per application being profiled ("Profile 1", "Invoicing app"…), each with its own
captured history and settings (proxies, schema), so that the work on one application never mixes with another.

    <root>/profiles.json            {"current": "Profile 1"}
    <root>/Profile 1/history.sqlite
    <root>/Profile 1/settings.json
"""
import json
import os
import re
import sqlite3
from pathlib import Path

HISTORY, SETTINGS, STATE = "history.sqlite", "settings.json", "profiles.json"
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')       # not allowed in a folder name (Windows is the strictest)
_NUMBERED = re.compile(r"Profile (\d+)$")


class Profiles:
    def __init__(self, root):
        self.root = Path(root).absolute()

    @staticmethod
    def clean_name(name):
        name = re.sub(r"\s+", " ", _BAD.sub(" ", str(name or ""))).strip(" .")
        return name[:80]

    def paths(self, name):
        folder = self.root / name
        return folder / HISTORY, folder / SETTINGS

    def names(self):
        if not self.root.is_dir():
            return []
        return sorted((d.name for d in self.root.iterdir() if d.is_dir()), key=_sort_key)

    def list(self):
        out = []
        for name in self.names():
            db, _ = self.paths(name)
            item = {"name": name, "folder": str(self.root / name), "sessions": 0, "queries": 0}
            if db.exists():
                try:
                    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as c:
                        item["sessions"] = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                        item["queries"] = c.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
                except sqlite3.Error:
                    pass
            out.append(item)
        return out

    def next_name(self):
        numbers = [int(m.group(1)) for m in map(_NUMBERED.match, self.names()) if m]
        return f"Profile {max(numbers, default=0) + 1}"

    def create(self, name=None, settings=None):
        """Creates the profile folder (with its settings) and returns its name."""
        name = self.clean_name(name) or self.next_name()
        if name.lower() in (n.lower() for n in self.names()):
            raise ValueError(f"a profile called “{name}” already exists")
        folder = self.root / name
        folder.mkdir(parents=True)
        if settings:
            (folder / SETTINGS).write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return name

    def current(self):
        """The profile in use last time; creates "Profile 1" the first time."""
        try:
            name = json.loads((self.root / STATE).read_text(encoding="utf-8")).get("current")
        except (FileNotFoundError, ValueError, AttributeError):
            name = None
        names = self.names()
        if name not in names:
            name = names[0] if names else self.create()
        return name

    def set_current(self, name):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (STATE + ".tmp")
        tmp.write_text(json.dumps({"current": name}), encoding="utf-8")
        os.replace(tmp, self.root / STATE)


def _sort_key(name):
    m = _NUMBERED.match(name)
    return (0, int(m.group(1)), "") if m else (1, 0, name.lower())
