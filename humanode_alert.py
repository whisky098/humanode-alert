#!/usr/bin/env python3
"""Watches the BioAuth status of a local Humanode node and sends Telegram alerts.

Settings come from environment variables (see config.env.example). To run it by hand:
    python3 humanode_alert.py /etc/humanode-alert/config.env

The chat id, timezone and notification switch are set from Telegram and kept in state.json.

To check the node controls without changing anything (run as the node user):
    python3 humanode_alert.py /etc/humanode-alert/config.env --engine-probe
"""

import base64
import html
import itertools
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None


def log(message):
    print(message, flush=True)


def load_env_file(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def setting(name, default=None):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if default is None:
        sys.exit(f"Missing required setting {name}")
    return default


FLAGS = {arg for arg in sys.argv[1:] if arg.startswith("--")}
DETECT_RPC = "--detect-rpc" in FLAGS  # used by install.sh
ENGINE_PROBE = "--engine-probe" in FLAGS
CONFIG_ARGS = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
CONFIG_FILE = CONFIG_ARGS[0] if CONFIG_ARGS else "/etc/humanode-alert/config.env"
if CONFIG_ARGS:
    load_env_file(CONFIG_FILE)

TOKEN = setting("TELEGRAM_TOKEN", "" if FLAGS else None)
# bot usernames are public, so /register needs this code to claim the bot
REGISTER_CODE = setting("REGISTER_CODE", "" if FLAGS else None)
RPC_URL = setting("RPC_URL", "http://127.0.0.1:9933")
# tried at startup when RPC_URL does not respond; newer nodes serve HTTP RPC on 9944
RPC_CANDIDATES = ["http://127.0.0.1:9944", "http://127.0.0.1:9933"]
WORKSPACE = setting("HUMANODE_WORKSPACE", "/root/.humanode/workspaces/default")
AUTH_URL_CMD = [f"{WORKSPACE}/humanode-peer", "bioauth", "auth-url", "--rpc-url-ngrok-detect",
                "--chain", f"{WORKSPACE}/chainspec.json"]
TUNNEL_CMD = [f"{WORKSPACE}/humanode-websocket-tunnel"]
# the Humanode Launcher installs its engine in ~/.humanode, next to the workspaces folder
ENGINE = setting("HUMANODE_ENGINE", os.path.join(os.path.dirname(os.path.dirname(WORKSPACE)), "humanode-engine"))
WARN_MINUTES = {int(m) for m in setting("WARN_MINUTES", "60,30,5,3,1").split(",") if m.strip()}
ALERT_REPEAT_MINUTES = int(setting("ALERT_REPEAT_MINUTES", "5"))
DAILY_REPORT_HOUR = 12
# systemd sets STATE_DIRECTORY (/var/lib/humanode-alert); otherwise use the current directory
STATE_FILE = os.path.join(os.environ.get("STATE_DIRECTORY", "."), "state.json")

# Telegram messages use HTML formatting: escape any value that is not ours with html.escape
SUCCESSFULLY_REGISTERED_MESSAGE = f"""✅ <b>Registered</b>

You will receive:
• ⏰ reminders {', '.join(map(str, sorted(WARN_MINUTES, reverse=True)))} minutes before BioAuth expires
• 🚨 alerts when BioAuth has expired or the node is down
• ☀️ a daily report at {DAILY_REPORT_HOUR}:00, if enabled

Use the buttons below, or /help for all commands."""
UNKNOWN_COMMAND = "🤔 Unknown command. Send /help to see all commands."
MISSING_CHAT_ID = "Chat id not found, register with '/register code', the code is REGISTER_CODE in config.env"
NOT_REGISTERED = "🔒 <b>This bot is not registered yet</b>\n\nSend <code>/register code</code>, the code is <code>REGISTER_CODE</code> in config.env."
WRONG_REGISTER_CODE = "⛔ <b>Wrong or missing code</b>\n\nSend <code>/register code</code>, the code is <code>REGISTER_CODE</code> in config.env."
SUCCESSFULLY_REGISTERED_TIME_ZONE_ID = "🕒 Timezone set to <b>{}</b>"
ENABLE_NOTIF = f"🔔 Daily report <b>enabled</b>, every day at {DAILY_REPORT_HOUR}:00"
DISABLED_NOTIF = "🔕 Daily report <b>disabled</b>"
CANT_GET_URL = "❌ <b>Can't get the authentication link</b>\n\nCheck the logs and HUMANODE_WORKSPACE in config.env."
HELP = f"""📖 <b>Commands</b>

<b>BioAuth</b>
/status – time left until the next BioAuth
/get_bioauth_link – new authentication link

<b>Node</b>
/node_status – node process and available updates
/restart_node, /stop_node, /start_node – control the node
/update_node – install a node update

<b>Settings</b>
/timezone – e.g. <code>/timezone Europe/Chisinau</code> or <code>/timezone UTC+3</code>
/enable_notification, /disable_notification – daily report at {DAILY_REPORT_HOUR}:00
/register – <code>/register code</code>, the code is in config.env

The buttons below run the same commands."""
# shown in the Telegram command menu next to the message field
BOT_COMMANDS = [
    ("menu", "Buttons for the commands"),
    ("status", "Time left until the next BioAuth"),
    ("get_bioauth_link", "Get a new authentication link"),
    ("node_status", "Node process and available updates"),
    ("restart_node", "Restart the node"),
    ("stop_node", "Stop the node"),
    ("start_node", "Start the node"),
    ("update_node", "Install a node update"),
    ("timezone", "Set the timezone, e.g. /timezone Europe/Chisinau"),
    ("enable_notification", "Enable the daily report at noon"),
    ("disable_notification", "Disable the daily report at noon"),
    ("register", "Register this chat: /register code"),
    ("help", "List of commands"),
]


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state():
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f, indent=2)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


