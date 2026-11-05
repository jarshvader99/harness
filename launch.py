#!/usr/bin/env python3
"""Run a harness mission against TurboFieldfare, managing the server's lifetime.

Starts TurboFieldfareServer on a loopback port, waits for its ready line, runs
the mission in main.py against it, then shuts the server down again. If a
TurboFieldfare server is already answering on the port, it is reused and left
running - this script only stops servers it started itself.

Usage:
    python3 launch.py                       # defaults: port 8080, built checkout
    python3 launch.py --max-iterations 3    # extra flags pass through to main.py
    python3 launch.py --keep-running        # leave the server up afterwards

Discovery order for the server binary:
    --server PATH
    $TURBOFIELDFARE_SERVER
    TurboFieldfareServer on PATH
    ~/Documents/turbo-fieldfare/.build/release/TurboFieldfareServer
    ./turbo-fieldfare/.build/release/TurboFieldfareServer
    ~/turbo-fieldfare/.build/release/TurboFieldfareServer

Discovery order for the model:
    --model-dir PATH
    $TURBOFIELDFARE_GTURBO
    ~/Library/Application Support/TurboFieldfare/gemma4.gturbo
"""

import argparse
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import main

EVENTS_ON = False


def say(*parts):
    """Chatter for humans. In --events mode it goes to stderr so stdout stays pure NDJSON."""
    print(*parts, file=sys.stderr if EVENTS_ON else sys.stdout, flush=True)


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "TurboFieldfare", "gemma4.gturbo"
)
SERVER_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), "Documents", "turbo-fieldfare", ".build", "release", "TurboFieldfareServer"),
    os.path.join(HERE, "turbo-fieldfare", ".build", "release", "TurboFieldfareServer"),
    os.path.join(os.path.expanduser("~"), "turbo-fieldfare", ".build", "release", "TurboFieldfareServer"),
]


def find_server(explicit):
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    env = os.environ.get("TURBOFIELDFARE_SERVER")
    if env:
        return env if os.access(env, os.X_OK) else None
    found = shutil.which("TurboFieldfareServer")
    if found:
        return found
    for candidate in SERVER_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def find_model(explicit):
    candidate = explicit or os.environ.get("TURBOFIELDFARE_GTURBO") or DEFAULT_MODEL
    return candidate if os.path.isfile(os.path.join(candidate, "manifest.json")) else None


