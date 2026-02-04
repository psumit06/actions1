import os
import re
import csv
import time
import requests
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError

# ============================================================
# CONFIG
# ============================================================

CSV_FILE = os.environ.get("SF_USER_CSV", "sf_users.csv")

SF_SANDBOX_URL = os.environ["SF_SANDBOX_URL"]
SF_DOMAIN = os.environ["SF_DOMAIN"]

MAILSAC_API_KEY = os.environ["MAILSAC_API_KEY"]

MAILSAC_HEADERS = {
    "Mailsac-Key": MAILSAC_API_KEY,
    "Accept": "*/*",
}

OTP_TIMEOUT = 120
TOKEN_TIMEOUT = 180
POLL_INTERVAL = 5

# ============================================================
# LOGGING
# ============================================================


def log(msg):
    print(f"[+] {msg}", flush=True)


# ============================================================
# CSV LOADER (BOM + CASE SAFE)
# ============================================================


def load_users_from_csv():
    with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]

        users = []
        for row in reader:
            users.append({k.strip().lower(): v.strip() for k, v in row.items()})

    if not users:
        raise RuntimeError("CSV is empty")

    if not {"username", "password"}.issubset(users[0]):
        raise RuntimeError(
            f"CSV must contain Username,Password — found {users[0].keys()}"
        )

    return users


# ============================================================
# MAILSAC HELPERS
# ============================================================


def purge_inbox(email, limit=50):
    log(f"🧹 Purging Mailsac inbox for {email}")

    # very old timestamp = delete everything
    until = (
        datetime(1985, 4, 12, 23, 20, 50, tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    url = (
        f"https://mailsac.com/api/addresses/{email}/messages"
        f"?until={until}&limit={limit}"
    )

    r = requests.delete(url, headers=MAILSAC_HEADERS, timeout=30)

    if r.status_code not in (200, 204):
        log(f"⚠️ Purge failed: {r.status_code} {r.text}")
    else:
        log("✅ Inbox purge request sent")


def get_latest_message(email):
    r = requests.get(
        f"https://mailsac.com/api/addresses/{email}/messages",
        headers=MAILSAC_HEADERS,
        timeout=20,
    )
    r.raise_for_status()

    msgs = r.json()
    return msgs[0] if msgs else None


def get_message_text(email, message_id):
    r = requests.get(
        f"https://mailsac.com/api/text/{email}/{message_id}",
        headers=MAILSAC_HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    return r.text


def wait_for_email(email, match_fn, timeout):
    start = time.time()

    while time.time() - start < timeout:
        msg = get_latest_message(email)

        if msg:
            body = get_message_text(email, msg["_id"])
            if match_fn(msg, body):
                return body

        time.sleep(POLL_INTERVAL)

    raise RuntimeError("Timed out waiting for email")


# ============================================================
# OTP FETCH
# ============================================================


def fetch_otp(email):
    log("📬 Waiting for OTP email...")

    body = wait_for_email(
        email,
        lambda msg, txt: re.search(
            r"Verification Code:\s*\d{6}", txt, re.I
        ),
        OTP_TIMEOUT,
    )

    match = re.search(r"Verification Code:\s*(\d{6})", body, re.I)

    if not match:
        match = re.search(r"\b(\d{6})\b", body)

    if not match:
        raise RuntimeError("OTP not found in email body")

    otp = match.group(1)

    log(f"OTP received: {otp}")
    return otp


# ============================================================
# TOKEN FETCH
# ============================================================


def fetch_security_token(email):
    log("📬 Waiting for security token email...")

    body = wait_for_email(
        email,
        lambda msg, txt: (
            "security token" in txt.lower()
            or "security token" in (msg.get("subject") or "").lower()
        ),
        TOKEN_TIMEOUT,
    )

    match = re.search(
        r"Security token\s*\(case-sensitive\):\s*([A-Za-z0-9]+)",
        body,
        re.I,
    )

    if not match:
        match = re.search(r"token\s+is\s+([A-Za-z0-9]+)", body, re.I)

    if not match:
        raise RuntimeError("Security token not found")

    token = match.group(1)

    log(f"🔐 New token: {token}")
    return token


# ============================================================
# SALESFORCE AUTOMATION
# ============================================================


def rotate_user(browser, user):
    username = user["username"]
    password = user["password"]

    mailsac_email = username.lower()

    log("=" * 70)
    log(f"Rotating token for: {username}")

    purge_inbox(mailsac_email)

    context = browser.new_context()
    page = context.new_page()

    page.goto(SF_SANDBOX_URL)

    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#Login")

    log("Waiting for MFA screen...")

    page.wait_for_selector("#emc", timeout=30000)

    otp = fetch_otp(mailsac_email)

    page.fill("#emc", otp)

    page.get_by_role("button", name="Verify").click()

    page.wait_for_url(re.compile("lightning.force.com"), timeout=60000)

    reset_url = (
        f"https://{SF_DOMAIN}.lightning.force.com/"
        "lightning/settings/personal/ResetApiToken/home"
    )

    log("Opening reset token page...")
    page.goto(reset_url)

    log("Switching to iframe...")

    frame = page.frame_locator("iframe[id*='vfFrameId'], iframe[src*='ResetApiToken']")

    reset_btn = frame.get_by_role("button", name="Reset Security Token")

    reset_btn.wait_for(timeout=30000)

    try:
        reset_btn.click()
    except:
        log("⚠️ Normal click failed — forcing")
        reset_btn.click(force=True)

    token = fetch_security_token(mailsac_email)

    log(f"🎉 TOKEN ROTATED for {username}")
    log(token)

    context.close()


# ============================================================
# MAIN
# ============================================================


def main():
    users = load_users_from_csv()

    log(f"Loaded {len(users)} users from CSV")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for user in users:
            try:
                rotate_user(browser, user)
            except Exception as e:
                log(f"❌ FAILED for {user['username']}: {e}")

        browser.close()


if __name__ == "__main__":
    main()
