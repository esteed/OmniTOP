"""OmniTOP's agent: lets OmniTOP on other PCs watch this Windows PC.

    omniTop.exe --agent              until it's closed
    omniTop.exe --install-agent      starts with Windows (asks for administrator rights)
    omniTop.exe --uninstall-agent    removes it again

It speaks CPU Graph's agent protocol (version 2), the one cpugraph-agent speaks
on Linux: plain text lines over TCP.

    agent -> viewer, once:  cpugraph-agent 2 host=<name> cpus=<n> kernel=<build> version=<v> os=windows omni=1
    agent -> viewer, once:  have net <adapter>...  and  have disk <disk>...
    viewer -> agent, any time:  interval <ms>      (10..10000; until then 1000)
    viewer -> agent, any time:  net auto | net <adapter>...  and  disk auto | disk <disk>...
    agent -> viewer, at the start and whenever the columns change:
        label net <adapter> <text>     its name as Windows shows it
        nets <adapter>:<link>...       link = Mb/s (0 = unknown), down, or missing
        label disk <disk> <text>
        about <disk> <bytes> <text>    its size and volume name
        disks <disk>[:missing]...
    agent -> viewer, each sample:
        s <ms> <all %> <cpu0 %>... <rx B/s> <tx B/s>... <read B/s> <write B/s> <busy %>...
    agent -> viewer, before closing:  err <reason>

OmniTOP's agents say omni=1 or more and add to it (omnitop-agent.py on Linux does the same):

    agent -> viewer, once:  sys {"os": "windows", "system": "Windows 11 Pro 25H2"}
    viewer -> agent, any time:  net all            every adapter that has had a link, virtual ones too
    viewer -> agent, any time:  disk all           every drive letter Windows has activity figures for
    viewer -> agent, any time:  mem <ms>           memory this often (250..60000), or 0 for none
    agent -> viewer:
        kind net <adapter> virtual|card
        usage <disk> <bytes used> <bytes>
        silent ["F: Cloud Drive"]      drives there are no activity figures for
        mem {...}                      as sysmon.LiveSystem.sample returns it

So that they seldom need replacing, agents that say omni=2 don't decide what's worth
showing: they send whatever raw figures a viewer asks for, and it makes sense of them.

    viewer -> agent, any time:  feed <name> sample {...}    with each sample
                                feed <name> <ms> {...}      this often (250 or more)
                                feed <name> off
    agent -> viewer:            feed <name> {"t": <ms>, ...}

Their hello says what feeds can hold (can=feed,counters,diskperf,drives here; see
omnitop-agent.py for Linux):

    counters   ["\\PhysicalDisk(*)\\Disk Reads/sec"]: performance counters of this PC, by
               their English names: {path: {instance: value}}
    diskperf   ["C:"] or ["*"]: drive letters' running totals, as perfmon.disk_performance
               returns them
    drives     ["*"]: {"C:": [type, label, bytes used, bytes]}

and, when this PC lets the agent watch its network, ports:

    viewer -> agent:  ports <ms>|off       traffic per port this often (100..10000)
                      probes on|off        packets to ports here that may be part of a scan
                      port <n>             the processes and connections on a port
    agent -> viewer:  portsinfo {"dynamic": [first, last], "udp": true, "error": ""}   before the first ports
                      ports {"dt", "t", "in", "out", "si", "so", "p", "s"}   as OmniTOP's own ticks
                      probes {"t": <now>, "p": [[time, remote, port, 0 TCP | 1 UDP, "open"|"closed",
                                                 remote port, local address]...]}
                      port {...}           as netwatch.describe returns it

Probes are the ones that aren't answers to this PC (netwatch.ProbeFilter): which of them make a
scan is for the viewer to judge. "s" is the agent's own traffic, to its viewers.

Names in the protocol are single words, so an adapter called "Realtek 2.5GbE"
travels as Realtek_2.5GbE and drive C: as C; the labels carry the real names.

It only reads what needs no rights, runs no commands for viewers, and answers
nothing else. Like the Linux agent, it isn't encrypted or authenticated: anyone
who can reach the port can read these numbers, and which programs talk to
which addresses on which ports.
"""
from __future__ import annotations

import collections
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import netwatch
import perfmon
import sysmon

PROTOCOL = 2
DEFAULT_MS, MIN_MS, MAX_MS = 1000, 10, 10000
MIN_MEM_MS, MAX_MEM_MS = 250, 60000
MAX_SELECTED = 16            # adapters or disks one viewer can ask for by name
MAX_ALL = 64                 # and that it gets if it asks for all of them
MAX_VIEWERS = 16
WRITE_TIMEOUT = 5.0          # a viewer that stops reading is dropped
MAX_COMMAND = 16384          # bytes in one command line
MAX_FEEDS, MAX_COUNTERS = 16, 64   # a viewer's feeds, and counters in one
MIN_FEED_MS = 250
MIN_PORTS_MS, MAX_PORTS_MS = 100, 10000
PORTS_IDLE = 10.0            # seconds the capture goes on after the last viewer stops wanting it
PORTS_SESSION = "omniTop agent"                       # the ETW session: not the app's, so both can run
PORTS_GUID = "A4855AF1-2863-4519-A126-2E4FFC65316D"
CAN = "feed,counters,diskperf,drives"   # what feeds can hold
LOOK_EVERY = 1.5             # seconds between looks at which adapters there are
DRIVES_EVERY = 3.0           # and drives
USAGE_EVERY = 10.0           # and how full they are
LOG_LIMIT = 256 * 1024

NAME = "omniTop agent"       # the scheduled task and firewall rule
LOCAL_SERVICE = "S-1-5-19"   # the account the installed agent runs as: few rights, no password

_WORD = re.compile(r"[A-Za-z0-9_.@!+-]{1,64}")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def word(text: str) -> str:
    """A name as one word of the protocol, or "" if it has characters that can't be in one."""
    w = re.sub(r"[^\x21-\x7e]+", "_", text) if text.isascii() else ""
    w = re.sub(r"[^A-Za-z0-9_.@!+-]", "_", w)[:64]
    return w if _WORD.fullmatch(w) and w.strip("_.") else ""


def printable(text: str, limit: int = 64) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:limit]


def adapter_words(found: list[dict]) -> dict[str, dict]:
    """Every adapter by the word it has in the protocol (found: perfmon.adapters()).

    Without the entries network filter drivers add for each adapter.
    """
    out: dict[str, dict] = {}
    for a in found:
        if a.get("filter") or a.get("hidden"):
            continue
        base = word(a["alias"]) or f"adapter{a['luid'] & 0xFFFFFF}"
        w, n = base, 2
        while w.lower() in (k.lower() for k in out):
            w, n = f"{base[:60]}.{n}", n + 1
        out[w] = a
    return out


