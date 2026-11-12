"""Emails me when a Kestra maintainer's issue is READY for contributors.
It only alerts. It never posts or comments anything.

An issue counts only if ALL of these are true:
  - it is in the kestra-io org, open, with nobody assigned and no linked PR
  - the author is a maintainer (GitHub author_association is OWNER or MEMBER)
  - the Kestra bot's "not ready for contributions" marker is NOT in the issue body
    (the marker is removed when the team moves the issue from Inbox to Backlog)
  - it has at most 2 comments (more than that is skipped: someone has probably claimed it)

Why it searches by "updated" and not "created": a maintainer usually opens an issue
first (marked not-ready) and the team makes it ready later by editing it. That edit
is the moment this script should alert on, so it watches edits too.
An issue is only ever alerted once.

Needs no extra packages. Same environment variables as alert.py:
MAIL_USER, MAIL_APP_PASSWORD, MAIL_TO, GITHUB_TOKEN (optional, raises the API limit).
Try it without sending mail:  DRY_RUN=1 LOOKBACK_MIN=1440 python kestra_alert.py"""
import json, os, smtplib, ssl, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

# ---- what to watch (edit these) ----
ORG = "kestra-io"
MAINTAINER_ROLES = {"OWNER", "MEMBER"}       # GitHub author_association values counted as maintainer
NOT_READY_MARKER = "kestra-bot:not-ready"    # bot comment at the top of an issue the team has not opened up
MAX_COMMENTS = 2                             # skip issues with more than this many comments (likely claimed)
SKIP_TITLES = ("CI Failure", "Dependency Dashboard")
MAX_IN_EMAIL = 20
# ------------------------------------

STATE_FILE = os.environ.get("STATE_FILE", "kestra_state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0"))
LOOP_SLEEP = int(os.environ.get("LOOP_SLEEP", "60"))
DEFAULT_WINDOW_MIN = 20      # first run, with no saved state
OVERLAP_MIN = 3              # re-check a little before the last run, in case search lags
MAX_BACK_MIN = 360           # never look back further than 6 hours
HEARTBEAT_DAYS = 7           # "still running" mail if nothing was sent for this long
FAILED = []


def fetch(url, headers=None):
    h = {"User-Agent": "kestra-issue-alerts", "Accept": "application/json"}
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


def is_ready_maintainer_issue(i):
    """True if this issue (search result or single-issue API object) passes every rule."""
    return (i.get("state", "open") == "open"
            and not i.get("assignees")
            and i.get("user", {}).get("type") != "Bot"
            and i.get("author_association") in MAINTAINER_ROLES
            and NOT_READY_MARKER not in (i.get("body") or "")
            and i.get("comments", 0) <= MAX_COMMENTS
            and not any(t in i["title"] for t in SKIP_TITLES))


def github_items(since):
    out = []
    q = (f"org:{ORG} is:issue is:open no:assignee -linked:pr "
         f"updated:>={since:%Y-%m-%dT%H:%M:%SZ}")
    for page in (1, 2, 3):                       # up to 300 recently updated issues
        d = fetch("https://api.github.com/search/issues?" +
                  urllib.parse.urlencode({"q": q, "per_page": 100, "page": page, "sort": "updated"}),
                  gh_headers())
        items = (d or {}).get("items", [])
        for i in items:
            if is_ready_maintainer_issue(i):
                out.append({"id": f"gh:{i['id']}", "name": i["repository_url"].split("repos/")[-1],
                            "title": i["title"], "url": i["html_url"], "api_url": i["url"],
                            "author": i["user"]["login"], "comments": i["comments"],
                            "labels": [l["name"] for l in i["labels"]]})
        if len(items) < 100:
            break
        time.sleep(2)
    return out


def still_ready(item):
    """Re-check right before alerting: the same rules, on the live issue."""
    d = fetch(item["api_url"], gh_headers())
    return bool(d) and is_ready_maintainer_issue(d)


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
    lines = []
    for x in items[:MAX_IN_EMAIL]:
        lab = f" ({', '.join(x['labels'][:3])})" if x["labels"] else ""
        lines.append(f"[{x['name']}] {x['title']}{lab}\n"
                     f"opened by maintainer {x['author']}, {x['comments']} comment(s)\n{x['url']}")
    if len(items) > MAX_IN_EMAIL:
        lines.append(f"... and {len(items) - MAX_IN_EMAIL} more. Check the repos.")
    body = ("Kestra issues by a maintainer, ready for contributors "
            "(checked just now, nobody assigned):\n\n" + "\n\n".join(lines))
    mail(f"{len(items)} Kestra issue(s) ready for contributors", body)


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
    items = [x for x in github_items(since) if x["id"] not in seen]
    items = [x for x in items if still_ready(x)]
    print(f"{now:%H:%M:%S} UTC: {len(items)} new ready maintainer issue(s) since {since:%H:%M}; "
          f"failed requests: {len(FAILED)}", flush=True)
    if items:
        send(items)                  # if this raises, state is not saved and the next check retries
        state["last_alert"] = now.isoformat()
    last = state.get("last_alert") or state.setdefault("last_alert", now.isoformat())
    if now - datetime.fromisoformat(last) > timedelta(days=HEARTBEAT_DAYS):
        mail("kestra alert script is still running",
             f"No new Kestra issue alerts for {HEARTBEAT_DAYS} days. This is only a check that it is alive.")
        state["last_alert"] = now.isoformat()
    seen.update(x["id"] for x in items)
    state["seen"] = sorted(seen)[-3000:]
    if not FAILED:                   # after a failed request, keep the old window so nothing is lost
        state["last_run"] = now.isoformat()
    if not DRY_RUN:
        json.dump(state, open(STATE_FILE, "w"))


def main():
    if os.environ.get("TEST_EMAIL") == "true":
        mail("Test email from the Kestra alert script",
             "If you can read this, the Gmail login and the secrets work.")
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
