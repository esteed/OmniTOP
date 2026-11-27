"""OmniTOP's settings file: the remote machines to watch.

The app writes it (the Remote tab's Add and Edit forms); nobody needs to edit it
by hand. It's JSON, in %LOCALAPPDATA%\\PortTrafficViz\\settings.json:

    {"machines": [{"id": "m1", "name": "File server", "address": "192.168.1.30",
                   "port": 7820, "os": "linux"}],
     "discover": true, "ignored": ["192.168.1.44:7820"]}

name may be empty (the machine's own host name is shown) and os is what the
person said it runs: "windows", "linux" or "" (the agent says what it really is).
discover says whether to look for agents on the local networks (discover.py), and
ignored lists the ones found that aren't to be offered again.

The first time, the machines in CPU Graph's settings.ini are taken over, since
earlier versions of this app read them from there.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading

AGENT_PORT = 7820
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PortTrafficViz")
PATH = os.path.join(APP_DIR, "settings.json")
CPU_GRAPH = os.path.join(os.environ.get("LOCALAPPDATA") or "", "CpuGraph", "settings.ini")
SYSTEMS = ("", "windows", "linux")
MAX_NAME = 64
MAX_IGNORED = 500

_HOST = re.compile(r"(?=.{1,253}$)(?!-)[A-Za-z0-9_-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9_-]{1,63}(?<!-))*\.?")


class Invalid(ValueError):
    """A field that can't be used: args are the field and what's wrong, for the form to show."""


def parse_endpoint(target: str) -> tuple[str, int]:
    """host, host:port, [v6addr] or [v6addr]:port, as CPU Graph's settings write them."""
    host, port_text = target.strip(), None
    if host.startswith("["):
        close = host.find("]")
        if close > 0:
            rest, host = host[close + 1:], host[1:close]
            if rest.startswith(":"):
                port_text = rest[1:]
    elif host.count(":") == 1:
        host, port_text = host.split(":")
    port = int(port_text) if port_text and port_text.isdigit() and 0 < int(port_text) < 65536 else AGENT_PORT
    return host, port


def clean(fields: dict) -> dict:
    """A machine's fields, checked. Raises Invalid with the field that's wrong."""
    if not isinstance(fields, dict):
        raise Invalid("address", "Nothing to save.")
    name = " ".join(str(fields.get("name") or "").split())
    if len(name) > MAX_NAME or not name.isprintable():
        raise Invalid("name", f"Use up to {MAX_NAME} ordinary characters.")
    address = str(fields.get("address") or "").strip()
    if address.startswith("[") and address.endswith("]"):
        address = address[1:-1]
    if not address:
        raise Invalid("address", "Enter the machine's host name or IP address.")
    try:
        ipaddress.ip_address(address.split("%", 1)[0])
        if not re.fullmatch(r"[0-9A-Fa-f:.]+(%[A-Za-z0-9_.-]+)?", address):
            raise ValueError
    except ValueError:
        looks_v4 = re.fullmatch(r"[0-9.]+", address)      # 300.1.1.1 isn't a host name either
        if looks_v4 or not _HOST.fullmatch(address):
            raise Invalid("address", "That isn't a host name or IP address.") from None
    port = fields.get("port", AGENT_PORT)
    if isinstance(port, str):
        port = port.strip()
        port = AGENT_PORT if not port else int(port) if port.isascii() and port.isdigit() and len(port) < 6 else 0
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise Invalid("port", "Use a port from 1 to 65535.")
    system = str(fields.get("os") or "").lower()
    if system not in SYSTEMS:
        raise Invalid("os", "Choose Windows or Linux.")
    return {"name": name, "address": address, "port": port, "os": system}


