"""Installs the Linux and macOS agent on a machine over SSH, for the Remote tab's form.

The form gives the machine's address, an account there and its password. Windows' own
ssh.exe (the OpenSSH client that comes with Windows 10 and 11) signs in. It asks this
program for the password: SSH_ASKPASS is omniTop.exe, or this file when OmniTOP runs
from source, started with OMNITOP_ASKPASS=1 in its environment, and it answers password
prompts only (not a key's passphrase, not a one-time code). A key that ssh already
uses works too, and then the password is only for sudo.

On the machine, one shell command reads the password and the agent from ssh's input,
keeps the agent in a temporary file, runs its --install --replace (with sudo, given
the password, unless the account is root or sudo needs none) and removes the file.
Nothing is kept: not the password, not the copy. A machine's host key is trusted the
first time, kept in known_hosts beside settings.json, and checked against the user's
own known_hosts as well.
"""
from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

FLAG = "OMNITOP_ASKPASS"                   # set for ssh's askpass: the program answers and stops
SECRET = "OMNITOP_ASKPASS_SECRET"
USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@-]{0,63}")
TIMEOUT = 180                              # seconds for the whole job
KEEP = 600                                 # seconds a finished job's result is kept
MARK = "@omt "                             # how the remote command says how far it got


# ---------------------------------------------------------------------------
# The askpass side: ssh runs this to get the password
# ---------------------------------------------------------------------------
def askpass(args: list[str]) -> int:
    """Answer one of ssh's questions on standard output. 0 if answered."""
    prompt = " ".join(args).lower()
    secret = os.environ.get(SECRET, "")
    if "password" in prompt and "passphrase" not in prompt and secret:
        answer = secret
    elif "yes/no" in prompt:               # a host key question: never yes
        answer = "no"
    else:
        return 1
    _write_out((answer + "\n").encode("utf-8"))
    return 0


def _write_out(data: bytes) -> None:
    """Write to standard output, even from omniTop.exe, which has no console and no sys.stdout."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetStdHandle.argtypes = [wintypes.DWORD]
        k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        handle = k32.GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))   # STD_OUTPUT_HANDLE
        if handle and handle != ctypes.c_void_p(-1).value:
            done = wintypes.DWORD()
            if k32.WriteFile(handle, data, len(data), ctypes.byref(done), None) and done.value == len(data):
                return
    os.write(1, data)


# ---------------------------------------------------------------------------
# The ssh side
# ---------------------------------------------------------------------------
def ssh_program() -> str | None:
    """Windows' ssh.exe, or ssh on the PATH."""
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        for folder in ("Sysnative", "System32"):          # a 32-bit Python sees SysWOW64 as System32
            path = os.path.join(root, folder, "OpenSSH", "ssh.exe")
            if os.path.isfile(path):
                return path
    return shutil.which("ssh")