def health(port, timeout=2.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


class Server:
    """A TurboFieldfareServer child process and its output."""

    def __init__(self, binary, model, port, startup_timeout, log_path=None):
        self.binary = binary
        self.model = model
        self.port = port
        self.startup_timeout = startup_timeout
        self.log_path = log_path
        self.process = None
        self.lines = queue.Queue()
        self.log = []
        self._reader = None
        self._handle = None

    def _drain(self):
        for line in self.process.stdout:
            self.log.append(line.rstrip("\n"))
            self.lines.put(line.rstrip("\n"))

    def start(self):
        command = [self.binary, "--model", self.model, "--port", str(self.port)]
        say(f"▶️  {' '.join(command)}")
        if self.log_path:
            return self._start_detached(command)
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.raise_with_log(f"server exited with status {self.process.returncode} before it was ready")
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if "ready at" in line:
                say(f"✅ {line}")
                return self
        self.raise_with_log(f"server did not report ready within {self.startup_timeout}s")
        return self

    def _start_detached(self, command):
        """Leave a server that outlives this process: its output goes to a file.

        A pipe would belong to this process, and the server would die of SIGPIPE
        on its first log line once we exit - so --keep-running needs a file.
        """
        os.makedirs(os.path.dirname(os.path.abspath(self.log_path)) or ".", exist_ok=True)
        self._handle = open(self.log_path, "a", encoding="utf-8", buffering=1)
        self.process = subprocess.Popen(
            command, stdout=self._handle, stderr=subprocess.STDOUT, text=True
        )
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.raise_with_log(
                    f"server exited with status {self.process.returncode} before it was ready"
                )
            if health(self.port, timeout=0.6):
                say(f"✅ TurboFieldfareServer ready on http://127.0.0.1:{self.port}")
                say(f"   log: {self.log_path}")
                return self
            time.sleep(0.3)
        self.raise_with_log(f"server did not report ready within {self.startup_timeout}s")
        return self

    def _tail(self, count=15):
        if self.log_path:
            try:
                with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                    return handle.read().splitlines()[-count:]
            except OSError:
                return []
        return self.log[-count:]

    def raise_with_log(self, message):
        self.stop()
        tail = "\n".join(f"  {line}" for line in self._tail()) or "  (no output)"
        raise RuntimeError(f"{message}\nserver output:\n{tail}")

    def stop(self):
        try:
            if self.process and self.process.poll() is None:
                say("⏹️  stopping TurboFieldfareServer")
                self.process.terminate()
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    print("   did not exit after SIGTERM; sending SIGKILL", file=sys.stderr)
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            if self._handle:
                self._handle.close()
                self._handle = None

    def report(self):
        if self.log:
            say("\n🗒️  server log (last 10 lines):")
            for line in self.log[-10:]:
                say(f"   {line}")


def main_launcher(argv=None):
    parser = argparse.ArgumentParser(
        description="Start TurboFieldfareServer, run a harness mission, stop the server.",
        epilog="Unrecognised flags are passed through to main.py, e.g. --max-iterations 3.",
    )
    parser.add_argument("--server", help="path to the TurboFieldfareServer binary")
    parser.add_argument("--model-dir", help=".gturbo model directory")
    parser.add_argument("--port", type=int, default=8080, help="loopback port (default 8080)")
    parser.add_argument("--startup-timeout", type=float, default=180.0,
                        help="seconds to wait for the ready line (default 180)")
    parser.add_argument("--keep-running", action="store_true", help="leave the server up afterwards")
    parser.add_argument("--events", action="store_true",
                        help="emit NDJSON from the harness and send launcher chatter to stderr")
    args, forwarded = parser.parse_known_args(argv)

    global EVENTS_ON
    EVENTS_ON = args.events
    if args.events:
        forwarded = forwarded + ["--events"]

    if health(args.port):
        say(f"🔗 Reusing the TurboFieldfare server already on http://127.0.0.1:{args.port} (left running)")
        return main.main(["--base-url", f"http://127.0.0.1:{args.port}/v1", *forwarded])

    binary = find_server(args.server)
    if not binary:
        print(
            "❌ TurboFieldfareServer not found.\n"
            "   Build it from a checkout:\n"
            "     swift build -c release --product TurboFieldfareServer\n"
            "   then pass it with --server PATH or set TURBOFIELDFARE_SERVER.\n"
            "   Searched:\n    " + "\n    ".join([args.server or "", *SERVER_CANDIDATES]).strip(),
            file=sys.stderr,
        )
        return 1

    model = find_model(args.model_dir)
    if not model:
        print(
            f"❌ No .gturbo model at {args.model_dir or DEFAULT_MODEL}\n"
            "   Pass --model-dir PATH or set TURBOFIELDFARE_GTURBO.\n"
            "   The Mac app and this server both own the model: quit the app first.",
            file=sys.stderr,
        )
        return 1

    # A kept server outlives this process, so its output has to go to a file.
    log_path = None
    if args.keep_running:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = os.path.join(HERE, "runs", f"server-{stamp}.log")

    server = Server(binary, model, args.port, args.startup_timeout, log_path)
    started = time.monotonic()
    code = 1
    try:
        server.start()
        say(f"   ready in {time.monotonic() - started:.1f}s")
        code = main.main(["--base-url", f"http://127.0.0.1:{args.port}/v1", *forwarded])
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("\n⚠️  interrupted", file=sys.stderr)
        code = 130
    finally:
        if args.keep_running:
            say(f"↔️  leaving the server on http://127.0.0.1:{args.port} "
                f"(pid {server.process.pid if server.process else '-'})")
            if log_path:
                say(f"   log: {log_path}")
        else:
            server.stop()
            if code != 0:
                server.report()
    return code


if __name__ == "__main__":
    raise SystemExit(main_launcher())
