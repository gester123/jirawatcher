#!/usr/bin/env python3
"""Autonomous Jira watcher: checks for new tickets every hour, forever.

Uses ankitpokhrel/jira-cli (same as check_jira.py). Needs no interaction:
  * remembers which tickets it has already reported (state file), so restarts
    and crashes never cause duplicate or missed alerts
  * the first run only records the existing tickets (use --report-existing to
    alert on them instead)
  * a failed check (network down, auth expired) is logged and retried on the
    next cycle; the process never exits because of it
  * MS Teams: --teams-webhook URL (or env TEAMS_WEBHOOK_URL) posts an Adaptive Card
  * new tickets go to the log, stdout and a desktop notification (if
    notify-send exists); optionally to a webhook (--webhook URL, Slack-style)

Examples:
    ./watch_jira.py -p PROJ                 # run in foreground
    nohup ./watch_jira.py -p PROJ &         # run in background
    ./watch_jira.py -p PROJ --once          # single check (for cron/systemd timer)

Run it at login with a systemd user service, e.g. ~/.config/systemd/user/watch-jira.service:
    [Service]
    ExecStart=/usr/bin/python3 /path/to/jira-watcher/watch_jira.py -p PROJ
    Restart=always
    [Install]
    WantedBy=default.target
then: systemctl --user enable --now watch-jira
"""
import argparse
import datetime
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
stop = threading.Event()


def run_jira(args):
    proc = subprocess.run(["jira", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip()
        if "No result found" in msg:  # jira-cli treats an empty search as an error
            return ""
        raise RuntimeError(f"jira failed: {msg}")
    return proc.stdout


def build_jql(opts, since):
    parts = [f'created >= "{since:%Y-%m-%d %H:%M}"']
    if opts.project:
        parts.append(f'project = "{opts.project}"')
    if opts.query:
        parts.append(f"({opts.query})")
    if opts.mine:
        parts.append("assignee = currentUser()")
    return " AND ".join(parts)


def fetch_new_candidates(opts, since):
    out = run_jira(["issue", "list", "--raw", "--paginate", f"0:{opts.limit}",
                    "--jql", build_jql(opts, since)]).strip()
    issues = json.loads(out) if out else []
    issues.sort(key=lambda i: i["fields"].get("created") or "")
    return issues


def load_state(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)  # atomic


def describe(issue):
    f = issue["fields"]
    return (f"{issue['key']} [{(f.get('priority') or {}).get('name', '-')}] "
            f"{f.get('summary', '')} "
            f"(by {(f.get('reporter') or {}).get('displayName', '?')}, "
            f"{(f.get('status') or {}).get('name', '?')})")


def issue_url(issue):
    """Browse URL derived from the REST 'self' link, e.g. https://x.atlassian.net/browse/KEY."""
    base = issue.get("self", "").split("/rest/api/")[0]
    return f"{base}/browse/{issue['key']}" if base else ""


def post_json(url, payload, name):
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:  # never let a notifier break the loop
        logging.warning("%s webhook failed: %s", name, e)


def teams_payload(issues):
    """Adaptive Card for a Teams Workflows ("Post to a channel when a webhook request is received")."""
    body = [{"type": "TextBlock", "weight": "Bolder", "size": "Medium", "wrap": True,
             "text": f"{len(issues)} new Jira ticket(s)"}]
    for i in issues:
        f = i["fields"]
        url = issue_url(i)
        key = f"[{i['key']}]({url})" if url else i["key"]
        body.append({"type": "TextBlock", "wrap": True, "spacing": "Medium",
                     "text": f"**{key}** {f.get('summary', '')}"})
        body.append({"type": "FactSet", "facts": [
            {"title": "Priority", "value": (f.get("priority") or {}).get("name", "-")},
            {"title": "Status", "value": (f.get("status") or {}).get("name", "?")},
            {"title": "Reporter", "value": (f.get("reporter") or {}).get("displayName", "?")},
        ]})
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4", "body": body}}]}


def notify(issues, webhook, teams_webhook=None):
    lines = [describe(i) for i in issues]
    body = "\n".join(lines)
    if shutil.which("notify-send"):
        title = f"{len(lines)} new Jira ticket(s)"
        subprocess.run(["notify-send", "-u", "normal", title, body], check=False)
    if webhook:
        post_json(webhook, {"text": "New Jira tickets:\n" + body}, "generic")
    if teams_webhook:
        post_json(teams_webhook, teams_payload(issues), "Teams")


# CHANGED 2026-11-06: NEW guard. jira-cli returns "No result found"/"No projects found" when auth fails,
# which used to look like "No new tickets". Validate by checking the project is visible first.
# Validate with: stop the token (e.g. JIRA_API_TOKEN=bad ./watch_jira.py -p DSO --once) -> expect ERROR "cannot see project".
def ensure_access(opts):
    """Raise if jira-cli cannot see the watched project (bad token / lost access)."""
    if not opts.project:
        return
    out = run_jira(["project", "list"])
    keys = {line.split()[0] for line in out.splitlines() if line.strip()}
    if opts.project not in keys:
        raise RuntimeError(f"cannot see project {opts.project} - jira-cli auth failed or access lost "
                           f"(token invalid/revoked?). Run `jira me` / `jira init`.")


# CHANGED 2026-11-06: NEW. Tell humans when the watcher goes blind / recovers (once per transition, not every cycle).
def alert(text, opts):
    logging.warning("ALERT %s", text)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-u", "critical", "Jira watcher", text], check=False)
    if opts.webhook:
        post_json(opts.webhook, {"text": text}, "generic")
    if opts.teams_webhook:
        post_json(opts.teams_webhook, {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard", "version": "1.4",
                        "body": [{"type": "TextBlock", "wrap": True, "text": text}]}}]}, "Teams")