state = load_state()


def parse_timezone(name):
    """Accepts IANA names (Europe/Chisinau) and UTC offsets (+3, -05:30, UTC+2)."""
    match = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?", name, re.IGNORECASE)
    if match:
        sign, hours, minutes = match.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        return timezone(-offset if sign == "-" else offset)
    if name.upper() in ("UTC", "GMT"):
        return timezone.utc
    if ZoneInfo is None:
        raise ValueError("timezone names need Python 3.9+, use an offset like UTC+3")
    return ZoneInfo(name)


def tz():
    return parse_timezone(state.get("timezone", "UTC"))


def connect_ipv4_first(address, timeout=None, source_address=None):
    """socket.create_connection that tries IPv4 before IPv6. On some servers the IPv6 route
    drops the first packet of a new connection, which delays every Telegram message by seconds."""
    host, port = address
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses.sort(key=lambda a: a[0] != socket.AF_INET)
    error = OSError(f"no address for {host}")
    for family, sock_type, proto, _, sockaddr in addresses:
        sock = socket.socket(family, sock_type, proto)
        try:
            sock.settimeout(5)  # fall back to the next address quickly
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            sock.settimeout(timeout)
            return sock
        except OSError as e:
            sock.close()
            error = e
    raise error


socket.create_connection = connect_ipv4_first  # used by urllib for every request


def post_json(url, payload, timeout=15):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def telegram(method, params, timeout=15):
    return post_json(f"https://api.telegram.org/bot{TOKEN}/{method}", params, timeout)["result"]


