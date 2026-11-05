"""Minimal Viable Harness — with TurboFieldfare (gemma4b) as the Brain.

The Harness asks a local model to act through OpenAI-style function calls
(write_file / read_file / run_shell / fetch_url / fetch_links), executes them in the
Environment, returns the results as tool messages, and repeats until the Task's
validator passes. Models that cannot call functions can still emit
[ACTION::ARGS] text triggers instead.

Long missions are compacted: when the conversation approaches the model's
context window, earlier turns are folded into a short summary so the run keeps
going instead of failing on a full context.

Usage:
    python3 launch.py                        # start server, run, stop server
    python3 main.py                          # auto-detect the local server
    python3 main.py --base-url URL --model NAME

TurboFieldfare's brain is the loopback server product, built from a
https://github.com/drumih/turbo-fieldfare checkout:

    swift build -c release --product TurboFieldfareServer
    .build/release/TurboFieldfareServer --model "/path/to/gemma4.gturbo"
    # -> TurboFieldfareServer ready at http://127.0.0.1:8080 model=gemma-4-26b-a4b-it

The Mac app and this server both own the model, so quit the app first. The
harness probes TurboFieldfare (:8080) before any other local server, and warns
loudly when it has to fall back to one that is not TurboFieldfare.
    python3 main.py --task tasks/summary.json # run a saved mission
    python3 main.py --serve-chat              # chat mode: NDJSON on stdin/stdout
    python3 main.py --simulate                # no model needed, canned replies
    python3 main.py --trace runs/my-run.jsonl # explicit trace path
    python3 main.py --no-trace                # skip the trace log
    python3 main.py --sandbox ./work          # where the model may read/write

Every run appends a JSONL trace to runs/ by default: one line per action with
the thought, args, result and observation, plus a run header (mission + model)
and a footer (completed, iterations, files, duration).

Files are real: the model reads and writes inside the sandbox directory only
(default sandbox/), and every path is resolved so it cannot escape.

Replies are read as a stream, so a long generation shows progress instead of a
silent wait. The socket timeout bounds silence, not the length of a turn: a
turn that keeps producing tokens is never killed for being long, and if a
stream does break, whatever arrived is kept as a partial answer.

run_shell runs commands in the sandbox directory with the user's privileges, so
every command goes through the approval gate first. --approve ask (the default)
prompts on a terminal, or over the NDJSON stream when a UI is driving.

Environment variables:
    TURBOFIELDFARE_BASE_URL   e.g. http://127.0.0.1:11434 or http://127.0.0.1:1234/v1
    TURBOFIELDFARE_MODEL      exact model id/tag exposed by the app
    TURBOFIELDFARE_API_KEY    only if the local server demands one
    TURBOFIELDFARE_TIMEOUT    seconds of silence before a turn is abandoned (default: 180)
    TURBOFIELDFARE_DEADLINE   hard ceiling on one turn, in seconds (default: 3600)
    TURBOFIELDFARE_MAX_TOKENS cap on one answer (default: 4096)
    TURBOFIELDFARE_SANDBOX    sandbox directory (default: sandbox/)
    TURBOFIELDFARE_CONTEXT    context window in tokens (default: 16384)
"""

import argparse
import http.client
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

DEFAULT_GOAL = "Create a file named test.txt"
DEFAULT_EXPECT_FILE = "test.txt"
DEFAULT_CONTEXT = 16384        # TurboFieldfareServer's default; --max-context changes it
COMPACT_AT = 0.70              # compact once the conversation is this full
DEFAULT_STALL = 180.0          # seconds without a token before a turn is abandoned
DEFAULT_DEADLINE = 3600.0      # hard ceiling on one turn, so nothing hangs forever
DEFAULT_MAX_TOKENS = 4096      # what the server would allow anyway; explicit and capped
DEFAULT_SHELL_TIMEOUT = 30.0
TASK_FIELDS = ("name", "goal", "expect_file", "expect_contains", "max_iterations")

# The model is small, so the protocol is spelled out and demonstrated. Which
# prompt it gets depends on whether the server accepts function declarations:
# advertising both at once makes it fall back to the text form.
TOOL_PROMPT = """You are the reasoning engine inside a local agent harness.

Each turn you get the MISSION and the result of your last action. Act by calling
one of the provided functions:

    write_file(filename, content)   create or overwrite a file
    read_file(filename)             read a file back
    run_shell(command)              run a shell command in the sandbox directory
    fetch_url(url)                  open a web page and read its text
    fetch_links(url)                list the links on a web page

Rules:
- filename is relative to the sandbox directory: no leading slash, no '..'.
- url must start with http:// or https://; the page text comes back as the result.
- Use fetch_url when the mission needs something from the web, then write what
  you learned with write_file.
- run_shell is for what the file tools cannot do: run a program, check its
  output, or change part of a file with sed or python. It has your user's
  privileges and the user approves each command, so keep commands small.
- To change one line of an existing file, use run_shell to patch it. Do not
  reprint a whole file to change a few characters: that is slow and risky.
- Files you write really exist on disk and stay there for later steps.
- Call the function itself. Never print commands such as [WRITE::...].
- One function call per turn; say briefly why in the message text.
- When the result shows the mission is done, answer in one short sentence and
  call no function.

Example: to create a file, call write_file with filename "test.txt" and content
"Hello from the Local-Harness"."""

TRIGGER_PROMPT = """You are the reasoning engine inside a local agent harness.

Each turn you get the MISSION and an OBSERVATION. Reply with one short thought,
then exactly one action trigger:

    [WRITE::filename|content]
    [READ::filename]
    [SHELL::command]
    [FETCH::url]

Rules:
- filename is relative to the sandbox directory: no leading slash, no '..'.
- url must start with http:// or https://; FETCH returns the page text.
- SHELL runs a command in the sandbox directory; the user approves each one.
- Files you write really exist on disk and stay there for later steps.
- content after the pipe is written verbatim. Never wrap it in backticks or quotes.
- Print the trigger as plain text, never inside a code block.
- Act every turn. When the observation says SUCCESS and the mission is met,
  reply with a short final answer and no trigger.

Example:
Thought: the mission needs a file, so I will write it.
[WRITE::test.txt|Hello from the Local-Harness]"""


# Used when a conversation outgrows the context window: the earlier turns are
# replaced by these notes, so the mission can carry on with the facts that matter.
SUMMARY_PROMPT = """You compress an agent's earlier turns so its work can continue in a small context window.

You get a transcript of what happened so far. Reply with at most 120 words of
notes, no preamble, covering:
- the mission and what has already been achieved
- exact filenames, URLs and values that matter
- what was tried and failed, so it is not repeated
- what still needs doing

Do not invent anything that is not in the transcript."""