def check(opts, state_path):
    ensure_access(opts)  # CHANGED 2026-11-06: was no check. Fails loudly instead of reporting "No new tickets" when blind.
    state = load_state(state_path)
    first_run = state is None
    now = datetime.datetime.now()
    if first_run:
        state = {"seen": [], "last_ok": None}
        since = now - datetime.timedelta(days=opts.initial_days)
    else:
        # overlap by a day so timezone skew / downtime can't hide a ticket; `seen` dedupes
        since = datetime.datetime.fromisoformat(state["last_ok"]) - datetime.timedelta(days=1)

    issues = fetch_new_candidates(opts, since)
    seen = set(state["seen"])
    new = [i for i in issues if i["key"] not in seen]

    if new and (not first_run or opts.report_existing):
        for i in new:
            logging.info("NEW %s", describe(i))
        notify(new, opts.webhook, opts.teams_webhook)
    elif first_run:
        logging.info("First run: recorded %d existing ticket(s), no alerts.", len(new))
    else:
        logging.info("No new tickets.")

    seen.update(i["key"] for i in new)
    save_state(state_path, {"seen": sorted(seen)[-5000:], "last_ok": now.isoformat()})


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--project", help="project key")
    p.add_argument("-q", "--query", help="extra JQL filter")
    p.add_argument("--mine", action="store_true", help="only tickets assigned to me")
    p.add_argument("-i", "--interval", type=int, default=3600, help="seconds between checks (default 3600)")
    p.add_argument("-n", "--limit", type=int, default=100, help="max tickets per check")
    p.add_argument("--initial-days", type=int, default=1, help="first run looks back this many days")
    p.add_argument("--report-existing", action="store_true", help="alert on tickets found on first run")
    p.add_argument("--webhook", help="URL to POST {'text': ...} to (Slack-compatible)")
    p.add_argument("--teams-webhook", default=os.environ.get("TEAMS_WEBHOOK_URL"),
                   help="MS Teams Workflows webhook URL (or set TEAMS_WEBHOOK_URL)")
    p.add_argument("--state", type=Path, help="state file (default: per-project file in ~/.local/state/watch_jira)")
    p.add_argument("--once", action="store_true", help="check once and exit")
    opts = p.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(STATE_DIR / "watch_jira.log")])
    if not shutil.which("jira"):
        sys.exit("jira-cli not found. Install it and run `jira init` first "
                 "(see https://github.com/ankitpokhrel/jira-cli).")

    state_path = opts.state or STATE_DIR / f"state_{opts.project or 'all'}.json"
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    logging.info("Watching Jira every %ds (project=%s)", opts.interval, opts.project or "all")
    blind = False  # CHANGED 2026-11-06: tracks failure streak for alert-once logic
    while not stop.is_set():
        try:
            check(opts, state_path)
            # CHANGED 2026-11-06: was just the try/except below. Alert once on recovery after a failure streak.
            if blind:
                alert("Jira watcher recovered: access restored, checks working again.", opts)
                blind = False
        except Exception as e:
            logging.error("Check failed, will retry next cycle: %s", e)
            # CHANGED 2026-11-06: alert once when failures start (not every cycle) so the watcher is never silently blind.
            if not blind:
                alert(f"Jira watcher is BLIND and will keep retrying: {e}", opts)
                blind = True
            if opts.once:
                sys.exit(1)
        if opts.once:
            break
        stop.wait(opts.interval)  # interruptible sleep
    logging.info("Stopped.")


if __name__ == "__main__":
    main()