def cpu_graph_machines(path: str | None = None) -> list[dict]:
    """The remotes= in CPU Graph's settings.ini."""
    try:
        with open(path or CPU_GRAPH, encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    targets: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("["):
            break                                          # only the top of the file names machines
        key, eq, value = line.partition("=")
        if eq and key.strip().lower() == "remotes" and value.strip().lower() not in ("auto", "none"):
            targets = [t for t in re.split(r"[;,\s]+", value) if t]
    out = []
    for t in targets:
        host, port = parse_endpoint(t)
        try:
            out.append(clean({"address": host, "port": port}))
        except Invalid:
            pass
    return out


class Settings:
    """The machines to watch, kept in the file. path=None keeps them in memory only (the demo)."""

    def __init__(self, path: str | None = PATH, machines: list[dict] | None = None) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.machines: list[dict] = []
        self.problem = ""                                  # why the file can't be used, if it can't
        self._discover = True
        self._ignored: list[str] = []
        if path is None:
            for m in machines or []:
                self._append(clean(m))
        else:
            self._load()

    def _append(self, m: dict) -> dict:
        n = 1
        used = {x["id"] for x in self.machines}
        while f"m{n}" in used:
            n += 1
        m = {"id": f"m{n}", **m}
        self.machines.append(m)
        return m

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data["machines"]
            if not isinstance(entries, list):
                raise ValueError("machines isn't a list")
        except FileNotFoundError:
            for m in cpu_graph_machines():
                self._append(m)
            if self.machines:
                try:
                    self._save()
                except OSError:
                    pass                                   # they're taken over again next time
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Keep what's there for the person to rescue, rather than writing over it.
            self.problem = f"{self.path} can't be read ({exc}). It's kept as settings.json.bad."
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            return
        self._discover = data.get("discover") is not False
        self._ignored = [str(x)[:64] for x in data.get("ignored") or [] if isinstance(x, str)][-MAX_IGNORED:]
        for e in entries:
            try:
                m = clean(e)
            except Invalid:
                continue
            mid = str(e.get("id") or "")
            if re.fullmatch(r"m\d{1,6}", mid) and all(mid != x["id"] for x in self.machines):
                self.machines.append({"id": mid, **m})
            else:
                self._append(m)

    def _save(self) -> None:
        if self.path is None:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temp = self.path + ".new"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump({"machines": self.machines, "discover": self._discover, "ignored": self._ignored}, f,
                      indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(temp, self.path)                        # whole or not at all

    def _same(self, m: dict, but: str = "") -> None:
        for x in self.machines:
            if x["id"] != but and (x["address"].lower(), x["port"]) == (m["address"].lower(), m["port"]):
                raise Invalid("address", f"{x['name'] or x['address']} already uses this address and port.")

    def _change(self, machines: list[dict]) -> None:
        before, self.machines = self.machines, machines
        try:
            self._save()
        except OSError as exc:
            self.machines = before
            raise Invalid("", f"The settings couldn't be saved: {exc.strerror or exc}") from None

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(m) for m in self.machines]

    def discover(self) -> bool:
        return self._discover

    def ignored(self) -> list[str]:
        with self.lock:
            return list(self._ignored)

    def set_discover(self, on: bool) -> None:
        with self.lock:
            self._discover = bool(on)
            self._keep()

    def ignore(self, endpoint: str) -> None:
        with self.lock:
            if endpoint not in self._ignored:
                self._ignored = [*self._ignored, endpoint[:64]][-MAX_IGNORED:]
                self._keep()

    def _keep(self) -> None:
        try:
            self._save()
        except OSError:
            pass                                           # kept for this run, anyway

    def add(self, fields: dict) -> dict:
        m = clean(fields)
        with self.lock:
            self._same(m)
            used = {x["id"] for x in self.machines}
            n = next(i for i in range(1, len(used) + 2) if f"m{i}" not in used)
            m = {"id": f"m{n}", **m}
            self._change([*self.machines, m])
        return dict(m)

    def update(self, mid: str, fields: dict) -> dict:
        m = clean(fields)
        with self.lock:
            if all(x["id"] != mid for x in self.machines):
                raise Invalid("", "That machine isn't in the list any more.")
            self._same(m, but=mid)
            m = {"id": mid, **m}
            self._change([m if x["id"] == mid else x for x in self.machines])
        return dict(m)

    def remove(self, mid: str) -> None:
        with self.lock:
            if all(x["id"] != mid for x in self.machines):
                raise Invalid("", "That machine isn't in the list any more.")
            self._change([x for x in self.machines if x["id"] != mid])

    def move(self, mid: str, by: int) -> None:
        """Up (by < 0) or down the list."""
        with self.lock:
            i = next((k for k, x in enumerate(self.machines) if x["id"] == mid), -1)
            if i < 0:
                raise Invalid("", "That machine isn't in the list any more.")
            rest = [x for x in self.machines if x["id"] != mid]
            rest.insert(max(0, min(len(rest), i + by)), self.machines[i])
            self._change(rest)
