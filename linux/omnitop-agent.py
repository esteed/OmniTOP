#!/usr/bin/env python3
"""OmniTOP's agent for Linux: lets OmniTOP on a Windows PC watch this machine.

    sudo python3 omnitop-agent.py --install              start with the system (systemd)
    sudo python3 omnitop-agent.py --uninstall            remove it again
    python3 omnitop-agent.py                             just run, until Ctrl+C

Options: --port N (default 7820), --listen ADDRESS (default: all addresses),
and with --install, --replace to stop CPU Graph's cpugraph-agent if it has the port.

Running --install again upgrades it. An upgrade is all or nothing: the new version is
tried before anything changes, and if it doesn't answer once it's in place, the one that
was there runs again.

One file, Python 3.6 or later, nothing else needed. It reads /proc and /sys (and asks
nvidia-smi, if there is one) and runs nothing else for viewers. The connection isn't
encrypted or authenticated: anyone who can reach the port can read this machine's CPU,
memory, network and disk activity, the names of its programs, adapters and disks, and
which programs talk to which addresses on which ports.

Installed, it's two services. omnitop-agent has the network port, and runs as a throwaway
account with no rights. omnitop-agent-ports finds out about ports, which takes rights: it
has those few and no others, listens only on a unix socket, and answers only the agent.

It speaks the protocol agent.py describes (cpugraph-agent's, with OmniTOP's additions).
So that it seldom needs replacing, it doesn't decide what's worth showing: besides the
usual samples it sends whatever raw figures a viewer asks for (see Feeds), and the viewer
makes sense of them. Only figures: what identifies the machine or its hardware (serial
numbers, addresses), command lines and files aren't among them.
"""
import argparse
import atexit
import collections
import ctypes
import errno
import glob
import json
import mmap
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

try:
    import pwd
except ImportError:          # the tests run on Windows
    pwd = None

VERSION = "2.3.0"
PROTOCOL = 2
DEFAULT_PORT = 7820
DEFAULT_MS, MIN_MS, MAX_MS = 1000, 10, 10000
MIN_MEM_MS, MAX_MEM_MS = 250, 60000
MAX_SELECTED, MAX_ALL = 16, 64
MAX_VIEWERS = 16
MAX_COMMAND = 16384
WRITE_TIMEOUT = 5.0
LOOK_EVERY = 1.5             # seconds between looks at which adapters and disks there are
MAX_FEEDS, MAX_PATTERNS, MAX_FILES = 16, 64, 1024   # a viewer's feeds; patterns and files in one
MAX_FILE, MAX_FEED = 256 << 10, 1 << 20             # bytes of one file, and of one feed's files
MIN_FEED_MS = 250
CAN = "feed,files,list,statvfs,nvidia,echo,sites"   # what feeds can hold, and latency
TOP_PROGRAMS = 30

NAME = "omnitop-agent"
INSTALLED = "/usr/local/lib/omnitop-agent/omnitop-agent.py"
UNIT = "/etc/systemd/system/omnitop-agent.service"
PORTS_NAME = "omnitop-agent-ports"
PORTS_UNIT = "/etc/systemd/system/omnitop-agent-ports.service"
PORTS_SOCKET = "/run/omnitop-agent/ports"
MIN_PORTS_MS = 100

ROOT = ""                    # tests read a made-up /proc and /sys from here


def now():
    return time.perf_counter()


def read(path, limit=1 << 20):
    """A file's text, or None."""
    try:
        with open(ROOT + path, "rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def read_int(path, default=0):
    try:
        return int((read(path, 64) or "").strip())
    except ValueError:
        return default


def printable(text, limit=80):
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:limit]


_WORD = re.compile(r"[A-Za-z0-9_.@!+-]{1,64}")


def word(text):
    """A name as one word of the protocol, or ""."""
    w = re.sub(r"[^A-Za-z0-9_.@!+-]", "_", text)[:64]
    return w if _WORD.fullmatch(w) and w.strip("_.") else ""


# ---------------------------------------------------------------------------
# What this machine is
# ---------------------------------------------------------------------------

def system_name():
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        text = read(path)
        if text:
            m = re.search(r'^PRETTY_NAME=(?:"(.*)"|\'(.*)\'|(.*))$', text, re.M)
            if m:
                return printable(m.group(1) or m.group(2) or m.group(3) or "Linux")
    return "Linux"


def distribution():
    text = read("/etc/os-release") or read("/usr/lib/os-release") or ""
    m = re.search(r'^ID="?([a-z0-9._-]+)"?$', text, re.M)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

class Cpu:
    """Busy % of all CPUs and of each, from /proc/stat."""

    def __init__(self):
        self.last = None
        self.n = max(1, len(self._times()) - 1)

    @staticmethod
    def _times():
        out = []
        for line in (read("/proc/stat") or "").splitlines():
            if not line.startswith("cpu"):
                break                                  # they come first
            f = line.split()
            v = [int(x) for x in f[1:9]]               # user nice system idle iowait irq softirq steal
            v += [0] * (8 - len(v))
            idle = v[3] + v[4]
            out.append((f[0], sum(v) - idle, sum(v)))
        return out

    def read(self):
        cur = self._times()
        last, self.last = self.last, cur
        if not cur or last is None:
            return None
        if [c[0] for c in cur] != [c[0] for c in last]:  # a CPU went on or off line
            return None
        out = []
        for (_, b, t), (_, lb, lt) in zip(cur, last):
            dt = t - lt
            out.append(100.0 * max(0, min(dt, b - lb)) / dt if dt > 0 else 0.0)
        out = out[:1 + self.n]
        return out + [0.0] * (1 + self.n - len(out))


# ---------------------------------------------------------------------------
# Network adapters
# ---------------------------------------------------------------------------

def net_counters():
    """adapter -> (received, sent) bytes, from /proc/net/dev."""
    out = {}
    for line in (read("/proc/net/dev") or "").splitlines()[2:]:
        name, colon, rest = line.partition(":")
        f = rest.split()
        if colon and len(f) >= 9:
            out[name.strip()] = (int(f[0]), int(f[8]))
    return out


def adapter(name):
    """{"card": whether it's hardware, "link": "down" or Mb/s as text (0 unknown)}.

    None for loopback, and for the ends of containers' links: their bridge has the same traffic.
    """
    base = "/sys/class/net/%s/" % name
    if read_int(base + "type", 1) == 772 or name.startswith(("veth", "cali")):
        return None
    flags = 0
    try:
        flags = int((read(base + "flags", 32) or "0").strip(), 16)
    except ValueError:
        pass
    state = (read(base + "operstate", 32) or "unknown").strip()
    up = bool(flags & 1) and (state == "up" or state == "unknown" and bool(flags & 0x40 or (read(base + "carrier", 8) or "").strip() == "1"))
    speed = read_int(base + "speed", 0) if up else 0
    card = os.path.exists(ROOT + base + "device")
    return {"card": card, "link": "down" if not up else str(speed if 0 < speed < 10000000 else 0)}


# ---------------------------------------------------------------------------
# Disks: whole ones, real or a virtual machine's. Not partitions, and not what's
# made of other disks or of files (LVM, RAID, loop devices, RAM disks).
# ---------------------------------------------------------------------------

NOT_DISKS = re.compile(r"(loop|ram|zram|sr|fd|dm-|md|nbd|mtd)\d|mmcblk\d+(boot|rpmb)")


class Disk(object):
    def __init__(self, name):
        self.name, self.word = name, word(name.replace("!", "_"))
        base = "/sys/block/%s/" % name
        self.size = read_int(base + "size") * 512
        self.model = printable(read(base + "device/model", 128) or "", 48)
        self.last, self.value = None, (0.0, 0.0, 0.0)  # read B/s, written B/s, busy %


def disk_names():
    out = []
    for path in glob.glob(ROOT + "/sys/block/*"):
        name = os.path.basename(path)
        if (not NOT_DISKS.match(name) and word(name.replace("!", "_")) and os.path.exists(path + "/device")
                and read_int("/sys/block/%s/size" % name)):
            out.append(name)
    return sorted(out, key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])[:MAX_ALL]


def disk_counters():
    """name -> (bytes read, bytes written, ms busy), from /proc/diskstats."""
    out = {}
    for line in (read("/proc/diskstats") or "").splitlines():
        f = line.split()
        if len(f) >= 13:
            out[f[2]] = (int(f[5]) * 512, int(f[9]) * 512, int(f[12]))
    return out


# ---------------------------------------------------------------------------
# Feeds: raw figures a viewer asks for, sent as they are for it to interpret
#
#   feed <name> sample {"files": ["/proc/diskstats"]}         with each sample
#   feed <name> 5000 {"files": ["/sys/class/hwmon/*/temp*_input"], "statvfs": ["*"]}    every 5 s
#   feed <name> off
# and back:  feed <name> {"t": <ms>, "files": {path: text}, ...}
#
#   files    contents of files. Patterns may have * and ?
#   list     the paths there are, without their contents
#   statvfs  [size, free, available, files, files free] of mounted filesystems, or ["*"] for all
#   nvidia   {"gpu": ["name", "memory.used"], "apps": ["pid"]}: nvidia-smi's rows
# What isn't allowed, or isn't there, is left out; "denied" names the former.
# ---------------------------------------------------------------------------

ALLOWED = re.compile(
    r"/proc/(?:stat|meminfo|vmstat|diskstats|loadavg|uptime|cpuinfo|version|mdstat|swaps|filesystems|mounts|partitions"
    r"|interrupts|softirqs|buddyinfo|zoneinfo|pagetypeinfo|schedstat|slabinfo|cgroups|devices)\Z"
    r"|/proc/pressure/[a-z]+\Z"
    r"|/proc/net/(?:dev|snmp6?|netstat|sockstat6?|softnet_stat|wireless|bonding/[^/]+)\Z"
    r"|/proc/sys/(?:kernel/(?:osrelease|version|pid_max|threads-max|random/entropy_avail)|fs/(?:file-nr|inode-nr|aio-nr)"
    r"|vm/[a-z_]+|net/netfilter/nf_conntrack_(?:count|max))\Z"
    r"|/proc/(?:spl/kstat|fs/(?:nfs|nfsd|xfs|ext4|jbd2))/.+"
    r"|/proc/\d+/(?:stat|statm|comm|schedstat|status)\Z"
    r"|/sys/(?:block|class/(?:block|net|hwmon|thermal|drm|power_supply|nvme|nvme-subsystem|infiniband|fc_host|scsi_host"
    r"|iscsi_host|scsi_disk|powercap|devfreq|mmc_host|edac|rtc)|bus/(?:node|cpu|event_source)|devices/system|devices/virtual"
    r"|fs/(?:cgroup|ext4|xfs|btrfs|bcache|f2fs)|module/(?:zfs|kvm|nvme_core)/parameters|kernel/mm)/.+")
# Not figures: what identifies the machine, its hardware or its neighbours
DENIED = re.compile(r"serial|wwid|wwn|uuid|guid|unique_id|eui64|nqn|hostid|address|port_name|node_name|fabric_name"
                    r"|/gids?/|/cid\Z|/csd\Z|vpd|uevent|edid|modalias|phys_(?:port|switch)_id|ifalias|asset_tag|secret|key", re.I)
FIELD = re.compile(r"[a-z0-9_.]{1,64}\Z")


def permitted(path):
    if not ALLOWED.match(path) or DENIED.search(path) or "/../" in path or path.endswith("/.."):
        return False
    real = os.path.realpath(ROOT + path).replace("\\", "/")
    root = os.path.realpath(ROOT or "/").replace("\\", "/").rstrip("/")
    real = real[len(root):] if real.startswith(root) else ""
    return real.startswith(("/proc/", "/sys/")) and not DENIED.search(real)


def expand(pattern):
    """The paths a pattern matches, or None if it's not one that can be asked for."""
    if (not isinstance(pattern, str) or not pattern.startswith(("/proc/", "/sys/")) or len(pattern) > 200
            or ".." in pattern or "**" in pattern or len(re.findall(r"[*?[]", pattern)) > 4):
        return None
    if not re.search(r"[*?[]", pattern):
        return [pattern]
    out = []
    for path in glob.iglob(ROOT + pattern):
        out.append(path[len(ROOT):].replace("\\", "/"))
        if len(out) >= MAX_FILES:
            break
    return sorted(out)


class Statvfs(object):
    """How full filesystems are. Asked on a thread, as a network filesystem can keep one waiting forever."""

    def __init__(self):
        self.lock = threading.Lock()
        self.values, self.asking = {}, set()

    def _ask(self, mount):
        try:
            st = os.statvfs(ROOT + mount if ROOT else mount)
            value = [st.f_blocks * st.f_frsize, st.f_bfree * st.f_frsize, st.f_bavail * st.f_frsize, st.f_files, st.f_ffree]
        except (OSError, AttributeError):
            value = None
        with self.lock:
            self.values[mount] = value
            self.asking.discard(mount)

    def get(self, mount):
        with self.lock:
            if mount not in self.asking:
                self.asking.add(mount)
                t = threading.Thread(target=self._ask, args=(mount,))
                t.daemon = True
                t.start()
            else:
                t = None                               # still waiting since last time
        if t:
            t.join(0.1)
        with self.lock:
            return self.values.get(mount)


STATVFS = Statvfs()


def mount_points():
    out = []
    for line in (read("/proc/1/mountinfo") or read("/proc/self/mountinfo") or "").splitlines():
        f = line.split(" - ")[0].split()
        if len(f) >= 5:
            out.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f[4]))
    return out


