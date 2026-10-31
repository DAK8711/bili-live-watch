#!/usr/bin/env python3
"""
Zero-server Bilibili live notifier.

Runs on a GitHub Actions cron (free, public repo). Checks the room's live
status, and only pings you when the state *changes* off -> live (or live -> off),
so you get one notification per stream instead of one every 5 minutes.

Env:
  BILI_ROOM_ID   required, the number in live.bilibili.com/<number>
  BILI_NAME      optional display name (auto-detected when possible)

Any combination of notification channels:
  BARK_URL           https://api.day.app/<key>
  SCT_KEY            Server酱 SendKey (WeChat)
  TG_BOT_TOKEN + TG_CHAT_ID
  NTFY_TOPIC         topic on https://ntfy.sh
  WEBHOOK_URL        any endpoint that accepts a JSON POST {"text": "..."}

Usage:
  python watch.py          normal run
  python watch.py --test   send one test notification, don't touch state
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOM = (os.environ.get("BILI_ROOM_ID") or "").strip()
NAME = (os.environ.get("BILI_NAME") or "").strip()
API_BASE = os.environ.get("BILI_API_BASE", "https://api.live.bilibili.com").rstrip("/")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
NOTIFY_OFF = (os.environ.get("NOTIFY_OFF") or "false").lower() in ("1", "true", "yes")

BARK_URL = (os.environ.get("BARK_URL") or "").strip().rstrip("/")
SCT_KEY = (os.environ.get("SCT_KEY") or "").strip()
TG_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT = (os.environ.get("TG_CHAT_ID") or "").strip()
NTFY_TOPIC = (os.environ.get("NTFY_TOPIC") or "").strip()
NTFY_URL = (os.environ.get("NTFY_URL") or "https://ntfy.sh").strip().rstrip("/")
WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TEST = "--test" in sys.argv


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _request(url, data=None, headers=None, method=None, timeout=20):
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": UA, "Referer": "https://live.bilibili.com/", **(headers or {})},
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _get_json(url, timeout=20):
    _, raw = _request(url, timeout=timeout)
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Bilibili
# --------------------------------------------------------------------------- #
def fetch_room():
    """Return {room_id, live, title, name, url}. Raises on API/network trouble."""
    url = f"{API_BASE}/room/v1/Room/get_info?room_id={urllib.parse.quote(ROOM)}"
    payload = _get_json(url)
    if payload.get("code") != 0:
        raise RuntimeError(
            f"Bilibili API code={payload.get('code')} msg={payload.get('message')!r} "
            "(wrong room id, or the API is rate-limiting this IP)"
        )

    d = payload["data"]
    real_id = d.get("room_id") or ROOM

    name = NAME
    if not name:
        try:  # best-effort: the anchor's display name
            info = _get_json(
                f"{API_BASE}/live_user/v1/UserInfo/get_anchor_in_room?roomid={real_id}"
            )
            name = (info.get("data") or {}).get("info", {}).get("uname") or ""
        except Exception:
            name = ""

    return {
        "room_id": real_id,
        "live": d.get("live_status") == 1,
        "title": (d.get("title") or "").strip(),
        "name": name or "直播间",
        "url": f"https://live.bilibili.com/{real_id}",
    }


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def send_bark(text):
    if not BARK_URL:
        return None
    head, _, body = text.partition("\n")
    url = f"{BARK_URL}/{urllib.parse.quote(head, safe='')}/{urllib.parse.quote(body, safe='')}?group=bilibili&level=timeSensitive"
    return _request(url)[0]


def send_serverchan(text):
    if not SCT_KEY:
        return None
    head, _, body = text.partition("\n")
    data = urllib.parse.urlencode({"title": head, "desp": body}).encode()
    return _request(
        f"https://sctapi.ftqq.com/{SCT_KEY}.send",
        data,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )[0]


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return None
    data = urllib.parse.urlencode(
        {"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    return _request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )[0]


def send_ntfy(text):
    if not NTFY_TOPIC:
        return None
    head, _, body = text.partition("\n")
    # JSON publishing: headers can't safely carry UTF-8, a JSON body can.
    return _request(
        NTFY_URL,
        json.dumps(
            {
                "topic": NTFY_TOPIC,
                "title": head,
                "message": body,
                # 5 = max: Android delivers it on the alarm channel, which can
                # bypass Do Not Disturb (if you grant ntfy that permission).
                "priority": 5,
                "tags": ["rotating_light"],
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        {"Content-Type": "application/json"},
    )[0]


def send_webhook(text):
    if not WEBHOOK_URL:
        return None
    return _request(
        WEBHOOK_URL,
        json.dumps({"text": text}).encode(),
        {"Content-Type": "application/json"},
    )[0]


CHANNELS = [
    ("bark", send_bark),
    ("serverchan", send_serverchan),
    ("telegram", send_telegram),
    ("ntfy", send_ntfy),
    ("webhook", send_webhook),
]


def notify(text):
    print("---- notification ----")
    print(text)
    print("----------------------")
    sent = False
    for label, fn in CHANNELS:
        try:
            status = fn(text)
            if status is not None:
                sent = True
                print(f"  {label}: HTTP {status}")
        except Exception as exc:
            print(f"  {label}: FAILED {exc}")
    if not sent:
        print("  (no channel configured — set at least one secret)")
    return sent


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        print(f"state unreadable ({exc}); treating as unknown")
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------- #
def main():
    if TEST:
        notify("🔔 测试通知\nBilibili 开播提醒已经接通。")
        return 0

    if not ROOM:
        print("ERROR: BILI_ROOM_ID is not set")
        return 1

    room = fetch_room()
    previous = load_state()
    was_live = None if previous is None else bool(previous.get("live"))

    print(
        f"room={room['room_id']} name={room['name']!r} live={room['live']} "
        f"was_live={was_live} title={room['title']!r}"
    )

    if room["live"] and was_live is not True:
        notify(f"🔴 {room['name']} 开播了\n{room['title']}\n{room['url']}")
    elif not room["live"] and was_live is True and NOTIFY_OFF:
        notify(f"⏹ {room['name']} 下播了\n{room['title']}")
    else:
        print("no change — staying quiet")

    save_state(
        {
            "live": room["live"],
            "title": room["title"],
            "room_id": room["room_id"],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # A transient Bilibili hiccup shouldn't paint the Actions tab red.
        print(f"WARN: {exc}")
        sys.exit(0)