def plain(text):
    """The text of an HTML message without the formatting, for the logs."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_html(method, params):
    """Calls a Telegram method with an HTML text. If Telegram rejects the formatting, sends plain text instead,
    so an alert is never lost."""
    try:
        return telegram(method, {**params, "parse_mode": "HTML"})
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        log(f"Telegram rejected the formatting ({e}), sending plain text")
        return telegram(method, {**params, "text": plain(params["text"])})


def send(text, chat_id=None):
    chat_id = chat_id or state.get("chat_id")
    if chat_id is None:
        log(f"{MISSING_CHAT_ID}. Message: {plain(text)}")
        return
    log(f"Sending: {plain(text)}")
    try:
        send_html("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        log(f"Failed to send message: {e}")


def bioauth_rpc(url, timeout=10):
    return post_json(url, {"jsonrpc": "2.0", "id": 1, "method": "bioauth_status", "params": []}, timeout)


def detect_rpc_url():
    """Returns the first endpoint that answers bioauth_status, starting with RPC_URL, or None."""
    for url in dict.fromkeys([RPC_URL] + RPC_CANDIDATES):
        try:
            if "result" in bioauth_rpc(url, timeout=5):
                return url
        except Exception:
            pass
    return None


auth_lock = threading.Lock()
tunnel = None


def new_auth_url():
    """Generates a new auth link. Only /get_bioauth_link calls it: frequent requests make the node hang."""
    global tunnel
    with auth_lock:
        try:
            # reuse a running tunnel, a second one fails with "conflicting connection already exists"
            if tunnel is None or tunnel.poll() is not None:
                log("Opening tunnel")
                tunnel = subprocess.Popen(TUNNEL_CMD)
            result = subprocess.run(AUTH_URL_CMD, capture_output=True, text=True, timeout=120)
        except Exception as e:
            log(f"Auth url command failed: {e}")
            return CANT_GET_URL
        log(result.stdout + result.stderr)
        url = "".join(result.stdout.splitlines())
        return url if result.returncode == 0 and url else CANT_GET_URL


def bioauth_status():
    """Returns ("active", expires_at) or (problem, alert message)."""
    try:
        response = bioauth_rpc(RPC_URL)
    except Exception as e:
        log(f"Humanode is down: {e}")
        return "down", "🔴 <b>Humanode is down</b>\n\nThe node does not respond."

    result = response.get("result")
    if isinstance(result, str):
        return "expired", ("❌ <b>BioAuth has expired</b>\n\nPress 🔗 BioAuth link or send /get_bioauth_link "
                           "to authenticate again.")
    try:
        return "active", datetime.fromtimestamp(result["Active"]["expires_at"] / 1000, tz())
    except (KeyError, TypeError):
        log(f"Unexpected response: {response}")
        return "invalid", "⚠️ <b>Can't get the BioAuth expiry time</b>\n\nCheck the node logs."


def bioauth_information(expires_at, title="🟢 <b>BioAuth active</b>"):
    seconds = max(int((expires_at - datetime.now(tz())).total_seconds()), 0)
    days, seconds = divmod(seconds, 24 * 3600)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    remaining = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"
    send(f"{title}\n\n📅 Next BioAuth: <b>{expires_at:%Y-%m-%d %H:%M}</b> {html.escape(expires_at.tzname() or '')}\n"
         f"⏳ Remaining: <b>{remaining}</b>")


class WebSocket:
    """Minimal WebSocket client for text messages, enough to talk to the Launcher engine."""

    def __init__(self, host, port, timeout=15):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self._recv_some()
        header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if b" 101 " not in header.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket handshake failed: {header.splitlines()[0].decode(errors='replace')}")

    def _recv_some(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("the engine closed the connection")
        return chunk

    def _read(self, size):
        while len(self.buffer) < size:
            self.buffer += self._recv_some()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _send_frame(self, opcode, payload):
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
        elif size < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
        mask = os.urandom(4)  # clients must mask every frame
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def recv(self, timeout):
        """Returns the next text message, answering pings and joining fragmented frames."""
        self.sock.settimeout(timeout)
        message = b""
        while True:
            first, second = self._read(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else None
            payload = self._read(size)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 0x8:
                raise ConnectionError("the engine closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
            elif opcode in (0x0, 0x1):
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()


class EngineError(Exception):
    pass


# pids of engines started by the bot, so they are not mistaken for the Launcher's engine
bot_engines = set()


def launcher_engine_running():
    """True when an engine started by the Humanode Launcher runs, i.e. the Launcher is connected."""
    if not os.path.isdir("/proc"):
        return False
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in bot_engines:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read()
        except OSError:
            continue
        if b"humanode-engine" in cmdline and b"--workspacePath" in cmdline:
            return True
    return False


class Engine:
    """Starts the Humanode Launcher engine on this server and calls its functions over WebSocket,
    the same way the Launcher UI does through its SSH tunnel. The engine has no public API: the
    protocol was taken from the Launcher's code. The engine exits by itself after we disconnect."""

    def __init__(self):
        if launcher_engine_running():
            raise EngineError("the Humanode Launcher is connected to the node, close it and try again")
        if not os.path.exists(ENGINE):
            raise EngineError(f"the engine was not found at {ENGINE}, set HUMANODE_ENGINE in {CONFIG_FILE}")
        command = [ENGINE, "--port", "0", "--workspacePath", WORKSPACE, "--idleTimeoutMs", "3000",
                   "--host", "127.0.0.1"]
        if os.geteuid() == 0 and shutil.which("systemd-run"):
            # a scope of its own, so the node started by the engine keeps running when the bot restarts
            command = ["systemd-run", "--scope", "--quiet", "--collect",
                       f"--unit=humanode-engine-{time.time_ns()}"] + command
        log(f"Starting the engine: {' '.join(command)}")
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        cwd=os.path.dirname(os.path.dirname(ENGINE)))
        bot_engines.add(self.process.pid)
        self.ids = itertools.count()
        self.callbacks = {}
        self.responses = {}
        ports = queue.Queue()
        threading.Thread(target=self._read_output, args=(ports,), daemon=True).start()
        try:
            port = ports.get(timeout=15)
            if port is None:
                raise EngineError(f"the engine exited with code {self.process.wait()}, check the logs")
            self.ws = WebSocket("127.0.0.1", port)
        except Exception as e:
            self.process.kill()
            if isinstance(e, queue.Empty):
                raise EngineError("the engine did not start in 15 seconds, check the logs")
            raise

    def _read_output(self, ports):
        # always drain the output, a full pipe would block the engine
        for line in self.process.stdout:
            log(f"engine: {line.rstrip()}")
            match = re.search(r"Listening on port (\d+)", line)
            if match:
                ports.put(int(match.group(1)))
        ports.put(None)

    def _serialize(self, value):
        if callable(value):
            callback_id = f"callback-{next(self.ids)}"
            self.callbacks[callback_id] = value
            return {"kind": "function", "data": callback_id}
        if isinstance(value, dict):
            return {"kind": "object", "data": {k: self._serialize(v) for k, v in value.items()}}
        if isinstance(value, (list, tuple)):
            return {"kind": "array", "data": [self._serialize(v) for v in value]}
        return {"kind": "raw", "data": value}

    def _deserialize(self, value):
        kind, data = value.get("kind"), value.get("data")
        if kind == "array":
            return [self._deserialize(v) for v in data]
        if kind == "object":
            return {k: self._deserialize(v) for k, v in data.items()}
        if kind == "function":
            return None  # unsubscribe handles of watch functions, not needed
        return data

    def pump(self, done, timeout):
        """Handles engine messages, running our callbacks, until done() is true."""
        deadline = time.monotonic() + timeout
        while not done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineError("timed out waiting for the engine")
            try:
                message = json.loads(self.ws.recv(timeout=remaining))
            except socket.timeout:
                raise EngineError("timed out waiting for the engine")
            kind = message.get("kind")
            if kind == "request":  # the engine calls one of our callbacks
                callback = self.callbacks.get(message.get("functionId"))
                if callback:
                    callback(*[self._deserialize(arg) for arg in message.get("data", [])])
                self.ws.send(json.dumps({"kind": "response", "requestId": message.get("requestId"),
                                         "data": {"kind": "raw", "data": None}}))
            elif kind == "response":
                self.responses[message.get("requestId")] = (True, self._deserialize(message.get("data") or {}))
            elif kind == "error":
                self.responses[message.get("requestId")] = (False, message.get("error"))

    def call(self, function, *args, timeout=120):
        request_id = str(next(self.ids))
        log(f"Engine call: {function}")
        self.ws.send(json.dumps({"kind": "request", "functionId": function, "requestId": request_id,
                                 "data": [self._serialize(arg) for arg in args]}))
        self.pump(lambda: request_id in self.responses, timeout)
        ok, value = self.responses.pop(request_id)
        if not ok:
            raise EngineError(f"{function} failed: {value}")
        return value

    def close(self):
        self.ws.close()
        try:
            self.process.wait(timeout=15)
            bot_engines.discard(self.process.pid)
        except subprocess.TimeoutExpired:
            log("The engine is still running after the disconnect")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