def nvidia_rows(what, fields):
    fields = [f for f in fields if isinstance(f, str) and FIELD.match(f)][:32]
    smi = None if ROOT else shutil.which("nvidia-smi")
    if not fields or not smi:
        return []
    try:
        out = subprocess.run([smi, "--query-%s=%s" % (what, ",".join(fields)), "--format=csv,noheader,nounits"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=10,
                             universal_newlines=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [[printable(x) for x in line.split(",")] for line in out.splitlines()[:256]]


def gather(spec):
    """What a feed holds now."""
    out, denied, room, count = {}, [], MAX_FEED, 0
    for kind in ("files", "list"):
        found = {}
        for pattern in spec.get(kind) or []:
            paths = expand(pattern)
            if paths is None:
                denied.append(pattern if isinstance(pattern, str) else "?")
                continue
            names = []
            for path in paths:
                if not permitted(path):
                    if paths == [pattern]:
                        denied.append(pattern)
                    continue
                if kind == "list":
                    if os.path.exists(ROOT + path):
                        names.append(path)
                    continue
                count += 1
                text = read(path, min(MAX_FILE, room)) if count <= MAX_FILES and room > 0 else None
                if text is not None:
                    room -= len(text)
                    found[path] = text
            if kind == "list":
                found[pattern] = names
        if found:
            out[kind] = found
    asked = spec.get("statvfs") or []
    if asked:
        here = mount_points()
        out["statvfs"] = {m: STATVFS.get(m) for m in (here if "*" in asked else [m for m in asked if m in here])[:256]}
    nv = spec.get("nvidia")
    if isinstance(nv, dict):
        out["nvidia"] = {"gpu": nvidia_rows("gpu", nv.get("gpu") or []), "apps": nvidia_rows("compute-apps", nv.get("apps") or [])}
    if denied:
        out["denied"] = denied[:16]
    return out


class Feed(object):
    def __init__(self, name, ms, spec):
        self.name, self.ms, self.spec = name, ms, spec  # ms is 0 for one with each sample
        self.due = 0.0

    def line(self, t):
        d = gather(self.spec)
        d["t"] = round(t * 1000, 3)
        return "feed %s %s\n" % (self.name, json.dumps(d, separators=(",", ":")))


def parse_feed(line):
    """("feed <name>", Feed or None to end it), or None if it makes no sense."""
    f = line.split(" ", 3)
    if len(f) < 3 or not re.match(r"[a-z0-9_]{1,32}\Z", f[1]):
        return None
    if f[2] == "off":
        return "feed " + f[1], None
    if len(f) < 4 or not (f[2] == "sample" or re.match(r"[0-9]{1,9}\Z", f[2])):
        return None
    try:
        spec = json.loads(f[3])
    except ValueError:
        return None
    if not isinstance(spec, dict):
        return None
    spec = {k: v for k, v in spec.items() if isinstance(v, (list, dict))}
    for k, v in spec.items():
        if isinstance(v, list):
            spec[k] = v[:MAX_PATTERNS]
    return "feed " + f[1], Feed(f[1], 0 if f[2] == "sample" else max(MIN_FEED_MS, min(3600000, int(f[2]))), spec)


# ---------------------------------------------------------------------------
# Ports: traffic per TCP and UDP port, the processes on a port, and probes
#
# TCP bytes are the kernel's own counts for each connection (sock_diag's tcp_info), read
# several times a second, and once more when a connection ends (the kernel says when).
# UDP bytes, and the packets that start TCP connections, come from a packet socket whose
# filter lets nothing else through. Which process has a socket is in /proc/<pid>/fd.
#
# The last two need rights the agent doesn't have. So all this is done by a service of
# its own, omnitop-agent-ports, which has just those rights and no network port: it
# listens on a unix socket and answers only to the agent.
# ---------------------------------------------------------------------------

TCP_STATES = {1: "Established", 2: "SYN sent", 3: "SYN received", 4: "FIN wait 1", 5: "FIN wait 2", 6: "Time wait",
              7: "Closed", 8: "Close wait", 9: "Last ACK", 10: "Listening", 11: "Closing"}
LISTENING = 10
SOL_PACKET = 263
V4_MAPPED = b"\0" * 10 + b"\xff\xff"   # ::ffff:a.b.c.d: an IPv4 address on an IPv6 socket
UDP_SENT_KEEP = 300.0        # seconds a port this machine sent UDP from is remembered: what comes to it is an answer
MAX_PROBES = 2000            # sent per message
RING_BLOCK, RING_BLOCKS, RING_FRAME = 1 << 16, 64, 256   # 4 MB of packet headers


def ip_text(raw, v6):
    if not v6:
        return socket.inet_ntop(socket.AF_INET, raw[:4])
    text = socket.inet_ntop(socket.AF_INET6, raw[:16])
    return text[7:] if text.startswith("::ffff:") and "." in text else text


class Diag(object):
    """The kernel's socket tables (NETLINK_SOCK_DIAG)."""

    MSG = struct.Struct("=BBBB HH 16s 16s I Q IIIII".replace(" ", ""))

    def __init__(self):
        self.s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.s.settimeout(5)
        self.seq = 0
        self.ended = None
        try:
            self.ended = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4)
            self.ended.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            self.ended.bind((0, 0b0101))               # SKNLGRP_INET_TCP_DESTROY and SKNLGRP_INET6_TCP_DESTROY
            self.ended.setblocking(False)
        except OSError:                                # an older kernel: what a connection sent last is missed
            self.ended = None

    @staticmethod
    def parse(data, at, end):
        """One inet_diag_msg: (cookie, family, state, local ip, port, remote ip, port, uid, inode, sent, received)."""
        family, state, _, _ = struct.unpack_from("BBBB", data, at)
        sport, dport = struct.unpack_from(">HH", data, at + 4)
        src, dst = data[at + 8:at + 24], data[at + 24:at + 40]
        cookie, = struct.unpack_from("=Q", data, at + 44)
        uid, inode = struct.unpack_from("=II", data, at + 64)
        sent = got = None
        a = at + 72
        while a + 4 <= end:
            alen, atype = struct.unpack_from("=HH", data, a)
            if alen < 4:
                break
            if atype == 2 and alen >= 4 + 136:          # INET_DIAG_INFO: bytes_acked and bytes_received of tcp_info
                sent, got = struct.unpack_from("=QQ", data, a + 4 + 120)
            a += (alen + 3) & ~3
        return cookie, family, state, src, sport, dst, dport, uid, inode, sent, got

    def dump(self, family, proto, states, info):
        self.seq += 1
        req = struct.pack("=BBBBI", family, proto, 2 if info else 0, 0, states) + b"\0" * 48
        self.s.send(struct.pack("=IHHII", 16 + len(req), 20, 0x301, self.seq, 0) + req)
        out = []
        while True:
            data = self.s.recv(1 << 18)
            at = 0
            while at + 16 <= len(data):
                n, kind, _, seq, _ = struct.unpack_from("=IHHII", data, at)
                if n < 16:
                    return out
                if seq == self.seq:
                    if kind == 3:                      # done
                        return out
                    if kind == 2:
                        raise OSError(-struct.unpack_from("=i", data, at + 16)[0], "sock_diag")
                    out.append(self.parse(data, at + 16, at + n))
                at += (n + 3) & ~3

    def finished(self):
        """Connections that have ended since the last time."""
        out = []
        while self.ended is not None:
            try:
                data = self.ended.recv(1 << 18)
            except BlockingIOError:
                break
            except OSError as exc:                     # more than it could hold: some are missed
                if exc.errno == errno.ENOBUFS:
                    continue
                break
            at = 0
            while at + 16 <= len(data):
                n, kind, _, _, _ = struct.unpack_from("=IHHII", data, at)
                if n < 16:
                    break
                if kind == 20:
                    out.append(self.parse(data, at + 16, at + n))
                at += (n + 3) & ~3
        return out


def proc_ip(text):
    """An address as /proc/net shows it: 32-bit words, each in this machine's byte order."""
    return b"".join(struct.pack("=I", int(text[i:i + 8], 16)) for i in range(0, len(text), 8))


def proc_sockets(family, proto, states):
    """A socket table from /proc/net, as Diag.dump gives it, but with made-up cookies and no byte counts."""
    name = ("tcp" if proto == 6 else "udp") + ("6" if family == socket.AF_INET6 else "")
    out = []
    for line in (read("/proc/net/" + name, 1 << 24) or "").splitlines()[1:]:
        f = line.split()
        try:
            here, lport = f[1].split(":")
            there, rport = f[2].split(":")
            state, uid, inode = int(f[3], 16), int(f[7]), int(f[9])
            src, dst, lport, rport = proc_ip(here), proc_ip(there), int(lport, 16), int(rport, 16)
        except (IndexError, ValueError):
            continue
        if states >> state & 1:
            out.append((hash((family, src, lport, dst, rport)), family, state, src, lport, dst, rport, uid, inode, None, None))
    return out


class Sockets(object):
    """The kernel's socket tables: sock_diag where it has inet_diag, /proc/net where it doesn't (some routers'
    kernels). Then there are no byte counts of connections, and TCP traffic is counted from packet headers."""

    MISSING = (errno.ENOENT, errno.EPROTONOSUPPORT, errno.EOPNOTSUPP, errno.EINVAL)

    def __init__(self):
        self.proc = set()                              # (family, protocol) read from /proc/net
        try:
            # OMNITOP_SOCKETS=proc: as if the kernel had no inet_diag (for trying that out)
            self.diag = None if os.environ.get("OMNITOP_SOCKETS") == "proc" else Diag()
        except OSError:
            self.diag = None

    @property
    def counts(self):
        """Whether the kernel counts the bytes of TCP connections (IPv4's, anyway)."""
        return self.diag is not None and (socket.AF_INET, 6) not in self.proc

    def dump(self, family, proto, states, info):
        if self.diag is not None and (family, proto) not in self.proc:
            try:
                return self.diag.dump(family, proto, states, info)
            except OSError as exc:
                if exc.errno not in self.MISSING:
                    raise
                self.proc.add((family, proto))
                log("ports: no sock_diag for %s%s here (%s): reading /proc/net instead" % (
                    "TCP" if proto == 6 else "UDP", "v6" if family == socket.AF_INET6 else "", exc.strerror or exc))
        return proc_sockets(family, proto, states)

    def finished(self):
        return self.diag.finished() if self.counts else []


def own_addresses():
    """{interface index: its addresses, as bytes}, asked of the kernel (NETLINK_ROUTE)."""
    out = {}
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    try:
        s.settimeout(5)
        s.send(struct.pack("=IHHIIB7x", 24, 22, 0x301, 1, 0, 0))   # RTM_GETADDR, dump
        while True:
            data = s.recv(1 << 18)
            at = 0
            while at + 16 <= len(data):
                n, kind, _, _, _ = struct.unpack_from("=IHHII", data, at)
                if n < 16 or kind in (2, 3):
                    return out
                if kind == 20:                         # RTM_NEWADDR
                    index, = struct.unpack_from("=I", data, at + 20)
                    a, addr = at + 24, None
                    while a + 4 <= at + n:
                        alen, atype = struct.unpack_from("=HH", data, a)
                        if alen < 4:
                            break
                        if atype == 2 or (atype == 1 and addr is None):   # IFA_LOCAL, or IFA_ADDRESS
                            addr = data[a + 4:a + alen]
                        a += (alen + 3) & ~3
                    if addr:
                        out.setdefault(index, set()).add(bytes(addr))
                at += (n + 3) & ~3
    finally:
        s.close()


def packet_filter(own4, own6, all_tcp):
    """A classic BPF program for the packet socket: IPv4 and IPv6 UDP, and TCP (only connections' first packets,
    SYN without ACK, unless all_tcp), to or from this machine's own addresses; UDP to multicast and broadcast
    addresses too. Not later fragments. own4 and own6: addresses as bytes; none known, any address."""
    prog, labels, count = [], {}, [0]

    def op(code, k=0, jt=None, jf=None):
        prog.append([code, jt, jf, k])

    def label(name):
        labels[name] = len(prog)

    def fresh():
        count[0] += 1
        return "L%d" % count[0]

    own4, own6 = sorted(own4)[:48], sorted(own6)[:12]
    op(0x28, 0xfffff000)                               # ldh the packet's protocol
    op(0x15, 0x0800, None, "six")
    # IPv4
    op(0x28, 6)                                        # flags and fragment offset
    op(0x45, 0x1fff, "drop4")                          # a later fragment: no ports in it
    op(0x30, 9)                                        # the protocol
    op(0x15, 17, "udp4")
    op(0x15, 6, None, "drop4")
    if not all_tcp:
        op(0xb1, 0)                                    # x = the IP header's length
        op(0x50, 13)                                   # TCP flags
        op(0x54, 0x12)
        op(0x15, 2, "own4", "drop4")                   # SYN, no ACK
    op(0x05, 0, None, None)                            # ja own4 (fixed below)
    prog[-1][3] = "own4"
    label("udp4")
    op(0x30, 16)
    op(0x35, 224, "accept4")                           # to a multicast or broadcast address
    label("own4")
    if own4:
        for side in (12, 16):
            op(0x20, side)
            for a in own4:
                op(0x15, struct.unpack("!I", a)[0], "accept4")
        label("drop4")
        op(0x06, 0)
    else:
        op(0x05, 0, None, None)
        prog[-1][3] = "accept4"
        label("drop4")
        op(0x06, 0)
    label("accept4")
    op(0x06, 128)
    # IPv6
    label("six")
    op(0x15, 0x86dd, None, "drop6")
    op(0x30, 6)                                        # the next header, right after: no extension headers
    op(0x15, 17, "udp6")
    op(0x15, 6, None, "drop6")
    if not all_tcp:
        op(0x30, 53)
        op(0x54, 0x12)
        op(0x15, 2, "own6", "drop6")
    op(0x05, 0, None, None)
    prog[-1][3] = "own6"
    label("udp6")
    op(0x30, 24)
    op(0x15, 0xff, "accept6")                          # to a multicast address
    label("own6")
    if own6:
        for side in (8, 24):
            for a in own6:
                words, nxt = struct.unpack("!IIII", a), fresh()
                for j, w in enumerate(words):
                    op(0x20, side + 4 * j)
                    op(0x15, w, "accept6" if j == 3 else None, nxt)
                label(nxt)
        label("drop6")
        op(0x06, 0)
    else:
        op(0x05, 0, None, None)
        prog[-1][3] = "accept6"
        label("drop6")
        op(0x06, 0)
    label("accept6")
    op(0x06, 128)
    out = []
    for i, (code, jt, jf, k) in enumerate(prog):
        if code == 0x05:                               # ja: k is the distance
            k = labels[k] - i - 1
        jt = labels[jt] - i - 1 if isinstance(jt, str) else 0
        jf = labels[jf] - i - 1 if isinstance(jf, str) else 0
        if not (0 <= jt < 256 and 0 <= jf < 256 and k >= 0):
            raise ValueError("filter too long")
        out.append((code, jt, jf, k))
    return out


class Headers(object):
    """Headers of UDP packets and of TCP ones (only those that start connections, unless all_tcp) to and from
    this machine's addresses, from a packet socket's ring, with the adapter each went through."""

    def __init__(self, all_tcp=False):
        self.all_tcp = all_tcp
        self.s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(3))   # every protocol and interface
        self.code, self.own = None, None
        self.refilter(set(), set())
        self.s.setsockopt(SOL_PACKET, 10, struct.pack("=i", 1))          # TPACKET_V2
        frames = RING_BLOCK * RING_BLOCKS // RING_FRAME
        # Setting the ring up also drops what came before the filter was in place.
        self.s.setsockopt(SOL_PACKET, 5, struct.pack("=IIII", RING_BLOCK, RING_BLOCKS, RING_FRAME, frames))
        self.ring = mmap.mmap(self.s.fileno(), RING_BLOCK * RING_BLOCKS)
        self.frames, self.at = frames, 0

    def refilter(self, own4, own6):
        """Only what's to or from these addresses, from now on."""
        if (own4, own6) == self.own:
            return
        try:
            program = packet_filter(own4, own6, self.all_tcp)
        except ValueError:                             # too many addresses to fit: any address
            program = packet_filter(set(), set(), self.all_tcp)
        code = b"".join(struct.pack("=HBBI", *i) for i in program)
        buf = ctypes.create_string_buffer(code, len(code))
        self.s.setsockopt(socket.SOL_SOCKET, 26, struct.pack("HP", len(program), ctypes.addressof(buf)))   # SO_ATTACH_FILTER
        self.code, self.own = buf, (set(own4), set(own6))

    def close(self):
        self.ring.close()
        self.s.close()

    def read(self):
        """([(interface, outgoing, v6, protocol, source, destination, source port, destination port, bytes, syn)], factor).

        bytes: of UDP or TCP payload. syn: a TCP packet that starts a connection (SYN without ACK).
        factor: what to multiply bytes by, to make up for packets that didn't fit the ring.
        """
        out, ring, unpack = [], self.ring, struct.unpack_from
        for _ in range(self.frames):
            f = self.at * RING_FRAME
            status, _, snap, _, net = unpack("=IIIHH", ring, f)
            if not status & 1:
                break
            proto, index, _, kind = unpack("=2xHiHB", ring, f + 32)
            if kind in (0, 4) and snap >= 28:           # to this machine, or from it
                v6 = socket.ntohs(proto) == 0x86dd
                p = f + net
                if v6:
                    l4, at, length = ring[p + 6], p + 40, unpack(">H", ring, p + 4)[0]
                    src, dst = ring[p + 8:p + 24], ring[p + 24:p + 40]
                else:
                    head = (ring[p] & 15) * 4
                    l4, at, length = ring[p + 9], p + head, unpack(">H", ring, p + 2)[0] - head
                    src, dst = ring[p + 12:p + 16], ring[p + 16:p + 20]
                if l4 == 17 and at + 8 <= p + snap:
                    sport, dport, size = unpack(">HHH", ring, at)
                    out.append((index, kind == 4, v6, 17, src, dst, sport, dport, max(0, size - 8), False))
                elif l4 == 6 and at + 14 <= p + snap:
                    sport, dport = unpack(">HH", ring, at)
                    flags, head = ring[at + 13], (ring[at + 12] >> 4) * 4
                    out.append((index, kind == 4, v6, 6, src, dst, sport, dport, max(0, length - head), flags & 0x12 == 2))
            ring[f:f + 4] = b"\0\0\0\0"                  # the kernel's again
            self.at = (self.at + 1) % self.frames
        packets, drops = struct.unpack("=II", self.s.getsockopt(SOL_PACKET, 6, 8))
        return out, packets / float(packets - drops) if 0 < drops < packets else 1.0