# ---------------------------------------------------------------------------
# Sampling: each viewer has its own, so they don't disturb each other
# ---------------------------------------------------------------------------

class Net:
    def __init__(self, name: str) -> None:
        self.word, self.label, self.alias = name, "", None
        self.card, self.gone = True, 0.0
        self.reader, self.link = None, "missing"
        self.value = (0.0, 0.0)


class Disk:
    def __init__(self, letter: str) -> None:
        self.word, self.drive = letter, letter + ":"
        self.reader, self.about, self.retry = None, "", 0.0
        self.usage = (0, 0)
        self.value = (0.0, 0.0, 0.0)


class Sampler:
    def __init__(self) -> None:
        try:
            self.cpu = perfmon.CpuTimes()
        except OSError:
            self.cpu = perfmon.SystemTimes()
        self.cpu.read()                       # percentages need two readings
        self.nets: list[Net] = []
        self.disks: list[Disk] = []
        self.changed = False                  # the columns, since the viewer was told
        self.all_nets = self.all_disks = False
        self.feeds: dict[str, Feed] = {}      # those sent with each sample
        self.silent: list[str] = []           # drives without activity figures
        self.news: list[str] = []             # other lines for the viewer
        self.look_at = time.monotonic() + LOOK_EVERY
        self.drives_at = self.usage_at = 0.0
        self.found = adapter_words(perfmon.adapters())

    def close(self) -> None:
        self.disks = []

    def have_lines(self) -> str:
        return f"have net {' '.join(self.found)}".rstrip() + "\n" + \
               f"have disk {' '.join(d[0] for d in perfmon.fixed_drives(26))}".rstrip() + "\n"

    def select_nets(self, names: list[str] | str | None) -> None:
        self.found = adapter_words(perfmon.adapters())
        self.all_nets = names == "all"
        self.nets = []
        if self.all_nets:
            self._look_nets(perfmon.now())
            return
        if names is None:
            auto = perfmon.auto_adapters(list(self.found.values()))
            names = [w for w, a in self.found.items() if a["alias"] in auto]
        for name in names:
            w = self._net_word(name)
            if w and len(self.nets) < MAX_SELECTED and all(w.lower() != n.word.lower() for n in self.nets):
                self.nets.append(Net(w))
        self._look_nets(perfmon.now())

    def _net_word(self, name: str) -> str:
        """The word for an adapter asked for by its word or its name (with _ for spaces)."""
        low = name.lower()
        for w, a in self.found.items():
            if low == w.lower() or low == re.sub(r"\s+", "_", a["alias"]).lower():
                return w
        return word(name)

    def _look_nets(self, t: float) -> None:
        """Open adapters that have come, and note the links of all of them."""
        mono = time.monotonic()
        if self.all_nets:
            here = {n.word for n in self.nets}
            for w, a in self.found.items():
                if a["up"] and w not in here and len(self.nets) < MAX_ALL:
                    self.nets.append(Net(w))
                    self.changed = True
            kept = [n for n in self.nets if n.word in self.found or not n.gone or mono - n.gone < 30]
            if len(kept) != len(self.nets):
                self.nets, self.changed = kept, True
        for n in self.nets:
            a = next((a for w, a in self.found.items() if w.lower() == n.word.lower()), None)
            if a is None:
                n.reader = None
                n.gone = n.gone or mono
            elif n.reader is None or n.alias != a["alias"]:
                n.alias, n.label, n.gone = a["alias"], printable(a["alias"]), 0.0
                if n.card != a["card"]:
                    n.card, self.changed = a["card"], True
                try:
                    n.reader = perfmon.NetReader.of(a)
                    n.reader.read(t)          # so the next reading is already a rate
                except OSError:
                    n.reader = None
            if n.reader is None:
                self._set_link(n, "missing")
            else:
                try:
                    speed = n.reader._refresh()
                    self._set_link(n, "down" if speed < 0 else str(int(speed / 1e6)))
                except OSError:
                    n.reader = None
                    self._set_link(n, "missing")

    def _set_link(self, n: Net, link: str) -> None:
        if n.link != link:
            n.link, self.changed = link, True

    def select_disks(self, names: list[str] | str | None) -> None:
        auto = names is None
        self.all_disks = names == "all"
        self.close()
        if self.all_disks:
            self.drives_at = self.usage_at = 0.0
            self._look_drives()
            return
        if auto:
            names = perfmon.fixed_drives()
        for name in names:
            m = re.fullmatch(r"([A-Za-z]):?\\?", name)
            if m and len(self.disks) < MAX_SELECTED and all(m[1].upper() != d.word for d in self.disks):
                self.disks.append(Disk(m[1].upper()))
        for d in self.disks:
            self._open_disk(d)
        if auto:                              # leave out drives without counters, such as cloud storage
            self.disks = [d for d in self.disks if d.reader]

    def _look_drives(self) -> None:
        """Every drive letter there is now, and how full each is."""
        mono = time.monotonic()
        if mono >= self.drives_at:
            self.drives_at = mono + DRIVES_EVERY
            have, disks, silent = {d.word: d for d in self.disks}, [], []
            for letter, kind in perfmon.drive_letters():
                if kind == perfmon.NETWORK:
                    silent.append(f"{letter} network drive")
                    continue
                d = have.pop(letter[0], None) or Disk(letter[0])
                if d.reader is None and mono >= d.retry:
                    self._open_disk(d)
                if d.reader is not None and len(disks) < MAX_ALL:
                    disks.append(d)
                elif perfmon.volume_label(letter) is not None:
                    silent.append(printable(f"{letter} {perfmon.volume_label(letter)}"))
            if [d.word for d in disks] != [d.word for d in self.disks]:
                self.changed = True
                self.usage_at = 0.0
            self.disks = disks
            if silent != self.silent:
                self.silent = silent
                self.news.append("silent " + json.dumps(silent) + "\n")
        if mono >= self.usage_at:
            self.usage_at = mono + USAGE_EVERY
            for d in self.disks:
                usage = perfmon.disk_usage(d.drive)
                if usage != d.usage and usage[1]:
                    d.usage = usage
                    self.news.append(f"usage {d.word} {usage[0]} {usage[1]}\n")

    def _open_disk(self, d: Disk) -> None:
        try:
            d.reader = perfmon.DiskReader(d.drive)
        except OSError:
            d.reader, d.retry = None, time.monotonic() + perfmon.RETRY_SECONDS
            return
        try:
            size = shutil.disk_usage(d.drive + "\\").total
        except OSError:
            size = 0
        d.about = f"{size} {printable(d.reader.detail)}".rstrip()
        self.changed = True

    def lists(self) -> str:
        """The lines that describe the sample columns."""
        self.changed = False
        out = [f"label net {n.word} {n.label}" for n in self.nets if n.label]
        if self.all_nets:
            out += [f"kind net {n.word} {'card' if n.card else 'virtual'}" for n in self.nets]
        out.append(" ".join(["nets"] + [f"{n.word}:{n.link}" for n in self.nets]))
        out += [f"label disk {d.word} {d.drive}" for d in self.disks]
        out += [f"about {d.word} {d.about}" for d in self.disks if d.reader]
        out.append(" ".join(["disks"] + [d.word if d.reader else d.word + ":missing" for d in self.disks]))
        return "\n".join(out) + "\n"

    def sample(self, t: float) -> str | None:
        """One "s" line, or None while there's nothing to report."""
        cpu = self.cpu.read()
        mono = time.monotonic()
        for n in self.nets:
            v = None
            if n.reader:
                try:
                    v = n.reader.read(t)
                except OSError:
                    n.reader = None
                    self._set_link(n, "missing")
            if v:                             # noticed at once, not at the next look
                self._set_link(n, "down" if v[2] < 0 else str(int(v[2] / 1e6)))
            n.value = (v[0] / 8, v[1] / 8) if v else (0.0, 0.0)
        for d in self.disks:
            v = None
            if d.reader:
                try:
                    v = d.reader.read(t)
                except OSError:
                    d.reader, d.retry, self.changed = None, mono + perfmon.RETRY_SECONDS, True
            d.value = (v[1], v[2], v[0]) if v else (0.0, 0.0, 0.0)
        if cpu is None:
            return None
        parts = [f"s {t * 1000:.3f}"] + [f"{x:.1f}" for x in cpu[:1 + self.cpu.n]]
        for rx, tx in (n.value for n in self.nets):
            parts += (f"{rx:.0f}", f"{tx:.0f}")
        for read, write, busy in (d.value for d in self.disks):
            parts += (f"{read:.0f}", f"{write:.0f}", f"{busy:.1f}")
        # A feed that goes with the samples comes just before the sample it was read with.
        return "".join(f.line(t) for f in self.feeds.values()) + " ".join(parts) + "\n"

    def look(self, t: float) -> None:
        """Every few seconds, between samples: what came, went or changed its link."""
        mono = time.monotonic()
        if mono < self.look_at:
            return
        self.look_at = mono + LOOK_EVERY
        self.found = adapter_words(perfmon.adapters())
        self._look_nets(t)
        if self.all_disks:
            self._look_drives()
            return
        for d in self.disks:
            if d.reader is None and mono >= d.retry:
                self._open_disk(d)