node_lock = threading.Lock()  # one node operation at a time
node_stopped_on_purpose = False  # no "Humanode is down" alerts after /stop_node


def node_is_running(engine):
    status = {}
    engine.call("watchIsNodeRunning", {"onStatus": lambda s: status.setdefault("running", bool((s or {}).get("isRunning")))})
    engine.pump(lambda: "running" in status, timeout=30)
    return status["running"]


def update_available(engine):
    return bool(engine.call("checkForBinariesUpdates").get("updateAvailable"))


def peer_pids():
    """Pids of running humanode-peer nodes, not short commands like `humanode-peer bioauth auth-url`."""
    if not os.path.isdir("/proc"):
        return []
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if os.path.basename(args[0]) == b"humanode-peer" and b"bioauth" not in args and b"key" not in args:
            pids.append(int(pid))
    return pids


def wait_until(condition, timeout):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(1)
    return True


def stop_node(engine):
    global node_stopped_on_purpose
    send("⏹ Stopping the node...")
    node_stopped_on_purpose = True
    engine.call("stopWorkspace", timeout=300)
    # the engine may not stop a node started by another engine (the Launcher's), or return before
    # the node has exited and released its database lock
    if not wait_until(lambda: not peer_pids(), 60):
        log(f"The node is still running after stopWorkspace, sending SIGTERM to {peer_pids()}")
        for pid in peer_pids():
            os.kill(pid, signal.SIGTERM)  # graceful shutdown, like Ctrl+C
        if not wait_until(lambda: not peer_pids(), 120):
            raise EngineError(f"the node did not stop (pids {peer_pids()}), check it on the server")
    send("✅ Node stopped")