class Owners(object):
    """Which process has a socket, and what it's called."""

    def __init__(self):
        self.by_inode, self.names = {}, {}
        self.scanned, self.unknown = 0.0, set()

    def pid(self, inode):
        if not inode:
            return 0
        pid = self.by_inode.get(inode)
        if pid is None:
            self.unknown.add(inode)
        return pid or 0

    def look(self):
        """Find the owners of sockets that have none yet: going through every process takes a while."""
        mono = time.monotonic()
        if not self.unknown or mono - self.scanned < 1.0:
            return
        self.unknown = set()
        found = {}
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return
        for p in pids:
            try:
                for fd in os.listdir("/proc/%s/fd" % p):
                    try:
                        link = os.readlink("/proc/%s/fd/%s" % (p, fd))
                    except OSError:
                        continue
                    if link.startswith("socket:["):
                        found.setdefault(int(link[8:-1]), int(p))
            except OSError:
                continue
        for inode in found:
            self.by_inode.pop(inode, None)
        self.by_inode = found
        self.names = {}
        took = time.monotonic() - mono
        self.scanned = time.monotonic() + 9 * took      # a tenth of the time at most

    def name(self, pid):
        if not pid:
            return ""
        n = self.names.get(pid)
        if n is None:
            n = self.names[pid] = printable(read("/proc/%d/comm" % pid, 64) or "", 32)
        return n


def process_about(pid):
    """What the bubble shows of a process. Command lines aren't among it."""
    out = {"pid": pid, "name": "", "path": "", "desc": "", "company": "", "started": None, "user": "", "mem": None,
           "threads": 0, "parent": None, "services": []}
    status = dict(line.split(":", 1) for line in (read("/proc/%d/status" % pid) or "").splitlines() if ":" in line)
    out["name"] = printable(status.get("Name", ""), 32)
    try:
        out["path"] = printable(os.readlink("/proc/%d/exe" % pid), 200)
    except OSError:
        pass
    try:
        uid = int(status["Uid"].split()[0])
        try:
            out["user"] = pwd.getpwuid(uid).pw_name
        except KeyError:
            out["user"] = "uid %d" % uid
    except (KeyError, ValueError, IndexError):
        pass
    for key, field in (("RssAnon", "mem"), ("Threads", "threads")):
        f = status.get(key, "").split()
        if f and f[0].isdigit():
            out[field] = int(f[0]) * (1024 if len(f) > 1 else 1)
    try:
        parent = int(status.get("PPid", "0"))
        if parent > 1:
            out["parent"] = {"pid": parent, "name": printable(read("/proc/%d/comm" % parent, 64) or "", 32)}
        stat = (read("/proc/%d/stat" % pid) or "").rsplit(")", 1)[1].split()
        boot = re.search(r"^btime (\d+)", read("/proc/stat") or "", re.M)
        out["started"] = int(boot.group(1)) + int(int(stat[19]) / os.sysconf("SC_CLK_TCK"))
    except (ValueError, IndexError, AttributeError):
        pass
    units = re.findall(r"/([^/\n]+\.service)\b", read("/proc/%d/cgroup" % pid) or "")
    if units:
        out["services"] = [units[-1]]
    return out


class Ports(object):
    """This machine's traffic per port. One thread drives it: tick()."""

    MAX_DETAIL_PROCS, MAX_DETAIL_CONNS = 4, 6

    def __init__(self, own_port=0):
        self.own_port = own_port
        self.lock = threading.Lock()
        self.diag = Sockets()
        self.headers, self.headers_error = None, ""
        self.addr_index, self.tcp_inodes = {}, {}       # address -> its interface; (local, remote port) -> inode
        self.owners = Owners()
        self.known = {}                                # cookie -> (sent, received) so far
        self.conns, self.listening, self.udp = {}, {}, {}
        self.sent_udp = {}                             # local port -> when UDP was last sent from it
        self.addresses, self.all_addresses = {}, set()
        self.listen_at = self.address_at = 0.0
        self.seen = {}                                 # port -> {pid: when}
        self.last = None
        self.probes = []
        self.look(0.0)
        for c in self.tcp():
            if c[9] is not None:
                self.known[c[0]] = (c[9], c[10])
        self.diag.finished()

    def capture(self, on):
        """Watch packets too: for UDP, and probes (and TCP, where the kernel doesn't count it)."""
        with self.lock:
            self._capture(on)

    def _capture(self, on):
        if on and self.headers is None:
            try:
                self.headers = Headers(all_tcp=not self.diag.counts)
                self._refilter()
                self.headers_error = ""
            except OSError as exc:
                self.headers_error = exc.strerror or str(exc)
        elif not on and self.headers is not None:
            self.headers.close()
            self.headers = None

    def _refilter(self):
        if self.headers is not None:
            self.headers.refilter(set(a for a in self.all_addresses if len(a) == 4), set(a for a in self.all_addresses if len(a) == 16))

    def close(self):
        self.capture(False)

    def tcp(self):
        out = []
        for family in (socket.AF_INET, socket.AF_INET6):
            out += self.diag.dump(family, 6, 0xFFF & ~((1 << 10) | (1 << 6) | (1 << 7) | 1), True)
        return out

    def look(self, mono):
        """Now and then: addresses, and what's listening."""
        if mono >= self.address_at:
            self.address_at = mono + 5
            try:
                self.addresses = own_addresses()
                self.all_addresses = set().union(*self.addresses.values()) if self.addresses else set()
                self.addr_index = dict((a, index) for index, addrs in self.addresses.items() for a in addrs)
                self._refilter()
            except OSError:
                pass
        if mono >= self.listen_at:
            self.listen_at = mono + 0.5
            listening, udp = {}, {}
            for family in (socket.AF_INET, socket.AF_INET6):
                for c in self.diag.dump(family, 6, 1 << LISTENING, False):
                    listening.setdefault(c[4], []).append(c)
                for c in self.diag.dump(family, 17, 0xFFFFFFFF, False):
                    udp.setdefault(c[4], []).append(c)
            self.listening, self.udp = listening, udp
            old = mono - UDP_SENT_KEEP
            if len(self.sent_udp) > 1000:
                self.sent_udp = {p: t for p, t in self.sent_udp.items() if t >= old}

    def tick(self, traffic=True, views=(None,)):
        """What's happened since the last one, for each of views: None for every adapter, or an interface index.
        {view: {"dt", "in", "out", "si", "so", "p", "s"}}, as the app's own ticks."""
        with self.lock:
            mono = time.monotonic()
            dt, self.last = (mono - self.last if self.last else 0.0), mono
            self.look(mono)
            if self.headers is not None and self.headers.all_tcp == self.diag.counts:   # how TCP is counted changed
                self._capture(False)
                self._capture(True)
            tables = dict((v, ({}, {}, [0, 0, 0, 0])) for v in set(views) | {None})

            def add(lport, rport, size, udp, out, inode, index):
                if size <= 0:
                    return
                mine = lport == self.own_port and self.own_port
                pid = self.owners.pid(inode)
                for v in ((None, index) if index is not None and index in tables else (None,)):
                    ports, own, totals = tables[v]
                    table = own if mine else ports
                    for port, k in ((lport, 0), (rport, 1)):
                        if port:
                            e = table.get(port)
                            if e is None:
                                e = table[port] = [0, 0, 0, 0, 0]
                            e[2 * udp + k] += size
                            e[4] = pid or e[4]
                    totals[1 if out else 0] += size
                    if mine:
                        totals[3 if out else 2] += size
                if pid:
                    for port in (lport, rport):
                        if port:
                            self.seen.setdefault(port, {})[pid] = mono

            if traffic and self.diag.counts:
                now_here = {}
                current = self.tcp()
                for c in self.diag.finished() + current:
                    if c[9] is None:
                        continue
                    sent0, got0 = self.known.get(c[0]) or (1, 0)   # the SYN counts as a byte sent
                    here = c[3][:4] if c[1] == socket.AF_INET else c[3][12:] if c[3][:12] == V4_MAPPED else c[3]
                    index = self.addr_index.get(here)
                    add(c[4], c[6], c[9] - sent0, 0, True, c[8], index)
                    add(c[4], c[6], c[10] - got0, 0, False, c[8], index)
                    now_here[c[0]] = (max(c[9], sent0), max(c[10], got0))
                self.known = {c[0]: now_here[c[0]] for c in current if c[0] in now_here}
                self.conns = {c[0]: c for c in current}
            elif traffic:                              # who has which connection: TCP's bytes come from the headers
                current = self.tcp()
                self.conns = {c[0]: c for c in current}
                self.tcp_inodes = dict(((c[4], c[6]), c[8]) for c in current)
            if self.headers is not None:
                packets, factor = self.headers.read()
                for index, outgoing, v6, l4, src, dst, sport, dport, size, syn in packets:
                    here, lport, there, rport = (src, sport, dst, dport) if outgoing else (dst, dport, src, sport)
                    mine = here in self.addresses.get(index, ()) or (index == 1 and here in self.all_addresses)
                    if l4 == 17:
                        if outgoing:
                            if mine:
                                self.sent_udp[lport] = mono
                                if traffic:
                                    add(lport, rport, int(size * factor), 1, True, self._udp_inode(lport), index)
                        elif mine or (not v6 and (here[0] >= 224)) or (v6 and here[0] == 255):
                            if traffic:
                                add(lport, rport, int(size * factor), 1, False, self._udp_inode(lport), index)
                            if mine and lport not in self.udp and mono - self.sent_udp.get(lport, -1e9) > UDP_SENT_KEEP:
                                self._probe(there, v6, lport, 1, "closed", rport, here)
                    else:
                        if traffic and not self.diag.counts and (mine or here in self.all_addresses):
                            add(lport, rport, int(size * factor), 0, outgoing, self.tcp_inodes.get((lport, rport), 0), index)
                        if syn and not outgoing and mine and there not in self.all_addresses:
                            self._probe(there, v6, lport, 0, "open" if lport in self.listening else "closed", rport, here)
            self.owners.look()
            for port in [p for p, d in self.seen.items() if max(d.values()) < mono - 120]:
                del self.seen[port]

            def rows(table, named):
                return [[p, e[0], e[1], e[2], e[3]] + ([self.owners.name(e[4])] if named else []) for p, e in table.items()]
            return dict((v, {"dt": round(dt, 4), "in": t[0], "out": t[1], "si": t[2], "so": t[3],
                             "p": rows(ports, True), "s": rows(own, False)}) for v, (ports, own, t) in tables.items())

    def _udp_inode(self, port):
        c = self.udp.get(port)
        return c[0][8] if c else 0

    def _probe(self, there, v6, lport, udp, kind, rport, here):
        if len(self.probes) < MAX_PROBES:
            self.probes.append([round(time.time(), 3), ip_text(there, v6), lport, udp, kind, rport, ip_text(here, v6)])

    def take_probes(self):
        with self.lock:
            out, self.probes = self.probes, []
            return out

    def details(self, port):
        """The processes and connections on a port, as netwatch.describe returns them."""
        with self.lock:
            mono = time.monotonic()
            self.listen_at = 0.0
            self.look(mono)
            conns = []
            try:
                every = []
                for family in (socket.AF_INET, socket.AF_INET6):
                    every += self.diag.dump(family, 6, 0xFFF & ~((1 << 10) | 1), False)
            except OSError:
                every = list(self.conns.values())
            for c in every:
                conns.append((0, c))
            for group in self.listening.values():
                conns += [(0, c) for c in group]
            for group in self.udp.values():
                conns += [(1, c) for c in group]
            on_port = [(u, c) for u, c in conns if c[4] == port or c[6] == port]
            for _, c in on_port:
                self.owners.pid(c[8])
            self.owners.scanned = min(self.owners.scanned, mono - 1.0)
            self.owners.look()

            def addr(raw, v6, p):
                t = ip_text(raw, v6)
                return "[%s]:%d" % (t, p) if ":" in t else "%s:%d" % (t, p)
            listening, by_pid = [], {}
            for u, c in on_port:
                v6, pid = c[1] == socket.AF_INET6, self.owners.by_inode.get(c[8], 0)
                if c[4] == port and (u or c[2] == LISTENING):
                    listening.append({"proto": "UDP" if u else "TCP", "addr": ip_text(c[3], v6), "pid": pid})
                by_pid.setdefault(pid, []).append({
                    "proto": "UDP" if u else "TCP", "local": addr(c[3], v6, c[4]), "remote": addr(c[5], v6, c[6]) if c[6] else "",
                    "state": "" if u else TCP_STATES.get(c[2], ""), "end": "local" if c[4] == port else "remote"})
            rank = {"Established": 0, "Listening": 2, "Time wait": 3, "Closed": 3}
            seen = {pid: mono - t for pid, t in self.seen.get(port, {}).items() if mono - t < 120}
            order = sorted(seen, key=lambda pid: seen[pid])
            order += sorted((pid for pid in by_pid if pid not in seen), key=lambda pid: -len(by_pid[pid]))
            procs = []
            for pid in order[:self.MAX_DETAIL_PROCS]:
                p = process_about(pid) if pid else {"pid": 0, "name": "The kernel, or a process that's gone", "path": "", "desc": "", "company": "",
                                                    "started": None, "user": "", "mem": None, "threads": 0, "parent": None, "services": []}
                mine = sorted(by_pid.get(pid, []), key=lambda c: rank.get(c["state"], 1))
                p["seen"] = round(seen[pid], 1) if pid in seen else None
                p["nconns"] = len(mine)
                p["conns"] = mine[:self.MAX_DETAIL_CONNS]
                procs.append(p)
            return {"port": port, "listening": listening[:16], "procs": procs, "more": max(0, len(order) - self.MAX_DETAIL_PROCS),
                    "time": int(time.time())}


