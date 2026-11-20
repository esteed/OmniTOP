# omniTop

A live isometric view of this Windows machine's network traffic, one bar per port,
and of its memory and GPU, one bar per gigabyte, plus real-time graphs of CPU,
network and disk activity for this PC and Linux machines on your network. The views
are tabs along the top of the window: **Network ports**, **Memory & GPU** and
**CPU, disk & network**. The app remembers which tab you had open.

## Network ports

- Ports 1–1024 are a 32 × 32 block of 5 px bars in front.
- Ports 1025–65535 are a 256 × 252 field of 2 px bars behind it, 256 ports per row.
  The darker band is the ephemeral range, 49152–65535: the short-lived local ports
  Windows assigns to outgoing connections.
- Bar height shows traffic on a log scale, from 100 b/s up to the scale you pick.
  On the big field, color runs from blue for light traffic through violet to red
  for heavy traffic. Ports 1–1024 run from amber to red instead.
- Each grid's title runs along one of its edges.
- White is traffic the app causes itself, stacked on top of everything else on that
  port. **Hide own traffic** leaves it out of the bars, the totals and the list.
- Traffic is sampled 20 times a second by default. **Samples** changes that to 10, 5,
  2 or 1 a second. Each bar shows the average over about the last half second, or
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
- **Port scans.** When one address probes 10 or more different ports of this
  machine within 30 seconds, it's a scan:
  - **Alert.** An alert opens at the bottom right of the grid area. It shows who
    is scanning, when it started, whether it's still going, and every port
    probed, with the ones that answered (open here) called out.
  - **View.** The view zooms out so both grids fit beside the alert.
  - **Port scan button.** The button next to All | TCP | UDP pulses bright red
    while the scan runs. Click it to bring back the latest alert.
  - **Grid.** Each probed port's square lights up red for a second.
  - **Other tabs.** If another tab is open, the Network ports tab flashes red.
    After the scan, a red dot stays on it until you open it.

  A scan is over once no probe has come for about 3 seconds, or up to 10 for a
  slow one. If the same address starts again after that, it's a new scan with
  its own alert. Dismiss an alert with **×**.
- Scroll the mouse wheel over the grid to zoom in and out around the pointer, and
  drag with the left button to move around. **Reset view** goes back to the start.
- The sample rate and the Hide own traffic setting are remembered between runs.

## Memory & GPU

- **System memory**: one bar per GB of physical address space, in address order
  (8 or 16 GB per row). Bar height and color show how much of that GB is in use,
  from blue (little) to red (all of it). Hover a bar for the split between in use,
  cached and free, and what the in-use memory is (process memory, file mappings,
  kernel pools...). A dashed outline marks a GB with no RAM at all, and the
  tooltip says when a GB is only partly RAM: devices such as the graphics card
  are mapped into part of the address space, usually just below 4 GB.
- **GPU memory**: one bar per GB of the graphics card's own memory. Windows reports
  how much is in use but not where on the card it's stored, so these fill in
  order rather than by address.
- Scroll to zoom and drag to move around, the same as on the Network ports tab.
  **Reset view** goes back to the start.
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
- These update once a second. The per-GB system memory map needs administrator
  rights, which live mode already has. Without them, the grid shows how much
  memory is in use, filled in order.

## CPU, disk & network

