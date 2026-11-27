"""Finds agents on this PC's local networks: OmniTOP's, and CPU Graph's cpugraph-agent.

While the window is open, every SWEEP_EVERY seconds (and when asked), every address of each
IPv4 network of this PC's adapters that are up is tried on the agents' port: 7820, and any
other port a watched machine's agent uses. Where a connection is made, the first line says
what answers (agents start with a hello saying what they are); that line is all that's read,
then the connection is closed. A network bigger than /22 has only the /24 around this PC's
address tried, so a sweep stays a few hundred quick connection attempts, a second or two.

What it finds and isn't watched yet is offered on the Remote tab, to add with a click. It's
published as "found" events (and GET /api/found): {"on", "busy", "last", "networks", "found":
[{"address", "port", "host", "os", "agent", "kind", "seen"}]}, kind being "omnitop" (an agent of
OmniTOP 2.0 or later), "old" (1.9's, on Windows) or "cpugraph" (CPU Graph's, on Linux).
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import socket
import threading
import time

import perfmon

SWEEP_EVERY = 600.0          # seconds between sweeps, while the window is open
FIRST_AFTER = 6.0            # the first one, after the app starts
CONNECT_TIMEOUT = 0.6
HELLO_TIMEOUT = 2.0
PARALLEL = 128
MAX_HOSTS = 4096             # per sweep
WIDEST = 22                  # networks wider than this are tried only around this PC's address, as /24
FORGET_AFTER = 3 * SWEEP_EVERY + 60   # a machine not seen in this long is no longer offered


def own_networks() -> tuple[list[ipaddress.IPv4Network], set[str]]:
    """This PC's IPv4 networks worth sweeping, and its own addresses."""
    import latency
    up = {a["luid"] for a in perfmon.adapters() if a["up"] and not a["filter"] and not a["hidden"]}
    nets, mine = [], set()
    for luid, addrs in latency.addresses().items():
        for addr, length in addrs:
            mine.add(addr)
            ip = ipaddress.IPv4Address(addr)
            if luid not in up or ip.is_loopback or ip.is_link_local or ip.is_multicast or length >= 31 or length == 0:
                continue
            net = ipaddress.IPv4Network(f"{addr}/{max(length, 24) if length < WIDEST else length}", strict=False)
            if net not in nets:
                nets.append(net)
    return nets, mine


def ask(address: str, port: int) -> dict | None:
    """What answers on address:port, if it's an agent."""
    try:
        with socket.create_connection((address, port), timeout=CONNECT_TIMEOUT) as s:
            s.settimeout(HELLO_TIMEOUT)
            raw = s.makefile("rb").readline(512)
    except OSError:
        return None
    try:
        protocol, fields = perfmon.parse_hello(raw, f"{address}:{port}")
    except perfmon.AgentError:
        return None
    os_name = fields.get("os", "").lower()
    # OmniTOP's agents from 2.0 say omni=; 1.9's (Windows only) didn't; CPU Graph's says neither omni nor os
    kind = "omnitop" if fields.get("omni") else "old" if os_name == "windows" else "cpugraph"
    return {"address": address, "port": port, "host": fields.get("host", "")[:64],
            "os": os_name if os_name in ("windows", "linux") else "linux", "agent": fields.get("version", "")[:32],
            "kind": kind, "protocol": protocol}


class Discovery:
    """Sweeps for agents now and then, and says what it found that isn't watched yet."""

    def __init__(self, monitor: perfmon.PerfMonitor, settings, hub, demo: bool = False) -> None:
        self.monitor, self.settings, self.hub, self.demo = monitor, settings, hub, demo
        self.lock = threading.Lock()
        self.found: dict[tuple[str, int], dict] = {}
        self.networks: list[str] = []
        self.busy = False
        self.last = 0.0                                    # when the last sweep ended (time.time())
        self.error = ""
        self.wake = threading.Event()
        self.asked = False                                 # a sweep was asked for
        self.stopping = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="discovery", daemon=True).start()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def look_now(self) -> None:
        self.asked = True
        self.wake.set()

    def set_on(self, on: bool) -> None:
        self.settings.set_discover(on)
        if on:
            self.look_now()
        self.publish()

    def ignore(self, address: str, port: int) -> None:
        self.settings.ignore(f"{address}:{port}")
        self.publish()

    def _watched(self) -> set[tuple[str, int]]:
        """Addresses and ports already watched: as entered, and as their names resolved."""
        out = {(m["address"].lower(), m["port"]) for m in self.settings.list()}
        for r in list(self.monitor.remotes):
            if r.machine.ip:
                out.add((r.machine.ip.lower(), r.port))
        return out

    def view(self) -> dict:
        watched, ignored = self._watched(), set(self.settings.ignored())
        with self.lock:
            found = [dict(f) for (a, p), f in sorted(self.found.items(), key=lambda kv: socket.inet_aton(kv[0][0]))
                     if (a.lower(), p) not in watched and f"{a}:{p}" not in ignored]
            return {"on": self.settings.discover(), "busy": self.busy, "last": round(self.last, 1), "error": self.error,
                    "networks": list(self.networks), "found": found, "now": round(time.time(), 1),
                    "every": SWEEP_EVERY}

    def publish(self) -> None:
        self.hub.publish(json.dumps(self.view(), separators=(",", ":")), "found")

    def _run(self) -> None:
        due = time.monotonic() + FIRST_AFTER
        while not self.stopping.is_set():
            self.wake.wait(max(0.5, min(5.0, due - time.monotonic())))
            self.wake.clear()
            if self.stopping.is_set():
                return
            asked = self.asked
            if not asked and (time.monotonic() < due or not self.settings.discover() or not self.monitor.active()):
                continue
            self.asked = False
            due = time.monotonic() + SWEEP_EVERY
            self._sweep()

    def _sweep(self) -> None:
        with self.lock:
            self.busy = True
        self.publish()
        try:
            if self.demo:
                time.sleep(1.5)
                nets, results = ["10.0.0.0/24"], [
                    {"address": "10.0.0.77", "port": perfmon.AGENT_PORT, "host": "media-server", "os": "linux",
                     "agent": "2.2.0", "kind": "omnitop", "protocol": 2},
                    {"address": "10.0.0.92", "port": perfmon.AGENT_PORT, "host": "old-nas", "os": "linux",
                     "agent": "", "kind": "cpugraph", "protocol": 2}]
            else:
                nets, results = self._sweep_networks()
            now = time.time()
            with self.lock:
                for r in results:
                    self.found[(r["address"], r["port"])] = {**r, "seen": round(now, 1)}
                for key, f in list(self.found.items()):
                    if now - f["seen"] > FORGET_AFTER:
                        del self.found[key]
                self.networks, self.error = nets, ""
        except Exception as exc:  # noqa: BLE001 - shown on the Remote tab; the next sweep tries again
            self.error = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            with self.lock:
                self.busy, self.last = False, time.time()
            self.publish()

    def _sweep_networks(self) -> tuple[list[str], list[dict]]:
        nets, mine = own_networks()
        ports = sorted({perfmon.AGENT_PORT} | {m["port"] for m in self.settings.list()})[:8]
        hosts: list[str] = []
        for net in nets:
            for ip in net.hosts():
                if str(ip) not in mine and len(hosts) < MAX_HOSTS:
                    hosts.append(str(ip))
        jobs = [(h, p) for h in hosts for p in ports][:MAX_HOSTS * 2]
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="sweep") as pool:
            for r in pool.map(lambda job: ask(*job), jobs):
                if r:
                    results.append(r)
        return [str(n) for n in nets], results