def dynamic_range():
    f = (read("/proc/sys/net/ipv4/ip_local_port_range") or "").split()
    return [int(f[0]), int(f[1])] if len(f) == 2 and f[0].isdigit() and f[1].isdigit() else [32768, 60999]


class Helper(object):
    """The ports service: an engine shared by whoever is connected, while anyone wants something of it.

    Lines in:   ticks <ms> (0: no more)   probes 0|1   port <n>   echo <ms> (0: no more)   procio <ms> (0: no more)
                nets <adapter>|all...    the traffic of these adapters (each a ports line of its own)
    Lines out:  info {...}   ports {..., "net": adapter}   probes {"t": now, "p": [...]}   port {...}   echo {...}
                procio {"t": now, "disks": {"*": [[name, pid, read B/s, written B/s, null, null]...]}, "e": ""}

    info says what's wrong, if anything is, and says it again while it is.

    echo is for an agent whose account may not ping (see EchoWatch): this service can.
    """

    def __init__(self, path, own_port, check_peer=True):
        self.path, self.own_port, self.check_peer = path, own_port, check_peer
        self.clients, self.lock = [], threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.engine = None
        self.echo = EchoWatch()
        self.names, self.names_at = {}, 0.0            # adapters' words -> their interface indexes
        self.procio_thread = None
        try:
            os.unlink(path)
        except OSError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        os.chmod(path, 0o666)                          # who may is decided by who they are
        self.sock.listen(8)
        self.sock.settimeout(0.5)

    @staticmethod
    def allowed(conn):
        """Root, or the agent's service: nobody else on this machine."""
        pid, uid, _ = struct.unpack("=iII", conn.getsockopt(socket.SOL_SOCKET, 17, 12))   # SO_PEERCRED
        return uid == 0 or uid == os.geteuid() or ("/%s.service" % NAME) in (read("/proc/%d/cgroup" % pid) or "")

    def serve(self):
        t = threading.Thread(target=self.work, name="work")
        t.daemon = True
        t.start()
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.check_peer and not self.allowed(conn):
                conn.close()
                continue
            conn.settimeout(5)
            c = {"conn": conn, "ms": 0, "probes": False, "due": 0.0, "told": False, "lock": threading.Lock(), "nets": [None],
                 "procio": 0}
            with self.lock:
                self.clients.append(c)
            t = threading.Thread(target=self.listen, args=(c,))
            t.daemon = True
            t.start()
        self.sock.close()

    def send(self, c, line):
        try:
            with c["lock"]:
                c["conn"].sendall(line.encode("utf-8"))
        except OSError:
            self.drop(c)

    def drop(self, c):
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)
        if "out" in c:
            self.echo.forget(c["out"])
        c["conn"].close()
        self.wake.set()

    def listen(self, c):
        buf = b""
        while True:
            try:
                data = c["conn"].recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            lines = (buf + data).split(b"\n")
            buf = lines.pop()
            if len(buf) > 256:
                break
            for raw in lines:
                text = raw.decode("ascii", "replace")
                f = text.split()
                if f and f[0] == "echo":                   # for an agent that may not ping
                    e = parse_command(text)
                    if e:
                        c.setdefault("out", Poster(self, c))
                        self.echo.want(c["out"], *e[1])
                    continue
                if f and f[0] == "nets":
                    c["nets"] = [None if n == "all" else n for n in f[1:9] if n == "all" or word(n) == n] or [None]
                    self.wake.set()
                    continue
                if len(f) != 2 or not f[1].isdigit():
                    continue
                n = int(f[1][:9])
                if f[0] == "ticks":
                    c["ms"] = max(MIN_PORTS_MS, min(10000, n)) if n else 0
                elif f[0] == "probes":
                    c["probes"] = bool(n)
                elif f[0] == "procio":
                    c["procio"] = max(250, min(60000, n)) if n else 0
                    with self.lock:
                        if c["procio"] and self.procio_thread is None:
                            self.procio_thread = threading.Thread(target=self.procio, name="procio")
                            self.procio_thread.daemon = True
                            self.procio_thread.start()
                elif f[0] == "port" and 0 < n < 65536:
                    try:
                        with self.lock:
                            engine = self.engine
                        own = engine or Ports(self.own_port)
                        self.send(c, "port " + json.dumps(own.details(n), separators=(",", ":")) + "\n")
                    except Exception as exc:  # noqa: BLE001
                        self.send(c, "port " + json.dumps({"port": n, "error": str(exc)[:200]}) + "\n")
                self.wake.set()
        self.drop(c)

    def procio(self):
        """Processes' disk I/O for the clients that want it, while one does."""
        io = ProcIO()
        while not self.stopping.is_set():
            with self.lock:
                clients = [c for c in self.clients if c["procio"]]
                if not clients:
                    self.procio_thread = None
                    return
            line = "procio " + json.dumps({"t": round(time.time(), 3), "disks": io.take(), "e": ""}, separators=(",", ":")) + "\n"
            for c in clients:
                self.send(c, line)
            self.stopping.wait(min(c["procio"] for c in clients) / 1000.0)

    def index_of(self, net):
        """An adapter's interface index, by its word: None for every adapter, -1 for one that isn't there."""
        if net is None:
            return None
        if time.monotonic() > self.names_at:
            self.names, self.names_at = {}, time.monotonic() + 5
            for path in glob.glob(ROOT + "/sys/class/net/*/ifindex"):
                name = os.path.basename(os.path.dirname(path))
                self.names[word(name)] = read_int("/sys/class/net/%s/ifindex" % name, -1)
        return self.names.get(net, -1)

    def work(self):
        idle_since = 0.0
        while not self.stopping.is_set():
            with self.lock:
                clients = list(self.clients)
            ms = [c["ms"] for c in clients if c["ms"]]
            probes = any(c["probes"] for c in clients)
            views = set(self.index_of(n) for c in clients if c["ms"] for n in c["nets"])
            if not ms and not probes:
                if self.engine is not None:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > 10:
                        self.engine.close()
                        self.engine = None
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            idle_since = 0.0
            try:
                if self.engine is None:
                    engine = Ports(self.own_port)
                    engine.capture(True)
                    with self.lock:
                        self.engine = engine
                started = time.monotonic()
                ticks = self.engine.tick(bool(ms), views or (None,))
                found = self.engine.take_probes()
            except Exception as exc:  # noqa: BLE001 - said once a while, and tried again
                problem = "%s: %s" % (type(exc).__name__, exc)
                log("ports: " + problem)
                if self.engine is not None:
                    self.engine.close()
                    with self.lock:
                        self.engine = None
                info = "info " + json.dumps({"dynamic": dynamic_range(), "udp": False,
                                             "error": "The omnitop-agent-ports service can't watch here: " + problem}) + "\n"
                for c in clients:
                    if c["ms"]:
                        c["told"] = False              # a proper one once it works
                        self.send(c, info)
                self.wake.wait(10)
                self.wake.clear()
                continue
            every = min(ms + ([500] if probes and not ms else [])) / 1000.0
            probes = "probes " + json.dumps({"t": round(time.time(), 3), "p": found}, separators=(",", ":")) + "\n"
            for c in clients:
                if c["ms"]:
                    if not c["told"]:
                        c["told"] = True
                        self.send(c, "info " + json.dumps({"dynamic": dynamic_range(), "udp": self.engine.headers is not None,
                                                           "error": self.engine.headers_error}) + "\n")
                    for n in c["nets"]:
                        d = dict(ticks.get(self.index_of(n)) or ticks[None], t=every)
                        if n:
                            d["net"] = n
                        self.send(c, "ports " + json.dumps(d, separators=(",", ":")) + "\n")
                else:
                    c["told"] = False
                if c["probes"] and found:
                    self.send(c, probes)
            took = time.monotonic() - started
            self.wake.wait(max(every - took, 3 * took, 0.02))   # a quarter of the time at most
            self.wake.clear()

    def stop(self):
        self.stopping.set()
        self.wake.set()


class ProcIO(object):
    """Every process's disk I/O, from /proc/<pid>/io (the bytes it had read from and written to storage),
    which takes root to read of other accounts' processes. Linux doesn't count it per disk, nor count
    operations: rows are for every disk together ("*"), with no reads or writes."""

    TOP = 16

    def __init__(self):
        self.last, self.at = {}, None

    def take(self):
        mono = time.monotonic()
        dt, self.at = (mono - self.at if self.at else None), mono
        now_io = {}
        try:
            pids = [p for p in os.listdir(ROOT + "/proc") if p.isdigit()]
        except OSError:
            pids = []
        for p in pids:
            text = read("/proc/%s/io" % p, 1024)
            if not text:
                continue
            got = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
            try:
                now_io[int(p)] = (int(got.get("read_bytes", 0)), int(got.get("write_bytes", 0)))
            except ValueError:
                continue
        rows = []
        if dt:
            for pid, (rb, wb) in now_io.items():
                old = self.last.get(pid)
                if old and (rb > old[0] or wb > old[1]):
                    rows.append((pid, max(0, rb - old[0]) / dt, max(0, wb - old[1]) / dt))
        self.last = now_io
        rows.sort(key=lambda r: -(r[1] + r[2]))
        out = [[printable(read("/proc/%d/comm" % pid, 64) or "", 32) or "PID %d" % pid, pid, round(r, 1), round(w, 1), None, None]
               for pid, r, w in rows[:self.TOP]]
        rest = rows[self.TOP:]
        if rest:
            out.append(["(other)", 0, round(sum(r[1] for r in rest), 1), round(sum(r[2] for r in rest), 1), None, None])
        return {"*": out}