CHAT_PROMPT = """You are a local assistant running on the user's Mac inside a harness that gives
you real tools. You are talking with the user, not working through an automated
mission.

    write_file(filename, content)   create or overwrite a file
    read_file(filename)             read a file back
    run_shell(command)              run a shell command in the sandbox directory
    fetch_url(url)                  open a web page and read its text
    fetch_links(url)                list the links on a web page

Rules:
- Files live in a sandbox directory: filename is relative, no leading slash, no '..'.
- url must start with http:// or https://.
- run_shell runs as the user and needs their approval, so use it only when it is
  the simplest way to answer, and to patch a file instead of reprinting it.
- Call a function when you need it to answer; never print commands like [WRITE::...].
- Ground what you say in the tool results, and stay brief: a few short paragraphs."""


def system_prompt(supports_tools):
    return TOOL_PROMPT if supports_tools else TRIGGER_PROMPT

# Declared to servers that support OpenAI-style function calling.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the sandbox directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Relative path, e.g. notes.md"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the sandbox directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Relative path, e.g. notes.md"},
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command in the sandbox directory and return its output. "
                "Use it to run programs, inspect output, or patch part of a file. "
                "Each command is shown to the user for approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "A bash command, e.g. python3 build.py or sed -i '' 's/a/b/' page.html",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Open a web page and return its readable text (scripts and markup stripped).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http:// or https:// URL"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_links",
            "description": "Open a web page and list the links on it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http:// or https:// URL"},
                },
                "required": ["url"],
            },
        },
    },
]


class Reply:
    """One model turn: text plus any function calls it asked the harness to run."""

    def __init__(self, content="", tool_calls=None, raw_tool_calls=None,
                 incomplete=False, elapsed=0.0):
        self.content = content or ""
        # [{"id", "name", "arguments" (dict or None), "arguments_raw"}]
        self.tool_calls = tool_calls or []
        # Echoed back verbatim so the server can match its own tool_call ids.
        self.raw_tool_calls = raw_tool_calls or []
        # True when the stream broke partway: the text we kept is partial.
        self.incomplete = incomplete
        self.elapsed = elapsed


class TraceLog:
    """Append-only JSONL record of a run.

    One JSON object per line, so a run can be replayed by feeding the saved
    args back through the Environment, and scored from the run_end footer.
    """

    def __init__(self, path):
        self.path = path
        self.run_id = uuid.uuid4().hex[:12]
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._handle = open(path, "a", encoding="utf-8")

    def write(self, record):
        record.setdefault("run_id", self.run_id)
        record.setdefault("time", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self):
        if not self._handle.closed:
            self._handle.close()


def default_trace_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join("runs", f"trace-{stamp}-{uuid.uuid4().hex[:6]}.jsonl")


def default_chat_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join("chats", f"chat-{stamp}-{uuid.uuid4().hex[:6]}.jsonl")


def load_task(path):
    """Read a saved mission.

    Shape (also what the app writes into tasks/):
        {"name", "goal", "expect_file", "expect_contains", "max_iterations"}
    Only "goal" is required; the rest fall back to the CLI defaults.
    """
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    if not isinstance(spec, dict):
        raise ValueError("a task file must be a JSON object")
    if not str(spec.get("goal") or "").strip():
        raise ValueError('a task file needs a non-empty "goal"')
    return spec


class Console:
    """Output sink: human-readable text, or NDJSON when a UI is driving."""

    def __init__(self, json_mode=False):
        self.json_mode = json_mode

    def emit(self, kind, text="", prefix="", error=False, **fields):
        if self.json_mode:
            record = {"type": kind, "text": text}
            record.update(fields)
            print(json.dumps(record, ensure_ascii=False), flush=True)
        elif text:
            print(f"{prefix}{text}", flush=True,
                  file=sys.stderr if error else sys.stdout)

    def error(self, text):
        self.emit("error", text, "❌ ", error=True)


class Task:
    """The mission and the success criteria."""
    def __init__(self, goal, validator_func):
        self.goal = goal
        self.validator = validator_func
        self.history = []


class Environment:
    """The Sandbox: real files, confined to one directory.

    Every path is resolved against the sandbox root and rejected if it escapes,
    so the model can read and write ordinary files without reaching anything
    else on the machine.
    """

    def __init__(self, root="sandbox"):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)

    def _resolve(self, filename):
        """Turn a model-supplied name into an absolute path inside the root."""
        name = (filename or "").strip()
        if not name:
            raise ValueError("empty filename")
        if os.path.isabs(name) or name.startswith(("/", "\\")):
            raise ValueError(f"absolute paths are not allowed: {name}")
        candidate = os.path.realpath(os.path.join(self.root, name))
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            raise ValueError(f"path escapes the sandbox: {name}")
        return candidate

    def write_file(self, filename, content):
        name = (filename or "").strip()
        try:
            path = self._resolve(name)
        except ValueError as exc:
            return f"ERROR: {exc}"
        try:
            os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            return f"ERROR: could not write {name}: {exc.strerror or exc}"
        return f"SUCCESS: Written {os.path.relpath(path, self.root)} ({len(content)} chars)"

    def read_file(self, filename):
        name = (filename or "").strip()
        try:
            path = self._resolve(name)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not os.path.isfile(path):
            return f"ERROR: File not found: {name}"
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError as exc:
            return f"ERROR: could not read {name}: {exc.strerror or exc}"

    def exists(self, filename):
        try:
            return os.path.isfile(self._resolve(filename))
        except ValueError:
            return False

    def files(self):
        """Relative paths of everything currently in the sandbox."""
        found = []
        for directory, _subdirs, names in os.walk(self.root):
            for name in names:
                found.append(os.path.relpath(os.path.join(directory, name), self.root))
        return sorted(found)