def start_node(engine):
    global node_stopped_on_purpose
    node_stopped_on_purpose = False
    if peer_pids():  # a second node would fail on the database lock
        raise EngineError(f"the node is already running (pids {peer_pids()}), use /restart_node")
    cfg = engine.call("readWorkspaceConfig").get("cfg") or {}
    if cfg.get("isValidator") and not cfg.get("mnemonicInserted"):  # the Launcher refuses to start too
        raise EngineError("the mnemonic is not inserted, insert it from the Launcher settings first")
    send("▶️ Starting the node...")
    engine.call("startWorkspace", timeout=300)


def install_update(engine):
    result, reported = {}, set()

    def on_progress(event):
        progress = float((event or {}).get("progress") or 0)
        percent = int(progress * 100 if progress <= 1 else progress) // 25 * 25
        if 0 < percent < 100 and percent not in reported:
            reported.add(percent)
            send(f"📦 Update {percent}%")

    stop_node(engine)
    send("📦 Installing the update...")
    try:
        engine.call("updateWorkspace", {"onProgress": on_progress,
                                        "onFinish": lambda *args: result.setdefault("finished", True),
                                        "onError": lambda *args: result.setdefault("error", args[0] if args else None)},
                    timeout=1800)
        engine.pump(lambda: result, timeout=1800)
    finally:
        start_node(engine)  # never leave the node stopped, even when the update failed
    if "error" in result:
        raise EngineError(f"the update failed: {result['error']}")
    send("✅ Update installed")


def report_node_started():
    send("⏳ Node started, waiting for its RPC to respond...")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            if "result" in bioauth_rpc(RPC_URL, timeout=5):
                send_status("✅ <b>Node is up</b>")
                return
        except Exception:
            pass
        time.sleep(5)
    send("⚠️ <b>The node RPC does not respond</b>\n\nThe node was started, but its RPC does not respond after "
         "5 minutes. Check it later with /status.")


def run_node_operation(operation):
    """Runs operation(engine) in the background, one at a time, and reports errors in Telegram."""
    def run():
        # a short wait covers a confirmation pressed while the previous engine is still closing
        if not node_lock.acquire(timeout=20):
            send("⏳ Another node operation is running, try again later.")
            return
        try:
            with Engine() as engine:
                started = operation(engine)
            if started:
                report_node_started()
        except Exception as e:
            log(f"Node operation failed: {e!r}")
            send(f"❌ <b>Node operation failed</b>\n\n{html.escape(str(e))}")
        finally:
            node_lock.release()

    threading.Thread(target=run, daemon=True).start()