class Poster(object):
    """A connection to the ports service, as something EchoWatch can post lines to."""

    def __init__(self, helper, c):
        self.helper, self.c = helper, c

    def post(self, line):
        self.helper.send(self.c, line)


def ports_helper(port):
    os.makedirs(os.path.dirname(PORTS_SOCKET), exist_ok=True)
    try:
        h = Helper(PORTS_SOCKET, port)
    except OSError as exc:
        raise SystemExit("The ports service can't listen on %s: %s" % (PORTS_SOCKET, exc))
    log("omnitop-agent-ports %s is ready" % VERSION)
    try:
        h.serve()
    except KeyboardInterrupt:
        h.stop()
    return 0


class PortsLink(object):
    """The agent's end: one connection to the ports service, for all the viewers that want something of it."""

    def __init__(self, own_port):
        self.own_port = own_port
        self.lock = threading.Lock()
        self.conn, self.path = None, PORTS_SOCKET
        self.wants = {}                                # viewer -> [ms, probes, adapter or None]
        self.asked = []                                # (viewer, port) whose details are on the way
        self.info = ""
        self.echo_ms, self.echo_sites, self.on_echo = 0, [], None   # round trips asked of the service, and where they go
        self.procio_wants = {}                         # viewer -> ms: processes' disk I/O

    def available(self):
        """Whether there's anything to ask. Run by root without being installed, the agent does it itself."""
        if os.path.exists(self.path):
            return True
        if not ROOT and hasattr(os, "geteuid") and os.geteuid() == 0:
            try:
                folder = tempfile.mkdtemp(prefix="omnitop-")
                atexit.register(shutil.rmtree, folder, True)   # not left behind (a --check during installs, say)
                h = Helper(os.path.join(folder, "ports"), self.own_port)
            except OSError:
                return False
            self.path = h.path
            t = threading.Thread(target=h.serve)
            t.daemon = True
            t.start()
            return True
        return False

    KEEP = object()

    def want(self, viewer, ms=None, probes=None, net=KEEP):
        with self.lock:
            w = self.wants.setdefault(viewer, [0, False, None])
            if net is not self.KEEP:
                w[2] = net
            if ms is not None:
                w[0] = ms
                if ms and self.info:
                    viewer.post("portsinfo " + self.info + "\n")
            if probes is not None:
                w[1] = probes
            if not w[0] and not w[1]:
                del self.wants[viewer]
            self._tell()
            if ms and self.conn is None:               # nobody to ask: say so, rather than send nothing
                viewer.post("portsinfo " + json.dumps({"dynamic": dynamic_range(), "udp": False, "error":
                            "The omnitop-agent-ports service isn't answering (sudo systemctl status omnitop-agent-ports says why)."}) + "\n")

    def forget(self, viewer):
        with self.lock:
            self.asked = [a for a in self.asked if a[0] is not viewer]
            if self.wants.pop(viewer, None) or self.procio_wants.pop(viewer, None):
                self._tell()

    def procio(self, viewer, ms):
        with self.lock:
            if ms:
                self.procio_wants[viewer] = ms
            else:
                self.procio_wants.pop(viewer, None)
            self._tell()
            if ms and self.conn is None:
                viewer.post("procio " + json.dumps({"t": round(time.time(), 3), "disks": {}, "e":
                            "Which processes do disk I/O takes the omnitop-agent-ports service, which isn't answering."}) + "\n")

    def echo(self, ms, sites=()):
        with self.lock:
            self.echo_ms, self.echo_sites = ms, list(sites)
            self._tell()

    def ask(self, viewer, port):
        with self.lock:
            if self._connected() and len(self.asked) < 32:
                self.asked.append((viewer, port))
                self._send("port %d\n" % port)

    def _tell(self):
        ms = [w[0] for w in self.wants.values() if w[0]]
        if (self.wants or self.echo_ms or self.procio_wants or self.conn) and self._connected():
            nets = sorted(set(w[2] or "all" for w in self.wants.values() if w[0])) or ["all"]
            self._send("ticks %d\nprobes %d\nnets %s\n" % (min(ms) if ms else 0, any(w[1] for w in self.wants.values()), " ".join(nets)) +
                       ("echo %s\n" % " ".join([str(self.echo_ms)] + ["%s=%s" % s for s in self.echo_sites])
                        if self.echo_ms or self.on_echo else "") +
                       "procio %d\n" % (min(self.procio_wants.values()) if self.procio_wants else 0))

    def _connected(self):
        if self.conn is None:
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.settimeout(3)
                conn.connect(self.path)
                conn.settimeout(None)
            except (OSError, AttributeError):
                return False
            self.conn = conn
            t = threading.Thread(target=self._read, args=(conn,), name="ports")
            t.daemon = True
            t.start()
        return True

    def _send(self, text):
        try:
            self.conn.sendall(text.encode("ascii"))
        except OSError:
            pass                                       # the reader notices

    def _read(self, conn):
        try:
            for raw in conn.makefile("rb"):
                line = raw.decode("utf-8", "replace")
                kind, _, rest = line.partition(" ")
                with self.lock:
                    wants = list(self.wants.items())
                    to = []
                    if kind == "info":
                        self.info = rest.strip()
                        line = "portsinfo " + rest
                        to = [v for v, w in wants if w[0]]
                    elif kind == "ports":                  # to the viewers of its adapter
                        m = re.search(r'"net":\s?"([^"]*)"', rest)
                        net = m.group(1) if m else None
                        to = [v for v, w in wants if w[0] and w[2] == net]
                    elif kind == "probes":
                        to = [v for v, w in wants if w[1]]
                    elif kind == "port":
                        m = re.match(r'\{"port":\s?(\d+)', rest)
                        port = int(m.group(1)) if m else 0
                        to = [v for v, p in self.asked if p == port]
                        self.asked = [a for a in self.asked if a[1] != port]
                    if kind == "procio":
                        to = list(self.procio_wants)
                    relay = self.on_echo if kind == "echo" else None
                for v in to:
                    v.post(line)
                if relay:
                    relay(line)
        except (OSError, ValueError):
            pass
        with self.lock:
            if self.conn is conn:
                self.conn = None
                self.info = ""
        conn.close()
        time.sleep(3)                                  # the service was restarted, say
        with self.lock:
            if (self.wants or self.echo_ms or self.procio_wants) and self.conn is None:
                self._tell()


# ---------------------------------------------------------------------------
# Latency: round trips from each adapter that has a default gateway to the sites a
# viewer names, or to the gateways (agent.py describes "echo"). Only addresses on the
# internet, and the gateways found here: a viewer can't have it time anything on this
# machine's own networks. Pings need no rights where net.ipv4.ping_group_range lets the
# account have ping sockets (systemd's default does); as root, a raw socket will do.
# Where the agent's account may do neither, the ports service, which may, does the
# pinging; without that, sites are timed by TCP handshakes, which need no rights. A site
# that doesn't answer pings is timed that way too.
# ---------------------------------------------------------------------------
MIN_ECHO_MS, MAX_ECHO_MS = 100, 10000
ECHO_TIMEOUT = 1.0           # seconds: a reply that takes longer is lost
ECHO_LOOK = 2.0              # between looks at which gateways there are
ECHO_TARGETS = 32
ECHO_SITES = 4
ECHO_PAYLOAD = b"OmniTOP latency." * 2
ECHO_TCP_AFTER = 6           # pings unanswered in a row before a site is timed by TCP handshakes
RESOLVE_EVERY = 300.0
GATEWAY_SITE = "gateway"
_SITE = re.compile(r"[a-z0-9_-]{1,16}=(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z")


def gateways():
    """adapter -> its default gateway (IPv4), the one with the lowest metric, from /proc/net/route."""
    best = {}
    for line in (read("/proc/net/route") or "").splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            dest, gw, flags, metric, mask = int(f[1], 16), int(f[2], 16), int(f[3], 16), int(f[6]), int(f[7], 16)
        except ValueError:
            continue
        if dest or mask or not gw or flags & 3 != 3:      # not a default route that's up and has a gateway
            continue
        if f[0] not in best or metric < best[f[0]][0]:
            best[f[0]] = (metric, socket.inet_ntoa(struct.pack("<I", gw)))
    return dict((name, v[1]) for name, v in best.items())


def ipv4_address(name):
    """An adapter's IPv4 address, or ""."""
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))   # SIOCGIFADDR
            return socket.inet_ntoa(r[20:24])
        finally:
            s.close()
    except (OSError, ImportError):
        return ""


_resolved = {}


def resolve_site(host):
    """A site's IPv4 address, looked up now and then. OSError if it has none, or none on the internet."""
    import ipaddress
    hit = _resolved.get(host)
    if hit and time.monotonic() < hit[1]:
        return hit[0]
    try:
        addrs = [i[4][0] for i in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)]
    except (OSError, UnicodeError) as exc:
        raise OSError("%s can't be looked up (%s)" % (host, getattr(exc, "strerror", None) or exc))
    addrs = [a for a in addrs if ipaddress.IPv4Address(a).is_global]
    if not addrs:
        raise OSError("%s isn't on the internet" % host)
    _resolved[host] = (addrs[0], time.monotonic() + RESOLVE_EVERY)
    return addrs[0]


def on_device(s, device):
    """Send through that adapter, whatever the routes say. Unprivileged since Linux 5.7; elsewhere, the address it's from does."""
    if device:
        try:
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), device.encode()[:15] + b"\0")
        except OSError:
            pass