class Feed:
    """Raw figures a viewer asked for, sent as they are for it to interpret."""

    def __init__(self, name: str, ms: int, spec: dict) -> None:
        self.name, self.ms = name, ms                 # ms is 0 for one with each sample
        self.due = 0.0
        # Only this PC's counters: a path can also name another machine.
        paths = [p for p in spec.get("counters") or [] if isinstance(p, str) and len(p) <= 400
                 and p.startswith("\\") and not p.startswith("\\\\")][:MAX_COUNTERS]
        self.paths, self.counters, self.retry = paths, None, 0.0
        self.disks = [d for d in spec.get("diskperf") or [] if isinstance(d, str)][:MAX_ALL]
        self.drives = bool(spec.get("drives"))

    def gather(self) -> dict:
        out: dict = {}
        mono = time.monotonic()
        if self.paths:
            if self.counters is None and mono >= self.retry:
                try:
                    self.counters = sysmon.Counters({p: p for p in self.paths}, "such")
                except OSError:
                    self.retry = mono + 60
            out["counters"] = {p: {k: round(x, 4) for k, x in v.items()} for p, v in self.counters.collect().items()} if self.counters else {}
        letters = [d for d, kind in perfmon.drive_letters() if kind != perfmon.NETWORK] if self.disks or self.drives else []
        if self.disks:
            out["diskperf"] = {}
            for d in letters if "*" in self.disks else [d for d in letters if d in self.disks]:
                try:
                    out["diskperf"][d] = perfmon.disk_performance(d)
                except OSError:
                    pass
        if self.drives:
            kinds = dict(perfmon.drive_letters())
            out["drives"] = {d: [kinds.get(d, 0), perfmon.volume_label(d), *perfmon.disk_usage(d)] for d in letters}
        return out

    def line(self, t: float) -> str:
        return f"feed {self.name} " + json.dumps({**self.gather(), "t": round(t * 1000, 3)}, separators=(",", ":")) + "\n"


class Memory:
    """This PC's memory, sampled for whoever asks: viewers that ask at about the same time share a sample."""

    FIXED = ("dimms", "phys")                 # parts of "ram" that don't change

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.source = self.value = None
        self.fixed: dict = {}
        self.at = self.names_at = 0.0
        self.names: dict[int, tuple] = {}

    def _name(self, pid: int) -> str:
        mono = time.monotonic()
        if mono - self.names_at > 5:
            self.names, self.names_at = netwatch.snapshot(), mono
        name = self.names.get(pid, (0, "", 0))[1]
        return name[:-4] if name.lower().endswith(".exe") else name

    def _keep(self, ram: dict) -> None:
        for k in self.FIXED:
            if k in ram:
                self.fixed[k] = ram.pop(k)

    def sample(self, max_age: float) -> tuple[dict, dict]:
        """(memory, the parts of it that don't change)."""
        with self.lock:
            mono = time.monotonic()
            if self.source is None:
                self.source = sysmon.LiveSystem(self._name, address_map=False)
                self._keep(self.source.sample()["ram"])   # rates need two
            if self.value is None or mono - self.at > max_age:
                d = self.source.sample()
                self._keep(d["ram"])
                self.value, self.at = d, mono
            return self.value, self.fixed


# ---------------------------------------------------------------------------
# Serving viewers
# ---------------------------------------------------------------------------

