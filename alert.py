"""Emails me when a new, unclaimed issue shows up in the projects I follow.
It only alerts. It never posts or comments anything."""
import json, os, smtplib, ssl, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

# ---- what to watch (edit these) ----
# query: GitHub search scope. gfi_only: only issues labelled "good first issue".
# max_comments: skip issues that already have more comments than this.
GITHUB = [
    {"query": "org:fossasia", "gfi_only": False, "max_comments": 2, "priority": True},
    {"query": "org:accordproject", "gfi_only": False, "max_comments": 2},
    {"query": "org:cBioPortal", "gfi_only": False, "max_comments": 2},
    {"query": "org:oppia", "gfi_only": True, "max_comments": 2},
]
JIRA = [
    # (name, base url, search endpoint, JQL without the time part)
    ("Fineract", "https://issues.apache.org/jira", "search",
     'project = FINERACT AND resolution = Unresolved AND assignee is EMPTY '
     'AND labels in (beginner, beginner-friendly)'),
    ("OpenMRS", "https://openmrs.atlassian.net", "search/jql",
     'project = TRUNK AND resolution = Unresolved AND assignee is EMPTY '
     'AND status in ("Ready for Work", "Waiting for Dev")'),
]
SKIP_TITLES = ("CI Failure", "Dependency Dashboard")
# ------------------------------------

LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", "20"))
SEEN_FILE = os.environ.get("SEEN_FILE", "seen.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
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
    """Re-check right before alerting: no assignee and not too many comments."""
    d = fetch(item["api_url"], gh_headers())
    return bool(d) and d.get("state") == "open" and not d.get("assignees")


def github_items(since):
    out = []
    for s in GITHUB:
        q = (f'{s["query"]} is:issue is:open no:assignee -linked:pr '
             f'comments:0..{s["max_comments"]} created:>={since:%Y-%m-%dT%H:%M:%SZ}')
        if s.get("gfi_only"):
            q += ' label:"good first issue"'
        d = fetch("https://api.github.com/search/issues?" +
                  urllib.parse.urlencode({"q": q, "per_page": 30, "sort": "created"}), gh_headers())
        for i in (d or {}).get("items", []):
            if i.get("user", {}).get("type") == "Bot" or any(t in i["title"] for t in SKIP_TITLES):
                continue
            out.append({"id": f"gh:{i['id']}", "name": i["repository_url"].split("repos/")[-1],
                        "title": i["title"], "url": i["html_url"], "api_url": i["url"],
                        "labels": [l["name"] for l in i["labels"]], "priority": s.get("priority", False)})
        time.sleep(2)
    return out


def jira_items():
    out = []
    for name, base, endpoint, jql in JIRA:
        j = f"{jql} AND created >= -{LOOKBACK_MIN}m"   # relative time avoids timezone mistakes
        d = fetch(f"{base}/rest/api/2/{endpoint}?" +
                  urllib.parse.urlencode({"jql": j, "maxResults": 30, "fields": "summary"}))
        for i in (d or {}).get("issues", []):
            out.append({"id": f"jira:{name}:{i['key']}", "name": name,
                        "title": f"{i['key']}: {i['fields']['summary']}",
                        "url": f"{base}/browse/{i['key']}", "api_url": None, "labels": [], "priority": False})
    return out


def send(items):
    items = sorted(items, key=lambda x: not x["priority"])
    lines = []
    for x in items:
        tag = "[FOSSASIA, move fast] " if x["priority"] else ""
        lab = f" ({', '.join(x['labels'][:3])})" if x["labels"] else ""
        lines.append(f"{tag}[{x['name']}] {x['title']}{lab}\n{x['url']}")
    body = "New unclaimed issues (checked just now, nobody assigned):\n\n" + "\n\n".join(lines)
    subject = f"{'FOSSASIA: ' if items[0]['priority'] else ''}{len(items)} new open-source issue(s)"
    if DRY_RUN:
        print("SUBJECT:", subject, "\n" + body)
        return
    user = os.environ["MAIL_USER"].strip()
    password = os.environ["MAIL_APP_PASSWORD"].replace(" ", "").strip()
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, os.environ["MAIL_TO"].strip()
    msg.set_content(body)
    print(f"mail login: user length {len(user)}, app password length {len(password)} (should be 16)")
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


def send_test():
    """One-off check that the Gmail login and the secrets work."""
    items = [{"id": "test", "name": "test", "title": "Test email from the alert script",
              "url": "https://github.com/charuishere/alertMe", "labels": [], "priority": False}]
    send(items)
    print("test email sent")


def main():
    if os.environ.get("TEST_EMAIL") == "true":
        send_test()
        return
    since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MIN)
    try:
        seen = set(json.load(open(SEEN_FILE)))
    except Exception:
        seen = set()
    items = [x for x in github_items(since) + jira_items() if x["id"] not in seen]
    items = [x for x in items if not x["api_url"] or still_free(x)]
    print(f"{len(items)} new free item(s) in the last {LOOKBACK_MIN} min; failed requests: {len(FAILED)}")
    if items:
        send(items)
    if not DRY_RUN:
        seen.update(x["id"] for x in items)
        json.dump(sorted(seen)[-3000:], open(SEEN_FILE, "w"))
    if FAILED:                       # a red run makes GitHub email me, so a broken watcher is not silent
        sys.exit(1)


if __name__ == "__main__":
    main()