CONFIRM_SECONDS = 10
confirmations = {}  # token: (operation, message_id, question) waiting for a Yes/No button


def ask_confirmation(question, operation):
    """Sends Yes/No buttons. The operation runs only if Yes is pressed within CONFIRM_SECONDS."""
    token = secrets.token_hex(4)
    buttons = [[{"text": "✅ Yes", "callback_data": f"yes:{token}"}, {"text": "❌ No", "callback_data": f"no:{token}"}]]
    log(f"Asking: {plain(question)}")
    try:
        message = send_html("sendMessage", {"chat_id": state["chat_id"], "reply_markup": {"inline_keyboard": buttons},
                                            "text": f"{question}\n\n⏱ <i>Confirm within {CONFIRM_SECONDS} seconds</i>"})
    except Exception as e:
        log(f"Failed to send message: {e}")
        return
    confirmations[token] = (operation, message["message_id"], question)
    timer = threading.Timer(CONFIRM_SECONDS, close_confirmation, args=(token, "⌛ <i>Expired, nothing was done</i>"))
    timer.daemon = True
    timer.start()


def close_confirmation(token, outcome):
    """Replaces the buttons with the outcome. Returns the operation if it was still waiting."""
    pending = confirmations.pop(token, None)  # the button and the timer race, only one gets it
    if pending is None:
        return None
    operation, message_id, question = pending
    try:
        send_html("editMessageText", {"chat_id": state["chat_id"], "message_id": message_id,
                                      "text": f"{question}\n\n{outcome}"})
    except Exception as e:
        log(f"Failed to edit message: {e}")
    return operation


MENU = [
    [("📊 Status", "/status"), ("🖥 Node status", "/node_status")],
    [("🔗 BioAuth link", "/get_bioauth_link")],
    [("🔄 Restart node", "/restart_node"), ("⏹ Stop node", "/stop_node"), ("▶️ Start node", "/start_node")],
    [("⬆️ Update node", "/update_node")],
    [("🔔 Daily report on", "/enable_notification"), ("🔕 Daily report off", "/disable_notification")],
]
MENU_COMMANDS = {label: command for row in MENU for label, command in row}
# labels without the emoji, as sent by menus pinned by an older version
MENU_COMMANDS.update({label.split(" ", 1)[1]: command for label, command in list(MENU_COMMANDS.items())})


def send_menu(text):
    """Pins the menu buttons under the message field. Pressing one sends its label, see MENU_COMMANDS."""
    keyboard = [[{"text": label} for label, _ in row] for row in MENU]
    log(f"Sending menu: {plain(text)}")
    try:
        send_html("sendMessage", {"chat_id": state["chat_id"], "text": text,
                                  "reply_markup": {"keyboard": keyboard, "is_persistent": True,
                                                   "resize_keyboard": True}})
    except Exception as e:
        log(f"Failed to send message: {e}")


def handle_button(callback):
    kind, _, value = (callback.get("data") or "").partition(":")
    chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
    answer = ""
    if chat_id != state.get("chat_id"):
        answer = "Not allowed"
    elif kind == "cmd":  # button of an older inline menu, same as typing the command
        handle_command(chat_id, value)
    else:  # yes/no confirmation
        operation = close_confirmation(value, "✅ <b>Confirmed</b>" if kind == "yes" else "❌ <b>Cancelled</b>")
        answer = ("Confirmed" if kind == "yes" else "Cancelled") if operation else "Expired"
        if operation and kind == "yes":
            run_node_operation(operation)
    telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": answer})


NODE_COMMANDS = {
    # command: (confirmation question, operation returning True when it started the node)
    "/stop_node": ("⏹ <b>Stop the node?</b>\n\nIt stops validating until you start it again.",
                   lambda engine: stop_node(engine)),
    "/start_node": ("▶️ <b>Start the node?</b>", lambda engine: start_node(engine) or True),
    "/restart_node": ("🔄 <b>Restart the node?</b>\n\nIt will be offline for a short time.",
                      lambda engine: stop_node(engine) or start_node(engine) or True),
}