def parse_command(line: str) -> tuple[str, object] | None:
    """("interval", ms), ("mem", ms), ("net", names, "all" or None for auto), ("disk", ...), ("feed <name>", Feed or
    None), ("ports", ms), ("probes", bool) or ("port <n>", n); None for anything else."""
    if line.startswith("feed "):
        return parse_feed(line)
    f = line.split()
    if len(f) == 2 and f[0] == "mem" and re.fullmatch(r"\d{1,9}", f[1]):
        return "mem", max(MIN_MEM_MS, min(MAX_MEM_MS, int(f[1]))) if int(f[1]) else 0
    if len(f) == 2 and f[0] == "interval" and re.fullmatch(r"\d{1,9}", f[1]):
        return "interval", max(MIN_MS, min(MAX_MS, int(f[1])))
    if len(f) == 2 and f[0] == "ports" and re.fullmatch(r"off|\d{1,6}", f[1]):
        return "ports", 0 if f[1] == "off" or not int(f[1]) else max(MIN_PORTS_MS, min(MAX_PORTS_MS, int(f[1])))
    if len(f) == 2 and f[0] == "probes" and f[1] in ("on", "off"):
        return "probes", f[1] == "on"
    if len(f) == 2 and f[0] == "port" and re.fullmatch(r"\d{1,5}", f[1]) and 0 < int(f[1]) < 65536:
        return "port " + f[1], int(f[1])
    if f and f[0] in ("net", "disk"):
        return f[0], None if f[1:] == ["auto"] else "all" if f[1:] == ["all"] else f[1:1 + MAX_SELECTED]
    return None


def parse_feed(line: str) -> tuple[str, Feed | None] | None:
    f = line.split(" ", 3)
    if len(f) < 3 or not re.fullmatch(r"[a-z0-9_]{1,32}", f[1]):
        return None
    if f[2] == "off":
        return "feed " + f[1], None
    if len(f) < 4 or not re.fullmatch(r"sample|[0-9]{1,9}", f[2]):
        return None
    try:
        spec = json.loads(f[3])
    except ValueError:
        return None
    if not isinstance(spec, dict):
        return None
    spec = {k: v for k, v in spec.items() if isinstance(v, list)}
    return "feed " + f[1], Feed(f[1], 0 if f[2] == "sample" else max(MIN_FEED_MS, min(3600000, int(f[2]))), spec)


class Viewer:
    """One connected viewer: a thread sends samples, another reads its commands, another sends memory and slow feeds."""

    def __init__(self, server: "AgentServer", sock: socket.socket, peer: str) -> None:
        self.server, self.sock, self.peer = server, sock, peer
        self.pending: dict[str, object] = {}      # the latest command of each kind
        self.lock = threading.Lock()
        self.wake = threading.Event()             # a command arrived, or the viewer left
        self.gone = False
        self.sending = threading.Lock()           # whole lines: samples and memory come from two threads
        self.mem_ms, self.mem_due = 0, 0.0
        self.mem_first = True
        self.mem_wake = threading.Event()
        self.mem_thread = None
        self.feeds: dict[str, Feed] = {}          # those with a pace of their own
        self.outbox: collections.deque[str] = collections.deque(maxlen=200)   # port traffic and details

    def send(self, text: str) -> None:
        with self.sending:
            self.sock.sendall(text.encode("utf-8"))

    def memory_line(self) -> str:
        d, fixed = self.server.memory.sample(self.mem_ms / 2000)
        if self.mem_first and fixed:
            d = {**d, "ram": {**d["ram"], **fixed}}
            self.mem_first = False
        return "mem " + json.dumps(d, separators=(",", ":")) + "\n"

    def post(self, line: str) -> None:
        """A line to send when it can be: not on the caller's thread, as this viewer may be slow."""
        self.outbox.append(line)
        self.mem_wake.set()

    def send_slowly(self) -> None:
        """Memory, the feeds with a pace of their own, and what PortWatch has for this viewer."""
        troubled: set[str] = set()
        while not self.gone and not self.server.stopping.is_set():
            try:
                while self.outbox:
                    self.send(self.outbox.popleft())
            except IndexError:
                pass
            except OSError:
                self.gone = True
                self.wake.set()
                return
            with self.lock:
                feeds = list(self.feeds.values())
            t = time.monotonic()
            soonest = t + 60
            for job in ([self] if self.mem_ms else []) + feeds:
                name = "memory" if job is self else "feed " + job.name
                ms = self.mem_ms if job is self else job.ms
                due = (self.mem_due if job is self else job.due) or t
                if t >= due:
                    line = ""
                    try:
                        line = self.memory_line() if job is self else job.line(perfmon.now())
                    except Exception as exc:  # noqa: BLE001 - the rest goes on without it, and it may work later
                        ms = 30000
                        if name not in troubled:
                            troubled.add(name)
                            self.server.log(f"{self.peer}: {name}: {type(exc).__name__}: {exc}")
                    try:
                        if line:
                            self.send(line)
                    except OSError:
                        self.gone = True          # it has stopped reading, or left
                        self.wake.set()
                        return
                    took = time.monotonic() - t
                    due = max(due + ms / 1000, time.monotonic() + took)   # never more than half the time
                    if job is self:
                        self.mem_due = due
                    else:
                        job.due = due
                soonest = min(soonest, due)
            if not self.outbox:
                self.mem_wake.wait(max(0.02, soonest - time.monotonic()))
            self.mem_wake.clear()

    def set_feed(self, sampler: Sampler, name: str, feed: Feed | None) -> None:
        with self.lock:
            self.feeds.pop(name, None)
            sampler.feeds.pop(name, None)
            if feed and len(self.feeds) + len(sampler.feeds) < MAX_FEEDS:
                (self.feeds if feed.ms else sampler.feeds)[name] = feed

    def read_commands(self) -> None:
        buf = b""
        try:
            while not self.server.stopping.is_set():
                try:
                    data = self.sock.recv(4096)
                except TimeoutError:
                    continue                      # quiet, not gone
                if not data:
                    break
                buf += data
                *lines, buf = buf.split(b"\n")
                if len(buf) > MAX_COMMAND or any(len(x) > MAX_COMMAND for x in lines):
                    break
                for raw in lines:
                    c = parse_command(raw.decode("utf-8", errors="replace"))
                    if c:
                        with self.lock:
                            self.pending[c[0]] = c[1]
                        self.wake.set()
        except OSError:
            pass
        self.gone = True
        self.wake.set()
        self.mem_wake.set()

    def run(self) -> None:
        log = self.server.log
        sampler = None
        try:
            self.sock.settimeout(WRITE_TIMEOUT)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sampler = Sampler()
            self.send(self.server.hello(sampler.cpu.n) + self.server.system_line() + sampler.have_lines())
            sampler.select_nets(None)
            sampler.select_disks(None)
            self.send(sampler.lists())
            log(f"{self.peer} connected")
            threading.Thread(target=self.read_commands, name=f"commands {self.peer}", daemon=True).start()
            self._stream(sampler)
            log(f"{self.peer} disconnected")
        except OSError as exc:
            log(f"{self.peer} disconnected: {exc}")
        except Exception as exc:  # noqa: BLE001 - one viewer's trouble stays its own
            log(f"{self.peer}: {type(exc).__name__}: {exc}")
        finally:
            self.gone = True
            self.server.ports.forget(self)
            self.mem_wake.set()
            if sampler:
                sampler.close()
            self.sock.close()

    @staticmethod
    def _news(sampler: Sampler) -> str:
        news, sampler.news = sampler.news, []
        return "".join(news)

    def _stream(self, sampler: Sampler) -> None:
        step = DEFAULT_MS / 1000
        last = time.perf_counter()                # the selections just took the first readings
        due = last + step
        while not self.gone and not self.server.stopping.is_set():
            wait = due - time.perf_counter()
            if wait > 0 and not self.wake.is_set():
                time.sleep(min(wait, 0.1))        # finer than waiting on the event, whose timer ticks every 15.6 ms
                continue
            if self.wake.is_set():
                self.wake.clear()
                with self.lock:
                    todo, self.pending = self.pending, {}
                if "interval" in todo:
                    step = todo["interval"] / 1000
                    due = last + step             # a whole new interval after the last reading
                if "net" in todo:
                    sampler.select_nets(todo["net"])
                if "disk" in todo:
                    sampler.select_disks(todo["disk"])
                for k, v in todo.items():
                    if k.startswith("feed "):
                        self.set_feed(sampler, k, v)
                if "mem" in todo:
                    self.mem_ms, self.mem_due = todo["mem"], 0.0
                self.mem_wake.set()
                if "ports" in todo or "probes" in todo:
                    self.server.ports.want(self, todo.get("ports"), todo.get("probes"))
                for k, v in todo.items():
                    if k.startswith("port "):
                        self.server.ports.ask(self, v)
                if self.mem_thread is None:
                    self.mem_thread = threading.Thread(target=self.send_slowly, name=f"slowly {self.peer}", daemon=True)
                    self.mem_thread.start()
                if "net" in todo or "disk" in todo:
                    self.send(sampler.lists() + self._news(sampler))
                continue
            last = time.perf_counter()
            line = sampler.sample(perfmon.now())
            if line:
                self.send(line)
            sampler.look(perfmon.now())
            if sampler.changed:                   # the samples from here on have these columns
                self.send(sampler.lists())
            if sampler.news:
                self.send(self._news(sampler))
            due += step                           # absolute deadlines: no drift
            if due <= time.perf_counter():        # fell behind (asleep, busy): start again from now
                due = time.perf_counter() + step


