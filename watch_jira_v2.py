#!/usr/bin/env python3
# CHANGED 2026-11-25: NEW FILE (V2). watch_jira.py (V1) is unchanged. Validate with:
#   ./watch_jira_v2.py -p DSO --print hourly      # preview the current hour's Queue Check
#   ./watch_jira_v2.py -p DSO --print eod --shift day   # preview the last day-shift EOD
"""Jira watcher V2: hourly Queue Check + end-of-shift (EOD) report, fully autonomous.

Every hour on the hour (8 AM, 9 AM, ...) it outputs:

    [4 pm] Queue Check
    - New Tickets:
    DSO-2534 [Medium] [P2] [AWS] prod-db-replica-lag (unassigned)
    - Ongoing activity:
    DSO-2530 [Medium] [P1] [Azure] frontdoor-example-error-5xx-critical
    - Blockers:
    No blockers

  New Tickets       created during the past hour and NOT assigned to me (unassigned or someone else)
  Ongoing activity  open tickets assigned to me (not Done, not a blocker)
  Blockers          open tickets assigned to me that are flagged (Impediment) or in a Pending status

At the end of each shift (day 8AM-5PM -> 5 PM, mid 4PM-1AM -> 1 AM, night 12AM-8AM -> 8 AM):

    EOD, Nov 25, 2026 - Day shift (8AM-5PM)
    DSO-2531 [Medium] [P1] [Azure] api-app-example-http-5xx - Closed
    DSO-2524 [Medium] [P2] [AWS] prod-api-response-time-warning - Escalated

  lists every ticket that was assigned to me during the shift and was closed (Done category ->
  "Closed") or handed to someone else ("Escalated") during it. The EOD date is the shift's start date.

Each report goes to stdout, the log, reports/<date>.txt, a desktop notification, and
--webhook / --teams-webhook if set. If Jira auth breaks it alerts once ("BLIND") and retries
every minute; missed reports (e.g. laptop asleep) are produced on wake-up.

Examples:
    ./watch_jira_v2.py -p DSO                    # run forever, all shifts
    ./watch_jira_v2.py -p DSO --shift day        # only my shift: hourly 8AM-5PM + 5 PM EOD
    ./watch_jira_v2.py -p DSO --print hourly     # print a report now, don't touch state
"""
import argparse
import datetime as dt
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "watch_jira"
REPORT_DIR = STATE_DIR / "reports"
stop = threading.Event()

# name: (start hour, length in hours). Ends: day 17:00, mid 01:00 (next day), night 08:00.
SHIFTS = {"day": (8, 9), "mid": (16, 9), "night": (0, 8)}
BLOCKER_JQL = 'flagged = Impediment OR status in ("Pending", "Pending Approval")'
CATCH_UP = dt.timedelta(hours=6)  # still send a missed EOD if we wake up within this long after it


# ---------- Jira ----------

