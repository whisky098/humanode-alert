#!/usr/bin/env python3
"""Watches the BioAuth status of a local Humanode node and sends Telegram alerts.

Settings come from environment variables (see config.env.example). To run it by hand:
    python3 humanode_alert.py /etc/humanode-alert/config.env

The chat id, timezone and notification switch are set from Telegram and kept in state.json.
"""

import json
import os
import re
import secrets
import socket
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


DETECT_RPC = sys.argv[1:] == ["--detect-rpc"]  # used by install.sh
CONFIG_FILE = sys.argv[1] if len(sys.argv) > 1 and not DETECT_RPC else "/etc/humanode-alert/config.env"
if len(sys.argv) > 1 and not DETECT_RPC:
    load_env_file(CONFIG_FILE)

TOKEN = setting("TELEGRAM_TOKEN", "" if DETECT_RPC else None)
# bot usernames are public, so /register needs this code to claim the bot
REGISTER_CODE = setting("REGISTER_CODE", "" if DETECT_RPC else None)
RPC_URL = setting("RPC_URL", "http://127.0.0.1:9933")
# tried at startup when RPC_URL does not respond; newer nodes serve HTTP RPC on 9944
RPC_CANDIDATES = ["http://127.0.0.1:9944", "http://127.0.0.1:9933"]
WORKSPACE = setting("HUMANODE_WORKSPACE", "/root/.humanode/workspaces/default")
AUTH_URL_CMD = [f"{WORKSPACE}/humanode-peer", "bioauth", "auth-url", "--rpc-url-ngrok-detect",
                "--chain", f"{WORKSPACE}/chainspec.json"]
TUNNEL_CMD = [f"{WORKSPACE}/humanode-websocket-tunnel"]
WARN_MINUTES = {int(m) for m in setting("WARN_MINUTES", "60,30,5,3,1").split(",") if m.strip()}
ALERT_REPEAT_MINUTES = int(setting("ALERT_REPEAT_MINUTES", "5"))
DAILY_REPORT_HOUR = 12
# systemd sets STATE_DIRECTORY (/var/lib/humanode-alert); otherwise use the current directory
STATE_FILE = os.path.join(os.environ.get("STATE_DIRECTORY", "."), "state.json")

SUCCESSFULLY_REGISTERED_MESSAGE = f"You have successfully registered, you will receive alerts every day (if have enabled notifications) with time left to authenticate and reminders {', '.join(map(str, sorted(WARN_MINUTES, reverse=True)))} minutes before expiry. To see commands for enable/disable notification type /help"
UNKNOWN_COMMAND = "Unknown command, to register for receive alert about your node send command '/register code', to set time zone id send command '/timezone time_zone_id'"
MISSING_CHAT_ID = "Chat id not found, need to register, to register type '/register code' in chat with bot, the code is REGISTER_CODE in config.env"
WRONG_REGISTER_CODE = "Wrong or missing code, send '/register code', the code is REGISTER_CODE in config.env"
SUCCESSFULLY_REGISTERED_TIME_ZONE_ID = "You have successfully set time zone id"
ENABLE_NOTIF = "Notifications have been enabled"
DISABLED_NOTIF = "Notifications have been disabled"
CANT_GET_URL = "Can't get url for authentication, check logs and HUMANODE_WORKSPACE in config.env"
HELP = """/register code -> register your node in this chat, the code is REGISTER_CODE in config.env
/timezone -> set timezone, example '/timezone Europe/Chisinau' or '/timezone UTC+3' (default UTC)
/enable_notification -> enable notification about node every day at noon
/disable_notification -> disable notification about node every day at noon
/get_bioauth_link -> get new auth link
/status -> time left until the next BioAuth
/help -> list of commands"""


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


def send(text, chat_id=None):
    chat_id = chat_id or state.get("chat_id")
    if chat_id is None:
        log(f"{MISSING_CHAT_ID}. Message: {text}")
        return
    log(f"Sending: {text}")
    try:
        telegram("sendMessage", {"chat_id": chat_id, "text": text})
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
        return "down", "Humanode is down"

    result = response.get("result")
    if isinstance(result, str):
        return "expired", "BioAuth has expired. Send /get_bioauth_link to get the authentication link"
    try:
        return "active", datetime.fromtimestamp(result["Active"]["expires_at"] / 1000, tz())
    except (KeyError, TypeError):
        log(f"Unexpected response: {response}")
        return "invalid", "Can't get expire time"


def bioauth_information(expires_at):
    seconds = max(int((expires_at - datetime.now(tz())).total_seconds()), 0)
    days, seconds = divmod(seconds, 24 * 3600)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    send(f"Next BioAuth on {expires_at:%Y-%m-%d %H:%M:%S}, remaining {days}d and {hours}h:{minutes}m:{seconds}s")


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
                bioauth_information(detail)  # back to normal after an alert
            elif reached - self.warned:
                minutes = max(round(left), 0)
                send(f"Your BioAuth will expire soon. You have {minutes} minute{'' if minutes == 1 else 's'} left")

            if (now.hour == DAILY_REPORT_HOUR and self.report_day != now.date()
                    and state.get("notifications", True)):
                self.report_day = now.date()
                bioauth_information(detail)
        elif status != self.status or time.monotonic() - self.last_alert >= ALERT_REPEAT_MINUTES * 60:
            send(detail)
            self.last_alert = time.monotonic()

        self.status = status
        self.warned = reached


def send_status():
    status, detail = bioauth_status()
    if status == "active":
        bioauth_information(detail)
    else:
        send(detail)


def handle_command(chat_id, text):
    owner = state.get("chat_id")
    command, _, arg = text.strip().partition(" ")
    command = command.split("@")[0]  # /help@my_bot in group chats
    arg = arg.strip()

    if command == "/register":
        if not secrets.compare_digest(arg.encode(), REGISTER_CODE.encode()):
            send(WRONG_REGISTER_CODE, chat_id)
            return
        if owner is not None and owner != chat_id:
            send("This bot has been registered to another chat, you will no longer receive alerts here.", owner)
        state["chat_id"] = chat_id
        save_state()
        log(f"Registered chat {chat_id}")
        send(SUCCESSFULLY_REGISTERED_MESSAGE)
        return

    if chat_id != owner:
        if owner is None:
            send(MISSING_CHAT_ID, chat_id)
        return  # ignore everyone except the owner

    if command == "/timezone":
        try:
            parse_timezone(arg)
        except Exception:
            send("Invalid timezone, example: '/timezone Europe/Chisinau'")
            return
        state["timezone"] = arg
        save_state()
        send(SUCCESSFULLY_REGISTERED_TIME_ZONE_ID)
    elif command == "/enable_notification":
        state["notifications"] = True
        save_state()
        send(ENABLE_NOTIF)
    elif command == "/disable_notification":
        state["notifications"] = False
        save_state()
        send(DISABLED_NOTIF)
    elif command == "/get_bioauth_link":
        threading.Thread(target=lambda: send(new_auth_url()), daemon=True).start()
    elif command == "/status":
        threading.Thread(target=send_status, daemon=True).start()
    elif command in ("/help", "/start"):  # Telegram sends /start when the bot is first opened
        send(HELP)
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
        send(f"Humanode RPC does not respond at {RPC_URL}, using {url} instead. "
             f"Set RPC_URL={url} in {CONFIG_FILE}")
        RPC_URL = url
    else:
        log(f"Humanode RPC responds at {RPC_URL}")


def main():
    if state.get("chat_id") is None:
        log(MISSING_CHAT_ID)
    check_rpc_url()
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
    else:
        main()