_tick = ctypes.WinDLL("kernel32").GetTickCount64
_tick.restype = ctypes.c_uint64


class AgentServer:
    def __init__(self, listen: str, port: int, version: str, log) -> None:
        self.version, self.log = version, log
        self.memory = Memory()
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_VIEWERS)
        if listen:
            family = socket.AF_INET6 if ":" in listen else socket.AF_INET
            self.sock = self._open(family, (listen, port), dual=False)
        else:
            try:
                self.sock = self._open(socket.AF_INET6, ("", port), dual=True)
            except OSError as exc:
                if exc.errno == 10048:            # in use
                    raise
                self.sock = self._open(socket.AF_INET, ("", port), dual=False)   # IPv6 is off here
        self.port = self.sock.getsockname()[1]
        self.ports = PortWatch(self.port, log)

    @staticmethod
    def _open(family: int, address: tuple, dual: bool) -> socket.socket:
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)   # no other program can share the port
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if dual else 1)
            s.bind(address)
            s.listen(16)
        except OSError:
            s.close()
            raise
        return s

    def hello(self, cpus: int) -> str:
        host = word(socket.gethostname()) or "unknown"
        return (f"cpugraph-agent {PROTOCOL} host={host} cpus={cpus} "
                f"kernel={perfmon.windows_version()} version={self.version} os=windows omni=2 "
                f"can={CAN}{',ports' if self.ports.available() else ''}\n")

    def system_line(self) -> str:
        boot = int(time.time() - _tick() / 1000)
        return "sys " + json.dumps({"os": "windows", "system": perfmon.windows_name(), "boot": boot,
                                    "machine": os.environ.get("PROCESSOR_ARCHITECTURE", "")}) + "\n"

    def serve(self) -> None:
        """Until stop()."""
        self.sock.settimeout(0.5)                 # so Ctrl+C and stop() are noticed
        while not self.stopping.is_set():
            try:
                conn, addr = self.sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self.stopping.is_set():
                    break
                self.log(f"accept failed: {exc}")
                time.sleep(0.1)
                continue
            peer = addr[0].removeprefix("::ffff:")
            if not self.slots.acquire(blocking=False):
                try:
                    conn.settimeout(1)
                    conn.sendall(b"err too many viewers\n")
                except OSError:
                    pass
                conn.close()
                self.log(f"{peer} refused: already serving {MAX_VIEWERS} viewers")
                continue
            threading.Thread(target=self._serve_one, args=(conn, peer), name=f"viewer {peer}", daemon=True).start()
        self.sock.close()

    def _serve_one(self, conn: socket.socket, peer: str) -> None:
        try:
            Viewer(self, conn, peer).run()
        finally:
            self.slots.release()

    def stop(self) -> None:
        self.stopping.set()
        self.ports.stop()


# ---------------------------------------------------------------------------
# Ports: what goes through each, and probes
# ---------------------------------------------------------------------------

class Names:
    """pid -> program name, for any process: from the list of them all, which needs no rights."""

    def __init__(self) -> None:
        self.map: dict[int, str] = {}
        self.at = -1e9

    def get(self, pid: int) -> str:
        if pid <= 4:
            return "System" if pid == 4 else ""
        age = time.monotonic() - self.at
        name = self.map.get(pid)
        if (name is None and age > 1) or age > 10:           # new processes, and IDs used again
            self.map = {p: os.path.splitext(v[1])[0] for p, v in netwatch.snapshot().items()}
            self.at = time.monotonic()
            name = self.map.get(pid)
        return name or ""


