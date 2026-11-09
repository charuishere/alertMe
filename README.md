# issue-alerts
Emails me when a new, unclaimed issue appears in the projects I follow. It never posts anything.

Edit the watch list at the top of `alert.py`.
Test without sending mail: `DRY_RUN=1 LOOKBACK_MIN=1440 python alert.py`

Setup (GitHub, private repo):
1. Create a private repo and push these files.
2. Gmail: turn on 2-step verification, then create an App Password (Google Account > Security > App passwords).
3. Repo > Settings > Secrets and variables > Actions, add: MAIL_USER (your gmail), MAIL_APP_PASSWORD (the app password), MAIL_TO (where to send).
4. Actions tab > issue-alerts > Run workflow once to test.