def askpass_program(folder: str) -> str:
    """What SSH_ASKPASS names: omniTop.exe itself, or a script that runs this file."""
    if getattr(sys, "frozen", False):
        return sys.executable
    me = os.path.abspath(__file__)
    if os.name == "nt":
        path = os.path.join(folder, "askpass.cmd")
        with open(path, "w", encoding="oem", errors="replace") as f:     # how cmd reads it
            f.write('@"%s" -I "%s" %%*\r\n' % (sys.executable, me))
    else:
        path = os.path.join(folder, "askpass.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec "%s" -I "%s" "$@"\n' % (sys.executable, me))
        os.chmod(path, 0o700)
    return path


def remote_command(port: int) -> str:
    """The command run there, for any login shell: /bin/sh -c '...', one line, with no ' or ! in it.

    Its input is the password's line, then the agent. It says "@omt in <system>" once
    signed in, "@omt copied" once it has the agent, and "@omt <problem>" if it can't go on."""
    run = 'python3 -I "$f" --install --replace --port %d' % port
    script = "; ".join([
        "echo @omt in $(uname -s)",
        "IFS= read -r p",
        'f=$(mktemp "${TMPDIR:-/tmp}/omnitop-agent.XXXXXX") || exit 90',
        'trap "rm -f $f" EXIT',
        'cat > "$f" || exit 90',
        "echo @omt copied",
        "command -v python3 >/dev/null 2>&1 || { echo @omt nopython; exit 91; }",
        'if [ "$(id -u)" = 0 ]; then ' + run,
        "elif command -v sudo >/dev/null 2>&1; then if sudo -n true 2>/dev/null; then sudo -n " + run,
        'elif [ -z "$p" ]; then echo @omt needpass; exit 93',
        'else printf "%s\\n" "$p" | sudo -S -p "" ' + run,
        "fi",
        "else echo @omt nosudo; exit 92",
        "fi",
    ])
    assert "'" not in script and "!" not in script
    return "/bin/sh -c '%s'" % script


def ssh_arguments(ssh: str, user: str, address: str, known_hosts: str, extra: list[str]) -> list[str]:
    hosts = '"%s" "~/.ssh/known_hosts"' % known_hosts.replace("\\", "/")
    return [ssh, "-T", "-o", "BatchMode=no", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + hosts, "-o", "NumberOfPasswordPrompts=1",
            "-o", "ConnectTimeout=12", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "LogLevel=ERROR", *extra, "-l", user, "--", address]


# ---------------------------------------------------------------------------
# What went wrong, in words
# ---------------------------------------------------------------------------
def explain(code: int, said: list[str], errors: list[str], system: str, user: str, address: str,
            known_hosts: str) -> tuple[str, str]:
    """(field, text) for a job that didn't work."""
    err = "\n".join(errors)
    low = err.lower()
    mac = system == "macos"
    if "@omt nopython" in said:
        return "", ("Python 3 isn't on that Mac. Install Apple's Command Line Tools there (xcode-select --install) and try again."
                    if mac else "Python 3 isn't installed there. Install it (sudo apt install python3, sudo dnf install python3, "
                                "or your system's way) and try again.")
    if "@omt nosudo" in said:
        return "user", ("sudo isn't installed there, so only root can install the agent. Sign in there as root, "
                        "install sudo (apt install sudo) and add %s to it (usermod -aG sudo %s), or use the steps below." % (user, user))
    if "@omt needpass" in said or "no password was provided" in low or "a password is required" in low:
        return "password", "sudo there needs %s's password. Enter it and try again." % user
    if "not in the sudoers" in low or "may not run sudo" in low or "not allowed to run sudo" in low:
        return "user", "%s isn't allowed to use sudo there. Use an account that is, or root." % user
    if "incorrect password" in low or "sorry, try again" in low:
        return "password", "That isn't %s's password for sudo there." % user
    if "terminal is required" in low or "must have a tty" in low:
        return "", "sudo there only works in a terminal (requiretty), so install the agent with the steps below."
    if "xcrun: error" in low or "no developer tools" in low or "command line tools" in low:
        return "", "Python 3 isn't on that Mac yet. Install Apple's Command Line Tools there (xcode-select --install) and try again."
    if code == 255 or not any(line.startswith(MARK + "in") for line in said):
        if "remote host identification has changed" in low or "host key verification failed" in low:
            return "address", ("That machine's SSH identity (its host key) isn't the one seen before. If it was reinstalled, "
                               "remove its line from %s (and from your own .ssh known_hosts, if it's there) and try again."
                               % known_hosts)
        if "permission denied" in low or "too many authentication failures" in low:
            return "password", "That user name or password isn't right there (or that account can't sign in with a password)."
        if "could not resolve" in low or "name or service not known" in low or "no such host" in low:
            return "address", "No machine has that name."
        if "connection refused" in low:
            return "address", ("That machine doesn't take SSH connections (TCP port 22). "
                               + ("Turn on Remote Login there (System Settings, General, Sharing)." if mac
                                  else "Turn on its SSH server (sudo systemctl enable --now ssh, or sshd)."))
        if "timed out" in low or "no route" in low or "unreachable" in low:
            return "address", "No answer from %s on TCP port 22." % address
        if "verification code" in low or "keyboard-interactive" in low:
            return "", "That machine asks for more than a password to sign in, so install the agent with the steps below."
    last = [line for line in errors if line.strip()][-3:] or [line for line in said if not line.startswith(MARK)][-3:]
    return "", " ".join(" ".join(last).split())[:400] or "It didn't work (exit code %d)." % code


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
class Pusher:
    """Install jobs, one per machine at a time. start() checks the fields; state() says how one is going.

    agent is the Linux and macOS agent's path, known_hosts the file host keys are kept
    in, extra any more ssh options (the tests' key)."""

    def __init__(self, agent: str, known_hosts: str, demo: bool = False, extra: list[str] | None = None):
        self.agent, self.known_hosts, self.demo, self.extra = agent, known_hosts, demo, list(extra or [])
        self.jobs: dict[int, dict] = {}
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def start(self, address: str, port: int, user: str, password: str, system: str) -> dict:
        """{"job": n}, or {"error", "field"} when a field is wrong."""
        user = str(user or "").strip()
        if not USER.fullmatch(user):
            return {"field": "user", "error": "Enter the account to sign in with there."}
        password = str(password or "")
        if len(password) > 256 or any(c in password for c in "\r\n\0"):
            return {"field": "password", "error": "That isn't a password this can send."}
        if address.startswith("-"):
            return {"field": "address", "error": "That isn't a host name or IP address."}
        if not self.demo and not ssh_program():
            return {"field": "", "error": "Windows' OpenSSH client isn't installed. Add it in Settings, System, Optional features "
                                          "(OpenSSH Client), or use the steps below."}
        with self.lock:
            now = time.time()
            for n, job in list(self.jobs.items()):
                if job["state"] != "busy" and now - job["at"] > KEEP:
                    del self.jobs[n]
            for n, job in self.jobs.items():
                if job["address"] == address and job["state"] == "busy":
                    return {"job": n}
            n = next(self.ids)
            job = self.jobs[n] = {"job": n, "address": address, "port": port, "state": "busy", "at": now,
                                  "text": "Signing in to %s as %s…" % (address, user), "field": "", "os": system}
        threading.Thread(target=self._run, args=(job, user, password, system), name="push-install", daemon=True).start()
        return {"job": n}

    def state(self, n: int) -> dict:
        with self.lock:
            job = self.jobs.get(n)
            return {k: job[k] for k in ("job", "state", "text", "field", "os")} if job else {
                "job": n, "state": "bad", "text": "That install isn't running any more.", "field": "", "os": ""}

    def _say(self, job: dict, text: str, state: str = "busy", field: str = "", system: str = "") -> None:
        with self.lock:
            job.update(text=text, state=state, field=field, at=time.time())
            if system:
                job["os"] = system

    def _run(self, job: dict, user: str, password: str, system: str) -> None:
        try:
            (self._demo if self.demo else self._ssh)(job, user, password, system)
        except Exception as exc:               # a job always ends
            self._say(job, "It didn't work: %s" % exc, "bad")

    def _demo(self, job: dict, user: str, password: str, system: str) -> None:
        time.sleep(1.2)
        if not password and user != "root":
            self._say(job, "That user name or password isn't right there (or that account can't sign in with a password).",
                      "bad", "password")
            return
        self._say(job, "Signed in. Copying the agent…")
        time.sleep(0.8)
        self._say(job, "Installing it…")
        time.sleep(1.5)
        self._say(job, "omnitop-agent is running on TCP port %d and starts with the %s." % (
            job["port"], "Mac" if system == "macos" else "system"), "good")

    def _ssh(self, job: dict, user: str, password: str, system: str) -> None:
        with open(self.agent, "rb") as f:
            program = f.read()
        os.makedirs(os.path.dirname(self.known_hosts) or ".", exist_ok=True)
        folder = tempfile.mkdtemp(prefix="omnitop-push-")
        try:
            env = dict(os.environ, SSH_ASKPASS=askpass_program(folder), SSH_ASKPASS_REQUIRE="force",
                       **{FLAG: "1", SECRET: password})
            env.setdefault("DISPLAY", "omnitop:0")
            args = ssh_arguments(ssh_program(), user, job["address"], self.known_hosts, self.extra)
            proc = subprocess.Popen(args + [remote_command(job["port"])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            del env
            said: list[str] = []
            errors: list[str] = []

            def feed():
                try:
                    proc.stdin.write(password.encode("utf-8") + b"\n" + program)
                    proc.stdin.close()
                except OSError:                # it stopped first: what it said explains why
                    pass

            def gather():
                for raw in proc.stderr:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line and not line.startswith("Warning: Permanently added"):
                        errors.append(line)
                        del errors[:-40]

            late: list[bool] = []

            def stop():
                late.append(True)
                proc.kill()

            threads = [threading.Thread(target=feed, daemon=True), threading.Thread(target=gather, daemon=True)]
            for t in threads:
                t.start()
            timer = threading.Timer(TIMEOUT, stop)
            timer.start()
            seen = system
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                said.append(line)
                del said[:-60]
                if line.startswith(MARK + "in"):
                    seen = "macos" if line.split()[-1] == "Darwin" else "linux" if line.split()[-1] == "Linux" else system
                    self._say(job, "Signed in. Copying the agent…", system=seen)
                elif line == MARK + "copied":
                    self._say(job, "Installing it…")
            code = proc.wait()
            timer.cancel()
            for t in threads:
                t.join(5)
            if code == 0:
                done = [line for line in said if line.startswith("omnitop-agent ") and " is running " in line]
                notes = [line for line in said if line and not line.startswith(MARK) and not line.startswith("omnitop-agent ")
                         and not line.startswith("In OmniTOP, choose")]
                self._say(job, " ".join((done[-1:] or ["The agent is installed."]) + notes[-2:]), "good", system=seen)
            elif late:
                self._say(job, "It took too long, so it was stopped.", "bad")
            else:
                field, text = explain(code, said, errors, seen, user, job["address"], self.known_hosts)
                self._say(job, text, "bad", field)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":                 # ssh's askpass, when OmniTOP runs from source
    sys.exit(askpass(sys.argv[1:]) if os.environ.get(FLAG) == "1" else 2)
