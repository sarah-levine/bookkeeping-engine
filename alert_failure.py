#!/usr/bin/env python3
"""Send an email alert when the sync_tracker workflow fails."""
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

subject = sys.argv[1] if len(sys.argv) > 1 else "Reconciliation Tracker Sheet Update FAILED"
pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

# Sender/recipient come from the environment so no personal address is baked in.
# ALERT_EMAIL_FROM is the Gmail account; ALERT_EMAIL_TO defaults to the sender.
sender    = os.environ.get("ALERT_EMAIL_FROM", "")
recipient = os.environ.get("ALERT_EMAIL_TO", sender)

if not pw or not sender:
    print("No Gmail password or sender configured (GMAIL_APP_PASSWORD / "
          "ALERT_EMAIL_FROM), skipping alert")
    sys.exit(0)

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"] = sender
msg["To"] = recipient
body = (
    "<p>The sync_tracker workflow failed to update the Google Sheet.</p>"
    '<p><a href="https://github.com/sarah-levine/Bookkeeping/actions">View logs</a></p>'
)
msg.attach(MIMEText(body, "html"))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
    s.login(sender, pw)
    s.sendmail(sender, [recipient], msg.as_string())

print("Alert sent")
