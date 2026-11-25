#!/usr/bin/env python3
"""Check Jira tickets through ankitpokhrel/jira-cli.

Examples:
    ./check_jira.py                      # tickets assigned to me, not done
    ./check_jira.py -p PROJ              # in project PROJ
    ./check_jira.py -s "In Progress"     # by status
    ./check_jira.py -q 'priority = High AND resolution = Unresolved'
    ./check_jira.py --created 2026-11-02 # every ticket created on that day
    ./check_jira.py --created yesterday -p PROJ --mine
    ./check_jira.py --show PROJ-123      # details of one ticket
    ./check_jira.py --json               # machine-readable output
"""
import argparse
import datetime
import json
import shutil
import subprocess
import sys


def run_jira(args):
    if not shutil.which("jira"):
        sys.exit("jira-cli not found. Install it and run `jira init` first "
                 "(see https://github.com/ankitpokhrel/jira-cli).")
    proc = subprocess.run(["jira", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip()
        if "No result found" in msg:  # jira-cli treats an empty search as an error
            return ""
        sys.exit(f"jira failed: {msg}")
    return proc.stdout


def parse_day(text):
    """Accept YYYY-MM-DD, 'today' or 'yesterday'."""
    today = datetime.date.today()
    if text == "today":
        return today
    if text == "yesterday":
        return today - datetime.timedelta(days=1)
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        sys.exit(f"Invalid date '{text}'. Use YYYY-MM-DD, 'today' or 'yesterday'.")


def created_jql(opts):
    """JQL for all tickets created on one day, any status, any reporter."""
    day = parse_day(opts.created)
    nxt = day + datetime.timedelta(days=1)
    parts = [f'created >= "{day}"', f'created < "{nxt}"']
    if opts.query:
        parts.append(f"({opts.query})")
    if opts.project:
        parts.append(f'project = "{opts.project}"')
    if opts.status:
        parts.append(f'status = "{opts.status}"')
    elif not opts.include_done:
        parts.append("statusCategory != Done")
    if opts.mine:
        parts.append("assignee = currentUser()")
    return " AND ".join(parts)  # jira-cli appends its own ORDER BY


def list_issues(opts):
    cmd = ["issue", "list", "--raw", "--paginate", f"0:{opts.limit}"]
    if opts.created:
        cmd += ["--jql", created_jql(opts)]
    elif opts.query:
        cmd += ["--jql", opts.query]
    else:
        if not opts.all:
            cmd += ["-a", run_jira(["me"]).strip()]
        if opts.status:
            cmd += ["-s", opts.status]
        else:
            # any finished status (Done, Closed, ...), whatever the workflow calls it
            cmd += ["--jql", "statusCategory != Done"]
        if opts.project:
            cmd += ["-p", opts.project]
    out = run_jira(cmd).strip()
    issues = json.loads(out) if out else []
    if opts.created:  # oldest first
        issues.sort(key=lambda i: i["fields"].get("created") or "")
    return issues


def adf_text(node):
    """Flatten a Jira description (plain string or Atlassian doc format) to text."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        sep = " " if node.get("type") in ("paragraph", "heading", "listItem") else ""
        return sep.join(adf_text(c) for c in node.get("content", []))
    if isinstance(node, list):
        return " ".join(adf_text(c) for c in node)
    return ""


def short_summary(issue, width=100):
    f = issue["fields"]
    text = " ".join(adf_text(f.get("description")).split()) or f.get("summary", "")
    for end in (". ", "! ", "? "):  # first sentence only
        if end in text:
            text = text.split(end)[0] + end.strip()
            break
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def print_summaries(issues):
    print("\nSummary:")
    for i in issues:
        print(f"  {i['key']}: {short_summary(i)}")


def print_table(issues):
    if not issues:
        print("No tickets found.")
        return
    rows = []
    for i in issues:
        f = i["fields"]
        rows.append((
            i["key"],
            (f.get("status") or {}).get("name", ""),
            (f.get("priority") or {}).get("name", ""),
            (f.get("assignee") or {}).get("displayName", "Unassigned"),
            (f.get("created") or "")[:16].replace("T", " "),
            f.get("summary", ""),
        ))
    head = ("KEY", "STATUS", "PRIORITY", "ASSIGNEE", "CREATED", "SUMMARY")
    widths = [max(len(str(r[c])) for r in [head, *rows]) for c in range(5)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths) + "  {}"
    print(fmt.format(*head))
    for r in rows:
        print(fmt.format(*r))
    print(f"\n{len(rows)} ticket(s)")
    print_summaries(issues)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--project", help="project key")
    p.add_argument("-s", "--status", help="filter by status")
    p.add_argument("-q", "--query", help="raw JQL (overrides other filters)")
    p.add_argument("-a", "--all", action="store_true",
                   help="don't restrict to tickets assigned to me")
    p.add_argument("-c", "--created", metavar="DAY",
                   help="all tickets created on DAY (YYYY-MM-DD, today, yesterday); "
                        "skips Done tickets, includes every assignee unless narrowed")
    p.add_argument("--include-done", action="store_true",
                   help="with --created: also list tickets in a Done status")
    p.add_argument("--mine", action="store_true",
                   help="with --created: only tickets assigned to me")
    p.add_argument("-n", "--limit", type=int, default=50, help="max tickets (default 50)")
    p.add_argument("--show", metavar="KEY", help="show a single ticket")
    p.add_argument("--json", action="store_true", help="output raw JSON")
    opts = p.parse_args()

    if opts.show:
        print(run_jira(["issue", "view", opts.show, "--plain"]))
        return

    issues = list_issues(opts)
    if opts.json:
        print(json.dumps(issues, indent=2))
    else:
        print_table(issues)


if __name__ == "__main__":
    main()
