# OmniTOP

A dashboard of what a Windows PC, and any number of other Windows and Linux machines,
are doing: CPU, memory, network, disks and graphics card, live, plus a map of this
PC's network traffic port by port.

- **Local** shows this PC: [Overview, CPU, Disk, Network](#overview-cpu-disk-and-network),
  [Memory](#memory) and [Ports](#ports).
- **Remote** lists the [other machines](#remote-machines) you watch. Click one's icon
  for its own Overview, CPU, Disk, Network and Memory.

The app opens on this PC's overview. **Esc**, or the logo, goes back there from any
page of this PC or of a remote machine. The version is at the top right, and in the
exe's file properties (Details).

Everything comes from the machines themselves: memory size, memory modules and slots,
the graphics card, drives and adapters, the ephemeral port range and so on are read,
not assumed.

## Overview, CPU, Disk and Network

- **Overview**: the CPU's average use, the network adapters, and the memory bar with
  its labels.
- **CPU**: a small graph per thread, or one of their average (C).
- **Disk**: a panel per drive letter on Windows, and per whole disk on Linux.
  - **The graph** has eight lines, each with a line to its label: throughput, IOPS,
    latency and busy time, reads solid and writes dashed. Each measure has its own scale,
    named along the top. The lines are smoothed over a second. Point at a label to make
    its line stand out, and click it to hide the line, or show it again; the other lines
    of that measure's scale fill the graph. Each disk remembers what's hidden.
  - **Pointing at the graph** marks the moment with a dot on each line, and the tip has
    what the lines show there: the second up to that moment, not the one sample under the
    pointer, which between bursts of I/O is mostly nothing.
  - **The bars** are the average I/O size and queue depth over the last 5 seconds.
  - Latency is how long an operation took, waiting included. The queue depth is how many
    were waiting or under way. Reads and writes overlap, so the time the disk was busy is
    shared out between them by how long each took.
  - On Windows, letters that Windows keeps no figures for, such as network drives and
    cloud storage, are named at the bottom. On Linux, partitions, LVM, RAID, loop devices
    and optical drives aren't shown: the disks under them are.
- **Network**: a graph per adapter. Green is received (↓) and coral is sent (↑), in bits
  per second. The axis runs to the link's speed, or fits the traffic for adapters whose
  link speed means nothing, such as VPNs.
  - **Latency and jitter**, the two bars at the end of each graph, are of the round trips
    to the adapter's default gateway (IPv4), named by its name, over the last 3 seconds.
    While the Network page shows, the gateway is pinged 4 times a second from the
    adapter's own address. Latency is the average round trip; jitter is how much one
    round trip differs from the next, on average; lost pings are counted under the bar.
    It's the first hop, the part of the way the adapter's own link decides. An adapter
    with no gateway, such as a storage network, says so. Point at the bars for the
    details.
  - Network cards are shown while they have a link.
  - Virtual adapters, such as a VPN's, are added once they carry real traffic: about 64
    kb/s for a second or more.
  - One that loses its link fades in and out for 10 seconds, and goes if it isn't back
    by then.

All graphs share one time axis, sampled 20 times a second by default. Every number is a
3-second average; the lines show the samples. The amber line is the 3-second average.
Hover a graph to read its value at that moment. At 20 samples/s the graphs show a third
of the chosen time span, so individual samples stay distinguishable.

| Key | Action |
|---|---|
| Esc | Back to this PC's overview |
| Space | Pause / resume the graphs (sampling continues) |
| Up / Down | Faster / slower sampling: 20, 10, 4, 2, 1 per second |
| Left / Right | Shorter / longer time span |
| C | On CPU: a graph per thread ↔ their average |
| M | On this PC's CPU: CPU time ↔ Processor utility (matches Task Manager) |
| A | Show / hide the 3-second average line |
| T | Always on top (desktop app) |

## Remote machines

The **Remote** tab lists the machines with each one's operating system, average CPU use,
network adapters and memory. From 12 machines up the graphs are smaller, and as many
machines go to a row as fit. From 100 up it's only numbers: the CPU, and the busiest disk.
**Show** picks one of these whatever the number. Click a machine for everything: the same
pages as for this PC, Ports included.

Under each machine's name is the IP address it's reached at (what its host name resolves
to, if it was added by name). A machine that can't be reached gets a red X over its icon
and **Connection Issues** under it, says why, and reconnects by itself.

**Found on your network.** OmniTOP looks for agents on this PC's networks: every address of
each IPv4 network of its adapters that are up (the /24 around this PC's address, for a
network wider than /22) is tried on port 7820, and on any other port a watched machine's
agent uses. Where something answers, only its first line is read, which says whether it's
an agent and which, and the connection is closed. That's a few hundred quick connection
attempts, a second or two, when the app starts and every 10 minutes while its window is
open. Agents it finds that aren't in the list are offered above it, with what they run:
**Add** watches one, **Add all** all of them, and **✕** stops it being offered. The line
under the list says which networks are looked at and when; **Look now** looks at once,
and **Stop looking** stops it for good (it's in the settings, like what isn't to be
offered). On a network someone else manages, ask first: a sweep like this can set off
their intrusion detection.

**Add machine** asks for the machine's host name or IP address, the agent's TCP port,
its operating system, and a name if you want another than its own. **Test connection**
tries it before you add it. The same form has the steps to install the agent, with the
commands filled in. Point at a machine in the list for the buttons that move, edit and
remove it.

Each machine runs an agent that streams its numbers:

- **Windows PCs**: the agent is part of `omniTop.exe`. Copy that to the PC and run
  `omniTop.exe --install-agent`. Windows asks for administrator rights, then it:
  - copies itself to `Program Files\omniTop agent`
  - adds a scheduled task, **omniTop agent**, that starts with Windows and runs as
    LOCAL SERVICE, an account with few rights; no one needs to be signed in
  - opens TCP port 7820 for it in Windows Firewall, on private and domain networks

  Run it again with a newer `omniTop.exe` to upgrade. `omniTop.exe --uninstall-agent`
  removes all of it. `omniTop.exe --agent` runs the agent without installing anything,
  until it's closed.
- **Linux machines**: the agent is one Python file,
  [omnitop-agent.py](linux/omnitop-agent.py), for Python 3.6 or later (up to 3.14). Copy
  it there and run `sudo python3 omnitop-agent.py --install`. It:
  - copies itself to `/usr/local/lib/omnitop-agent`
  - adds a systemd service, **omnitop-agent**, that runs as a throwaway account with no
    rights of its own
  - adds a second one, **omnitop-agent-ports**, for the Ports page (see below)
  - opens TCP port 7820 in firewalld or ufw, if one is running

  `--replace` stops CPU Graph's cpugraph-agent if it has the port, and `--uninstall`
  removes all of it. Without systemd, start `python3 omnitop-agent.py` your own way.

  On **Debian** (13, trixie, and earlier), a minimal install may have neither Python nor
  sudo. As root (`su -`), `apt install python3` if `python3 --version` finds none, and
  run `python3 omnitop-agent.py --install` without sudo. Debian runs no firewall unless
  one was set up: with your own nftables rules, let TCP port 7820 in yourself.

`--agent-port 7821` (Windows) or `--port 7821` (Linux) uses another port, and `--listen
192.168.1.20` one address only.

**Upgrading an agent** is the same command again; it keeps its port. It's all or nothing:
if the new version doesn't answer, the one that was there runs again. On Linux it's also
tried out first, before anything changes.

**Agents seldom need upgrading.** Since version 2.1 they don't decide what's worth showing:
besides the usual samples they send whatever raw figures OmniTOP asks for, and OmniTOP makes
sense of them. On Linux that's files of figures under `/proc` and `/sys`, filesystem sizes
and `nvidia-smi`'s columns; on Windows performance counters, drives' totals and sizes. Only
figures: serial numbers, hardware addresses, command lines and people's files aren't among
them, whoever asks. The same goes for ports: the agents send traffic per port and the
packets that may be probes, and which of those make a port scan is decided in OmniTOP. So
OmniTOP's scan rules can change without touching the agents. OmniTOP goes on working with
agents from 2.0 on, and says on the page when one can't send what that page needs.

Older agents work too: CPU Graph's own cpugraph-agent, and version 1.9's on Windows. They
send no memory, only throughput and busy time of disks, and no ports. Agents before 2.2
don't measure latency.

**Ports of remote machines.** A remote machine's Ports page is this PC's, for that machine:
the grids, the busiest ports, the bubbles about the processes on a pinned port, and port
scan alerts. Its traffic comes 5 times a second, and only while its Ports page is open. Scan
detection runs all the time for every connected machine, whatever page is open. A scan of a
remote machine shows on that machine's Ports page. The machine's card in the list says
it's being scanned, and then when it was, until you click that note (or press Delete on the
machine); the note is kept across restarts. Until you've seen the scan, the Remote tab and
the machine's Ports tab throb. White is the agent's own traffic: the stream to OmniTOP.
What it takes on each system:

- **Windows**: the agent watches the network the way OmniTOP does on this PC, with an ETW
  session of its own, and needs no more rights for it. The session runs only while some
  viewer wants ports or probes, and without ports wanted it only takes the events that
  tell probes. Details of processes it can't open (other accounts') come from what Windows
  tells any program: their path, start time and memory, but not their user.
- **Linux**: watching ports takes root: the packet headers that show UDP traffic and probes,
  and which process has which socket. So that the agent itself needn't run as root, a
  second service, **omnitop-agent-ports**, does that and nothing else. It runs the same
  file with only the rights it needs (`CAP_NET_RAW`, `CAP_SYS_PTRACE`,
  `CAP_DAC_READ_SEARCH`), a read-only system, and no network port: it listens on a unix
  socket, `/run/omnitop-agent/ports`, and answers only root and the omnitop-agent service.
  It captures only while the agent asks. TCP traffic comes from the kernel's socket
  statistics (`sock_diag`), UDP and probes from a packet socket with a filter that keeps
  only UDP headers and the first packet of each TCP connection. If it isn't running, the
  rest of the agent works as before, and the Ports page says why.

Latency takes ICMP echo (ping). The Windows agent uses Windows' own ping functions, which
need no rights. The Linux agent uses a ping socket, which needs none either where
`net.ipv4.ping_group_range` allows it, as systemd's default does; where it doesn't, the
omnitop-agent-ports service, which may send ICMP, does the pinging for it. Either way only
the gateways the agent finds itself are pinged: a viewer can't have it ping anything else.

The agents only read and run no commands. The connection isn't encrypted or authenticated:
anyone who can reach the port can read the machine's CPU, memory, network and disk activity,
the names of its programs, adapters and drives, and which programs talk to which addresses
on which ports. To limit that, restrict the firewall rule to your PC's address.

The app connects to the agents only while its window is open. The machine you're
looking at sends at the chosen rate. The others send up to 4 samples a second, or fewer
the more of them there are. Shown as numbers, they send no memory either.

The machines are kept in `%LOCALAPPDATA%\PortTrafficViz\settings.json`, which the app
writes, with whether to look for agents and the ones found that aren't to be offered. The
first time, it takes over the machines in CPU Graph's `settings.ini`.

## Ports

- Ports 1–1024 are a 32 × 32 block of 5 px bars in front.
- Ports 1025–65535 are a 256 × 252 field of 2 px bars behind it, 256 ports per row.
  The darker band is the ephemeral range: the short-lived local ports Windows
  assigns to outgoing connections. It's read from Windows when the app starts
  (49152–65535 by default; `netsh int ipv4 show dynamicport tcp` shows it). A
  remote machine's agent says its own (on Linux, 32768–60999 by default).
- Bar height shows traffic on a log scale, from 100 b/s up to the scale you pick.
  On the big field, color runs from blue for light traffic through violet to red
  for heavy traffic. Ports 1–1024 run from amber to red instead.
- Each grid's title runs along one of its edges.
- White is traffic the app causes itself, stacked on top of everything else on that
  port. **Hide own traffic** leaves it out of the bars, the totals and the list.
- Traffic is sampled 20 times a second by default. **Samples** changes that to 10, 5,
  2 or 1 a second (this PC only: remote machines send 5 a second). Each bar shows the average over about the last half second, or
  over one sample at 2 or 1 a second.
- The **Busiest ports** list updates once a second. Click a port there to pin it:
  pinned ports stay at the top of the list and are outlined on the grid until you
  click them again. You can pin as many as you like.
- Rest the pointer on a pinned port for 2 seconds to open a bubble about the
  processes using it:
  - each process's name, description, path, PID, user, memory and start time
  - the process that started it, and the services a svchost runs
  - its connections on that port
  - who's listening on the port

  A bubble stays open until you close it with **×**, and refreshes every few
  seconds. Up to four can be open side by side; trying a fifth says so.
- **Traffic jumps flash.** A port whose traffic suddenly rises well above its
  usual level gets a pulsing cyan patch on the grid and an ↑ in the list. So do
  the ports of a process whose traffic rises that way together. The rise has to
  be at least 4 times its usual level, and big next to everything else (at least
  2 Mb/s and a quarter of all traffic). Flashing stops 3 seconds after the
  traffic settles. A rise that keeps going becomes the new usual level after a
  minute or two.
- **In** and **Out** in the header are bars on the grid's log scale, up to
  1 Gb/s, colored like the big field's bars as traffic rises.
- **Port scans.** A scan is a methodical search through this machine's ports. A
  machine that probes a handful of them isn't scanning, and raises no alert. These
  do:

  | Who | How | Ports probed | Within |
  |---|---|---|---|
  | One machine | In any order | 25 | 1 minute |
  | One machine | In any order, slowly | 40 | 15 minutes |
  | One machine | In order, up or down, at most 8 apart | 15 | 5 minutes |
  | Several machines together | In order, up or down, at most 8 apart | 20 | 5 minutes |
  | Several machines together | In any order, each probing 5 or more that the others don't | 40, or 100 | 1 minute, or 15 |

  Only different ports count, and at least half must be closed or blocked ones.

  - **Alert.** An alert opens at the bottom right of the grid area. It shows who
    is scanning (one machine, or several in one alert), in what order, when it
    started, whether it's still going, and every port probed, with the ones that
    answered (open here) called out.
  - **View.** The view zooms out so both grids fit beside the alert.
  - **Port scan button.** The button next to All | TCP | UDP pulses bright red
    while the scan runs. Click it to bring back the latest alert.
  - **Ports tab.** Throbs red while a scan of the machine shown is under way, whatever
    page is open.
  - **Grid.** Each probed port's square lights up red for a second.
  - **Other tabs.** A scan that began while its Ports page wasn't showing makes the tabs
    that lead there throb until that page is opened: the Ports tab, Local if Remote is
    open, and for a remote machine the Remote tab. Its card in the list says so too.

  A scan is over once no probe has come for about 3 seconds, or 30 if several
  machines are making it, or up to 2 minutes for a slow one. If it starts again
  after that, it's a new scan with its own alert. Dismiss an alert with **×**.
- Scroll the mouse wheel over the grid to zoom in and out around the pointer, and
  drag with the left button to move around. **Reset view** goes back to the start.
- The sample rate and the Hide own traffic setting are remembered between runs.
- Each machine has its own pinned ports while the app runs.

## Memory

- **System memory**: all of RAM as one bar, from 0 on the left to the installed
  size on the right, with a scale sized to the memory found. It's split into what
  the memory holds, each kind in its own color:
  - in use, in greens for programs (process memory, compressed, mapped files,
    shared memory) and pinks for the kernel (paged pool, nonpaged pool, page
    tables, other kernel memory)
  - modified, standby and free
  - hardware reserved, the memory the firmware keeps for devices, at the far right
  Each kind is labelled with its size on a line that leaves the bar at an angle and
  runs to where there's room. Neighbours alternate above and below the bar, so small
  kinds next to each other spread out. A label that has no room is left off; the
  legend and hover still name it.
- Lines under the bar show the totals Task Manager uses: **In use**, **Available**
  (standby plus free), **Cached** (modified plus standby) and **Committed**. Committed
  memory can be more than RAM; then its line ends in an arrow.
- **Memory by program**: the 30 programs using the most RAM, each a segment on the
  same scale as the bar above, labelled the same way. A program's processes are
  added up by name, and its size is the private working set, as in Task Manager's
  Memory column. Then come the other programs, memory in use that no one program
  owns (shared and system), memory not in use, and hardware reserved. Hover a
  program for its process count, working set and committed memory.
- **Physical memory**: the memory slots as the firmware describes them, each module
  drawn as the part it is, grouped by channel, with its brand, size, type, speed,
  rank, voltage, part number and serial number made part of the picture: engraved
  in a heat spreader, printed on a bare module's circuit board, or etched on
  soldered chips. The firmware doesn't say what a module looks like, so the product
  line in its part number picks the look. Empty slots are drawn as open sockets.
  - **Addresses.** The physical address bar below shows what each address holds, from
    0 to the end of RAM: each column is a slice (64 MB or more, at most 600 of them),
    split into in use, modified, standby and free, with the part in use colored by
    what most of it holds. These are real physical addresses, the ones a kernel
    debugger or memory dump shows, with RAM starting and ending where Windows says
    it does. Gaps have no RAM: devices such as the graphics card are mapped there,
    usually just below 4 GB.
  - **Hover the bar** for a column's address range, its page frame numbers, what it
    holds and which parts of it are RAM. Click to copy the range's start address.
  - **Address** at the top points out an address typed in hex, with or without `0x`,
    as debuggers print them (``00000001`a2b3c000`` works), and says what its page
    holds: the list it's on, what it's used for, its priority, and for process memory
    the virtual address it's mapped at. **Esc** in the box clears it.
- **GPU memory**: the graphics card's own memory as one bar, split by the apps using
  it, memory not owned by one app, and free. A machine without a graphics card that has
  its own memory shows nothing about one.
- Hover anything for details: what each kind of memory is, the standby priorities,
  how much of the paged pool is in the page file, what "other kernel memory" holds,
  the page files, each program, each module, and each address slice's use.
- The page scrolls when it's taller than the window.
- **Samples** sets how often everything on this page updates: 20, 10, 4, 2 or 1 a
  second. **Up** and **Down** change it too. It's remembered between runs. The bars and charts follow every
  sample; the numbers and lists redraw at most 4 times a second, so they stay
  readable.
- **Utilization**: GPU 3D and CPU over the last 60 seconds, like Task Manager.
- **Committed memory**: over the last 60 seconds. This is the memory Windows has
  promised to programs, kept in RAM or the page file. It's drawn against memory in
  use and, as a dashed line, the commit limit (RAM plus page file). When committed
  memory reaches the limit, programs can't get more memory until something closes
  or the page file grows. Hover the **Committed** number in the header for the peak
  since Windows started.
- Below the charts are the GPU's other engines (copy, compute, video decode and
  encode), and the apps using the most GPU memory. "Not owned by one app" is the
  driver's memory and memory that apps share with each other.
- The finer split of memory in use and the physical address map need
  administrator rights, which live mode already has. Without them, the bar splits
  memory in use into programs, compressed, file cache, the pools and drivers, and
  the address bar shows where RAM is, without what each address holds. That's also what
  remote Windows PCs show.
- Remote Linux machines show the kinds of memory Linux has: process memory, shared
  memory, huge pages, kernel slab, page tables, other kernel memory, dirty, page cache
  and free, with swap in place of committed memory. They show programs, but not modules
  or addresses, which only root can read. Graphics cards are NVIDIA's (from `nvidia-smi`)
  and AMD's.

## Run it

Download `omniTop.exe` from the
[latest release](https://github.com/esteed/OmniTOP/releases/latest), or
build it yourself (see below).

| | |
|---|---|
| `omniTop.exe` | The desktop app. Windows asks for administrator rights, then the view opens in its own window. Close the window to stop. |
| `omniTop.exe --demo` | Simulated machines and traffic, with a port scan now and then, no administrator rights needed. |
| `start.cmd`, `start-demo.cmd` | The same view in your browser instead, run from source (needs Python). |

The exe needs nothing installed. It uses the Edge WebView2 runtime that comes with Windows 11.

If you decline the administrator prompt, the window offers **Try again** or
**Show simulated traffic**.

## Build the exe

Run `build.cmd`. It needs 64-bit Python 3.10 or later and, the first time, an
internet connection: it sets up pywebview, PyInstaller and Pillow in
`%LOCALAPPDATA%\PortTrafficViz\build` (outside Google Drive) and writes
`dist\omniTop.exe`. Rebuild after editing anything in `web\`, `linux\` or a `.py`.

The version is `VERSION` in `server.py`; raise it for each release. The build
stamps it into the exe's file properties. It also draws the icon: a ring split like
the memory bar around a performance graph, each size from 16 to 256 px drawn
separately.

## How it works

`server.py` starts a real-time ETW session on the `Microsoft-Windows-Kernel-Network`
provider, which reports every TCP and UDP send and receive with its byte count,
local port, remote port and process ID. It adds those up per port and pushes a
summary to the view at the chosen sample rate over Server-Sent Events.

The app's own traffic is mostly that stream, from the capture process to the
window over 127.0.0.1. Capture counts as its own any traffic on loopback
connections to its own web server port, plus anything sent by its own processes:
the capture process, the window and the window's browser engine (WebView2). In
demo mode nothing is captured, so the server counts the bytes it sends instead.

`desktop.py` is the app. Its window runs with your normal rights. Capture runs in
a second, elevated copy of the exe that has no window and quits when the window
closes. If you start the exe as administrator, capture runs in the window's own
process instead.

Each connection has two ports. A browser fetching a web page uses a dynamic local
port (say 52814) and remote port 443. The **Both ends** filter counts those bytes
at both ports, **Local** only at 52814, and **Remote** only at 443.

`netwatch.py` is behind the bubbles and the scan alerts.

- **Bubbles**: the port's connections and listeners come from the same tables as
  `netstat -ano`. The processes on it come from those, plus the ones capture saw
  moving data on it in the last two minutes. Process details come from the
  process itself, its file's version information, and the service manager.
  Everything shown is what Task Manager and netstat show any user. Command lines
  are left out, so the elevated capture process doesn't pass anything more
  private to the window.
- **Scans**: capture also listens to the `Microsoft-Windows-TCPIP` provider, for
  event 1214. That's a packet the transport layer dropped, such as a probe of a
  port nothing listens on that Windows Firewall discarded. It also counts
  accepted connections (Kernel-Network's accept events) for the ports that
  answer.
- **What isn't a probe**: much of what Windows drops isn't aimed at finding ports,
  and is left out.
  - Anything to or from an address that isn't one machine's: broadcast, including
    the subnet's, multicast and loopback, and this PC's own addresses. A scan of this
    PC from itself doesn't count, so test with another machine.
  - Replies to this PC. Resets from a service that refuses its connections, late
    packets of connections it closed, and answers to UDP it sent all arrive at ports
    of its own. Capture reports every TCP connection it starts (event 1002,
    whether or not it succeeds) and every port it sends UDP from, and the
    connection tables fill in what was there before. They're remembered for 15 and 5
    minutes. Packets from a well-known port (below 1024) to a higher one are replies
    too, and so are ones that all come from the same service port.
  - Traceroutes (UDP 33434–33534).
- **In order** means the longest run of ports, each probed after the one before and
  at most 8 from it, so a few out of place don't hide it. Most of the machine's
  ports must belong to the run.
- **Several machines**: machines that look for the same services probe the same
  ports, and keep doing it. Machines sharing out a scan hardly ever probe the same
  one. A scan that comes from many made-up addresses at once (decoys) probes the same
  ports from each, and gets one alert.
- **Not seen**: scans too slow for the table above, and half-open probes of ports
  that are open. The closed ports give the scan away.
- **Without Windows Firewall**: Windows answers probes of closed ports with a reset
  instead of ignoring them. On loopback they're reported as dropped all the same;
  that hasn't been tested with another machine.

`sysmon.py` samples memory, the GPU and the CPU at the rate chosen on the Memory page
and sends the result on the same stream.

- **System memory totals**: modified, standby (by priority), free, the pools and
  driver memory come from the `Memory` performance counters Resource Monitor reads.
  Memory in use is what's left of the usable RAM. Compressed memory is the working
  set of the Memory Compression process, read every 2 seconds because the
  `Process` counters are slow to collect. The page files come from
  `NtQuerySystemInformation` (`SystemPageFileInformation`).
- **System memory map**: it reads a sample of about 65,000 entries from Windows'
  page database, around 1,000 per GB. This uses the same undocumented interface
  as Sysinternals RAMMap (`NtQuerySystemInformation` with
  `SystemSuperfetchInformation`). Each entry says which list a page is on (in use,
  modified, standby, free) and what it's used for. That splits memory in use into
  process memory, mapped files, shared memory, page tables and other kernel memory
  (the pools come from the counters, which are exact), and gives each address
  slice its mix. Reading them all takes about 30 ms on a 128 GB machine. At faster
  rates it reads an interleaved part of them each time (a twentieth at 20 a
  second), so the whole set is read once a second at any rate. The address map
  goes to the view at most 4 times a second. The
  Address box's page is one more entry from the same place.
- **Physical addresses**: where RAM is comes from the same interface, or without
  administrator rights from the resource map in the registry
  (`HARDWARE\RESOURCEMAP\System Resources\Physical Memory`).
- **Programs**: one `NtQuerySystemInformation` call (`SystemProcessInformation`) lists
  every process with its private working set, working set and commit, once a
  second; no rights needed.
- **Memory modules**: from the firmware's SMBIOS tables (`GetSystemFirmwareTable`,
  no rights needed): memory arrays and their slots (type 16), each slot's module
  (type 17), and where memory sits in address space (type 19). The brand comes
  from the part number when it's a known one, since the firmware often names the
  DRAM maker or gives a JEDEC code instead. Serial numbers are shown as the firmware
  gives them; they stay on this PC like everything else here.
- **GPU**: its memory size comes from D3DKMT. Use and engine utilization come from
  the performance counters Task Manager reads (`GPU Engine`, `GPU Adapter Memory`,
  `GPU Process Memory`).
- **CPU**: from `GetSystemTimes`.
- **Committed memory**: total, limit and peak come from `GetPerformanceInfo`.

`perfmon.py` started as [CPU Graph](https://github.com/esteed/CpuGraph)'s sampler, ported
to Python. It reads everything for this PC each tick, on its own clock, at the rate
chosen on its pages.
- **CPU threads**: all of them come from one `NtQuerySystemInformation` call.
- **Network**: each adapter's counters and link speed come from one `GetIfEntry2` call.
  `NetTracker` picks the ones to show, for this PC and for remote machines alike.
- **Disks**: each drive letter's running totals, from `IOCTL_DISK_PERFORMANCE`, which the
  `LogicalDisk` performance counters are made from. `DiskRates` works out everything from
  running totals, for this PC, for remote Windows PCs (the same call) and for remote Linux
  machines (`/proc/diskstats`).

Each remote machine has its own thread, which connects to its agent, reads samples
and reconnects every 5 s after a failure. Agent timestamps are mapped onto the app's
clock using the fastest network trip seen so far, so all rows line up.

`agent.py` is the Windows agent and `linux/omnitop-agent.py` the Linux one. They speak
`cpugraph-agent`'s protocol (version 2), with additions that `agent.py` describes: every
adapter and disk, memory, feeds of raw figures, ports, and latency. For a remote machine's scans,
the agent leaves out what only it can tell isn't a probe (replies to connections it made,
and to UDP it sent; `netwatch.ProbeFilter` on Windows), and the app runs a `ScanWatch` for
each machine on what's left. The Windows agent uses the
readers `perfmon.py` and `sysmon.py` use for this PC, and needs no rights of its own; the
Linux one reads `/proc` and `/sys`. Names in the protocol are single words, so `Realtek
2.5GbE` travels as `Realtek_2.5GbE`, with a `label` line carrying the real name. Each of
up to 16 viewers has its own readings and sample rate. With no one watching, they use no
CPU. The Windows agent keeps a short log in
`%LOCALAPPDATA%\PortTrafficViz\agent.log` (installed, under LOCAL SERVICE's profile:
`C:\Windows\ServiceProfiles\LocalService`).

`latency.py` pings adapters' gateways: their gateways come from `GetIpForwardTable2`, their
addresses from `GetUnicastIpAddressTable`, and the round trips, timed with the performance
counter, from `IcmpSendEcho2Ex`. Its `Window` turns round trips, this PC's or an agent's
(the `echo` lines), into the average, jitter and losses of the last 3 seconds. `discover.py`
looks for agents on this PC's networks.

`settings.py` keeps the list of remote machines. The page is `web/`: `shell.js` has the
tabs, `perf.js` the graphs, `disk.js` the disk panels, `views.js` the pages of one machine,
`remote.js` the list of machines, what's found on the network and the form, `memory.js` and
`dimm.js` memory, `app.js` and `watch.js` the ports.

The server only listens on 127.0.0.1 and refuses requests whose `Host` header
isn't its own address.

## Notes

- OmniTOP was called Port traffic until version 1.5. It keeps that version's settings,
  and still uses its folder, `%LOCALAPPDATA%\PortTrafficViz`.
- For live capture, the program copies itself to `%LOCALAPPDATA%\PortTrafficViz`
  and runs the elevated copy from there, because elevated processes can't always
  see Google Drive or mapped network drive letters.
- The exe isn't code-signed. Some antivirus products are wary of PyInstaller
  executables in general; if yours quarantines it, add an exception or use
  `start.cmd` instead.
- This shows traffic to and from this machine's own sockets, not other devices
  on your network.
- Under very heavy traffic (hundreds of thousands of packets a second), Python
  can fall behind and ETW drops events, so the bars read low.
