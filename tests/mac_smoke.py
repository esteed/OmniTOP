"""Tries the agent on a Mac (or any machine) as OmniTOP would, and says what came back.

    python3 tests/mac_smoke.py                    the agent from this checkout, on a spare loopback port
    python3 tests/mac_smoke.py 127.0.0.1:7821     an agent that's already running (say, installed)

Exits 1 if something a Mac's agent should send didn't come, or made no sense.
"""
import importlib.util
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(what, ok, extra=""):
    print(("ok    " if ok else "FAIL  ") + what + ("  " + str(extra)[:300] if extra else ""))
    if not ok:
        failures.append(what)


if len(sys.argv) > 1:
    host, port = sys.argv[1].rsplit(":", 1)
    port = int(port)
else:
    spec = importlib.util.spec_from_file_location("agent", os.path.join(HERE, "..", "linux", "omnitop-agent.py"))
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    srv = agent.Server("127.0.0.1", 0, lambda t: print("   log:", t))
    threading.Thread(target=srv.serve, daemon=True).start()
    host, port = "127.0.0.1", srv.port

c = socket.create_connection((host, port), timeout=10)
f = c.makefile("rb")
hello = f.readline().decode().strip()
print("hello:", hello)
fields = dict(p.split("=", 1) for p in hello.split()[2:] if "=" in p)
check("it's an OmniTOP agent", hello.startswith("cpugraph-agent 2") and fields.get("omni") == "2")
mac = sys.platform == "darwin"
if mac:
    check("it says it's a Mac", fields.get("os") == "macos", fields.get("os"))
can = set(fields.get("can", "").split(","))
c.sendall(b"interval 250\nnet all\ndisk all\nmem 500\n" +
          (b'feed disks sample {"iokit": ["*"]}\n' if "iokit" in can else b'feed disks sample {"files": ["/proc/diskstats"]}\n') +
          b"echo 500 google=google.com microsoft=microsoft.com att=att.com\n" + (b"procio 1000\n" if "procio" in can else b""))
seen, first = {}, {}
c.settimeout(1)
end = time.time() + 9
while time.time() < end:
    try:
        line = f.readline().decode("utf-8", "replace").rstrip("\n")
    except socket.timeout:
        continue
    if not line:
        break
    kind = line.split(" ", 1)[0]
    seen[kind] = seen.get(kind, 0) + 1
    first.setdefault(kind, line)
    first["last " + kind] = line
print("lines:", seen)

sysline = json.loads(first.get("sys", "sys {}")[4:])
check("its system", bool(sysline.get("system")), sysline.get("system"))
nets = first.get("last nets", "nets").split()[1:]
check("network adapters", bool(nets), nets)
disks = first.get("last disks", "disks").split()[1:]
check("disks", bool(disks), disks)
samples = [first[k] for k in ("s", "last s") if k in first]
if samples:
    v = [float(x) for x in samples[-1].split()[1:]]
    cpus = int(fields.get("cpus", "0"))
    check("CPU figures, one per CPU and the total", len(v) >= 2 + cpus and all(0 <= x <= 100 for x in v[1:2 + cpus]), v[:2 + cpus])
else:
    check("samples", False)
mem = json.loads(first.get("last mem", "mem {}")[4:])
ram = mem.get("ram") or {}
check("memory: total and what's in it", ram.get("total", 0) > 0 and sum(b for _, b in ram.get("uses", [])) > 0,
      dict(installed=ram.get("installed"), total=ram.get("total"), avail=ram.get("avail"), uses=ram.get("uses")))
feed = json.loads(first.get("last feed", "feed disks {}").split(" ", 2)[2])
if "iokit" in can:
    stats = feed.get("iokit") or {}
    check("disks' running totals (IOKit)", stats and all(len(x) == 6 for x in stats.values()), stats)
echo = json.loads(first.get("last echo", "echo {}")[5:])
rows = echo.get("r", [])
check("latency to the sites", any(r[2] is not None for r in rows) and {r[3] for r in rows if len(r) > 3} >= {"google"},
      (echo.get("g"), rows[:3], echo.get("e")))
if "procio" in can:
    pio = json.loads(first.get("last procio", "procio {}")[7:])
    check("processes' disk I/O", "disks" in pio and not pio.get("e"), str(pio)[:200])
c.close()
print("%d failed" % len(failures) if failures else "all ok")
sys.exit(1 if failures else 0)