class PortWatch:
    """This PC's traffic per port, and the probes of it, for the viewers that ask.

    One ETW session, of its own so that OmniTOP can run here too, shared by every viewer, and only
    while one wants something: everything while one shows ports, only what tells probes otherwise.
    """

    def __init__(self, own_port: int, log) -> None:
        self.own_port, self.log = own_port, log
        self.lock = threading.Lock()
        self.wants: dict[Viewer, list] = {}       # viewer -> [ms, probes]
        self.told: set[Viewer] = set()            # have had portsinfo
        self.wake = threading.Event()
        self.stopping = False
        self.thread: threading.Thread | None = None
        self.capture = self.acc = self.filter = None
        self.full: bool | None = None
        self.error, self.tried = "", False
        self.dynamic: list[int] | None = None
        self.seen = netwatch.PortSeen()
        self.names = Names()
        self.services = netwatch.Services()
        self.asking = threading.BoundedSemaphore(2)
        self.pending = 0

    @staticmethod
    def leftover() -> None:
        """Stop a session left by an agent that was ended from outside: it would go on costing time."""
        import server
        try:
            server.EtwCapture(server.Accumulator(), session=(PORTS_SESSION, server.GUID.parse(PORTS_GUID)))._stop_session()
        except OSError:
            pass

    def _start(self, full: bool):
        import server                             # the app's own capture
        acc, filt = server.Accumulator(), netwatch.ProbeFilter()
        c = server.EtwCapture(acc, own_port=self.own_port, scans=filt,
                              session=(PORTS_SESSION, server.GUID.parse(PORTS_GUID)),
                              events=None if full else server.EtwCapture.PROBES_ONLY)
        c.start()
        return c, acc, filt

    def available(self) -> bool:
        """Whether this PC lets the agent watch its network: tried once."""
        with self.lock:
            if not self.tried:
                self.tried = True
                if self.capture is None:
                    try:
                        self._start(False)[0].stop()
                    except OSError as exc:
                        self.error = exc.strerror or str(exc)
                        self.log(f"no ports: {self.error}")
            return not self.error or self.capture is not None

    def want(self, viewer: Viewer, ms: int | None = None, probes: bool | None = None) -> None:
        with self.lock:
            w = self.wants.setdefault(viewer, [0, False])
            if ms is not None:
                if ms and not w[0]:
                    self.told.discard(viewer)
                w[0] = ms
            if probes is not None:
                w[1] = probes
            if not any(w):
                del self.wants[viewer]
            if self.thread is None and self.wants and not self.stopping:
                self.thread = threading.Thread(target=self._run, name="ports", daemon=True)
                self.thread.start()
        self.wake.set()

    def forget(self, viewer: Viewer) -> None:
        with self.lock:
            self.wants.pop(viewer, None)
            self.told.discard(viewer)
        self.wake.set()

    def ask(self, viewer: Viewer, port: int) -> None:
        """Describe a port to a viewer, on a thread of its own: it takes a moment."""
        with self.lock:
            if self.pending >= 8:
                return
            self.pending += 1

        def answer() -> None:
            with self.asking:
                try:
                    procs = netwatch.snapshot()
                    d = netwatch.describe(port, netwatch.connections(), self.seen.pids(port),
                                          lambda pid: netwatch.process_info(pid, procs, self.services))
                except Exception as exc:  # noqa: BLE001 - said to the viewer instead
                    d = {"port": port, "error": f"{type(exc).__name__}: {exc}"[:200]}
                finally:
                    with self.lock:
                        self.pending -= 1
            viewer.post("port " + json.dumps(d, separators=(",", ":")) + "\n")

        threading.Thread(target=answer, name=f"port {port}", daemon=True).start()

    def stop(self) -> None:
        self.stopping = True
        self.wake.set()
        self._end()

    def _end(self) -> None:
        if self.capture is not None:
            self.capture.stop()
        self.capture = self.acc = self.filter = None
        self.full = None

    def _info(self) -> str:
        if self.dynamic is None:
            import server
            d = server.dynamic_ports()
            self.dynamic = [d["start"], d["end"]]
        scans = self.capture.scans_error if self.capture is not None else ""
        return "portsinfo " + json.dumps({"dynamic": self.dynamic, "udp": True, "error": self.error, "scans": scans}) + "\n"

    def _run(self) -> None:
        idle_since, last, retry = 0.0, time.perf_counter(), 0.0
        while not self.stopping:
            with self.lock:
                wants = [(v, w[0], w[1]) for v, w in self.wants.items()]
            ms = [m for _, m, _ in wants if m]
            if not wants:
                idle_since = idle_since or time.monotonic()
                if self.capture is not None and time.monotonic() - idle_since > PORTS_IDLE:
                    self._end()
                if self.wake.wait(1.0):
                    self.wake.clear()
                continue
            idle_since = 0.0
            full = bool(ms)
            if self.capture is None or self.full != full:
                if time.monotonic() < retry:
                    self.wake.wait(1.0)
                    self.wake.clear()
                    continue
                self._end()
                try:
                    self.capture, self.acc, self.filter = self._start(full)
                    self.full, self.error = full, ""
                except OSError as exc:
                    self.error, retry = exc.strerror or str(exc), time.monotonic() + 30
                    self.log(f"ports: {self.error}")
                    for v, m, _ in wants:
                        if m:
                            v.post(self._info())
                    continue
                last = time.perf_counter()
            every = min(ms) / 1000 if ms else 0.5
            delay = last + every - time.perf_counter()
            if delay > 0:
                if self.wake.wait(delay):
                    self.wake.clear()
                continue
            now = time.perf_counter()
            dt, last = now - last, now
            ports, own, (b_in, b_out, own_in, own_out, _) = self.acc.drain()
            self.seen.note(ports, time.monotonic())
            found = self.filter.take()
            line = probes = ""
            if ms:
                rows = [[p, e[0], e[1], e[2], e[3], self.names.get(e[4])] for p, e in ports.items()]
                own_rows = [[p, e[0], e[1], e[2], e[3]] for p, e in own.items()]
                line = "ports " + json.dumps({"dt": round(dt, 4), "t": every, "in": b_in, "out": b_out, "si": own_in,
                                              "so": own_out, "p": rows, "s": own_rows}, separators=(",", ":")) + "\n"
            if found:
                probes = "probes " + json.dumps({"t": round(time.time(), 3), "p": found}, separators=(",", ":")) + "\n"
            for v, m, p in wants:
                if m:
                    if v not in self.told:
                        self.told.add(v)
                        v.post(self._info())
                    v.post(line)
                if p and probes:
                    v.post(probes)
        self._end()


