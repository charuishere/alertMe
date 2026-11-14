"""Emails me when a new, unclaimed issue shows up in the projects I follow.
It only alerts. It never posts or comments anything.

With LOOP_MINUTES set it keeps checking (every LOOP_SLEEP seconds) until that time is up,
then tells the workflow to start a new run, so a single run covers hours, not minutes."""
import json, os, smtplib, ssl, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

# ---- what to watch (edit these) ----
# query: GitHub search scope. gfi_only: only issues labelled "good first issue".
# max_comments: skip issues that already have more comments than this.
GITHUB = [
    {"query": "org:fossasia", "gfi_only": False, "max_comments": 2, "priority": True},
    # Kestra's bot puts "not ready" in the body of issues the team has not opened up yet, and removes it when it
    # moves the issue to Backlog. That is an edit, so for Kestra we search by "updated", not "created".
    {"query": "org:kestra-io", "gfi_only": False, "max_comments": 2,
     "skip_body_marker": "kestra-bot:not-ready", "by": "updated"},
    {"query": "org:accordproject", "gfi_only": False, "max_comments": 2},
    {"query": "org:cBioPortal", "gfi_only": False, "max_comments": 2},
    {"query": "org:oppia", "gfi_only": True, "max_comments": 2},
]
JIRA = [
    # (name, base url, search endpoint, JQL without the time part, max comments or None)
    # Fineract people claim a ticket by commenting, not by being assigned, so skip tickets with comments.
    ("Fineract", "https://issues.apache.org/jira", "search",
     'project = FINERACT AND resolution = Unresolved AND assignee is EMPTY '
     'AND labels in (beginner, beginner-friendly)', 0),
    ("OpenMRS", "https://openmrs.atlassian.net", "search/jql",
     'project = TRUNK AND resolution = Unresolved AND assignee is EMPTY '
     'AND status in ("Ready for Work", "Waiting for Dev")', None),
]
# Jira source name -> GitHub repo to search for a pull request that already mentions the ticket key.
# People often open a PR without commenting on the ticket, so the comment filter alone is not enough.
JIRA_PR_REPO = {"Fineract": "apache/fineract"}
SKIP_TITLES = ("CI Failure", "Dependency Dashboard")
MAX_IN_EMAIL = 20
# ------------------------------------

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0"))
LOOP_SLEEP = int(os.environ.get("LOOP_SLEEP", "60"))
DEFAULT_WINDOW_MIN = 20      # first run, with no saved state
OVERLAP_MIN = 3              # re-check a little before the last run, in case search lags
MAX_BACK_MIN = 360           # never look back further than 6 hours
HEARTBEAT_DAYS = 7           # "still running" mail if nothing was sent for this long
FAILED = []


def fetch(url, headers=None):
    h = {"User-Agent": "issue-alerts", "Accept": "application/json"}
    h.update(headers or {})
    err = None
    for _ in range(3):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60))
        except Exception as e:
            err = e
            time.sleep(3)
    FAILED.append(url.split("?")[0])
    print("request failed:", url[:90], err, file=sys.stderr)
    return None


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    return h


def still_free(item):
    """Re-check right before alerting: still open and nobody assigned."""
    d = fetch(item["api_url"], gh_headers())
    return bool(d) and d.get("state") == "open" and not d.get("assignees")


def has_pr(item):
    """True if an open or merged PR already mentions this Jira ticket, None if the lookup failed."""
    repo = JIRA_PR_REPO.get(item["name"])
    if not repo or not item["id"].startswith("jira:"):
        return False
    key = item["id"].split(":")[-1]
    time.sleep(2)                                    # stay under the search rate limit
    d = fetch("https://api.github.com/search/issues?" +
              urllib.parse.urlencode({"q": f"repo:{repo} type:pr {key} in:title,body", "per_page": 30}), gh_headers())
    if d is None:
        return None
    return any(i.get("state") == "open" or (i.get("pull_request") or {}).get("merged_at")
               for i in d.get("items", []))


def github_items(since):
    out = []
    for s in GITHUB:
        by = s.get("by", "created")
        q = (f'{s["query"]} is:issue is:open no:assignee -linked:pr '
             f'comments:0..{s["max_comments"]} {by}:>={since:%Y-%m-%dT%H:%M:%SZ}')
        if s.get("gfi_only"):
            q += ' label:"good first issue"'
        d = fetch("https://api.github.com/search/issues?" +
                  urllib.parse.urlencode({"q": q, "per_page": 100, "sort": by}), gh_headers())
        for i in (d or {}).get("items", []):
            if i.get("user", {}).get("type") == "Bot" or any(t in i["title"] for t in SKIP_TITLES):
                continue
            if s.get("skip_body_marker") and s["skip_body_marker"] in (i.get("body") or ""):
                continue                   # the team has not opened this issue for contributors yet
            out.append({"id": f"gh:{i['id']}", "name": i["repository_url"].split("repos/")[-1],
                        "title": i["title"], "url": i["html_url"], "api_url": i["url"],
                        "labels": [l["name"] for l in i["labels"]], "priority": s.get("priority", False)})
        time.sleep(2)
    return out