class _PageText(HTMLParser):
    """Reduce HTML to the text a reader cares about, plus the links on the page."""

    SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas"}
    BREAK = {"p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "main", "header", "footer", "blockquote", "pre", "aside"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links = []  # [[href, label]]
        self._parts = []
        self._skip = 0
        self._in_title = False
        self._link = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            self.links.append([attributes.get("href") or "", ""])
            self._link = len(self.links) - 1
        elif tag in self.BREAK:
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self.BREAK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "a":
            self._link = None
        elif tag in self.BREAK:
            self._parts.append("\n")

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title = f"{self.title} {text}".strip()
        elif self._skip == 0:
            self._parts.append(text + " ")
            if self._link is not None:
                self.links[self._link][1] = f"{self.links[self._link][1]} {text}".strip()

    def text(self):
        joined = "".join(self._parts)
        lines = [" ".join(line.split()) for line in joined.split("\n")]
        return "\n".join(line for line in lines if line)


class Browser:
    """The harness's window on the web: fetch a page and return readable text.

    Kept apart from the Environment so file access stays confined to the sandbox
    while network access stays read-only and http(s)-only.
    """

    USER_AGENT = "TurboHarness/1.0 (local agent harness)"
    MAX_BYTES = 2_000_000
    MAX_CHARS = 6000
    TIMEOUT = 20.0

    def _load(self, url):
        url = (url or "").strip()
        if not url:
            raise ValueError("empty url")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"only http:// and https:// URLs are supported: {url}")
        request = urllib.request.Request(url, headers={
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.TIMEOUT) as response:
                raw = response.read(self.MAX_BYTES)
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
                final = response.geturl() or url
        except urllib.error.HTTPError as exc:
            raise ValueError(f"HTTP {exc.code} {exc.reason} for {url}") from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"could not reach {url}: {exc.reason}") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ValueError(f"could not fetch {url}: {exc}") from exc
        return final, content_type, raw.decode(charset, "replace")

    @staticmethod
    def _looks_like_html(content_type, text):
        if "html" in (content_type or ""):
            return True
        head = text.lstrip()[:200].lower()
        return head.startswith(("<!doctype", "<html", "<head", "<body"))

    def fetch(self, url):
        try:
            final, content_type, text = self._load(url)
        except ValueError as exc:
            return f"ERROR: {exc}"

        header = f"URL: {final}\n"
        if self._looks_like_html(content_type, text):
            page = _PageText()
            try:
                page.feed(text)
                page.close()
            except Exception:  # malformed markup: keep whatever was parsed
                pass
            body = page.text()
            if page.title:
                header += f"TITLE: {page.title}\n"
        else:
            body = text.strip()

        clipped = body[: self.MAX_CHARS]
        note = "" if len(body) <= self.MAX_CHARS else f"\n…[truncated, {len(body)} chars total]"
        return f"{header}TYPE: {content_type}\nTEXT:\n{clipped or '(no readable text)'}{note}"

    def links(self, url, limit=40):
        try:
            final, _content_type, text = self._load(url)
        except ValueError as exc:
            return f"ERROR: {exc}"

        page = _PageText()
        try:
            page.feed(text)
            page.close()
        except Exception:
            pass

        found = []
        seen = set()
        for href, label in page.links:
            href = (href or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absolute = urljoin(final, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            found.append((label or absolute, absolute))
            if len(found) >= limit:
                break
        if not found:
            return f"URL: {final}\n(no links found)"
        lines = [f"URL: {final}", f"LINKS ({len(found)}):"]
        lines += [f"{index}. {label[:80]} -> {target}" for index, (label, target) in enumerate(found, 1)]
        return "\n".join(lines)


class Shell:
    """Runs commands for the model, inside the sandbox directory.

    This is a real terminal with the user's privileges, which is why it sits
    behind the approval gate: the gate, not this class, is what decides whether
    a command is allowed to run. Output is clipped so one chatty command cannot
    flood the context window.
    """

    MAX_OUTPUT = 4000
    HEAD = 3000
    TAIL = 900

    def __init__(self, root, timeout=DEFAULT_SHELL_TIMEOUT):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)
        self.timeout = max(1.0, float(timeout))

    @staticmethod
    def _merge(*chunks):
        parts = []
        for chunk in chunks:
            if not chunk:
                continue
            parts.append(chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk)
        return "".join(parts).replace("\r\n", "\n").strip("\n")

    def _clip(self, text):
        if len(text) <= self.MAX_OUTPUT:
            return text
        dropped = len(text) - self.HEAD - self.TAIL
        return (text[: self.HEAD]
                + f"\n…[{dropped} chars clipped]…\n"
                + text[-self.TAIL:])

    def run(self, command):
        command = (command or "").strip()
        if not command:
            return "ERROR: empty command"
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
            "HOME": self.root,
            "PWD": self.root,
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TERM": "dumb",
        }
        started = time.monotonic()
        try:
            finished = subprocess.run(
                ["/bin/bash", "-c", command],
                cwd=self.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = self._clip(self._merge(exc.stdout, exc.stderr))
            return (f"COMMAND: {command}\n"
                    f"ERROR: still running after {self.timeout:.0f}s and was stopped\n"
                    f"OUTPUT SO FAR:\n{output or '(no output)'}")
        except OSError as exc:
            return f"ERROR: could not run the command: {exc.strerror or exc}"

        output = self._clip(self._merge(finished.stdout, finished.stderr))
        elapsed = time.monotonic() - started
        return (f"COMMAND: {command}\n"
                f"EXIT: {finished.returncode} in {elapsed:.1f}s\n"
                f"OUTPUT:\n{output or '(no output)'}")


class InputChannel:
    """One reader for stdin, shared by chat turns and approval answers.

    Lines arrive from the UI as NDJSON: ``{"type": "user"}`` for a chat turn
    and ``{"type": "approval"}`` for an answer to a pending request. Reading
    both from one thread means a chat can be blocked waiting for an approval
    answer without the two fighting over stdin.
    """

    def __init__(self):
        self.turns = queue.Queue()
        self.approvals = queue.Queue()
        self.closed = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                target = self.approvals if payload.get("type") == "approval" else self.turns
                target.put(payload)
        except (OSError, ValueError):
            pass
        finally:
            self.closed.set()
            self.turns.put(None)
            self.approvals.put(None)

    def next_turn(self):
        """The next chat/command payload, or None once stdin has closed."""
        return self.turns.get()

    def wait_for_approval(self, request_id, timeout):
        """Block until this request is answered, or the wait runs out."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                payload = self.approvals.get(timeout=remaining)
            except queue.Empty:
                return None
            if payload is None:
                return None
            if payload.get("id") == request_id:
                return payload


class TerminalApprovals:
    """Asks the person at the keyboard, y/n style, in a real terminal."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr

    def ask(self, tool, detail, timeout):
        print(f"\n\U0001F510 {tool} wants to run:\n    {detail}\n"
              f"    allow? [y]es / [n]o / [a]lways: ", end="", file=self.stream, flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=self.stream)
            return "deny"
        if answer in ("a", "always"):
            return "always"
        return "allow" if answer in ("y", "yes", "") else "deny"


class StreamApprovals:
    """Asks whoever is reading the event stream - the Mac app - and waits."""

    def __init__(self, console, channel, timeout=300.0):
        self.console = console
        self.channel = channel
        self.timeout = timeout
        self.counter = 0

    def ask(self, tool, detail, timeout=None):
        self.counter += 1
        request_id = f"apr-{self.counter:04d}"
        self.console.emit("approval_request", detail, "\U0001F510 ",
                          id=request_id, tool=tool, command=detail)
        payload = self.channel.wait_for_approval(request_id, timeout or self.timeout)
        if payload is None:
            return "timeout"
        decision = str(payload.get("decision") or "deny").lower()
        return decision if decision in ("allow", "deny", "always") else "deny"


class ApprovalGate:
    """The one place that decides whether a consequential tool call may run.

    Policies:
        ask    - put the call to the user (terminal prompt or UI request)
        allow  - run everything without asking
        deny   - refuse everything

    Answering "always" to a prompt turns the policy into allow for the rest of
    the session, so a long run does not turn into a click-by-click exercise.
    """

    def __init__(self, policy="ask", broker=None, trace=None):
        self.policy = policy if policy in ("ask", "allow", "deny") else "ask"
        self.broker = broker
        self.trace = trace
        self.approved = 0
        self.denied = 0

    def review(self, tool, detail):
        """Returns (allowed, note). The note is what the model is told."""
        if self.policy == "allow":
            return True, "approved"
        if self.policy == "deny":
            return False, "the user's policy does not allow this"
        if self.broker is None:
            return False, "nobody is available to approve it"

        decision = self.broker.ask(tool, detail, None)
        if decision == "always":
            self.policy = "allow"
            decision = "allow"
        if decision == "timeout":
            self.denied += 1
            note = "no answer came back, so it was not run"
        elif decision == "allow":
            self.approved += 1
            note = "approved by the user"
        else:
            self.denied += 1
            note = "the user declined"
        if self.trace:
            self.trace.write({"type": "approval", "tool": tool, "detail": detail,
                              "decision": decision})
        return decision == "allow", note


class LocalModel:
    """Talks to a locally served model over Ollama's API or an OpenAI-compatible API."""

    def __init__(self, backend, base, model, api_key="", timeout=DEFAULT_STALL,
                 deadline=DEFAULT_DEADLINE, max_tokens=DEFAULT_MAX_TOKENS):
        self.backend = backend
        self.base = base.rstrip("/")
        self.model = model
        self.api_key = api_key
        # `timeout` bounds silence on the socket, not the length of a turn.
        self.timeout = timeout
        self.deadline = deadline
        self.max_tokens = max_tokens

    # --- transport -------------------------------------------------

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @classmethod
    def _get_json(cls, url, api_key="", timeout=2.5):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if api_key:
            request.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def _post_json(self, url, payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**self._headers(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"{self.backend} HTTP {exc.code} from {url}: {body}") from exc

    # --- discovery -------------------------------------------------

    @classmethod
    def _list_ollama(cls, base, api_key):
        data = cls._get_json(f"{base}/api/tags", api_key)
        return [m.get("name") for m in data.get("models", []) if m.get("name")]

    @classmethod
    def _list_openai(cls, base, api_key):
        data = cls._get_json(f"{base}/models", api_key)
        return [m.get("id") for m in data.get("data", []) if m.get("id")]

    @classmethod
    def _list_turbofieldfare(cls, base, api_key):
        """TurboFieldfare serves an OpenAI subset plus a /health marker on the root."""
        root = base[:-3] if base.endswith("/v1") else base
        health = cls._get_json(f"{root}/health", api_key)
        if health.get("status") != "ok":
            raise RuntimeError("not a TurboFieldfare server")
        return cls._list_openai(base, api_key)

    @staticmethod
    def _candidates(explicit_base):
        env_base = explicit_base or os.environ.get("TURBOFIELDFARE_BASE_URL")
        if env_base:
            base = env_base.rstrip("/")
            if base.endswith("/v1"):
                return [("turbofieldfare", base), ("openai", base),
                        ("ollama", base[:-3].rstrip("/"))]
            return [("turbofieldfare", f"{base}/v1"), ("ollama", base),
                    ("openai", f"{base}/v1")]
        return [
            # TurboFieldfare first: its loopback server is the intended brain.
            ("turbofieldfare", "http://127.0.0.1:8080/v1"),
            ("ollama", "http://127.0.0.1:11434"),
            ("openai", "http://127.0.0.1:1234/v1"),   # LM Studio
            ("openai", "http://127.0.0.1:8000/v1"),   # vLLM
        ]

    @staticmethod
    def _pick(names, wanted):
        if wanted:
            return wanted
        for hint in ("turbofieldfare", "fieldfare", "turbo", "gemma-4", "gemma4b", "gemma"):
            for name in names:
                if hint in name.lower():
                    return name
        return names[0] if names else None

    @classmethod
    def connect(cls, base_url=None, model=None):
        """Find a running local server and return a client for it."""
        api_key = os.environ.get("TURBOFIELDFARE_API_KEY", "")
        timeout = float(os.environ.get("TURBOFIELDFARE_TIMEOUT", DEFAULT_STALL))
        deadline = float(os.environ.get("TURBOFIELDFARE_DEADLINE", DEFAULT_DEADLINE))
        max_tokens = int(os.environ.get("TURBOFIELDFARE_MAX_TOKENS", DEFAULT_MAX_TOKENS))
        wanted = model or os.environ.get("TURBOFIELDFARE_MODEL")
        tried = []

        for backend, base in cls._candidates(base_url):
            try:
                if backend == "ollama":
                    names = cls._list_ollama(base, api_key)
                elif backend == "turbofieldfare":
                    names = cls._list_turbofieldfare(base, api_key)
                else:
                    names = cls._list_openai(base, api_key)
            except Exception as exc:  # not running / wrong shape / refused
                tried.append(f"  - {base} ({exc.__class__.__name__})")
                continue
            chosen = cls._pick(names, wanted)
            if not chosen:
                tried.append(f"  - {base} (no models loaded)")
                continue
            return cls(backend, base, chosen, api_key, timeout, deadline, max_tokens)

        raise RuntimeError(
            "Could not reach a local model server. Tried:\n"
            + "\n".join(tried)
            + "\n\nThe app alone does not serve requests: its model is exposed by the\n"
            "TurboFieldfareServer product, built from a turbo-fieldfare checkout:\n"
            "  swift build -c release --product TurboFieldfareServer\n"
            "  .build/release/TurboFieldfareServer --model /path/to/gemma4.gturbo\n"
            "Or point the harness at a server that is already running:\n"
            "  TURBOFIELDFARE_BASE_URL=http://127.0.0.1:PORT TURBOFIELDFARE_MODEL=<model-id> python3 main.py\n"
            "Or run without a model: python3 main.py --simulate"
        )

    # --- generation -------------------------------------------------

    @property
    def supports_tools(self):
        """The OpenAI-shaped servers take function declarations; Ollama's does not here."""
        return self.backend in ("openai", "turbofieldfare")

    @staticmethod
    def _normalise_calls(raw_calls, fallback_id="call"):
        calls = []
        for index, call in enumerate(raw_calls or []):
            function = call.get("function") or {}
            arguments_raw = function.get("arguments")
            arguments = None
            if isinstance(arguments_raw, dict):
                arguments = arguments_raw
            elif isinstance(arguments_raw, str):
                try:
                    arguments = json.loads(arguments_raw)
                except json.JSONDecodeError:
                    arguments = None
            calls.append({
                "id": call.get("id") or f"{fallback_id}-{index}",
                "name": function.get("name") or "",
                "arguments": arguments,
                "arguments_raw": arguments_raw,
            })
        return calls

    def chat(self, messages, tools=True, on_delta=None):
        if self.backend == "ollama":
            data = self._post_json(
                f"{self.base}/api/chat",
                {"model": self.model, "messages": messages, "stream": False,
                 "options": {"temperature": 0.2}},
            )
            message = data.get("message") or {}
            raw = message.get("tool_calls") or []
            return Reply(message.get("content") or "", self._normalise_calls(raw), raw)

        payload = {"model": self.model, "messages": messages,
                   "temperature": 0.2, "stream": True}
        if self.max_tokens:
            payload["max_tokens"] = int(self.max_tokens)
        if self.supports_tools and tools:
            payload["tools"] = TOOLS
        return self._stream(f"{self.base}/chat/completions", payload, on_delta)

    def _stream(self, url, payload, on_delta=None):
        """Read the answer as it arrives instead of waiting for all of it.

        The socket timeout bounds silence, not total duration, so a turn that
        keeps producing tokens is never killed for being long - which is what
        used to happen when the model rewrote a whole file. If the stream does
        break, whatever arrived is kept and reported as partial rather than
        thrown away with the turn.
        """
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**self._headers(), "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            method="POST",
        )

        content = []
        calls = {}
        finish = None
        started = time.monotonic()
        failure = None

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    if time.monotonic() - started > self.deadline:
                        failure = f"still generating after {self.deadline:.0f}s"
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(f"{self.backend} error: {str(chunk['error'])[:300]}")
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            content.append(piece)
                            if on_delta is not None:
                                on_delta(piece)
                        for tool_delta in delta.get("tool_calls") or []:
                            index = tool_delta.get("index")
                            slot = calls.setdefault(
                                len(calls) if index is None else index,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            if tool_delta.get("id"):
                                slot["id"] = tool_delta["id"]
                            function = tool_delta.get("function") or {}
                            if function.get("name"):
                                slot["name"] = function["name"]
                            if function.get("arguments"):
                                slot["arguments"] += function["arguments"]
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"{self.backend} HTTP {exc.code} from {url}: {body}") from exc
        except (socket.timeout, TimeoutError):
            failure = f"no token for {self.timeout:.0f}s"
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                failure = f"no token for {self.timeout:.0f}s"
            else:
                failure = f"connection lost ({reason})"

        text = "".join(content)
        raw_calls = []
        for index in sorted(calls):
            slot = calls[index]
            if not slot["name"]:
                continue
            raw_calls.append({
                "id": slot["id"] or f"call-{index}",
                "type": "function",
                "function": {"name": slot["name"],
                             "arguments": slot["arguments"] or "{}"},
            })

        if failure and not text and not raw_calls:
            # Callers add "model request failed: ", so do not repeat it here.
            raise RuntimeError(failure)

        elapsed = time.monotonic() - started
        if failure:
            note = f"[the answer was cut off after {elapsed:.0f}s: {failure}]"
            text = f"{text}\n\n{note}" if text else note
        return Reply(text, self._normalise_calls(raw_calls), raw_calls,
                     incomplete=bool(failure), elapsed=elapsed)

    def summarize(self, transcript):
        """Fold a transcript into notes for compaction; no tools, short answer."""
        reply = self.chat([
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": transcript},
        ], tools=False)
        return reply.content.strip()


class SimulatedModel:
    """Canned stand-in so the loop runs without a model server."""
    def __init__(self):
        self.turns = 0

    def chat(self, messages, on_delta=None):
        self.turns += 1
        if self.turns == 1:
            return Reply("Thought: the mission needs a file, so I will write it.\n"
                         "[WRITE::test.txt|Hello from the Local-Harness]")
        return Reply("Thought: the file exists and the mission is complete.")


def estimate_tokens(messages):
    """Rough token count for a conversation; enough to decide when to compact."""
    chars = 0
    for message in messages:
        content = message.get("content")
        chars += len(content) if isinstance(content, str) else len(json.dumps(content or ""))
        calls = message.get("tool_calls")
        if calls:
            chars += len(json.dumps(calls))
    return int(chars / 3.6) + 4 * len(messages)


def _transcript(messages):
    lines = []
    for message in messages:
        if message.get("role") == "tool":
            lines.append("RESULT: " + str(message.get("content") or "")[:600])
            continue
        text = str(message.get("content") or "").strip()
        if text:
            lines.append(f"{str(message.get('role')).upper()}: {text[:1500]}")
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            lines.append(f"TOOL CALL: {function.get('name')}({str(function.get('arguments'))[:300]})")
    return "\n".join(lines)[:14000]


def _digest(messages):
    """Deterministic notes, used when the model cannot write a summary."""
    notes = []
    for message in messages:
        if message.get("role") == "tool":
            notes.append("- result: " + " ".join(str(message.get("content") or "").split())[:140])
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            notes.append(f"- {function.get('name')}({str(function.get('arguments'))[:100]})")
    return "\n".join(notes[-40:])[:2500] or "(no earlier actions recorded)"


def compact_messages(model, messages, context_limit, console, trace=None):
    """Fold older turns into a summary when the conversation outgrows the window.

    The system prompt and the first user message (the mission) are pinned; the
    tail keeps roughly a quarter of the window, cut on a turn boundary so a tool
    result is never left without the assistant call that produced it. Mutates
    `messages` in place and returns True when it replaced anything.
    """
    if not context_limit or context_limit <= 0 or len(messages) <= 4:
        return False
    used = estimate_tokens(messages)
    if used < int(context_limit * COMPACT_AT):
        return False

    keep_budget = max(800, int(context_limit * 0.25))
    index = 2
    for position in range(len(messages) - 1, 1, -1):
        index = position
        if estimate_tokens(messages[position:]) >= keep_budget:
            break
    while index < len(messages) and messages[index].get("role") == "tool":
        index += 1

    older = messages[2:index]
    if not older:
        return False

    transcript = _transcript(older)
    summary = ""
    summarizer = getattr(model, "summarize", None)
    if callable(summarizer):
        try:
            summary = (summarizer(transcript) or "").strip()
        except Exception as exc:  # a failed summary must not fail the run
            console.emit("warning", f"summary failed ({exc}); using a digest instead",
                         "⚠️  ", error=True)
    if not summary:
        summary = _digest(older)

    messages[2:index] = [{
        "role": "user",
        "content": "COMPACTED SUMMARY OF EARLIER TURNS (the original messages were "
                   "dropped to fit the context window):\n" + summary,
    }]
    after = estimate_tokens(messages)
    console.emit("compaction",
                 f"Compacted {len(older)} earlier message(s): ~{used} → ~{after} tokens",
                 "🗜️  ", before=used, after=after, dropped=len(older))
    if trace:
        trace.write({"type": "compaction", "before_tokens": used, "after_tokens": after,
                     "dropped_messages": len(older), "summary": summary})
    return True


def run_tool(env, browser, call, gate=None, shell=None):
    """Map a declared function call onto a real action.

    Returns (action, canonical args, result). The canonical args keep the old
    trace shape - "filename|content" for writes - so logs stay replayable
    whichever route produced them.
    """
    name = call.get("name") or ""
    arguments = call.get("arguments")
    if not isinstance(arguments, dict):
        raw = str(call.get("arguments_raw"))[:200]
        return (name.upper() or "TOOL", "",
                f"ERROR: {name or 'tool'} arguments were not a JSON object: {raw}")
    if name == "write_file":
        filename = str(arguments.get("filename", "")).strip()
        content = arguments.get("content")
        content = "" if content is None else str(content)
        return "WRITE", f"{filename}|{content}", env.write_file(filename, content)
    if name == "read_file":
        filename = str(arguments.get("filename", "")).strip()
        return "READ", filename, env.read_file(filename)
    if name == "run_shell":
        command = str(arguments.get("command", "")).strip()
        if not command:
            return "SHELL", "", "ERROR: run_shell needs a command"
        if gate is not None:
            allowed, note = gate.review("run_shell", command)
            if not allowed:
                return "SHELL", command, f"DENIED: {note}. The command did not run."
        return "SHELL", command, (shell or Shell(env.root)).run(command)
    if name == "fetch_url":
        url = str(arguments.get("url", "")).strip()
        return "FETCH", url, browser.fetch(url)
    if name == "fetch_links":
        url = str(arguments.get("url", "")).strip()
        return "FETCH_LINKS", url, browser.links(url)
    return (name.upper() or "TOOL", "",
            f"ERROR: Unknown tool '{name}'. Use write_file, read_file, run_shell, "
            "fetch_url or fetch_links.")


class Harness:
    """The Orchestrator: The Interceptor and the Evaluator."""
    def __init__(self, task, env, model, max_iterations=8, max_idle=2, trace=None,
                 console=None, browser=None, context_limit=DEFAULT_CONTEXT, compact=True,
                 gate=None, shell=None):
        self.task = task
        self.env = env
        self.model = model
        self.max_iterations = max_iterations
        self.max_idle = max_idle
        self.trace = trace
        self.console = console or Console()
        self.browser = browser or Browser()
        self.context_limit = context_limit
        self.compact = compact
        self.gate = gate
        self.shell = shell or Shell(env.root)
        self._received = 0
        self._last_note = 0.0
        # The 'Trigger' marker: [ACTION:: — args run to that trigger's closing bracket
        self.trigger = re.compile(r"\[(\w+)::")

    def run_loop(self, initial_prompt):
        self.console.emit("mission", self.task.goal, "🚀 Mission: ")
        started = time.monotonic()
        self._trace({
            "type": "run_start",
            "mission": self.task.goal,
            "initial_prompt": initial_prompt,
            "max_iterations": self.max_iterations,
            "model": {
                "class": type(self.model).__name__,
                "backend": getattr(self.model, "backend", "simulated"),
                "base": getattr(self.model, "base", None),
                "model": getattr(self.model, "model", None),
            },
        })
        self.uses_tools = bool(getattr(self.model, "supports_tools", False))
        messages = [
            {"role": "system", "content": system_prompt(self.uses_tools)},
            {"role": "user", "content": f"MISSION: {self.task.goal}\n\n{initial_prompt}"},
        ]
        idle = 0

        for iteration in range(1, self.max_iterations + 1):
            self.console.emit("iteration", f"Iteration {iteration}", "🔄 ",
                              iteration=iteration)

            # 0. Keep the conversation inside the model's context window
            if self.compact:
                compact_messages(self.model, messages, self.context_limit,
                                 self.console, self.trace)

            # 1. The 'Brain' generates a response
            self._received = 0
            self._last_note = time.monotonic()
            try:
                reply = self.model.chat(messages, on_delta=self._generating)
            except (RuntimeError, OSError, http.client.HTTPException) as exc:
                message = f"model request failed: {exc}"
                self.console.error(message)
                self._trace({"type": "run_error", "iteration": iteration, "error": message})
                self._end_run(False, iteration, started, error=message)
                return False
            if reply.incomplete:
                self.console.emit("warning",
                                  f"The model's turn was cut off after {reply.elapsed:.0f}s; "
                                  "keeping what arrived.", "⚠️  ")
            assistant = {"role": "assistant", "content": reply.content or None}
            if reply.raw_tool_calls:
                assistant["tool_calls"] = reply.raw_tool_calls
            messages.append(assistant)
            self.console.emit("thought", reply.content.strip() or "(no text)", "🧠 Thought: ")

            # 2. Function calls first, then any text triggers for older backends
            observations = []
            tool_messages = []

            for call in reply.tool_calls:
                action, args, result = self._run_tool(call)
                shown = call["arguments"] if call["arguments"] is not None else call["arguments_raw"]
                self.console.emit("tool", f"{call['name']}({json.dumps(shown)[:110]})",
                                  "🔧 Tool call: ", tool=call["name"], arguments=call["arguments"])
                self.console.emit("result", result, "📥 Result: ", ok=not result.startswith("ERROR"))

                # 3. The 'Hands' have run; report back the way the protocol expects
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })
                self._record_step(iteration, reply.content, action, args, result, "tool",
                                  f"{call['name']} -> {result}", call["name"], call["arguments"])
                observations.append(f"{call['name']} -> {result}")

                if self.task.validator(self.env):
                    self.console.emit("success", "MISSION COMPLETE!", "✅ ")
                    self._end_run(True, iteration, started)
                    return True

            triggers = self._parse_actions(reply.content)
            for action, args in triggers:
                self.console.emit("action", f"{action} {args.strip()[:120]}", "⚡ Action: ",
                                  action=action, args=args.strip())
                result = self._execute(action, args.strip())
                self.console.emit("result", result, "📥 Result: ", ok=not result.startswith("ERROR"))
                self._record_step(iteration, reply.content, action, args.strip(), result, "text",
                                  f"{action} -> {result}")
                observations.append(f"{action} -> {result}")

                # 4. The 'Feedback' — the observation drives the next thought
                if self.task.validator(self.env):
                    self.console.emit("success", "MISSION COMPLETE!", "✅ ")
                    self._end_run(True, iteration, started)
                    return True

            messages.extend(tool_messages)

            if reply.tool_calls or triggers:
                idle = 0
                if triggers:
                    messages.append({"role": "user", "content": "OBSERVATION:\n" + "\n".join(observations)})
            else:
                idle += 1
                message = (("No function call found. Call write_file or read_file, or answer "
                            "briefly if the mission is already done.") if self.uses_tools else
                           ("No action trigger found. Reply with exactly one "
                            "[WRITE::filename|content] or [READ::filename] trigger."))
                self._trace({"type": "no_action", "iteration": iteration,
                             "thought": reply.content.strip(), "observation": message})
                messages.append({"role": "user", "content": "OBSERVATION:\n" + message})

            if idle >= self.max_idle:
                self.console.emit("warning", "No actions taken twice in a row — stopping.", "⚠️  ")
                self._end_run(False, iteration, started)
                return False

        self.console.emit("warning", "Iteration budget exhausted before the mission validated.", "⏹️  ")
        self._end_run(False, self.max_iterations, started)
        return False

    def _trace(self, record):
        if self.trace:
            self.trace.write(record)

    def _generating(self, piece):
        """Report life during a long turn, without printing every token."""
        self._received += len(piece)
        now = time.monotonic()
        if now - self._last_note < 10:
            return
        self._last_note = now
        self.console.emit("generating", f"generating… {self._received} chars", "… ",
                          chars=self._received)

    def _end_run(self, completed, iteration, started, error=None):
        record = {
            "type": "run_end",
            "completed": completed,
            "iterations": iteration,
            "steps": len(self.task.history),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "files": self.env.files(),
        }
        if error:
            record["error"] = error
        self._trace(record)
        if self.trace:
            self.trace.close()

    def _parse_actions(self, text):
        """Split a response into (action, args) pairs.

        Args run from the marker to the last closing bracket before the next
        trigger, so written content may contain newlines and brackets of its own
        (pretty-printed JSON, lists, markdown links) without being truncated.
        """
        starts = list(self.trigger.finditer(text))
        actions = []
        for index, start in enumerate(starts):
            limit = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            segment = text[start.end():limit]
            close = segment.rfind("]")
            if close == -1:
                continue  # unterminated trigger, treat as no action
            actions.append((start.group(1), segment[:close]))
        return actions

    def _record_step(self, iteration, thought, action, args, result, via, observation,
                     tool=None, arguments=None):
        entry = {
            "iteration": iteration,
            "thought": thought.strip(),
            "action": action,
            "args": args,
            "result": result,
            "via": via,
        }
        if tool:
            entry["tool"] = tool
            entry["arguments"] = arguments
        self.task.history.append(entry)
        self._trace({"type": "step", **entry, "observation": observation})

    def _run_tool(self, call):
        """Run a declared function call through the same hands a mission uses."""
        return run_tool(self.env, self.browser, call, self.gate, self.shell)

    def _execute(self, action, args):
        if action == "WRITE":
            if "|" not in args:
                return "ERROR: WRITE needs the form [WRITE::filename|content]"
            filename, content = args.split("|", 1)
            filename = filename.strip()
            if not filename:
                return "ERROR: WRITE needs a filename before the pipe"
            return self.env.write_file(filename, content)

        if action == "READ":
            return self.env.read_file(args)

        if action == "SHELL":
            if self.gate is not None:
                allowed, note = self.gate.review("run_shell", args)
                if not allowed:
                    return f"DENIED: {note}. The command did not run."
            return self.shell.run(args)

        if action == "FETCH":
            return self.browser.fetch(args)

        return f"ERROR: Unknown action '{action}'. Use WRITE, READ, SHELL or FETCH."