def run_jira(args):
    proc = subprocess.run(["jira", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip()
        if "No result found" in msg:  # jira-cli treats an empty search as an error
            return ""
        raise RuntimeError(f"jira failed: {msg}")
    return proc.stdout


def search(opts, jql):
    if opts.query:  # CHANGED 2026-11-25: was no -q. Same -q as V1: extra JQL applied to every query.
        jql = f"({opts.query}) AND ({jql})"
    if opts.project:
        jql = f'project = "{opts.project}" AND ({jql})'
    out = run_jira(["issue", "list", "--raw", "--paginate", f"0:{opts.limit}", "--jql", jql]).strip()
    issues = json.loads(out) if out else []
    issues.sort(key=lambda i: int(i["key"].split("-")[-1]), reverse=True)  # newest first, like the samples
    return issues


def ensure_access(opts):
    """jira-cli returns empty results (not an error) when auth fails; detect that explicitly."""
    if not opts.project:
        return
    out = run_jira(["project", "list"])
    if opts.project not in {line.split()[0] for line in out.splitlines() if line.strip()}:
        raise RuntimeError(f"cannot see project {opts.project} - jira-cli auth failed or access lost "
                           f"(token invalid/revoked?). See README: Re-initialize with a new token.")


def jql_time(t):
    return f'"{t:%Y-%m-%d %H:%M}"'  # Jira reads this in the user's profile timezone (Asia/Manila)


def line(issue, suffix=""):
    f = issue["fields"]
    prio = (f.get("priority") or {}).get("name", "-")
    return f"{issue['key']} [{prio}] {f.get('summary', '')}{suffix}"


# ---------- reports ----------

def hour_label(t):
    # CHANGED 2026-11-25: was hour only. Adds minutes when -i makes reports land off the hour.
    hm = t.strftime('%I').lstrip('0') + (t.strftime(':%M') if t.minute else "")
    return f"[{hm} {t.strftime('%p').lower()}]"  # [4 pm], [4:05 pm]


# CHANGED 2026-11-25: NEW. Report slots follow -i, aligned to midnight (3600 -> 8:00, 9:00; 300 -> 8:00, 8:05).
def slot_of(t, interval):
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + dt.timedelta(seconds=int((t - midnight).total_seconds()) // interval * interval)


def who(i):  # CHANGED 2026-11-25: moved out of hourly_report so live alerts can use it too
    name = (i["fields"].get("assignee") or {}).get("displayName")
    return f" ({name or 'unassigned'})"


def hourly_report(opts, slot, since):
    new = search(opts, f"created >= {jql_time(since)} AND created < {jql_time(slot)} "
                       f"AND (assignee is EMPTY OR assignee != currentUser())")
    mine_open = "assignee = currentUser() AND statusCategory != Done"
    blockers = search(opts, f"{mine_open} AND ({opts.blocker_jql})")
    ongoing = search(opts, f"{mine_open} AND NOT ({opts.blocker_jql})")

    out = [f"{hour_label(slot)} Queue Check", "- New Tickets:"]
    out += [line(i, who(i)) for i in new] or ["no new tickets"]
    out += ["- Ongoing activity:"]
    out += [line(i) for i in ongoing] or ["no ongoing activity"]
    out += ["- Blockers:"]
    out += [line(i) for i in blockers] or ["No blockers"]
    return out


def eod_report(opts, name, start, end):  # CHANGED 2026-11-25: was (opts, start, end); name = shift
    during = f"DURING ({jql_time(start)}, {jql_time(end)})"
    was_mine = f"assignee WAS currentUser() {during}"
    closed = search(opts, f"{was_mine} AND assignee = currentUser() "
                          f"AND statusCategory = Done AND status CHANGED {during}")
    # it was mine during the shift and was handed to someone else (or unassigned) during it
    escalated = search(opts, f"{was_mine} AND (assignee is EMPTY OR assignee != currentUser()) "
                             f"AND assignee CHANGED {during}")
    rows = [(i, " - Closed") for i in closed] + [(i, " - Escalated") for i in escalated]
    rows.sort(key=lambda r: int(r[0]["key"].split("-")[-1]), reverse=True)
    # CHANGED 2026-11-25: was "EOD, Nov 25, 2026". Now also names the shift, e.g. "EOD, Nov 25, 2026 - Day shift (8AM-5PM)"
    hr = lambda t: t.strftime("%I").lstrip("0") + t.strftime("%p")
    return [f"EOD, {start:%b} {start.day}, {start.year} - {name.capitalize()} shift ({hr(start)}-{hr(end)})"] + \
        ([line(i, sfx) for i, sfx in rows] or ["No closed tickets"])


def shift_bounds(name, end_on_or_before):
    """Most recent (start, end) of shift `name` whose end is <= end_on_or_before."""
    h, length = SHIFTS[name]
    start = end_on_or_before.replace(hour=h, minute=0, second=0, microsecond=0)
    while start + dt.timedelta(hours=length) > end_on_or_before:
        start -= dt.timedelta(days=1)
    return start, start + dt.timedelta(hours=length)


def in_shift(t, names):
    """True if t falls inside any of the shifts (end hour included, so 5 PM counts for day)."""
    for n in names:
        h, length = SHIFTS[n]
        for back in (0, 1):  # a shift that started today or yesterday (mid runs past midnight)
            s = (t - dt.timedelta(days=back)).replace(hour=h, minute=0, second=0, microsecond=0)
            if s <= t <= s + dt.timedelta(hours=length):
                return True
    return False


# ---------- output ----------

def post_json(url, payload, name):
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:  # never let a notifier break the loop
        logging.warning("%s webhook failed: %s", name, e)


def teams_card(lines):
    body = [{"type": "TextBlock", "wrap": True, "spacing": "None",
             "weight": "Bolder" if n == 0 or l.startswith("- ") else "Default", "text": l}
            for n, l in enumerate(lines)]
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4", "body": body}}]}


def publish(lines, opts, when, urgent=False, save=True):  # CHANGED 2026-11-25: added save (live alerts skip reports/)
    text = "\n".join(lines)
    print(text + "\n", flush=True)
    logging.info("REPORT\n%s", text)
    if save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        with open(REPORT_DIR / f"{when:%Y-%m-%d}.txt", "a") as fh:
            fh.write(text + "\n\n")
    if shutil.which("notify-send"):
        # CHANGED 2026-11-25: added "--". Was printing "Unknown option - New Tickets: ..." because the
        # body starts with "-" and notify-send read it as an option. Validate: no "Unknown option" in the terminal.
        subprocess.run(["notify-send", "-u", "critical" if urgent else "normal", "--",
                        lines[0], "\n".join(lines[1:])], check=False)
    if opts.webhook:
        post_json(opts.webhook, {"text": text}, "generic")
    if opts.teams_webhook:
        post_json(opts.teams_webhook, teams_card(lines), "Teams")


# ---------- state / loop ----------

def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)  # atomic


def tick(opts, state_path):
    """Produce whatever report is due. Raises on Jira failure (state not advanced -> retried)."""
    now = dt.datetime.now()
    state = load_state(state_path)
    # CHANGED 2026-11-25: NEW live check, every -i seconds (like V1). Alerts each new ticket as it appears;
    # the hourly Queue Check below still consolidates the whole hour. Skipped when -i is 3600 or more.
    # Validate: ./watch_jira_v2.py -p DSO -i 300 -> "[8:15 pm] Live: ..." lines between the hourly reports.
    if opts.interval < 3600:
        live = slot_of(now, opts.interval)
        last_live = state.get("last_live")
        last_live = dt.datetime.fromisoformat(last_live) if last_live else live - dt.timedelta(seconds=opts.interval)
        if live > last_live:
            ensure_access(opts)
            # 5 min overlap so a ticket indexed late is not missed; live_seen stops duplicates
            since = max(last_live, live - dt.timedelta(hours=24)) - dt.timedelta(minutes=5)
            seen = set(state.get("live_seen", []))
            new = [i for i in search(opts, f"created >= {jql_time(since)}") if i["key"] not in seen]
            if new:
                publish([f"{hour_label(live)} Live: {len(new)} new ticket(s)"] + [line(i, who(i)) for i in new],
                        opts, live, save=False)
            else:
                print(f"{hour_label(live)} Live: no new tickets", flush=True)
                logging.info("Live: no new tickets")
            seen.update(i["key"] for i in new)
            state["live_seen"] = sorted(seen, key=lambda k: int(k.split("-")[-1]))[-500:]
            state["last_live"] = live.isoformat()
            save_state(state_path, state)

    # CHANGED 2026-11-25: was slot_of(now, opts.interval). The Queue Check is always on the hour again.
    slot = slot_of(now, 3600)

    last = state.get("last_hourly")
    last = dt.datetime.fromisoformat(last) if last else slot - dt.timedelta(hours=1)
    if slot > last:
        if in_shift(slot, opts.shift):
            ensure_access(opts)
            since = max(last, slot - dt.timedelta(hours=24))  # covers missed hours, capped at a day
            publish(hourly_report(opts, slot, since), opts, slot)
        state["last_hourly"] = slot.isoformat()
        save_state(state_path, state)

    for name in opts.shift:
        start, end = shift_bounds(name, now)
        done = state.setdefault("last_eod", {}).get(name)
        if (not done or dt.datetime.fromisoformat(done) < end) and now - end <= CATCH_UP:
            ensure_access(opts)
            publish(eod_report(opts, name, start, end), opts, start)
            state["last_eod"][name] = end.isoformat()
            save_state(state_path, state)


def alert(text, opts):
    logging.warning("ALERT %s", text)
    publish([text], opts, dt.datetime.now(), urgent=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--project", default="DSO", help="project key (default DSO)")
    p.add_argument("--shift", nargs="+", choices=list(SHIFTS), default=list(SHIFTS),
                   help="shift(s) to report: hourly checks only inside them, EOD at their end "
                        "(default: all three)")
    p.add_argument("--blocker-jql", default=BLOCKER_JQL, help=f"what counts as a blocker (default: {BLOCKER_JQL})")
    p.add_argument("-n", "--limit", type=int, default=100, help="max tickets per query (jira-cli max 100)")
    p.add_argument("--webhook", help="URL to POST {'text': ...} to (Slack-compatible)")
    p.add_argument("--teams-webhook", default=os.environ.get("TEAMS_WEBHOOK_URL"),
                   help="MS Teams Workflows webhook URL (or set TEAMS_WEBHOOK_URL)")
    p.add_argument("--state", type=Path, help="state file (default ~/.local/state/watch_jira/v2_<project>.json)")
    p.add_argument("--print", choices=["hourly", "eod"], dest="preview",
                   help="print one report for now (EOD = last finished shift of --shift) and exit; state untouched")
    # CHANGED 2026-11-25: NEW. Same flags as V1 so the same command line works with either script.
    p.add_argument("-i", "--interval", type=int, default=3600,
                   help="seconds between live new-ticket checks, aligned to the clock, like V1 (e.g. 300). "
                        "The Queue Check stays on the hour. Default 3600 = no live checks; min 60")
    p.add_argument("-q", "--query", help="extra JQL filter applied to every query (same as V1)")
    p.add_argument("--once", action="store_true", help="do whatever report is due now and exit (same as V1)")
    p.add_argument("--mine", action="store_true", help="V1 flag; accepted, no effect (V2 already splits mine/not mine)")
    p.add_argument("--initial-days", type=int, help="V1 flag; accepted, no effect in V2")
    p.add_argument("--report-existing", action="store_true", help="V1 flag; accepted, no effect in V2")
    opts = p.parse_args()
    if opts.interval < 60:
        p.error("-i/--interval must be at least 60 seconds")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(STATE_DIR / "watch_jira_v2.log")])
    if not shutil.which("jira"):
        sys.exit("jira-cli not found. Install it and run `jira init` first.")

    if opts.preview:
        ensure_access(opts)
        now = dt.datetime.now()
        if opts.preview == "hourly":
            slot = slot_of(now, 3600)  # CHANGED 2026-11-25: Queue Check is always hourly; -i only drives live alerts
            print("\n".join(hourly_report(opts, slot, slot - dt.timedelta(hours=1))))
        else:
            # CHANGED 2026-11-25: keeps the shift name so the header can show it
            name, (start, end) = max(((n, shift_bounds(n, now)) for n in opts.shift), key=lambda b: b[1][1])
            print("\n".join(eod_report(opts, name, start, end)))
        return

    state_path = opts.state or STATE_DIR / f"v2_{opts.project or 'all'}.json"
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    logging.info("V2 started (project=%s, shifts=%s)", opts.project, ",".join(opts.shift))
    for flag in ("mine", "initial_days", "report_existing"):  # CHANGED 2026-11-25: say so instead of silently ignoring
        if getattr(opts, flag):
            logging.warning("--%s is a V1 flag and has no effect in V2", flag.replace("_", "-"))
    # CHANGED 2026-11-25: was "Queue Check every N min". -i now only drives the live checks.
    live = f"live check every {opts.interval // 60} min, " if opts.interval < 3600 else ""
    if not opts.once:
        print(f"Jira watcher V2 running (project={opts.project}, shifts={','.join(opts.shift)}). "
              f"{live}Queue Check on the hour; log: {STATE_DIR / 'watch_jira_v2.log'}", flush=True)

    blind = False
    while not stop.is_set():
        try:
            tick(opts, state_path)
            if blind:
                alert("Jira watcher V2 recovered: access restored, reports working again.", opts)
                blind = False
        except Exception as e:
            logging.error("Report failed, retrying in 60s: %s", e)
            if not blind:
                alert(f"Jira watcher V2 is BLIND and will keep retrying: {e}", opts)
                blind = True
            if opts.once:  # CHANGED 2026-11-25: NEW, same as V1 --once
                sys.exit(1)
        if opts.once:  # CHANGED 2026-11-25: NEW, same as V1 --once
            break
        # short waits so a laptop suspend or clock change can't make us miss the hour by much
        stop.wait(60 - dt.datetime.now().second + 5 if not blind else 60)
    logging.info("Stopped.")


if __name__ == "__main__":
    main()