def tcp_rtt(source, dest, device="", port=443):
    """The time a TCP handshake takes, in ms: one round trip. A refusal is an answer too."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # closed at once, nothing left over
        on_device(s, device)
        if source:
            s.bind((source, 0))
        s.setblocking(False)
        start = time.perf_counter()
        s.connect_ex((dest, port))
        _, done, failed = select.select([], [s], [s], ECHO_TIMEOUT)
        took = (time.perf_counter() - start) * 1000
        if not done and not failed:
            return None
        return took if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) in (0, errno.ECONNREFUSED) else None
    except OSError:
        return None
    finally:
        s.close()


def icmp_checksum(data):
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


class Pinger(object):
    """ICMP echo to one address."""

    def __init__(self, source, dest, device=""):
        self.dest, self.raw, self.seq = dest, False, 0
        try:
            self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EPERM):
                raise
            try:
                self.s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            except OSError:
                raise OSError(exc.errno, "this account may not ping (net.ipv4.ping_group_range)")
            self.raw = True
        self.ident = (os.getpid() ^ id(self)) & 0xFFFF
        on_device(self.s, device)
        if source:
            try:
                self.s.bind((source, 0))
            except OSError:
                pass

    def ping(self):
        """The round trip in ms, or None if no answer came in time."""
        self.seq = (self.seq + 1) & 0xFFFF
        packet = struct.pack("!BBHHH", 8, 0, 0, self.ident, self.seq) + ECHO_PAYLOAD
        if self.raw:
            packet = packet[:2] + struct.pack("!H", icmp_checksum(packet)) + packet[4:]
        start = time.perf_counter()
        self.s.sendto(packet, (self.dest, 0))
        while True:
            left = start + ECHO_TIMEOUT - time.perf_counter()
            if left <= 0:
                return None
            self.s.settimeout(left)
            try:
                data, there = self.s.recvfrom(2048)
            except socket.timeout:
                return None
            took = (time.perf_counter() - start) * 1000
            if self.raw:                               # with its IP header
                data = data[(data[0] & 15) * 4:] if data else b""
            if there[0] != self.dest or len(data) < 8:
                continue
            kind, _, _, ident, seq = struct.unpack("!BBHHH", data[:8])
            if kind == 0 and seq == self.seq and (not self.raw or ident == self.ident):
                return took

    def close(self):
        self.s.close()


class Prober(object):
    """Round trips from one adapter to one address: pings, or TCP handshakes once pings go unanswered,
    or from the start if this process may not ping."""

    def __init__(self, source, dest, device):
        self.source, self.dest, self.device = source, dest, device
        self.misses, self.method, self.pinger = 0, "icmp", None
        try:
            self.pinger = Pinger(source, dest, device)
        except OSError:
            self.method = "tcp"

    def ping(self):
        if self.method == "icmp":
            rtt = self.pinger.ping()
            if rtt is not None:
                self.misses = 0
            else:
                self.misses += 1
                if self.misses >= ECHO_TCP_AFTER and tcp_rtt(self.source, self.dest, self.device) is not None:
                    self.method = "tcp"
            return rtt, "icmp"
        return tcp_rtt(self.source, self.dest, self.device), "tcp"

    def close(self):
        if self.pinger:
            self.pinger.close()


def can_ping():
    """Whether this process may send ICMP echo requests: a ping socket, or a raw one."""
    for kind in (socket.SOCK_DGRAM, socket.SOCK_RAW):
        try:
            socket.socket(socket.AF_INET, kind, socket.IPPROTO_ICMP).close()
            return True
        except (OSError, AttributeError):
            pass
    return False


class EchoWatch(object):
    """Times round trips for the viewers that ask, and only while one does: one set of them, as often as
    the most eager viewer wants, to the sites any of them asked for (or the gateways), each on a thread
    of its own. If this process may not ping, the ports service (link) does, and its answers are passed on."""

    def __init__(self, link=None):
        self.lock = threading.Lock()
        self.wants = {}                                # viewer -> (ms, [(site, host)])
        self.ms, self.sites = 0, []
        self.results, self.workers, self.error = [], {}, ""
        self.thread, self.relook = None, False
        self.wake = threading.Event()
        self.link, self.via = link, None               # via: whether it's the ports service that pings

    def want(self, viewer, ms, sites=()):
        with self.lock:
            if ms:
                self.wants[viewer] = (ms, list(sites))
            else:
                self.wants.pop(viewer, None)
            self.ms = min(w[0] for w in self.wants.values()) if self.wants else 0
            sites = list(collections.OrderedDict.fromkeys(s for w in self.wants.values() for s in w[1]))[:ECHO_SITES]
            if sites != self.sites:
                self.sites, self.relook = sites, True
            if self.ms and self.via is None:
                self.via = bool(self.link) and not can_ping() and self.link.available()
                if self.via:
                    self.link.on_echo = self._relay
            if self.ms and self.thread is None and not self.via:
                self.thread = threading.Thread(target=self._run, name="latency")
                self.thread.daemon = True
                self.thread.start()
            via, ms, sites = self.via, self.ms, list(self.sites)
        if via:
            self.link.echo(ms, sites)
        self.wake.set()

    def _relay(self, line):
        with self.lock:
            viewers = list(self.wants)
        for v in viewers:
            v.post(line)

    def forget(self, viewer):
        self.want(viewer, 0)

    def stop(self):
        with self.lock:
            self.wants, self.ms = {}, 0
        self.wake.set()

    def _look(self):
        with self.lock:
            sites = list(self.sites)
        found, gws, problems = {}, {}, []
        for name, gw in sorted(gateways().items()):
            w = word(name)
            if not w:
                continue
            gws[w], source = gw, ipv4_address(name)
            for site, host in sites or [(GATEWAY_SITE, None)]:
                try:
                    dest = gw if host is None else resolve_site(host)
                except OSError as exc:
                    if str(exc) not in problems:
                        problems.append(str(exc))
                    continue
                if len(found) < ECHO_TARGETS:
                    found["%s|%s" % (w, site)] = (source, dest, name)
        with self.lock:
            for key, wk in list(self.workers.items()):
                if found.get(key) != wk[0]:
                    wk[1].set()
                    del self.workers[key]
            for key, target in found.items():
                if key not in self.workers:
                    stop = threading.Event()
                    self.workers[key] = [target, stop]
                    t = threading.Thread(target=self._ping, args=(key, target, stop), name="ping " + target[1])
                    t.daemon = True
                    t.start()
        return gws, "; ".join(problems)

    def _ping(self, key, target, stop):
        p = Prober(*target)
        try:
            due = time.perf_counter()
            while not stop.is_set():
                sent = time.time()
                try:
                    rtt, method = p.ping()
                except OSError as exc:
                    self.error, rtt, method = "Can't ping %s: %s" % (target[1], exc.strerror or exc), None, "icmp"
                    stop.wait(1.0)
                if stop.is_set():
                    return
                with self.lock:
                    adapter, site = key.rsplit("|", 1)
                    row = [adapter, round(sent, 3), None if rtt is None else round(rtt, 3)]
                    self.results.append(row + ([site, method] if self.sites else []))   # 2.2's rows, of gateways
                    ms = self.ms or 1000
                due += ms / 1000.0
                wait = due - time.perf_counter()
                if wait < 0:
                    due, wait = time.perf_counter(), 0
                stop.wait(wait)
        finally:
            p.close()

    def _run(self):
        gws, problem, look_at = {}, "", 0.0
        while True:
            with self.lock:
                ms = self.ms
                if not ms:                             # nobody wants it: end, and a new want starts it again
                    for w in self.workers.values():
                        w[1].set()
                    self.workers, self.results, self.thread = {}, [], None
                    return
                relook, self.relook = self.relook, False
            if relook or time.monotonic() >= look_at:
                (gws, problem), look_at = self._look(), time.monotonic() + ECHO_LOOK
            self.wake.wait(ms / 1000.0)
            self.wake.clear()
            with self.lock:
                done, self.results = self.results, []
                viewers = list(self.wants)
            if viewers:
                error = "; ".join(x for x in (self.error, problem) if x)
                line = "echo " + json.dumps({"t": round(time.time(), 3), "g": gws, "r": done, "e": error},
                                            separators=(",", ":")) + "\n"
                for v in viewers:
                    v.post(line)


# ---------------------------------------------------------------------------
# Memory, programs and graphics cards
# ---------------------------------------------------------------------------

def meminfo():
    out = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        name, colon, rest = line.partition(":")
        f = rest.split()
        if colon and f and f[0].isdigit():
            out[name] = int(f[0]) * (1024 if len(f) > 1 and f[1] == "kB" else 1)
    return out


def installed_memory(total):
    """RAM fitted, from the memory blocks the kernel manages; if it doesn't say, what it can use."""
    try:
        size = int((read("/sys/devices/system/memory/block_size_bytes", 64) or "0").strip(), 16)
    except ValueError:
        size = 0
    if size:
        blocks = [p for p in glob.glob(ROOT + "/sys/devices/system/memory/memory[0-9]*")
                  if (read(p[len(ROOT):] + "/online", 8) or "1").strip() == "1"]
        if blocks and size * len(blocks) >= total:
            return size * len(blocks)
    return total