def node_status(engine):
    running = node_is_running(engine)
    update = update_available(engine)
    send(f"🖥 <b>Node status</b>\n\n"
         f"Process: {'🟢 running' if running else '🔴 stopped'}\n"
         f"Update: {'⬆️ available, press ⬆️ Update node' if update else '✅ up to date'}")


def update_node(confirmed):
    def operation(engine):
        if not update_available(engine):
            send("✅ Your node is up to date")
            return False
        if not confirmed:
            ask_confirmation("⬆️ <b>Node update available</b>\n\nUpdating stops the node, installs the update and "
                             "starts the node again. Install now?", update_node(True))
            return False
        install_update(engine)
        return True
    return operation


def notify_if_update_available():
    """Daily check, only notifies: updates are installed with /update_node."""
    if not os.path.exists(ENGINE) or not node_lock.acquire(blocking=False):
        return
    try:
        with Engine() as engine:
            if update_available(engine):
                send("⬆️ <b>A new node version is available</b>\n\nPress ⬆️ Update node to install it.")
    except Exception as e:
        log(f"Update check failed: {e!r}")
    finally:
        node_lock.release()


def engine_probe():
    """Read-only check that the bot can talk to the Launcher engine on this server."""
    print(f"Engine: {ENGINE} (exists: {os.path.exists(ENGINE)})")
    print(f"Workspace: {WORKSPACE}")
    print(f"Launcher engine running: {launcher_engine_running()}")
    print(f"Own systemd scope: {os.geteuid() == 0 and bool(shutil.which('systemd-run'))}")
    with Engine() as engine:
        print("isWorkspaceInitialized:", engine.call("isWorkspaceInitialized"))
        cfg = engine.call("readWorkspaceConfig").get("cfg") or {}
        print("config keys:", sorted(cfg))
        print("isValidator:", cfg.get("isValidator"), " mnemonicInserted:", cfg.get("mnemonicInserted"))
        print("node running:", node_is_running(engine), " humanode-peer pids:", peer_pids())
        print("checkForBinariesUpdates:", engine.call("checkForBinariesUpdates"))
    print("engine still running after disconnect:", engine.process.poll() is None)


class Monitor:
    def __init__(self):
        self.status = None
        self.last_alert = 0.0
        self.warned = set()
        self.report_day = None

    def check(self):
        """Runs every minute: warns before BioAuth expires and alerts when it expired or the node is down."""
        status, detail = bioauth_status()
        now = datetime.now(tz())
        reached = set()

        if status == "active":
            left = (detail - now).total_seconds() / 60
            reached = {m for m in WARN_MINUTES if left <= m}
            if self.status not in (None, "active"):
                bioauth_information(detail, "✅ <b>Back to normal</b>")  # after an alert
            elif reached - self.warned:
                minutes = max(round(left), 0)
                send(f"⏰ <b>BioAuth expires in {minutes} minute{'' if minutes == 1 else 's'}</b>\n\n"
                     "Authenticate soon, press 🔗 BioAuth link to get the link.")

            if (now.hour == DAILY_REPORT_HOUR and self.report_day != now.date()
                    and state.get("notifications", True)):
                self.report_day = now.date()
                bioauth_information(detail, "☀️ <b>Daily report</b>")
                threading.Thread(target=notify_if_update_available, daemon=True).start()
        elif status == "down" and (node_stopped_on_purpose or node_lock.locked()):
            pass  # stopped from Telegram or a node operation is running
        elif status != self.status or time.monotonic() - self.last_alert >= ALERT_REPEAT_MINUTES * 60:
            send(detail)
            self.last_alert = time.monotonic()

        self.status = status
        self.warned = reached


def send_status(title="🟢 <b>BioAuth active</b>"):
    status, detail = bioauth_status()
    if status == "active":
        bioauth_information(detail, title)
    else:
        send(detail)


def send_auth_link():
    url = new_auth_url()
    send(CANT_GET_URL if url == CANT_GET_URL else
         f"🔗 <b>BioAuth link</b>\n\nOpen it on your phone to authenticate:\n{html.escape(url)}")