class ChatSession:
    """A conversation with the local model, driven over stdin/stdout NDJSON.

    The app writes one JSON object per line to stdin:

        {"type": "user", "text": "hi"}
        {"type": "reset"}
        {"type": "quit"}

    and reads the same event stream a mission produces. Tool calls work exactly
    as they do in a mission, so the assistant can write files or read pages and
    answer from what it found.
    """

    MAX_TOOL_ROUNDS = 6

    def __init__(self, model, env, console, trace=None,
                 context_limit=DEFAULT_CONTEXT, compact=True, gate=None, shell=None):
        self.model = model
        self.env = env
        self.browser = Browser()
        self.console = console
        self.trace = trace
        self.context_limit = context_limit
        self.compact = compact
        self.gate = gate
        self.shell = shell or Shell(env.root)
        self.messages = [{"role": "system", "content": CHAT_PROMPT}]
        self.turns = 0

    def handle_user(self, text):
        text = (text or "").strip()
        if not text:
            return
        self.turns += 1
        self.console.emit("chat", text, "👤 ", role="user", turn=self.turns)
        self._log("user", text)
        self.messages.append({"role": "user", "content": text})

        for _round in range(self.MAX_TOOL_ROUNDS):
            if self.compact:
                compact_messages(self.model, self.messages, self.context_limit,
                                 self.console, self.trace)
            self.console.emit("chat_status", "thinking…", "… ")
            try:
                reply = self.model.chat(self.messages, on_delta=self._delta)
            except (RuntimeError, OSError, http.client.HTTPException) as exc:
                message = f"model request failed: {exc}"
                self.console.error(message)
                self.console.emit("chat_status", "error", "", error=message)
                self._trace({"type": "chat_error", "error": message})
                return

            if reply.incomplete:
                self.console.emit("warning",
                                  f"The answer was cut off after {reply.elapsed:.0f}s; "
                                  "keeping what arrived.", "⚠️  ")

            assistant = {"role": "assistant", "content": reply.content or None}
            if reply.raw_tool_calls:
                assistant["tool_calls"] = reply.raw_tool_calls
            self.messages.append(assistant)
            if reply.content.strip():
                # Log first: the transcript is what the UI's history reads.
                self._log("assistant", reply.content.strip())
                self.console.emit("chat", reply.content.strip(), "🤖 ",
                                  role="assistant", turn=self.turns)
            if not reply.tool_calls:
                self.console.emit("chat_status", "idle", "")
                return

            for call in reply.tool_calls:
                action, args, result = run_tool(self.env, self.browser, call,
                                                self.gate, self.shell)
                shown = call["arguments"] if call["arguments"] is not None else call["arguments_raw"]
                self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                      "content": result})
                self._log("tool", f"{call['name']}({args[:90]}) -> {result[:400]}")
                self.console.emit("tool", f"{call['name']}({json.dumps(shown)[:110]})",
                                  "🔧 Tool call: ", tool=call["name"], arguments=call["arguments"])
                self.console.emit("result", result[:600], "📥 Result: ",
                                  ok=not result.startswith("ERROR"))
                self._trace({"type": "chat_step", "tool": call["name"], "action": action,
                             "args": args, "result": result[:600]})

        self.console.emit("warning", f"Stopped after {self.MAX_TOOL_ROUNDS} tool rounds.", "⚠️  ")
        self.console.emit("chat_status", "idle", "")

    def reset(self):
        self.messages = [{"role": "system", "content": CHAT_PROMPT}]
        self.console.emit("chat_reset", "conversation cleared", "🧹 ")

    def _delta(self, piece):
        """Pass each token on as it arrives, so the pane fills in live."""
        self.console.emit("chat_delta", piece, "")

    def _log(self, role, text):
        self._trace({"type": "message", "role": role, "text": text})

    def _trace(self, record):
        if self.trace:
            self.trace.write(record)