class Programs(object):
    """RAM each program uses by itself, with its processes added up by name."""

    EVERY = 2.0

    def __init__(self):
        self.at, self.value = 0.0, None

    def sample(self):
        mono = time.monotonic()
        if self.value is not None and mono < self.at:
            return self.value
        self.at = mono + self.EVERY
        page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        progs = {}
        try:
            pids = [p for p in os.listdir(ROOT + "/proc") if p.isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            f = (read("/proc/%s/statm" % pid, 256) or "").split()
            if len(f) < 3 or f[1] == "0":              # gone, or a kernel thread
                continue
            name = (read("/proc/%s/comm" % pid, 64) or "").strip() or "PID %s" % pid
            g = progs.setdefault(printable(name, 32), [0, 0, 0, 0])
            g[0] += max(0, int(f[1]) - int(f[2])) * page
            g[1] += int(f[1]) * page
            g[3] += 1
        rows = sorted(progs.items(), key=lambda kv: kv[1][0], reverse=True)
        rest = rows[TOP_PROGRAMS:]
        self.value = {"top": [[n] + v for n, v in rows[:TOP_PROGRAMS]],
                      "other": [sum(v[0] for _, v in rest), sum(v[3] for _, v in rest), len(rest)],
                      "total": sum(v[0] for _, v in rows)}
        return self.value


class Gpu(object):
    """A graphics card with its own memory: NVIDIA's from nvidia-smi, AMD's from /sys."""

    MIN = 512 << 20
    EVERY = 2.0

    def __init__(self):
        self.smi = None if ROOT else shutil.which("nvidia-smi")
        self.at, self.value, self.busy = 0.0, None, False

    def _nvidia(self):
        try:
            def ask(what):
                return subprocess.run([self.smi] + what + ["--format=csv,noheader,nounits"], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=10,
                                      universal_newlines=True).stdout.splitlines()
            rows = [[x.strip() for x in line.split(",")] for line in
                    ask(["--query-gpu=index,name,memory.total,memory.used,utilization.gpu,utilization.encoder,utilization.decoder"])]
            rows = [r for r in rows if len(r) >= 5 and r[2].isdigit() and r[3].isdigit()]
            if not rows:
                self.smi = None
                return None
            r = max(rows, key=lambda r: int(r[2]))
            procs = {}
            for line in ask(["-i", r[0], "--query-compute-apps=pid,used_memory"]):
                f = [x.strip() for x in line.split(",")]
                if len(f) == 2 and f[0].isdigit() and f[1].isdigit():
                    name = (read("/proc/%s/comm" % f[0], 64) or "").strip() or "PID " + f[0]
                    procs[name] = procs.get(name, 0) + (int(f[1]) << 20)
            used, total = int(r[3]) << 20, int(r[2]) << 20
            engines = [[label, float(v)] for label, v in zip(("3D", "Video encode", "Video decode"), r[4:7])
                       if re.match(r"^\d+(\.\d+)?$", v)]
            top = sorted(procs.items(), key=lambda kv: kv[1], reverse=True)[:8]
            return {"name": printable(r[1]), "vram": total, "used": used, "shared": 0, "engines": engines,
                    "procs": [list(p) for p in top], "unowned": max(0, used - sum(procs.values()))}
        except (OSError, ValueError, subprocess.SubprocessError):
            self.smi = None
            return None

    def _amd(self):
        best = None
        for path in glob.glob(ROOT + "/sys/class/drm/card[0-9]*/device/mem_info_vram_total"):
            base = os.path.dirname(path)[len(ROOT):] + "/"
            total = read_int(base + "mem_info_vram_total")
            if total >= self.MIN and (best is None or total > best["vram"]):
                name = (read(base + "product_name", 128) or "").strip() or "AMD graphics"
                used = read_int(base + "mem_info_vram_used")
                best = {"name": printable(name), "vram": total, "used": used,
                        "shared": read_int(base + "mem_info_gtt_used"),
                        "engines": [["3D", float(read_int(base + "gpu_busy_percent"))]], "procs": [], "unowned": used}
        return best

    def _refresh(self):
        self.value = self._nvidia() if self.smi else self._amd()
        self.busy = False

    def sample(self):
        mono = time.monotonic()
        if mono >= self.at and not self.busy:
            self.at, self.busy = mono + self.EVERY, True
            if self.smi:                               # it can take a second: show the last figures meanwhile
                t = threading.Thread(target=self._refresh)
                t.daemon = True
                t.start()
            else:
                self._refresh()
        return self.value


class Memory(object):
    """This machine's memory, sampled for whoever asks: viewers that ask at about the same time share a sample."""

    def __init__(self):
        self.lock = threading.Lock()
        self.value, self.at, self.installed = None, 0.0, None
        self.programs, self.gpu, self.cpu = Programs(), Gpu(), Cpu()
        self.cpu.read()

    def sample(self, max_age):
        with self.lock:
            mono = time.monotonic()
            if self.value is None or mono - self.at > max_age:
                self.value, self.at = self._sample(), mono
            return self.value

    def _sample(self):
        m = meminfo()
        g = m.get
        total, free = g("MemTotal", 0), g("MemFree", 0)
        if self.installed is None:
            self.installed = installed_memory(total)
        shmem, dirty = g("Shmem", 0), g("Dirty", 0) + g("Writeback", 0)
        cache = max(0, g("Cached", 0) + g("Buffers", 0) + g("SReclaimable", 0) - shmem)
        dirty = min(dirty, cache)
        cache -= dirty
        inuse = max(0, total - free - cache - dirty)
        huge = g("HugePages_Total", 0) * g("Hugepagesize", 0)
        parts = [("proc", g("AnonPages", 0)), ("shared", shmem), ("huge", huge), ("slab", g("SUnreclaim", 0)),
                 ("ptables", g("PageTables", 0) + g("SecPageTables", 0))]
        known = sum(b for _, b in parts)
        scale = min(1.0, float(inuse) / known) if known else 0.0
        parts = [(k, int(b * scale)) for k, b in parts if b or k != "huge"]
        parts.append(("kernel", max(0, inuse - sum(b for _, b in parts))))
        swap = g("SwapTotal", 0)
        cpu = self.cpu.read()
        return {"ram": {"installed": self.installed, "total": total, "avail": g("MemAvailable", free + cache),
                        "err": None, "countersErr": None,
                        "lists": {"free": free, "modified": dirty, "standby": [0, cache, 0]},
                        "uses": [list(p) for p in parts], "kernel": [], "pagedAll": 0,
                        "progs": self.programs.sample()},
                "commit": None, "swap": {"total": swap, "used": max(0, swap - g("SwapFree", 0))},
                "cpu": round(cpu[0], 1) if cpu else None, "gpu": self.gpu.sample(), "gpuErr": None}


# ---------------------------------------------------------------------------
# Sampling: each viewer has its own, so they don't disturb each other
# ---------------------------------------------------------------------------

class Net(object):
    def __init__(self, name):
        self.name, self.word = name, word(name)
        self.card, self.link, self.last = True, "missing", None
        self.value, self.gone = (0.0, 0.0), 0.0


class Sampler(object):
    def __init__(self):
        self.cpu = Cpu()
        self.cpu.read()                                # percentages need two readings
        self.nets, self.disks = [], []
        self.all_nets = self.all_disks = False
        self.changed = False                           # the columns, since the viewer was told
        self.feeds = {}                                # those sent with each sample
        self.look_at = time.monotonic() + LOOK_EVERY

    def have_lines(self):
        names = sorted(n for n in net_counters() if adapter(n))
        return "have net %s\nhave disk %s\n" % (" ".join(word(n) for n in names), " ".join(word(d.replace("!", "_")) for d in disk_names()))

    def select_nets(self, names):
        self.all_nets = names == "all"
        self.nets = []
        counters = net_counters()
        if not self.all_nets:
            found = {n: adapter(n) for n in sorted(counters)}
            if names is None:
                cards = [n for n, a in found.items() if a and a["card"] and a["link"] != "down"]
                names = cards or [n for n, a in found.items() if a and a["link"] != "down"][:1]
            wanted = []
            for name in names:
                real = next((n for n in found if n == name or word(n) == name), name)
                if word(real) and real not in wanted:
                    wanted.append(real)
            self.nets = [Net(n) for n in wanted[:MAX_SELECTED]]
        self._look_nets(counters)

    def _look_nets(self, counters):
        mono = time.monotonic()
        if self.all_nets:
            here = {n.name for n in self.nets}
            for name in sorted(counters):
                a = adapter(name) if name not in here and word(name) else None
                if a and a["link"] != "down" and len(self.nets) < MAX_ALL:
                    self.nets.append(Net(name))
                    self.changed = True
            kept = [n for n in self.nets if n.name in counters or not n.gone or mono - n.gone < 30]
            if len(kept) != len(self.nets):
                self.nets, self.changed = kept, True
        for n in self.nets:
            a = adapter(n.name) if n.name in counters else None
            if a is None:
                n.gone = n.gone or mono
                n.last = None
            else:
                n.gone = 0.0
            link, card = (a["link"], a["card"]) if a else ("missing", n.card)
            if (link, card) != (n.link, n.card):
                n.link, n.card, self.changed = link, card, True

    def select_disks(self, names):
        self.all_disks = names in ("all", None)
        self.disks = []
        if self.all_disks:
            self._look_disks()
        else:
            here = {word(n.replace("!", "_")): n for n in disk_names()}
            self.disks = [Disk(here[n]) for n in names if n in here][:MAX_SELECTED]
        self.changed = True

    def _look_disks(self):
        old = {d.name: d for d in self.disks}
        names = disk_names()
        if names != [d.name for d in self.disks]:
            self.disks, self.changed = [old.get(n) or Disk(n) for n in names], True

    def lists(self):
        """The lines that describe the sample columns."""
        self.changed = False
        out = ["label net %s %s" % (n.word, printable(n.name)) for n in self.nets]
        if self.all_nets:
            out += ["kind net %s %s" % (n.word, "card" if n.card else "virtual") for n in self.nets]
        out.append(" ".join(["nets"] + ["%s:%s" % (n.word, n.link) for n in self.nets]))
        out += ["label disk %s %s" % (d.word, printable(d.name)) for d in self.disks]
        out += ["about %s %d %s" % (d.word, d.size, d.model) for d in self.disks]
        out.append(" ".join(["disks"] + [d.word for d in self.disks]))
        return "\n".join(out) + "\n"

    def sample(self, t):
        """One "s" line, or None while there's nothing to report."""
        cpu = self.cpu.read()
        counters = net_counters()
        for n in self.nets:
            c, last = counters.get(n.name), n.last
            n.last = (t, c[0], c[1]) if c else None
            if c and last and t > last[0] and c[0] >= last[1] and c[1] >= last[2]:
                dt = t - last[0]
                n.value = ((c[0] - last[1]) / dt, (c[1] - last[2]) / dt)
            else:
                n.value = (0.0, 0.0)
        stats = disk_counters() if self.disks else {}
        for d in self.disks:
            c, last = stats.get(d.name), d.last
            d.last = (t,) + c if c else None
            if c and last and t > last[0] and all(a >= b for a, b in zip(c, last[1:])):
                dt = t - last[0]
                d.value = ((c[0] - last[1]) / dt, (c[1] - last[2]) / dt, min(100.0, (c[2] - last[3]) / (dt * 10)))
            else:
                d.value = (0.0, 0.0, 0.0)
        if cpu is None:
            return None
        parts = ["s %.3f" % (t * 1000)] + ["%.1f" % x for x in cpu]
        for n in self.nets:
            parts += ["%.0f" % x for x in n.value]
        for d in self.disks:
            parts += ["%.0f" % d.value[0], "%.0f" % d.value[1], "%.1f" % d.value[2]]
        # A feed that goes with the samples comes just before the sample it was read with.
        return "".join(f.line(t) for f in self.feeds.values()) + " ".join(parts) + "\n"

    def look(self):
        """Every so often, between samples: what came, went or changed its link."""
        mono = time.monotonic()
        if mono < self.look_at:
            return
        self.look_at = mono + LOOK_EVERY
        self._look_nets(net_counters())
        if self.all_disks:
            self._look_disks()


# ---------------------------------------------------------------------------
# Serving viewers
# ---------------------------------------------------------------------------

def parse_command(line):
    """("interval", ms), ("mem", ms), ("net", names, "all" or None for auto), ("disk", ...), ("feed <name>", Feed or
    None), ("ports", ms), ("probes", bool), ("echo", ms) or ("port <n>", n); None for anything else."""
    if line.startswith("feed "):
        return parse_feed(line)
    f = line.split()
    if len(f) == 2 and f[0] in ("interval", "mem") and re.match(r"^[0-9]{1,9}$", f[1]):
        v = int(f[1])
        if f[0] == "interval":
            return "interval", max(MIN_MS, min(MAX_MS, v))
        return "mem", max(MIN_MEM_MS, min(MAX_MEM_MS, v)) if v else 0
    if len(f) == 2 and f[0] == "ports" and re.match(r"(off|[0-9]{1,6})$", f[1]):
        return "ports", 0 if f[1] == "off" else max(MIN_PORTS_MS, min(10000, int(f[1]))) if int(f[1]) else 0
    if len(f) == 2 and f[0] == "probes" and f[1] in ("on", "off"):
        return "probes", f[1] == "on"
    if len(f) == 2 and f[0] == "procio" and re.match(r"(off|[0-9]{1,6})$", f[1]):
        return "procio", 0 if f[1] == "off" or not int(f[1]) else max(250, min(60000, int(f[1])))
    if len(f) == 2 and f[0] == "portsnet" and (f[1] == "all" or word(f[1]) == f[1]):
        return "portsnet", None if f[1] == "all" else f[1]
    if 2 <= len(f) <= 2 + ECHO_SITES and f[0] == "echo" and re.match(r"(off|[0-9]{1,6})$", f[1]):
        sites = [tuple(x.split("=", 1)) for x in f[2:] if _SITE.match(x)]
        return "echo", (0 if f[1] == "off" or not int(f[1]) else max(MIN_ECHO_MS, min(MAX_ECHO_MS, int(f[1]))), sites)
    if len(f) == 2 and f[0] == "port" and re.match(r"[0-9]{1,5}$", f[1]) and 0 < int(f[1]) < 65536:
        return "port " + f[1], int(f[1])
    if f and f[0] in ("net", "disk"):
        return f[0], None if f[1:] == ["auto"] else "all" if f[1:] == ["all"] else f[1:1 + MAX_SELECTED]
    return None


class Viewer(object):
    """One connected viewer: a thread sends samples, another reads its commands, another sends memory and slow feeds."""

    def __init__(self, server, sock, peer):
        self.server, self.sock, self.peer = server, sock, peer
        self.pending = {}
        self.lock, self.sending = threading.Lock(), threading.Lock()
        self.wake, self.mem_wake = threading.Event(), threading.Event()
        self.gone, self.mem_ms, self.mem_thread, self.mem_due = False, 0, None, 0.0
        self.feeds = {}                                # those with a pace of their own
        self.outbox = collections.deque(maxlen=200)    # lines from the ports service

    def send(self, text):
        with self.sending:
            self.sock.sendall(text.encode("utf-8"))

    def start_thread(self, target, name):
        t = threading.Thread(target=target, name=name)
        t.daemon = True
        t.start()
        return t

    def read_commands(self):
        buf = b""
        try:
            while not self.server.stopping.is_set():
                try:
                    data = self.sock.recv(4096)
                except socket.timeout:
                    continue                           # quiet, not gone
                if not data:
                    break
                buf += data
                lines = buf.split(b"\n")
                buf = lines.pop()
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

    def post(self, line):
        """A line to send when it can be: not on the caller's thread, as this viewer may be slow."""
        self.outbox.append(line)
        self.mem_wake.set()

    def send_slowly(self):
        """Memory, the feeds with a pace of their own, and what the ports service says."""
        troubled = set()
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
            t, soonest = now(), now() + 60
            jobs = [("memory", self.mem_ms)] if self.mem_ms else []
            jobs += [(f, f.ms) for f in feeds]
            for job, ms in jobs:
                due = (self.mem_due if job == "memory" else job.due) or t
                if t >= due:
                    try:
                        if job == "memory":
                            line = "mem " + json.dumps(self.server.memory.sample(ms / 2000.0), separators=(",", ":")) + "\n"
                        else:
                            line = job.line(now())
                    except Exception as exc:  # noqa: BLE001 - the rest goes on without it, and it may work later
                        name, line, ms = job if job == "memory" else "feed " + job.name, "", 30000
                        if name not in troubled:
                            troubled.add(name)
                            self.server.log("%s: %s: %s: %s" % (self.peer, name, type(exc).__name__, exc))
                    if line:
                        try:
                            self.send(line)
                        except OSError:
                            self.gone = True           # it has stopped reading, or left
                            self.wake.set()
                            return
                    took = now() - t
                    due = max(due + ms / 1000.0, now() + took)   # never more than half the time
                    if job == "memory":
                        self.mem_due = due
                    else:
                        job.due = due
                soonest = min(soonest, due)
            if not self.outbox:
                self.mem_wake.wait(max(0.02, soonest - now()))
            self.mem_wake.clear()

    def set_feed(self, sampler, name, feed):
        with self.lock:
            self.feeds.pop(name, None)
            sampler.feeds.pop(name, None)
            if feed and len(self.feeds) + len(sampler.feeds) < MAX_FEEDS:
                (self.feeds if feed.ms else sampler.feeds)[name] = feed

    def run(self):
        log = self.server.log
        try:
            self.sock.settimeout(WRITE_TIMEOUT)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sampler = Sampler()
            self.send(self.server.hello(sampler.cpu.n) + self.server.system_line() + sampler.have_lines())
            sampler.select_nets(None)
            sampler.select_disks(None)
            self.send(sampler.lists())
            log("%s connected" % self.peer)
            self.start_thread(self.read_commands, "commands")
            self.stream(sampler)
            log("%s disconnected" % self.peer)
        except OSError as exc:
            log("%s disconnected: %s" % (self.peer, exc))
        except Exception as exc:  # noqa: BLE001 - one viewer's trouble stays its own
            log("%s: %s: %s" % (self.peer, type(exc).__name__, exc))
        finally:
            self.gone = True
            self.server.link.forget(self)
            self.server.echo.forget(self)
            self.mem_wake.set()
            self.sock.close()

    def stream(self, sampler):
        step = DEFAULT_MS / 1000.0
        last = now()
        due = last + step
        while not self.gone and not self.server.stopping.is_set():
            wait = due - now()
            if wait > 0:
                if not self.wake.wait(wait):
                    continue
                self.wake.clear()
                with self.lock:
                    todo, self.pending = self.pending, {}
                if "interval" in todo:
                    step = todo["interval"] / 1000.0
                    due = last + step                  # a whole new interval after the last reading
                if "net" in todo:
                    sampler.select_nets(todo["net"])
                if "disk" in todo:
                    sampler.select_disks(todo["disk"])
                for k in todo:
                    if k.startswith("feed "):
                        self.set_feed(sampler, k, todo[k])
                if "mem" in todo:
                    self.mem_ms, self.mem_due = todo["mem"], 0.0
                self.mem_wake.set()
                link = self.server.link
                if "ports" in todo or "probes" in todo or "portsnet" in todo:
                    link.want(self, todo.get("ports"), todo.get("probes"), todo["portsnet"] if "portsnet" in todo else link.KEEP)
                if "echo" in todo:
                    self.server.echo.want(self, *todo["echo"])
                if "procio" in todo:
                    link.procio(self, todo["procio"])
                for k in todo:
                    if k.startswith("port "):
                        link.ask(self, todo[k])
                if self.mem_thread is None:
                    self.mem_thread = self.start_thread(self.send_slowly, "slowly")
                if "net" in todo or "disk" in todo:
                    self.send(sampler.lists())
                continue
            last = now()
            line = sampler.sample(last)
            if line:
                self.send(line)
            sampler.look()
            if sampler.changed:                        # the samples from here on have these columns
                self.send(sampler.lists())
            due += step                                # absolute deadlines: no drift
            if due <= now():                           # fell behind (asleep, busy): start again from now
                due = now() + step


class Server(object):
    def __init__(self, listen, port, log):
        self.log, self.memory = log, Memory()
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_VIEWERS)
        if listen:
            self.sock = self._open(socket.AF_INET6 if ":" in listen else socket.AF_INET, (listen, port), False)
        else:
            try:
                self.sock = self._open(socket.AF_INET6, ("", port), True)
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    raise
                self.sock = self._open(socket.AF_INET, ("", port), False)   # IPv6 is off here
        self.port = self.sock.getsockname()[1]
        self.link = PortsLink(self.port)
        self.echo = EchoWatch(self.link)

    @staticmethod
    def _open(family, address, dual):
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name == "posix":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if dual else 1)
            s.bind(address)
            s.listen(16)
        except OSError:
            s.close()
            raise
        return s

    def hello(self, cpus):
        host = word(socket.gethostname()) or "unknown"
        kernel = (read("/proc/sys/kernel/osrelease", 128) or "unknown").strip().replace(" ", "_")
        return "cpugraph-agent %d host=%s cpus=%d kernel=%s version=%s os=linux omni=2 can=%s\n" % (
            PROTOCOL, host, cpus, kernel, VERSION, CAN + (",ports,portsnet,procio" if self.link.available() else ""))

    @staticmethod
    def system_line():
        boot = re.search(r"^btime (\d+)", read("/proc/stat") or "", re.M)
        return "sys " + json.dumps({"os": "linux", "system": system_name(), "distribution": distribution(),
                                    "machine": os.uname().machine if hasattr(os, "uname") else "",
                                    "boot": int(boot.group(1)) if boot else 0, "python": sys.version.split()[0]}) + "\n"

    def serve(self):
        self.sock.settimeout(0.5)                      # so Ctrl+C and stop() are noticed
        while not self.stopping.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if self.stopping.is_set():
                    break
                self.log("accept failed: %s" % exc)
                time.sleep(0.1)
                continue
            peer = addr[0][7:] if addr[0].startswith("::ffff:") else addr[0]
            if not self.slots.acquire(False):
                try:
                    conn.settimeout(1)
                    conn.sendall(b"err too many viewers\n")
                except OSError:
                    pass
                conn.close()
                continue
            t = threading.Thread(target=self._serve_one, args=(conn, peer))
            t.daemon = True
            t.start()
        self.sock.close()

    def _serve_one(self, conn, peer):
        try:
            Viewer(self, conn, peer).run()
        finally:
            self.slots.release()

    def stop(self):
        self.stopping.set()
        self.echo.stop()


