# jira-watcher

Python scripts built on [jira-cli](https://github.com/ankitpokhrel/jira-cli) (run `jira init` first).

- `watch_jira.py` – autonomous watcher; checks for new tickets every hour and alerts (log, desktop notification, optional webhook).
- `check_jira.py` – one-off listing of tickets (table or JSON).

```
./watch_jira.py -p PROJ          # run the watcher
./watch_jira.py -p PROJ --once   # single check (cron)
./check_jira.py -p PROJ -a       # list open tickets
```

State and logs: `~/.local/state/watch_jira/`. See the docstring in `watch_jira.py` for a systemd user service.

<!-- CHANGED 2026-11-25: added the "V2: hourly Queue Check + EOD" section below (new; nothing else edited). Validate with `./watch_jira_v2.py --print hourly` and `./watch_jira_v2.py --print eod --shift day`. -->

## V2: hourly Queue Check + EOD (`watch_jira_v2.py`)

Instead of alerting on each ticket, V2 posts one report every hour on the hour and an EOD at the end of each shift.

```
./watch_jira_v2.py                      # project DSO, all shifts, runs forever
./watch_jira_v2.py --shift day          # only my shift: hourly 8 AM-5 PM + EOD at 5 PM
./watch_jira_v2.py --print hourly       # preview the current Queue Check, then exit
./watch_jira_v2.py --print eod --shift mid   # preview the last mid-shift EOD
```

<!-- CHANGED 2026-11-25: added the V1-compatible flags note below. Validate with `./watch_jira_v2.py -p DSO -i 300 --once`. -->
V2 takes the same flags as V1, so you can swap the script name and keep the command: `./watch_jira_v2.py -p DSO -i 300` checks for new tickets every 5 minutes, lined up to the clock (8:00, 8:05, …), and alerts on each one as it appears (`[8:15 pm] Live: 1 new ticket(s)`). The Queue Check still comes on the hour and covers everything from that hour. Live alerts go to the terminal, the desktop and Teams/webhook, but not to `reports/`. With the default of 3600 there are no live checks, only the hourly Queue Check. <!-- CHANGED 2026-11-25: was "-i 300 sends a Queue Check every 5 minutes". -i now drives live alerts only. --> `-q` and `--once` work as in V1. V2 accepts `--mine`, `--initial-days` and `--report-existing` but ignores them, and logs a warning saying so.

| Report | When | Contents |
|---|---|---|
| `[4 pm] Queue Check` | every hour (inside the chosen shifts) | **New Tickets**: created in the past hour, not assigned to me. **Ongoing activity**: my open tickets. **Blockers**: my open tickets that are flagged or in Pending / Pending Approval (`--blocker-jql` to change). |
| `EOD, Nov 25, 2026 - Day shift (8AM-5PM)` <!-- CHANGED 2026-11-25: header now names the shift --> | 5 PM (day 8AM-5PM), 1 AM (mid 4PM-1AM), 8 AM (night 12AM-8AM) | Tickets that were mine during the shift and were closed (`- Closed`) or handed to someone else (`- Escalated`). Dated by the shift's start day. |

Reports go to the terminal, `~/.local/state/watch_jira/reports/<date>.txt`, a desktop notification, and Teams/webhook if set. If the laptop was asleep it produces the missed Queue Check on wake (covering every missed hour) and any EOD that ended in the last 6 hours. Log: `~/.local/state/watch_jira/watch_jira_v2.log`. Notes like "User activity" are not in Jira, so add them by hand.

<!-- CHANGED 2026-11-06: added the Teams and Troubleshooting sections below (new; nothing above was edited). Validate by reading them against watch_jira.py (--teams-webhook, ensure_access, alert). -->

## MS Teams alerts

Create a Teams **Workflows** webhook ("Post to a channel when a webhook request is received"), then:

```
export TEAMS_WEBHOOK_URL='https://prod-xx.logic.azure.com/...'   # or pass --teams-webhook URL
./watch_jira.py -p PROJ
```

New tickets are posted as an Adaptive Card (key linked to Jira, summary, priority, status, reporter). Treat the URL as a secret; with systemd, keep it in an `EnvironmentFile=`.

<!-- CHANGED 2026-11-16: added this "Re-initialize with a new token" section (new; the Troubleshooting section below is unchanged). Validate by following the steps and checking `jira project list` shows DSO. -->

## Re-initialize with a new token

Do this whenever the watcher goes BLIND ("No projects found" in `~/.local/state/watch_jira/watch_jira.log`) or after you regenerate the token.

1. **Create the token.** id.atlassian.com -> Security -> API tokens -> Create API token. Sign in as the same account as `login` in `~/.config/.jira/.config.yml` (a token only works with the email of the account that created it).
2. **Store it.** Edit the `export JIRA_API_TOKEN=...` line in `~/.bashrc` (currently line 136) and paste the new value. Then load it in your shell:
   ```
   source ~/.bashrc
   ```
   Programs that were already running (the watcher, VS Code, Claude Code) keep the old value, so restart them after this step.
3. **Re-initialize jira-cli.** Either run `jira init` and answer the prompts, or do it in one command:
   ```
   jira init --force --installation cloud --server https://<your-site>.atlassian.net \
       --login <you@company.com> --auth-type basic --project DSO
   ```
   `jira init` reads the token from `JIRA_API_TOKEN`; it does not store it in the config file.
   Copy your real `server` and `login` from `~/.config/.jira/.config.yml`. <!-- CHANGED 2026-11-25: server/login replaced with placeholders before publishing to a public repo -->
4. **Verify.** `jira me` should print your login and `jira project list` should list DSO. "No projects found" means the token is still wrong or was not loaded in this shell.
5. **Restart the watcher** from a shell that has the new token:
   ```
   pkill -f watch_jira.py; ./watch_jira.py -p DSO -i 300
   ```
   The log should say "Watching Jira" followed by "No new tickets", or a recovered alert if it had been BLIND. If you run it under systemd, put the token in the service's `EnvironmentFile=` and run `systemctl --user restart watch-jira`.

## Troubleshooting

**Watcher says "No new tickets" but you know there are some.** Usually jira-cli lost authentication: Jira then answers as an anonymous user, and searches return empty instead of an error. Symptoms: `jira project list` prints "No projects found", `jira issue view KEY` returns 404, and `curl -u "$LOGIN:$JIRA_API_TOKEN" https://<site>.atlassian.net/rest/api/3/myself` returns 401 (`x-seraph-loginreason: AUTHENTICATED_FAILED`).

Fix:
1. Create a new API token at id.atlassian.com -> Security -> API tokens, signed in as the same account as `login` in `~/.config/.jira/.config.yml`. A token only works with the email of the account that created it.
2. `export JIRA_API_TOKEN='<new token>'` (also in the shell profile or service environment the watcher is started from; restart VS Code/the service so it sees the new value).
3. `jira init`, then check `jira project list` shows your project.
4. Restart the watcher.

The watcher now guards against this: each cycle it checks that the project is visible (`ensure_access`). If not, it logs an ERROR and sends one "BLIND" alert (desktop, `--webhook`, Teams), then one "recovered" alert when access returns. This guard does not notice if the watcher process itself dies; use systemd `Restart=always` for that.