def handle_command(chat_id, text):
    owner = state.get("chat_id")
    text = MENU_COMMANDS.get(text.strip(), text)  # a menu button sends its label
    command, _, arg = text.strip().partition(" ")
    command = command.split("@")[0]  # /help@my_bot in group chats
    arg = arg.strip()

    if command == "/register":
        if not secrets.compare_digest(arg.encode(), REGISTER_CODE.encode()):
            send(WRONG_REGISTER_CODE, chat_id)
            return
        if owner is not None and owner != chat_id:
            send("ℹ️ <b>This bot was registered to another chat</b>\n\nYou will no longer receive alerts here.", owner)
        state["chat_id"] = chat_id
        save_state()
        log(f"Registered chat {chat_id}")
        send_menu(SUCCESSFULLY_REGISTERED_MESSAGE)
        return

    if chat_id != owner:
        if owner is None:
            send(NOT_REGISTERED, chat_id)
        return  # ignore everyone except the owner

    if command == "/timezone":
        try:
            parse_timezone(arg)
        except Exception:
            send("⚠️ <b>Invalid timezone</b>\n\nExamples: <code>/timezone Europe/Chisinau</code>, "
                 "<code>/timezone UTC+3</code>")
            return
        state["timezone"] = arg
        save_state()
        send(SUCCESSFULLY_REGISTERED_TIME_ZONE_ID.format(html.escape(arg)))
    elif command == "/enable_notification":
        state["notifications"] = True
        save_state()
        send(ENABLE_NOTIF)
    elif command == "/disable_notification":
        state["notifications"] = False
        save_state()
        send(DISABLED_NOTIF)
    elif command == "/get_bioauth_link":
        send("🔗 Generating a new BioAuth link...")
        threading.Thread(target=send_auth_link, daemon=True).start()
    elif command == "/status":
        threading.Thread(target=send_status, daemon=True).start()
    elif command in NODE_COMMANDS:
        question, operation = NODE_COMMANDS[command]
        if arg == "confirm":
            run_node_operation(operation)
        else:
            ask_confirmation(question, operation)
    elif command == "/update_node":
        run_node_operation(update_node(arg == "confirm"))
    elif command == "/node_status":
        run_node_operation(node_status)
    elif command in ("/help", "/start", "/menu"):  # Telegram sends /start when the bot is first opened
        send_menu(HELP)
    else:
        send(UNKNOWN_COMMAND)


def poll_commands():
    offset = None
    while True:
        params = {"timeout": 50}
        if offset is not None:
            params["offset"] = offset
        try:
            updates = telegram("getUpdates", params, timeout=60)
        except Exception as e:
            log(f"Telegram polling failed: {e}")
            time.sleep(10)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            if "callback_query" in update:  # a Yes/No button was pressed
                try:
                    handle_button(update["callback_query"])
                except Exception as e:
                    log(f"Button failed: {e}")
                continue
            message = update.get("message") or {}
            if "text" in message:
                try:
                    handle_command(message["chat"]["id"], message["text"])
                except Exception as e:
                    log(f"Command failed: {e}")


def check_rpc_url():
    global RPC_URL
    url = detect_rpc_url()
    if url is None:
        log(f"Humanode RPC does not respond at {RPC_URL}, is the node running?")
    elif url != RPC_URL:
        send(f"⚠️ <b>Node RPC address changed</b>\n\nNo answer at {html.escape(RPC_URL)}, using "
             f"{html.escape(url)} instead. Set <code>RPC_URL={html.escape(url)}</code> in "
             f"{html.escape(CONFIG_FILE)}.")
        RPC_URL = url
    else:
        log(f"Humanode RPC responds at {RPC_URL}")


def main():
    if state.get("chat_id") is None:
        log(MISSING_CHAT_ID)
    check_rpc_url()
    try:
        telegram("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]})
    except Exception as e:
        log(f"Failed to set the command menu: {e}")
    threading.Thread(target=poll_commands, daemon=True).start()

    monitor = Monitor()
    while True:
        try:
            monitor.check()
        except Exception as e:
            log(f"Check failed: {e}")
        time.sleep(60 - time.time() % 60)  # start of every minute


if __name__ == "__main__":
    if DETECT_RPC:
        print(detect_rpc_url() or "")
    elif ENGINE_PROBE:
        engine_probe()
    else:
        main()