# --- EXECUTION ---

def attach_model(args, console):
    """Connect to the local model (or the simulated one) and report what answered."""
    if args.simulate:
        console.emit("log", "Running with the simulated model.", "🧩 ")
        return SimulatedModel()

    try:
        model = LocalModel.connect(args.base_url, args.model)
    except RuntimeError as exc:
        console.error(str(exc))
        return None

    if getattr(args, "max_tokens", None):
        model.max_tokens = args.max_tokens

    explicit = args.base_url or os.environ.get("TURBOFIELDFARE_BASE_URL")
    if model.backend == "turbofieldfare":
        console.emit("model", f"TurboFieldfare at {model.base} using '{model.model}'", "🔌 ",
                     backend=model.backend, base=model.base, model=model.model)
    else:
        console.emit("model", f"Connected to {model.backend} at {model.base} using '{model.model}'",
                     "🔌 ", backend=model.backend, base=model.base, model=model.model)
        if not explicit:
            console.emit(
                "warning",
                "Not TurboFieldfare: no loopback server was found on :8080, so the mission "
                "would run on this model instead. Start the real one with "
                ".build/release/TurboFieldfareServer --model /path/to/gemma4.gturbo",
                "⚠️  ", error=True,
            )
    return model


def interactive_stdin():
    """True when a person could answer a prompt on this stdin."""
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def chat_loop(args, console):
    """Answer chat turns read from stdin until stdin closes."""
    sandbox = Environment(args.sandbox or os.environ.get("TURBOFIELDFARE_SANDBOX") or "sandbox")
    console.emit("log", f"Sandbox: {sandbox.root}", "📂 ", path=sandbox.root)

    model = attach_model(args, console)
    if model is None:
        return 1

    trace = None
    path = args.chat_log or default_chat_path()
    try:
        trace = TraceLog(path)
        console.emit("log", f"Transcript: {os.path.abspath(path)}", "💬 ", transcript=path)
    except OSError as exc:
        console.emit("warning", f"Chat transcript disabled ({exc})", "⚠️  ", error=True)

    # One reader for stdin: it carries both chat turns and approval answers.
    channel = InputChannel()
    broker = (StreamApprovals(console, channel, args.approval_timeout)
              if args.approve == "ask" else None)
    gate = ApprovalGate(args.approve, broker, trace)

    session = ChatSession(model, sandbox, console, trace,
                          context_limit=args.context, compact=not args.no_compact,
                          gate=gate, shell=Shell(sandbox.root, args.shell_timeout))
    console.emit("chat_ready", "ready", "💬 ",
                 model=getattr(model, "model", "simulated"),
                 backend=getattr(model, "backend", "simulated"),
                 transcript=os.path.abspath(path) if trace else None,
                 approvals=gate.policy)

    try:
        while True:
            payload = channel.next_turn()
            if payload is None:
                break
            kind = payload.get("type")
            if kind == "user":
                session.handle_user(payload.get("text") or payload.get("content") or "")
            elif kind == "reset":
                session.reset()
            elif kind == "quit":
                break
    except KeyboardInterrupt:
        pass
    finally:
        if trace:
            trace.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local-Harness driven by TurboFieldfare (gemma4b).")
    parser.add_argument("--base-url", help="local server base URL (default: auto-detect)")
    parser.add_argument("--model", help="model id/tag exposed by the server")
    parser.add_argument("--simulate", action="store_true", help="run with canned replies, no model")
    parser.add_argument("--task", help="run a saved mission from a JSON file (see load_task)")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="turn budget (default: 8, or the saved task's)")
    parser.add_argument("--sandbox", help="directory the model may read and write (default: sandbox/)")
    parser.add_argument("--trace", help="JSONL trace path (default: runs/trace-<utc>-<id>.jsonl)")
    parser.add_argument("--no-trace", action="store_true", help="do not write a trace log")
    parser.add_argument("--goal", default=None,
                        help=f"mission goal handed to the model (default: {DEFAULT_GOAL})")
    parser.add_argument("--expect-file", default=None,
                        help=f"success check: this file exists (default: {DEFAULT_EXPECT_FILE})")
    parser.add_argument("--expect-contains", default=None,
                        help="success check: the file also contains this text")
    parser.add_argument("--context", type=int, default=None,
                        help=f"context window in tokens (default: {DEFAULT_CONTEXT})")
    parser.add_argument("--no-compact", action="store_true",
                        help="never summarise earlier turns")
    parser.add_argument("--serve-chat", action="store_true",
                        help="chat mode: one NDJSON user turn per stdin line")
    parser.add_argument("--chat-log", help="chat transcript path (default: chats/chat-<utc>-<id>.jsonl)")
    parser.add_argument("--approve", choices=("ask", "allow", "deny"), default="ask",
                        help="run_shell policy: ask the user (default), or allow/deny outright")
    parser.add_argument("--approval-timeout", type=float, default=300.0,
                        help="seconds to wait for an approval answer (default 300)")
    parser.add_argument("--shell-timeout", type=float, default=DEFAULT_SHELL_TIMEOUT,
                        help=f"seconds a shell command may run (default {DEFAULT_SHELL_TIMEOUT:.0f})")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help=f"cap on one answer (default {DEFAULT_MAX_TOKENS})")
    parser.add_argument("--events", action="store_true",
                        help="emit NDJSON events on stdout instead of human text")
    args = parser.parse_args(argv)

    console = Console(args.events)
    if args.context is None:
        args.context = int(os.environ.get("TURBOFIELDFARE_CONTEXT", DEFAULT_CONTEXT))

    if args.serve_chat:
        return chat_loop(args, console)

    # 1. Define the Task: a saved mission first, then flags, then the defaults
    spec = {}
    if args.task:
        try:
            spec = load_task(args.task)
        except (OSError, ValueError) as exc:
            console.error(f"could not read task {args.task}: {exc}")
            return 1
        console.emit("task", str(spec.get("name") or os.path.basename(args.task)), "🗂️  ",
                     task=args.task, goal=spec.get("goal"))

    goal = args.goal or spec.get("goal") or DEFAULT_GOAL
    expect_file = args.expect_file or spec.get("expect_file") or DEFAULT_EXPECT_FILE
    expect_contains = (args.expect_contains if args.expect_contains is not None
                       else spec.get("expect_contains"))
    max_iterations = args.max_iterations or int(spec.get("max_iterations") or 8)

    def validator(env):
        if not env.exists(expect_file):
            return False
        if expect_contains is None:
            return True
        return expect_contains in env.read_file(expect_file)

    mission = Task(goal=goal, validator_func=validator)

    # 2. Setup the Environment (a real directory on disk)
    sandbox = Environment(args.sandbox or os.environ.get("TURBOFIELDFARE_SANDBOX") or "sandbox")
    console.emit("log", f"Sandbox: {sandbox.root}", "📂 ", path=sandbox.root)

    # 3. Attach the Brain
    model = attach_model(args, console)
    if model is None:
        return 1
    console.emit("log",
                 f"Context {args.context} tokens, compaction "
                 f"{'off' if args.no_compact else f'on at {int(COMPACT_AT * 100)}%'}",
                 "🧮 ", context=args.context, compaction=not args.no_compact)

    # 4. Launch the Harness
    trace = None
    if not args.no_trace:
        path = args.trace or default_trace_path()
        try:
            trace = TraceLog(path)
            console.emit("log", f"Tracing to {os.path.abspath(path)}", "📝 ", trace=path)
        except OSError as exc:
            console.emit("warning", f"Trace disabled ({exc})", "⚠️  ", error=True)

    # 5. Who answers approval requests: a terminal prompt, or the UI driving us
    channel = None
    broker = None
    if args.approve == "ask":
        if args.events or not interactive_stdin():
            channel = InputChannel()
            broker = StreamApprovals(console, channel, args.approval_timeout)
        else:
            broker = TerminalApprovals()
    gate = ApprovalGate(args.approve, broker, trace)
    if args.approve == "ask":
        console.emit("log", "Shell commands will wait for your approval.", "🔐 ",
                     approvals="ask")
    else:
        console.emit("log", f"Shell commands are {args.approve}d without asking.", "🔐 ",
                     approvals=args.approve)

    harness = Harness(mission, sandbox, model, max_iterations=max_iterations,
                      trace=trace, console=console, context_limit=args.context,
                      compact=not args.no_compact, gate=gate,
                      shell=Shell(sandbox.root, args.shell_timeout))
    try:
        completed = harness.run_loop("Start the mission.")
    finally:
        if trace:
            trace.close()

    console.emit("finished", f"{len(mission.history)} step(s) recorded", "📚 ",
                 completed=completed, steps=len(mission.history), sandbox=sandbox.root)
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
