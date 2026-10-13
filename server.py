#!/usr/bin/env python3
"""UQISC AGM live voting server.

Standard library only - run with:  python3 server.py
  Audience:   http://<host>:8000/
  Big screen: http://<host>:8000/screen
  Admin:      http://<host>:8000/admin

Environment variables (all optional):
  ADMIN_PASSWORD     password for /admin and /screen (random one printed if unset)
  PORT               default 8000
  CODE_COUNT         number of personal voting codes to generate (default 300)
  DATA_FILE          where state is saved (default ./data.json)
  PUBLIC_URL         address the big-screen QR code points to (default: this laptop's network address)
"""
import csv
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
# Web files sit next to this script. Only these types are ever served, so server.py,
# codes.json (the voting codes) and data.json can never be downloaded.
STATIC_TYPES = {".html", ".css", ".js", ".webp", ".png", ".svg", ".ico"}
DATA_FILE = Path(os.environ.get("DATA_FILE", ROOT / "data.json"))
CODES_FILE = ROOT / "codes.json"  # the printed voting codes - deploy this file with the app
PORT = int(os.environ.get("PORT", 8000))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_hex(3)
CODE_COUNT = int(os.environ.get("CODE_COUNT", 300))
CODE_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"  # no I/L/O - too easy to misread
CODE_DIGITS = "23456789"  # no 0/1

# Elected positions, in the same order as the nomination poster.
POSITIONS = [
    {"id": "president", "title": "President", "group": "T5", "seats": 1},
    {"id": "treasurer", "title": "Treasurer", "group": "T5", "seats": 1},
    {"id": "secretary", "title": "Secretary", "group": "T5", "seats": 1},
    {"id": "vp-creative", "title": "Vice President - Creative", "group": "T5", "seats": 1},
    {"id": "vp-operations", "title": "Vice President - Operations", "group": "T5", "seats": 1},
    {"id": "marketing", "title": "Marketing", "group": "Creative Team", "seats": 2},
    {"id": "design", "title": "Design", "group": "Creative Team", "seats": 2},
    {"id": "coordination", "title": "Coordination (Events)", "group": "Operations Team", "seats": 2},
    {"id": "community", "title": "Community (Sponsorship)", "group": "Operations Team", "seats": 2},
    {"id": "international", "title": "International Officer", "group": "International Officer", "seats": 1},
]
POSITIONS_BY_ID = {p["id"]: p for p in POSITIONS}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def new_id():
    return secrets.token_hex(5)


def clean(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def fresh_state():
    return {
        "event": {"title": "Annual General Meeting", "subtitle": "UQISC " + time.strftime("%Y")},
        "candidates": {p["id"]: [] for p in POSITIONS},
        "polls": [],
        "activePollId": None,
        "awards": [],
        "screen": {"mode": "waiting", "pollId": None, "awardId": None},
        "checkinOpen": True,
        "attendees": [],
        "codeUse": {},  # voting code -> attendee id
    }


def generate_codes(n):
    """n unique 4-character codes, each mixing letters and digits."""
    alphabet = CODE_LETTERS + CODE_DIGITS
    codes = set()
    while len(codes) < n:
        c = "".join(secrets.choice(alphabet) for _ in range(4))
        if any(x in CODE_LETTERS for x in c) and any(x in CODE_DIGITS for x in c):
            codes.add(c)
    return sorted(codes)


def load_codes():
    try:
        return json.loads(CODES_FILE.read_text())
    except FileNotFoundError:
        codes = generate_codes(CODE_COUNT)
        CODES_FILE.write_text(json.dumps(codes, indent=0))
        print(f"Generated {len(codes)} voting codes in {CODES_FILE.name}")
        return codes


def load_state():
    state = fresh_state()
    try:
        saved = json.loads(DATA_FILE.read_text())
        state.update(saved)
    except FileNotFoundError:
        pass
    except Exception as e:  # corrupt file - keep a copy, start fresh
        print(f"Could not read {DATA_FILE}: {e}. Starting fresh.")
        try:
            DATA_FILE.rename(DATA_FILE.with_suffix(".corrupt.json"))
        except OSError:
            pass
    for p in POSITIONS:
        state["candidates"].setdefault(p["id"], [])
    state.pop("codes", None)
    return state


state = load_state()
CODES = load_codes()
cond = threading.Condition()
version = 0
join_attempts = {}  # ip -> [window_start, count]


def save():
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, DATA_FILE)


