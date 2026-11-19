#!/usr/bin/env python3
"""Ask Stalwart whether it will accept mail for an address, without sending any."""
import smtplib

TARGETS = ["ada.mensah@atheniq.institute", "ghost-probe@atheniq.institute"]

server = smtplib.SMTP("127.0.0.1", 25, timeout=10)
server.ehlo("i1.local")
print("MAIL FROM:", server.mail("probe@innotel.us"))
for address in TARGETS:
    try:
        print(address, "->", server.rcpt(address))
    except smtplib.SMTPRecipientsRefused as refused:
        print(address, "-> REFUSED", refused.recipients)
server.quit()