def jira_items(since, now):
    out = []
    minutes = int((now - since).total_seconds() // 60) + 1
    for name, base, endpoint, jql, max_comments in JIRA:
        j = f"{jql} AND created >= -{minutes}m"   # relative time avoids timezone mistakes
        d = fetch(f"{base}/rest/api/2/{endpoint}?" +
                  urllib.parse.urlencode({"jql": j, "maxResults": 50, "fields": "summary,comment"}))
        for i in (d or {}).get("issues", []):
            if max_comments is not None and (i["fields"].get("comment") or {}).get("total", 0) > max_comments:
                continue
            out.append({"id": f"jira:{name}:{i['key']}", "name": name,
                        "title": f"{i['key']}: {i['fields']['summary']}",
                        "url": f"{base}/browse/{i['key']}", "api_url": None, "labels": [], "priority": False})
    return out


def mail(subject, body):
    if DRY_RUN:
        print("SUBJECT:", subject, "\n" + body)
        return
    user = os.environ["MAIL_USER"].strip()
    password = os.environ["MAIL_APP_PASSWORD"].replace(" ", "").strip()
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, os.environ["MAIL_TO"].strip()
    msg.set_content(body)
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
        return
    except Exception as e:
        print("port 465 failed:", type(e).__name__, e, file=sys.stderr)
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:   # second try
        s.starttls(context=ctx)
        s.login(user, password)
        s.send_message(msg)


def send(items):
    items = sorted(items, key=lambda x: not x["priority"])
    lines = []
    for x in items[:MAX_IN_EMAIL]:
        tag = "[FOSSASIA, move fast] " if x["priority"] else ""
        lab = f" ({', '.join(x['labels'][:3])})" if x["labels"] else ""
        lines.append(f"{tag}[{x['name']}] {x['title']}{lab}\n{x['url']}")
    if len(items) > MAX_IN_EMAIL:
        lines.append(f"... and {len(items) - MAX_IN_EMAIL} more. Check the repos.")
    body = "New unclaimed issues (checked just now, nobody assigned):\n\n" + "\n\n".join(lines)
    subject = f"{'FOSSASIA: ' if items[0]['priority'] else ''}{len(items)} new open-source issue(s)"
    mail(subject, body)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def window_start(state, now):
    if os.environ.get("LOOKBACK_MIN"):            # manual override, used for tests
        return now - timedelta(minutes=int(os.environ["LOOKBACK_MIN"]))
    if state.get("last_run"):
        start = datetime.fromisoformat(state["last_run"]) - timedelta(minutes=OVERLAP_MIN)
    else:
        start = now - timedelta(minutes=DEFAULT_WINDOW_MIN)
    return max(start, now - timedelta(minutes=MAX_BACK_MIN))


def run_once(state):
    """One check. Changes `state` in place and saves it."""
    FAILED.clear()
    now = datetime.now(timezone.utc)
    seen = set(state.get("seen", []))
    since = window_start(state, now)
    items = [x for x in github_items(since) + jira_items(since, now) if x["id"] not in seen]
    items = [x for x in items if not x["api_url"] or still_free(x)]
    items = [x for x in items if has_pr(x) is False]      # skip tickets with a PR; retry later if the lookup failed
    print(f"{now:%H:%M:%S} UTC: {len(items)} new free item(s) since {since:%H:%M}; "
          f"failed requests: {len(FAILED)}", flush=True)
    if items:
        send(items)                  # if this raises, state is not saved and the next check retries
        state["last_alert"] = now.isoformat()
    last = state.get("last_alert") or state.setdefault("last_alert", now.isoformat())
    if now - datetime.fromisoformat(last) > timedelta(days=HEARTBEAT_DAYS):
        mail("alert script is still running",
             f"No new issue alerts for {HEARTBEAT_DAYS} days. This is only a check that it is alive.")
        state["last_alert"] = now.isoformat()
    seen.update(x["id"] for x in items)
    state["seen"] = sorted(seen)[-3000:]
    if not FAILED:                   # after a failed request, keep the old window so nothing is lost
        state["last_run"] = now.isoformat()
    if not DRY_RUN:
        json.dump(state, open(STATE_FILE, "w"))


def main():
    if os.environ.get("TEST_EMAIL") == "true":
        mail("Test email from the alert script",
             "If you can read this, the Gmail login and the secrets work.\n"
             "https://github.com/charuishere/alertMe")
        print("test email sent")
        return
    state = load_state()
    if not LOOP_MINUTES:
        run_once(state)
        sys.exit(1 if FAILED else 0)
    end = time.time() + LOOP_MINUTES * 60
    while True:
        try:
            run_once(state)
        except Exception as e:       # e.g. the mail failed: report it, keep watching
            FAILED.append("iteration")
            print("check failed:", type(e).__name__, e, file=sys.stderr, flush=True)
        if time.time() + LOOP_SLEEP >= end:
            break
        time.sleep(LOOP_SLEEP)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:                          # ended normally, so the workflow may start the next run
        open(out, "a").write("restart=true\n")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