class Log:
    """A short log file, started over when it gets long."""

    def __init__(self, path: str, echo=None) -> None:
        self.path, self.echo, self.lock = path, echo, threading.Lock()

    def __call__(self, text: str) -> None:
        if self.echo:
            self.echo(text)
        try:
            with self.lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                long = os.path.exists(self.path) and os.path.getsize(self.path) > LOG_LIMIT
                with open(self.path, "w" if long else "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass


def in_services_session() -> bool:
    """Whether this runs where Windows runs services, with no one there to see a message."""
    session = wintypes.DWORD()
    ctypes.WinDLL("kernel32").ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    return session.value == 0


def _empty(folder: str, keep: str) -> bool:
    """Delete what's in a folder except the file `keep`. Links are removed, never followed. True if that's all that's left."""
    clear = True
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return False
    for e in entries:
        if os.path.normcase(e.path) == os.path.normcase(keep):
            continue
        try:
            if e.is_junction() or (e.is_symlink() and e.is_dir()):
                os.rmdir(e.path)                  # the link itself
            elif e.is_dir(follow_symlinks=False):
                done = _empty(e.path, keep)
                clear = clear and done
                if done and not keep.lower().startswith(e.path.lower() + os.sep):
                    os.rmdir(e.path)
            else:
                os.unlink(e.path)
        except OSError:
            clear = False                         # still in use
    return clear


def sweep(temp: str, keep: str = "", seconds: float = 0.0) -> int:
    """Delete copies of this program left in a temp folder; returns how many.

    The exe unpacks itself there to run, and deletes that when it ends, but
    not when it's ended from outside, or by Windows shutting down. The file
    that tells a copy of this program goes last, so one that was still partly
    in use after `seconds` is known for what it is next time. Only the account
    a copy was unpacked by can see into it, administrators included.
    """
    gone = 0
    deadline = time.monotonic() + seconds
    try:
        names = os.listdir(temp)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(temp, name)
        mark = os.path.join(path, "web", "dimm.js")
        if (not name.startswith("_MEI") or os.path.normcase(path) == os.path.normcase(keep)
                or os.path.islink(path) or os.path.isjunction(path) or not os.path.isfile(mark)):
            continue
        while not _empty(path, mark) and time.monotonic() < deadline:
            time.sleep(0.25)
        if _empty(path, mark):
            try:
                os.unlink(mark)
                os.rmdir(os.path.dirname(mark))
                os.rmdir(path)
                gone += 1
            except OSError:
                pass
    return gone


def cleanup(log_path: str) -> int:
    """Run as the installed agent's account when it's being removed: delete what it left in its folders."""
    unpacked = getattr(sys, "_MEIPASS", "")
    if not unpacked or not in_services_session():
        return 0
    sweep(os.path.dirname(unpacked), unpacked, seconds=10)   # this copy deletes itself when it ends
    try:
        os.remove(log_path)
        os.rmdir(os.path.dirname(log_path))       # only if that was all
    except OSError:
        pass
    return 0


def run(listen: str, port: int, version: str, log_path: str, echo=None) -> int:
    log = Log(log_path, echo)
    unpacked = getattr(sys, "_MEIPASS", "")
    if unpacked and in_services_session():        # only the installed agent runs there: the others are stale
        sweep(os.path.dirname(unpacked), unpacked)
    try:
        srv = AgentServer(listen, port, version, log)
    except OSError as exc:
        why = "another program is using it" if exc.errno == 10048 else (exc.strerror or str(exc))
        log(f"can't listen on port {port}: {why}")
        raise SystemExit(f"The agent can't listen on TCP port {port}: {why}.") from None
    log(f"omniTop agent {version} listening on {listen or 'all addresses'}, TCP port {srv.port}")
    PortWatch.leftover()
    try:
        _k32 = ctypes.WinDLL("kernel32")
        _k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        _k32.GetCurrentProcess.restype = wintypes.HANDLE
        _k32.SetPriorityClass(_k32.GetCurrentProcess(), 0x00008000)   # above normal: samples on time
        srv.serve()
    except KeyboardInterrupt:
        srv.stop()
    return 0


# ---------------------------------------------------------------------------
# Starting with Windows: a scheduled task and a firewall rule (administrators)
# ---------------------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]


def known_folder(guid: GUID) -> str:
    """One of Windows' folders, asked of Windows rather than taken from the environment."""
    shell = ctypes.WinDLL("shell32")
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                           ctypes.POINTER(ctypes.c_void_p)]
    out = ctypes.c_void_p()
    if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)):
        raise OSError("Windows didn't say where one of its folders is.")
    try:
        return ctypes.wstring_at(out.value)
    finally:
        ole = ctypes.WinDLL("ole32")
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree(out)


PROGRAM_FILES = GUID(0x905E63B6, 0xC1BF, 0x494E, (ctypes.c_ubyte * 8)(0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0xD2, 0x1A))
DOWNLOADS = GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))


def program_files() -> str:
    return known_folder(PROGRAM_FILES)


def system_tool(name: str) -> str:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.WinDLL("kernel32").GetSystemDirectoryW(buf, 260)
    return os.path.join(buf.value, name)


def tool(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([system_tool(name), *args], capture_output=True, text=True, errors="replace",
                          stdin=subprocess.DEVNULL, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)


def check(p: subprocess.CompletedProcess, what: str) -> None:
    if p.returncode:
        raise OSError(f"{what}: {(p.stderr or p.stdout or '').strip() or f'error {p.returncode}'}")


def install_dir() -> str:
    return os.path.join(program_files(), "omniTop agent")


def installed_exe() -> str:
    return os.path.join(install_dir(), "omniTop.exe")


def xml_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def task_xml(exe: str, port: int, arguments: str = "") -> str:
    """The scheduled task: from start-up, as LOCAL SERVICE, for as long as Windows runs."""
    arguments = arguments or f"--agent --agent-port {port}"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Lets OmniTOP on other PCs watch this PC's CPU, memory, network and disk activity (TCP port {port}).</Description>
  </RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
  <Principals>
    <Principal id="Author"><UserId>{LOCAL_SERVICE}</UserId><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{xml_text(exe)}</Command><Arguments>{arguments}</Arguments></Exec>
  </Actions>
</Task>
"""


def remove_rules() -> None:
    tool("netsh.exe", "advfirewall", "firewall", "delete", "rule", f"name={NAME}")


def installed_processes(end: bool = False) -> int:
    """How many processes run from the installed copy; with end, they're ended."""
    path = os.path.normcase(installed_exe())
    was = ctypes.c_ubyte()                        # another account's processes: needs the debug privilege
    ctypes.WinDLL("ntdll").RtlAdjustPrivilege(20, 1, 0, ctypes.byref(was))
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = ctypes.WinDLL("psapi")
    psapi.EnumProcesses.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    ids, got = (wintypes.DWORD * 16384)(), wintypes.DWORD()
    if not psapi.EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(got)):
        return 0
    count = 0
    for pid in ids[:got.value // 4]:
        h = k32.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, pid) if pid != os.getpid() else None   # query, end, wait
        if not h:
            continue
        try:
            buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) and os.path.normcase(buf.value) == path:
                count += 1
                if end:
                    k32.TerminateProcess(h, 0)
                    k32.WaitForSingleObject(h, 5000)
        finally:
            k32.CloseHandle(h)
    return count