This tab is [CPU Graph 2.0](https://github.com/esteed/CpuGraph) brought into the app.
It has one row per machine: **This PC** first, then each Linux machine running
`cpugraph-agent`. Each row has three columns:

- **CPU threads**: one small graph per logical processor. The row title shows the
  machine's name, thread count and total CPU use.
- **Network adapters**: one strip per adapter. Green is received (↓) and coral is
  sent (↑), in bits per second. The axis runs to the adapter's link speed, or fits
  the traffic for adapters that don't report a speed.
- **Disk devices**: one strip per disk. Violet is read (R) and pink is written (W),
  in bytes per second, with how busy the disk was. The axis fits the larger of the
  two, in 1-2-5 steps from 1 MB/s.

All graphs share one time axis, sampled 20 times a second by default. Every number
is a 3-second average; the lines show the samples. The amber line is the 3-second
average. Hover a graph to read its value at that moment. At 20 samples/s the graphs
show a third of the chosen time span, so individual samples stay distinguishable.

The header has the same controls as CPU Graph, and so do its keys, which work while
the tab is open:

| Key | Action |
|---|---|
| Space | Pause / resume the graphs (sampling continues) |
| Up / Down | Faster / slower sampling: 20, 10, 4, 2, 1 per second |
| Left / Right | Shorter / longer time span |
| R / N / D | Show / hide the Linux machines, the network column, the disk column |
| M | This PC's total: CPU time ↔ Processor utility (matches Task Manager) |
| C | A graph per CPU thread ↔ one graph of all threads per machine |
| A | Show / hide the 3-second average line |
| T | Always on top (desktop app) |
| E | Open settings.ini in Notepad (desktop app) |

**Choosing adapters, drives and Linux machines.** The tab reads CPU Graph's own
settings file, `%LOCALAPPDATA%\CpuGraph\settings.ini`, so both apps show the same
things. It never writes to that file, apart from creating a starting one if you press
E on a PC that doesn't have one. The file's lines are explained in
[CPU Graph's README](https://github.com/esteed/CpuGraph#settings-choosing-machines-adapters-and-disks):

- `networks=`, `disks=` and `scale.<adapter>=` choose this PC's adapters, drives and
  network scales.
- `remotes=` lists Linux machines (up to six).
- A machine's `[section]` picks its adapters and disks.

Changes apply within a couple of seconds. Without the file, the tab shows the
connected network cards, every fixed drive, and no Linux machines.

**Linux machines** run `cpugraph-agent` from the CPU Graph repository; its README
explains how to build and install it. Agents 2.0 and later send every CPU thread,
the chosen adapters and the chosen disks. Older agents send only the total CPU, and
their row says so. A machine that can't be reached says why across its row and
reconnects by itself. The app connects to the agents only while its window is open.

## Run it

Download `omniTop.exe` from the
[latest release](https://github.com/esteed/port-traffic-viz/releases/latest), or
build it yourself (see below).

| | |
|---|---|
| `omniTop.exe` | The desktop app. Windows asks for administrator rights, then the view opens in its own window. Close the window to stop. |
| `omniTop.exe --demo` | Simulated traffic, with a port scan now and then, no administrator rights needed. |
| `start.cmd`, `start-demo.cmd` | The same view in your browser instead, run from source (needs Python). |

The exe needs nothing installed. It uses the Edge WebView2 runtime that comes with Windows 11.

If you decline the administrator prompt, the window offers **Try again** or
**Show simulated traffic**.

## Build the exe

Run `build.cmd`. It needs 64-bit Python 3.10 or later and, the first time, an
internet connection: it sets up pywebview, PyInstaller and Pillow in
`%LOCALAPPDATA%\PortTrafficViz\build` (outside Google Drive) and writes
`dist\omniTop.exe`. Rebuild after editing anything in `web\`, `server.py`,
`sysmon.py`, `perfmon.py`, `netwatch.py` or `desktop.py`.

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
  answer. Broadcast, multicast and loopback packets don't count.
- **Late packets**: some drops are just late, a server answering a connection
  this machine already closed. Those are left out: packets from a server's
  well-known port (below 1024), and packets from an address to a local port this
  machine had a connection on with it in the last 3 minutes.
- **Without Windows Firewall**: Windows answers probes of closed ports with a
  reset instead of dropping them, so scans of closed ports aren't seen.

`sysmon.py` samples memory, the GPU and the CPU once a second and sends the result
on the same stream.

- **System memory map**: it reads a sample of about 65,000 entries from Windows'
  page database, around 1,000 per GB. This uses the same undocumented interface
  as Sysinternals RAMMap (`NtQuerySystemInformation` with
  `SystemSuperfetchInformation`). Each entry says which list a page is on (in use,
  modified, standby, free) and what it's used for. A sample takes about 40 ms on a
  128 GB machine.
- **GPU**: its memory size comes from D3DKMT. Use and engine utilization come from
  the performance counters Task Manager reads (`GPU Engine`, `GPU Adapter Memory`,
  `GPU Process Memory`).
- **CPU**: from `GetSystemTimes`.
- **Committed memory**: total, limit and peak come from `GetPerformanceInfo`.

`perfmon.py` is CPU Graph's sampler, ported to Python. It reads everything for This
PC each tick, on its own clock, at the rate chosen in the CPU, disk & network tab.
- **CPU threads**: all of them come from one `NtQuerySystemInformation` call.
- **Network**: each adapter's counters and link speed come from one `GetIfEntry2` call.
- **Disks**: from the `LogicalDisk` performance counters.

Each Linux machine has its own thread, which connects to its agent, reads samples
and reconnects every 5 s after a failure. Agent timestamps are mapped onto the app's
clock using the fastest network trip seen so far, so all rows line up.

The server only listens on 127.0.0.1 and refuses requests whose `Host` header
isn't its own address.

## Notes

- omniTop was called Port traffic until version 1.5. It keeps that version's settings,
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