def log(text):
    print(text, flush=True)                            # systemd's journal adds the time


def run(listen, port):
    try:
        srv = Server(listen, port, log)
    except OSError as exc:
        raise SystemExit("The agent can't listen on TCP port %d: %s" % (
            port, "another program is using it" if exc.errno == errno.EADDRINUSE else exc.strerror or exc))
    log("omnitop-agent %s listening on %s, TCP port %d" % (VERSION, listen or "all addresses", srv.port))
    try:
        srv.serve()
    except KeyboardInterrupt:
        srv.stop()
    return 0


# ---------------------------------------------------------------------------
# Starting with the system (systemd)
# ---------------------------------------------------------------------------

UNIT_TEXT = """[Unit]
Description=OmniTOP agent (lets OmniTOP on other machines watch this one)
Documentation=https://github.com/esteed/OmniTOP
After=network-online.target omnitop-agent-ports.service
Wants=network-online.target omnitop-agent-ports.service

[Service]
ExecStart=%(python)s -I %(program)s --port %(port)d%(listen)s
Restart=always
RestartSec=2
# Keep sampling on time when the machine is busy.
Nice=-5

# Runs as a throwaway unprivileged user: it only needs to read /proc and /sys
# and accept TCP connections.
DynamicUser=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
MemoryMax=128M
TasksMax=128

[Install]
WantedBy=multi-user.target
"""

# Root, for the few rights it's left: seeing packet headers, and which process has a socket.
PORTS_UNIT_TEXT = """[Unit]
Description=OmniTOP agent's port watcher (answers only omnitop-agent, on a unix socket)
Documentation=https://github.com/esteed/OmniTOP

[Service]
ExecStart=%(python)s -I %(program)s --ports-helper --port %(port)d
Restart=always
RestartSec=5
# 2: the program there doesn't know --ports-helper (an older version put back by hand).
RestartPreventExitStatus=2
RuntimeDirectory=omnitop-agent
RuntimeDirectoryMode=0755
CapabilityBoundingSet=CAP_NET_RAW CAP_SYS_PTRACE CAP_DAC_READ_SEARCH
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_PACKET AF_INET AF_INET6
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
MemoryMax=192M
TasksMax=64

[Install]
WantedBy=multi-user.target
"""


def command(*args, **kw):
    """Run a command; (exit status, output)."""
    try:
        p = subprocess.run(list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=kw.get("timeout", 60), universal_newlines=True)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def need_root(what):
    if os.geteuid() != 0:
        raise SystemExit("Run this as root: sudo python3 %s %s" % (sys.argv[0], what))


def port_free(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.settimeout(2)
            return s.makefile("rb").readline(256).decode("utf-8", errors="replace").strip() or "another program"
    except OSError:
        return ""


def firewall(port, add):
    """Open or close the port in firewalld or ufw, if one is running. Returns what was done.

    firewalld is told both for now and for good, and isn't reloaded: that would drop the
    rules that others (virtual machines, containers) have added while it runs.
    """
    spec, done = "%d/tcp" % port, "Opened" if add else "Closed"
    if shutil.which("firewall-cmd") and command("firewall-cmd", "--state")[0] == 0:
        zones = [z for z in command("firewall-cmd", "--get-active-zones")[1].splitlines() if z and not z[0].isspace()]
        changed = False
        for zone in [z.split()[0] for z in zones] or [""]:
            for lasting in ([], ["--permanent"]):
                args = ["firewall-cmd"] + lasting + (["--zone=" + zone] if zone else [])
                if (command(*(args + ["--query-port=" + spec]))[0] == 0) != add:
                    command(*(args + ["--add-port=" + spec if add else "--remove-port=" + spec]))
                    changed = True
        return "%s TCP port %d in firewalld." % (done, port) if changed else ""
    if shutil.which("ufw") and "Status: active" in command("ufw", "status")[1]:
        command("ufw", *(["allow", spec] if add else ["delete", "allow", spec]))
        return "%s TCP port %d in ufw." % (done, port)
    return ""


def installed_port():
    m = re.search(r"--port (\d+)", read(UNIT) or "")
    return int(m.group(1)) if m else 0


def try_out(python, program):
    """Run a copy of the agent briefly, on a port of its own, and see that it works. Returns what's wrong, or ""."""
    status, out = command(python, "-I", program, "--check", timeout=30)
    return "" if status == 0 else out or "it didn't start"


def check():
    """--check: serve on a spare loopback port, and read a sample and memory from it as a viewer would."""
    said = []
    srv = Server("127.0.0.1", 0, said.append)
    t = threading.Thread(target=srv.serve)
    t.daemon = True
    t.start()
    wanted = {"cpugraph-agent", "sys", "nets", "disks", "s", "mem", "feed"}
    ports, about = {"portsinfo", "ports", "port"}, ""
    try:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as c:
            c.settimeout(3)
            c.sendall(b'interval 100\nnet all\ndisk all\nmem 250\nfeed check sample {"files": ["/proc/stat"]}\n'
                      b'ports 100\nprobes on\nport 22\n')
            lines, deadline = c.makefile("rb"), time.monotonic() + 8
            while (wanted or ports) and time.monotonic() < deadline:
                line = lines.readline()
                if not line:
                    break
                word = line.split(b" ", 1)[0].strip().decode("ascii", "replace")
                wanted.discard(word)
                ports.discard(word)
                if word == "cpugraph-agent" and b",ports" not in line:
                    ports.clear()
                    about = "It can't see ports: that takes root, or the installed omnitop-agent-ports service."
                if word == "portsinfo" and b'"udp": false' in line:
                    about = "It sees TCP, but not UDP or probes: " + line.decode("utf-8", "replace").split('"error": ')[-1].strip(' }\n')
    except OSError as exc:
        said.append(str(exc))
    srv.stop()
    if ports:
        about = "Ports don't work here (no %s)." % ", ".join(sorted(ports))
    if wanted:
        raise SystemExit("It didn't send %s. %s" % (", ".join(sorted(wanted)), " / ".join(said[-3:])))
    print("omnitop-agent %s works here.%s" % (VERSION, " " + about if about else " Ports too."))
    return 0


def read_whole(path):
    """A file's bytes, or None."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def write_whole(path, data, mode):
    """Replace a file in one step, so there's never half of one."""
    with open(path + ".new", "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path + ".new", mode)
    os.replace(path + ".new", path)


def start_service(port, version, listen):
    """Start what's installed and wait for it. Returns what's wrong, or ""."""
    for args in (("daemon-reload",), ("enable", NAME), ("restart", NAME)):
        status, out = command("systemctl", *args)
        if status:
            return "systemctl %s failed: %s" % (" ".join(args), out)
    if listen and listen not in ("0.0.0.0", "::"):     # not necessarily reachable from here
        time.sleep(2)
        return "" if command("systemctl", "is-active", NAME)[0] == 0 else "it stopped again"
    for _ in range(20):
        time.sleep(0.5)
        there = port_free(port)
        if there.startswith("cpugraph-agent") and (not version or "version=%s " % version in there + " "):
            return ""
    return "it isn't answering on TCP port %d" % port


def install(port, listen, replace):
    need_root("--install")
    me = os.path.abspath(__file__)
    old_unit, old_program, old_ports = read_whole(UNIT), read_whole(INSTALLED), read_whole(PORTS_UNIT)
    old_port = installed_port()
    if not port:                                       # an upgrade keeps its port and address
        port = old_port or DEFAULT_PORT
        m = re.search(r"--listen (\S+)", (old_unit or b"").decode("utf-8", "replace"))
        listen = listen or (m.group(1) if m else "")
    if not shutil.which("systemctl") or not os.path.isdir("/run/systemd/system"):
        raise SystemExit("This machine doesn't use systemd. Start the agent your own way, with:\n"
                         "  python3 %s --port %d" % (me, port))
    python = os.path.realpath(sys.executable)
    wrong = try_out(python, me)                        # before anything is touched
    if wrong:
        raise SystemExit("This version doesn't work on this machine, so nothing was changed.\n" + wrong)
    command("systemctl", "stop", NAME)
    replaced = False

    def undo(why):
        """Put back what was running before, and stop."""
        command("systemctl", "stop", NAME)
        if old_unit and old_program:
            write_whole(INSTALLED, old_program, 0o644)
            write_whole(UNIT, old_unit, 0o644)
            if old_ports:
                write_whole(PORTS_UNIT, old_ports, 0o644)
            else:
                remove_ports()
            again = start_service(old_port, "", "")
            if old_ports:
                command("systemctl", "restart", PORTS_NAME)
            why += "\n" + ("The version that was there is running again." if not again else "The version that was there didn't start either: " + again)
        else:
            command("systemctl", "disable", NAME)
            remove_ports()
            for path in (UNIT, INSTALLED):
                try:
                    os.remove(path)
                except OSError:
                    pass
            command("systemctl", "daemon-reload")
            if replaced and command("systemctl", "enable", "--now", "cpugraph-agent")[0] == 0:
                why += "\ncpugraph-agent is running again."
        raise SystemExit(why)

    there = port_free(port)
    if there.startswith("cpugraph-agent") and "omni=" not in there:
        if not replace:
            undo("CPU Graph's cpugraph-agent is using port %d. Either replace it, with\n"
                 "  sudo python3 %s --install --replace\nor use another port, with\n"
                 "  sudo python3 %s --install --port %d" % (port, sys.argv[0], sys.argv[0], port + 1))
        command("systemctl", "disable", "--now", "cpugraph-agent")
        replaced = True
        time.sleep(0.5)
        there = port_free(port)
    if there:
        undo("TCP port %d is in use by something else. Choose another with --port." % port)
    os.makedirs(os.path.dirname(INSTALLED), exist_ok=True)
    os.chmod(os.path.dirname(INSTALLED), 0o755)
    if me != INSTALLED:
        write_whole(INSTALLED, read_whole(me), 0o644)
    if shutil.which("restorecon"):
        command("restorecon", "-R", os.path.dirname(INSTALLED))
    write_whole(UNIT, (UNIT_TEXT % {"python": python, "program": INSTALLED, "port": port,
                                    "listen": " --listen " + listen if listen else ""}).encode("utf-8"), 0o644)
    write_whole(PORTS_UNIT, (PORTS_UNIT_TEXT % {"python": python, "program": INSTALLED, "port": port}).encode("utf-8"), 0o644)
    wrong = start_service(port, VERSION, listen)
    if wrong:
        print(command("journalctl", "-u", NAME, "-n", "20", "--no-pager")[1])
        undo("The new version was installed, but %s." % wrong)
    for args in (("enable", PORTS_NAME), ("restart", PORTS_NAME)):
        command("systemctl", *args)
    for _ in range(10):
        time.sleep(0.3)
        if os.path.exists(PORTS_SOCKET) and command("systemctl", "is-active", PORTS_NAME)[0] == 0:
            break
    else:                                              # the rest works without it
        print("Port traffic isn't available: %s.service didn't start. (journalctl -u %s)" % (PORTS_NAME, PORTS_NAME))
    if old_port and old_port != port:
        firewall(old_port, False)
    done = firewall(port, True)
    if done:
        print(done)
    if replaced:
        print("It takes the place of cpugraph-agent, which is stopped. "
              "(To go back: systemctl disable --now omnitop-agent && systemctl enable --now cpugraph-agent)")
    print("omnitop-agent %s is running on TCP port %d and starts with the system.%s" % (
        VERSION, port, "" if old_unit else "\nIn OmniTOP, choose Remote, Add machine, and enter this machine's name or address."))
    return 0


def remove_ports():
    command("systemctl", "disable", "--now", PORTS_NAME)
    try:
        os.remove(PORTS_UNIT)
    except OSError:
        pass


def uninstall():
    need_root("--uninstall")
    port = installed_port()
    command("systemctl", "disable", "--now", NAME)
    remove_ports()
    for path in (UNIT, INSTALLED):
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(os.path.dirname(INSTALLED))
    except OSError:
        pass
    command("systemctl", "daemon-reload")
    done = firewall(port, False) if port else ""
    print("omnitop-agent is removed." + (" " + done if done else ""))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Lets OmniTOP on a Windows PC watch this Linux machine.")
    ap.add_argument("--port", type=int, default=0, help="TCP port (default %d, or the one it was installed with)" % DEFAULT_PORT)
    ap.add_argument("--listen", default="", help="an address to listen on (default all)")
    ap.add_argument("--install", action="store_true", help="start with the system (systemd)")
    ap.add_argument("--replace", action="store_true", help="with --install: stop cpugraph-agent if it has the port")
    ap.add_argument("--uninstall", action="store_true", help="remove the installed agent")
    ap.add_argument("--ports-helper", action="store_true", help=argparse.SUPPRESS)   # the omnitop-agent-ports service
    ap.add_argument("--check", action="store_true", help="see whether it works on this machine, and stop")
    ap.add_argument("--version", action="version", version="omnitop-agent " + VERSION)
    args = ap.parse_args()
    if not 0 <= args.port < 65536:
        raise SystemExit("--port must be between 1 and 65535.")
    if not re.match(r"^[0-9A-Fa-f:.%a-z]*$", args.listen):
        raise SystemExit("--listen must be an IP address.")
    if args.check:
        return check()
    if args.ports_helper:
        return ports_helper(args.port or DEFAULT_PORT)
    if args.install:
        return install(args.port, args.listen, args.replace)
    if args.uninstall:
        return uninstall()
    return run(args.listen, args.port or DEFAULT_PORT)


if __name__ == "__main__":
    sys.exit(main())