def changed(persist=True):
    """Call while holding `cond` after any mutation."""
    global version
    version += 1
    if persist:
        save()
    cond.notify_all()


# ---------------------------------------------------------------- lookups

def find_poll(poll_id):
    return next((p for p in state["polls"] if p["id"] == poll_id), None)


def active_poll():
    p = find_poll(state["activePollId"])
    return p if p and p["status"] == "open" else None


def attendee_by_token(token):
    if not token:
        return None
    for a in state["attendees"]:
        if hmac.compare_digest(a["token"], token):
            return a
    return None


def tally(poll):
    counts = {o["id"]: 0 for o in poll["options"]}
    for choices in poll["votes"].values():
        for c in choices:
            if c in counts:
                counts[c] += 1
    real = sorted(poll["options"], key=lambda o: -counts[o["id"]])
    seats = poll["seats"]
    winners, tied = [], []
    if real:
        cutoff = max(1, counts[real[min(seats, len(real)) - 1]["id"]])
        above = [o["id"] for o in real if counts[o["id"]] > cutoff]
        at = [o["id"] for o in real if counts[o["id"]] == cutoff]
        if len(above) + len(at) <= seats:
            winners = above + at
        else:  # more people level on the cutoff than seats left
            winners, tied = above, at
    return {
        "ballots": len(poll["votes"]),
        "counts": counts,
        "winners": winners,
        "tied": tied,
        "tie": bool(tied),
    }


# ---------------------------------------------------------------- views

def public_poll(p):
    return {
        "id": p["id"],
        "kind": p["kind"],
        "title": p["title"],
        "subtitle": p["subtitle"],
        "seats": p["seats"],
        "maxChoices": p["maxChoices"],
        "status": p["status"],
        "options": [
            {"id": o["id"], "label": o["label"], "sub": o.get("sub", "")}
            for o in p["options"]
        ],
    }


def audience_view(token):
    a = attendee_by_token(token)
    p = active_poll()
    view = {
        "type": "audience",
        "event": state["event"],
        "checkinOpen": state["checkinOpen"],
        "attendee": None,
        "poll": None,
        "myVote": None,
    }
    if a:
        view["attendee"] = {"name": a["name"]}
        if p:
            view["poll"] = public_poll(p)
            view["myVote"] = p["votes"].get(token)
    return view