def wait_for(test, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not test():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def stop_installed() -> None:
    """End the installed agent: its task, then any process of its still running."""
    tool("schtasks.exe", "/End", "/TN", NAME)
    installed_processes(end=True)


def register(exe: str, port: int, arguments: str = "") -> None:
    """Create the scheduled task, or change it."""
    xml = os.path.join(install_dir(), "task.xml")   # only administrators can write here
    try:
        with open(xml, "w", encoding="utf-16") as f:
            f.write(task_xml(exe, port, arguments))
        check(tool("schtasks.exe", "/Create", "/TN", NAME, "/XML", xml, "/F"), "Couldn't create the scheduled task")
    finally:
        try:
            os.remove(xml)
        except OSError:
            pass


def answers(port: int, seconds: float, version: str = "") -> str:
    """The hello line the agent on this PC (of that version) answers with, or ""."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                line = s.makefile("rb").readline(512).decode("utf-8", errors="replace").strip()
                if line.startswith("cpugraph-agent ") and f"version={version} " in line + " " if version else line.startswith("cpugraph-agent "):
                    return line
        except OSError:
            pass
        time.sleep(0.5)
    return ""


def patiently(do, what: str) -> None:
    """Do something to a file that a process which has just ended can still hold for a moment."""
    for _ in range(20):
        try:
            do()
            return
        except PermissionError:
            time.sleep(0.25)
    raise OSError(f"{what}: it's in use.")


def local_addresses() -> list[str]:
    try:
        found = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return []
    return sorted(a for a in found if not a.startswith(("127.", "169.254.")))


def install(source: str, port: int, version: str) -> str:
    """Copy the program to Program Files, open its port, and start it with Windows. Returns what to tell the user.

    An upgrade is all or nothing: if the new version doesn't answer, the one that was there runs again.
    """
    folder, exe = install_dir(), installed_exe()
    new, old = exe + ".new", exe + ".old"
    upgrade = os.path.exists(exe)
    was = tool("schtasks.exe", "/Query", "/TN", NAME, "/XML").stdout if upgrade else ""
    m = re.search(r"--agent-port (\d+)", was)
    before = int(m.group(1)) if m else 0
    port = port or before or perfmon.AGENT_PORT       # an upgrade keeps its port
    os.makedirs(folder, exist_ok=True)
    itself = os.path.normcase(os.path.abspath(source)) == os.path.normcase(exe)
    if not itself:
        shutil.copyfile(source, new)                  # all of it, before anything that runs is touched
    stop_installed()
    try:
        if not itself:
            if os.path.exists(old):
                patiently(lambda: os.remove(old), f"Couldn't remove {old}")
            if os.path.exists(exe):
                patiently(lambda: os.replace(exe, old), f"Couldn't replace {exe}")
            patiently(lambda: os.replace(new, exe), f"Couldn't replace {exe}")
        register(exe, port)
        check(tool("schtasks.exe", "/Run", "/TN", NAME), "Couldn't start the agent")
        if not answers(port, 40, version):
            raise OSError(f"The agent was installed, but it isn't answering on TCP port {port}.")
    except (OSError, subprocess.SubprocessError) as exc:
        stop_installed()
        if os.path.exists(old):
            patiently(lambda: os.replace(old, exe), f"Couldn't put back {exe}")
        try:
            os.remove(new)
        except OSError:
            pass
        if upgrade and not itself:
            register(exe, before or port)
            tool("schtasks.exe", "/Run", "/TN", NAME)
            raise OSError(f"{exc}\n\nThe version that was there runs again.") from None
        raise
    remove_rules()
    check(tool("netsh.exe", "advfirewall", "firewall", "add", "rule", f"name={NAME}", "dir=in", "action=allow",
               f"program={exe}", "protocol=TCP", f"localport={port}", "profile=private,domain"),
          "Couldn't open the port in Windows Firewall")
    for leftover in (old, new):
        try:
            os.remove(leftover)
        except OSError:
            pass
    where = " or ".join([socket.gethostname()] + local_addresses()[:3])
    if port != perfmon.AGENT_PORT:
        where += f", with port {port}"
    return (f"OmniTOP on other PCs can now watch this PC. The agent starts with Windows and needs no one signed in.\n\n"
            f"On the other PC, open OmniTOP's Remote tab, choose Add machine, and enter\n{where}\n\n"
            f"TCP port {port} is open on private and domain networks. To remove the agent, run\n"
            f"omniTop.exe --uninstall-agent")


def uninstall() -> str:
    if os.path.normcase(sys.executable) == os.path.normcase(installed_exe()):
        raise OSError("The installed agent can't remove itself. Run this with your own copy of omniTop.exe.")
    folder, exe = install_dir(), installed_exe()
    remove_rules()
    stop_installed()
    if os.path.exists(exe):                       # its account clears up what it left in its own folders
        try:
            register(exe, 0, "--agent-cleanup")
            check(tool("schtasks.exe", "/Run", "/TN", NAME), "")
            if wait_for(lambda: installed_processes() > 0, 10):
                wait_for(lambda: installed_processes() == 0, 30)
        except (OSError, subprocess.SubprocessError):
            pass                                  # what's left is a log and some temporary files
        stop_installed()
    tool("schtasks.exe", "/Delete", "/TN", NAME, "/F")
    for path in (exe, exe + ".new", exe + ".old"):
        for _ in range(20):
            try:
                if os.path.exists(path):
                    os.remove(path)
                break
            except PermissionError:
                time.sleep(0.25)
    try:
        os.rmdir(folder)
    except OSError:
        pass
    left = [what for what, there in (("its program", os.path.exists(exe)),
                                     ("its scheduled task", not tool("schtasks.exe", "/Query", "/TN", NAME).returncode)) if there]
    if left:
        raise OSError(f"Couldn't remove {' and '.join(left)}.")
    return "The agent is removed: other PCs can't watch this one any more, and its port is closed."