def lan_ip():
    """This machine's address on the local network (no traffic is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def join_url():
    """Where the big-screen QR code sends phones. Worked out on each request so a Wi-Fi change is picked up."""
    url = os.environ.get("PUBLIC_URL", "").strip()
    return (url.rstrip("/") + "/") if url else f"http://{lan_ip()}:{PORT}/"


def checkin_info():
    return {"checkinOpen": state["checkinOpen"], "attendeeCount": len(state["attendees"])}


def screen_view():
    s = state["screen"]
    mode, poll, award = s["mode"], None, None
    if mode in ("voting", "results"):
        p = find_poll(s["pollId"])
        if p:
            poll = public_poll(p)
            poll["ballots"] = len(p["votes"])
            if mode == "results":
                poll["tally"] = tally(p)
        else:
            mode = "waiting"
    elif mode == "award":
        award = next((a for a in state["awards"] if a["id"] == s["awardId"]), None)
        if not award:
            mode = "waiting"
    return {
        "type": "screen",
        "event": state["event"],
        "mode": mode,
        "poll": poll,
        "award": award,
        "joinUrl": join_url(),
        **checkin_info(),
    }


def admin_view():
    polls = []
    for p in state["polls"]:
        ap = public_poll(p)
        ap.update(
            positionId=p.get("positionId"),
            superseded=p.get("superseded", False),
            tally=tally(p),
            openedAt=p.get("openedAt"),
            closedAt=p.get("closedAt"),
        )
        polls.append(ap)
    return {
        "type": "admin",
        "event": state["event"],
        "positions": POSITIONS,
        "candidates": state["candidates"],
        "polls": polls,
        "activePollId": state["activePollId"] if active_poll() else None,
        "awards": state["awards"],
        "screen": state["screen"],
        "attendees": [{"id": a["id"], "name": a["name"], "code": a.get("code"), "joinedAt": a["joinedAt"]}
                      for a in state["attendees"]],
        "codesTotal": len(CODES),
        "codesUsed": len(state["codeUse"]),
        **checkin_info(),
    }


def view_for(role, token):
    if role == "admin":
        return admin_view()
    if role == "screen":
        return screen_view()
    return audience_view(token)


# ---------------------------------------------------------------- admin actions

def require(cond_, message, status=400):
    if not cond_:
        raise ApiError(status, message)


def close_open_polls():
    for p in state["polls"]:
        if p["status"] == "open":
            p["status"] = "closed"
            p["closedAt"] = time.time()
    state["activePollId"] = None


def get_candidates(body):
    pid = body.get("positionId")
    require(pid in POSITIONS_BY_ID, "Unknown position.")
    return pid, state["candidates"][pid]


def act_add_candidate(b):
    pid, cands = get_candidates(b)
    name = clean(b.get("name"), 60)
    require(name, "Candidate name is required.")
    require(len(cands) < 30, "Too many candidates.")
    cands.append({"id": new_id(), "name": name, "blurb": clean(b.get("blurb"), 140)})


def act_remove_candidate(b):
    pid, cands = get_candidates(b)
    state["candidates"][pid] = [c for c in cands if c["id"] != b.get("candidateId")]


def move_item(items, item_id, direction):
    i = next((i for i, x in enumerate(items) if x["id"] == item_id), None)
    require(i is not None, "Not found.")
    j = i + (1 if direction > 0 else -1)
    if 0 <= j < len(items):
        items[i], items[j] = items[j], items[i]


def act_move_candidate(b):
    _, cands = get_candidates(b)
    move_item(cands, b.get("candidateId"), int(b.get("dir", 1)))


def start_poll(poll):
    close_open_polls()
    poll.update(status="open", votes={}, openedAt=time.time(), closedAt=None)
    state["polls"].append(poll)
    state["activePollId"] = poll["id"]
    return {"pollId": poll["id"]}


def act_open_position_poll(b):
    pid, cands = get_candidates(b)
    require(cands, "Add at least one candidate first.")
    pos = POSITIONS_BY_ID[pid]
    for old in state["polls"]:
        if old.get("positionId") == pid:
            old["superseded"] = True
    options = [{"id": c["id"], "label": c["name"], "sub": c.get("blurb", "")} for c in cands]
    return start_poll({
        "id": new_id(),
        "kind": "position",
        "positionId": pid,
        "title": pos["title"],
        "subtitle": pos["group"] if pos["group"] != pos["title"] else "",
        "seats": pos["seats"],
        "maxChoices": pos["seats"],
        "options": options,
    })


def act_create_custom_poll(b):
    title = clean(b.get("title"), 140)
    require(title, "Question is required.")
    labels = [clean(x, 80) for x in (b.get("options") or [])]
    labels = [x for x in labels if x][:12]
    require(len(labels) >= 2, "Add at least two options.")
    max_choices = max(1, min(int(b.get("maxChoices") or 1), len(labels)))
    options = [{"id": new_id(), "label": x, "sub": ""} for x in labels]
    return start_poll({
        "id": new_id(),
        "kind": "custom",
        "positionId": None,
        "title": title,
        "subtitle": clean(b.get("subtitle"), 200),
        "seats": 1,
        "maxChoices": max_choices,
        "options": options,
    })


def get_poll(b):
    p = find_poll(b.get("pollId"))
    require(p, "Poll not found.", 404)
    return p


def act_close_poll(b):
    p = get_poll(b)
    if p["status"] == "open":
        p["status"] = "closed"
        p["closedAt"] = time.time()
    if state["activePollId"] == p["id"]:
        state["activePollId"] = None


def act_reopen_poll(b):
    p = get_poll(b)
    close_open_polls()
    p["status"] = "open"
    p["closedAt"] = None
    state["activePollId"] = p["id"]


def act_delete_poll(b):
    p = get_poll(b)
    state["polls"] = [x for x in state["polls"] if x["id"] != p["id"]]
    if state["activePollId"] == p["id"]:
        state["activePollId"] = None
    if state["screen"]["pollId"] == p["id"]:
        state["screen"].update(mode="waiting", pollId=None)


def act_set_screen(b):
    mode = b.get("mode")
    require(mode in ("waiting", "voting", "results", "award"), "Unknown screen mode.")
    s = state["screen"]
    if mode in ("voting", "results"):
        get_poll(b)
        s.update(mode=mode, pollId=b["pollId"], awardId=None)
    elif mode == "award":
        require(any(a["id"] == b.get("awardId") for a in state["awards"]), "Award not found.")
        s.update(mode=mode, awardId=b["awardId"], pollId=None)
    else:
        s.update(mode="waiting", pollId=None, awardId=None)


def act_add_award(b):
    title = clean(b.get("title"), 80)
    recipient = clean(b.get("recipient"), 80)
    require(title and recipient, "Award title and recipient are required.")
    state["awards"].append({"id": new_id(), "title": title, "recipient": recipient, "blurb": clean(b.get("blurb"), 240)})


def act_remove_award(b):
    state["awards"] = [a for a in state["awards"] if a["id"] != b.get("awardId")]
    if state["screen"]["awardId"] == b.get("awardId"):
        state["screen"].update(mode="waiting", awardId=None)


def act_move_award(b):
    move_item(state["awards"], b.get("awardId"), int(b.get("dir", 1)))


def act_set_checkin(b):
    state["checkinOpen"] = bool(b.get("open"))


def release_codes(attendee_ids):
    state["codeUse"] = {c: a for c, a in state["codeUse"].items() if a not in attendee_ids}


def act_remove_attendee(b):
    """Signs the person out and frees their code so they can check in again (e.g. new phone)."""
    aid = b.get("attendeeId")
    state["attendees"] = [a for a in state["attendees"] if a["id"] != aid]
    release_codes({aid})


def act_reset_attendance(b):
    release_codes({a["id"] for a in state["attendees"]})
    state["attendees"] = []


def act_regenerate_codes(b):
    require(b.get("confirm") == "NEW CODES", "Type NEW CODES to confirm.")
    global CODES
    CODES = generate_codes(CODE_COUNT)
    CODES_FILE.write_text(json.dumps(CODES, indent=0))
    state["attendees"] = []
    state["codeUse"] = {}


def act_set_event(b):
    state["event"] = {"title": clean(b.get("title"), 80) or "Annual General Meeting", "subtitle": clean(b.get("subtitle"), 80)}


def act_factory_reset(b):
    require(b.get("confirm") == "RESET", 'Type RESET to confirm.')
    state.clear()
    state.update(fresh_state())  # codes.json is untouched, so printed cards stay valid


ACTIONS = {
    "login": lambda b: None,
    "addCandidate": act_add_candidate,
    "removeCandidate": act_remove_candidate,
    "moveCandidate": act_move_candidate,
    "openPositionPoll": act_open_position_poll,
    "createCustomPoll": act_create_custom_poll,
    "closePoll": act_close_poll,
    "reopenPoll": act_reopen_poll,
    "deletePoll": act_delete_poll,
    "setScreen": act_set_screen,
    "addAward": act_add_award,
    "removeAward": act_remove_award,
    "moveAward": act_move_award,
    "setCheckin": act_set_checkin,
    "regenerateCodes": act_regenerate_codes,
    "removeAttendee": act_remove_attendee,
    "resetAttendance": act_reset_attendance,
    "setEvent": act_set_event,
    "factoryReset": act_factory_reset,
}


# ---------------------------------------------------------------- public actions

def client_ip(handler):
    fwd = handler.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or handler.client_address[0]


def do_join(body, ip):
    now = time.time()
    window = join_attempts.setdefault(ip, [now, 0])
    if now - window[0] > 60:
        window[:] = [now, 0]
    window[1] += 1
    require(window[1] <= 15, "Too many attempts. Wait a minute and try again.", 429)

    existing = attendee_by_token(body.get("token"))
    if existing:
        return {"token": existing["token"], "name": existing["name"]}
    require(state["checkinOpen"], "Check-in is closed. Please see the committee at the front.", 403)
    name = clean(body.get("name"), 50)
    require(len(name) >= 2, "Please enter your full name.")
    entered = re.sub(r"[^A-Z0-9]", "", str(body.get("code", "")).upper())
    card = next((c for c in CODES if hmac.compare_digest(c, entered)), None)
    require(card, "That code isn't valid. Check the card you were given.", 403)
    require(card not in state["codeUse"],
            "This code has already been used to check in. If it's your card, please see the committee at the front.", 409)
    attendee = {"id": new_id(), "token": secrets.token_urlsafe(24), "name": name, "code": card, "joinedAt": now}
    state["codeUse"][card] = attendee["id"]
    state["attendees"].append(attendee)
    return {"token": attendee["token"], "name": name}


def do_vote(body):
    token = body.get("token")
    require(attendee_by_token(token), "You're not checked in.", 401)
    p = active_poll()
    require(p and p["id"] == body.get("pollId"), "This vote has closed.", 409)
    require(token not in p["votes"], "You've already voted in this poll.", 409)
    choices = body.get("choices")
    require(isinstance(choices, list) and choices, "Pick at least one option.")
    choices = list(dict.fromkeys(str(c) for c in choices))
    ids = {o["id"] for o in p["options"]}
    require(all(c in ids for c in choices), "Invalid choice.")
    require(len(choices) <= p["maxChoices"], f"You can choose up to {p['maxChoices']}.")
    p["votes"][token] = choices


def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Poll", "Group", "Status", "Ballots", "Option", "Votes", "Result"])
    for p in state["polls"]:
        if p.get("superseded"):
            continue
        t = tally(p)
        for o in p["options"]:
            result = ""
            if o["id"] in t["winners"]:
                result = "Elected" if p["kind"] == "position" else "Most votes"
            elif o["id"] in t["tied"]:
                result = "TIE"
            w.writerow([p["title"], p["subtitle"], p["status"], t["ballots"], o["label"], t["counts"][o["id"]], result])
    w.writerow([])
    w.writerow(["Attendee", "Code", "Checked in"])
    for a in state["attendees"]:
        w.writerow([a["name"], a.get("code", ""), time.strftime("%Y-%m-%d %H:%M", time.localtime(a["joinedAt"]))])
    return out.getvalue()


# ---------------------------------------------------------------- http

PAGES = {"/": "index.html", "/screen": "screen.html", "/admin": "admin.html", "/codes": "codes.html", "/qr": "qr.html"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UQISC-AGM"

    def log_message(self, fmt, *args):
        pass

    def send_body(self, status, body, content_type, extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, obj):
        self.send_body(status, json.dumps(obj), "application/json")

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        require(length <= 64_000, "Request too large.", 413)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Bad JSON.")

    def is_admin(self, key):
        return bool(key) and hmac.compare_digest(str(key), ADMIN_PASSWORD)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/stream":
            role = q.get("role", "audience")
            if role in ("admin", "screen") and not self.is_admin(q.get("key")):
                return self.send_json(401, {"error": "Wrong password."})
            return self.stream(role, q.get("token"))
        if url.path == "/api/admin/codes":
            if not self.is_admin(q.get("key")):
                return self.send_json(401, {"error": "Wrong password."})
            with cond:
                body = {"event": state["event"], "codes": CODES}
            return self.send_json(200, body)
        if url.path == "/api/admin/export.csv":
            if not self.is_admin(q.get("key")):
                return self.send_json(401, {"error": "Wrong password."})
            with cond:
                body = export_csv()
            return self.send_body(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="uqisc-agm-results.csv"'})
        self.serve_static(url.path)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            body = self.read_json()
            with cond:
                if url.path == "/api/join":
                    result = do_join(body, client_ip(self))
                    changed()
                elif url.path == "/api/vote":
                    do_vote(body)
                    result = {"ok": True}
                    changed()
                elif url.path.startswith("/api/admin/"):
                    action = ACTIONS.get(url.path[len("/api/admin/"):])
                    require(action, "Unknown action.", 404)
                    require(self.is_admin(self.headers.get("X-Admin-Key")), "Wrong password.", 401)
                    result = action(body) or {"ok": True}
                    changed()
                else:
                    raise ApiError(404, "Not found.")
            self.send_json(200, result)
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except (ValueError, TypeError):
            self.send_json(400, {"error": "Invalid request."})

    def stream(self, role, token):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        last, seen = None, -1
        try:
            while True:
                with cond:
                    if version == seen:
                        cond.wait(timeout=15)
                    seen = version
                    payload = json.dumps(view_for(role, token))
                if payload != last:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    last = payload
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def serve_static(self, path):
        name = PAGES.get(path.rstrip("/") or "/", path.lstrip("/"))
        file = (ROOT / name).resolve()
        if file.parent != ROOT or file.suffix.lower() not in STATIC_TYPES or not file.is_file():
            return self.send_body(404, "Not found", "text/plain")
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.send_body(200, file.read_bytes(), ctype)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("=" * 60)
    print("  UQISC AGM is running")
    print(f"  Audience:   http://localhost:{PORT}/")
    print(f"  Big screen: http://localhost:{PORT}/screen")
    print(f"  Admin:      http://localhost:{PORT}/admin")
    print(f"  Admin password: {ADMIN_PASSWORD}")
    if not os.environ.get("ADMIN_PASSWORD"):
        print("  (random - set ADMIN_PASSWORD to choose your own)")
    print("=" * 60, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
