#!/usr/bin/env python3
"""
monitor_multi.py
Concurrent HTTP + optional ICMP monitoring for many URLs.
Sends a single aggregated email containing all failures and recoveries per run.
Designed to be run every 30 minutes (systemd timer / cron) using --once,
or run as a daemon (no --once).
"""

import os
import time
import logging
import subprocess
from datetime import datetime
from typing import Tuple, Dict, List
import concurrent.futures
import json
from pathlib import Path

import requests
from requests.exceptions import RequestException, Timeout, ConnectionError
import smtplib
from email.message import EmailMessage

# ---------- Container-friendly defaults (configurable via env) ----------
# container base app dir
APP_DIR = Path(os.getenv("APP_DIR", "/app"))
DATA_DIR = Path(os.getenv("MONITOR_DATA_DIR", str(APP_DIR / "data")))
SRC_DIR = Path(os.getenv("APP_SRC_DIR", str(APP_DIR / "src")))

# ensure data dir exists early
DATA_DIR.mkdir(parents=True, exist_ok=True)

URLS_FILE = os.getenv("MONITOR_URLS_FILE", str(SRC_DIR / "urls.txt"))
CHECK_TIMEOUT = int(os.getenv("MONITOR_HTTP_TIMEOUT", "10"))
HTTP_RETRIES = int(os.getenv("MONITOR_HTTP_RETRIES", "2"))
RETRY_DELAY = int(os.getenv("MONITOR_RETRY_DELAY", "2"))
CONCURRENCY = int(os.getenv("MONITOR_CONCURRENCY", "10"))  # thread pool size

# logs/state inside data dir (writable)
LOG_FILE = os.getenv("MONITOR_LOG_FILE", str(DATA_DIR / "website_monitor_multi.log"))
STATE_FILE = os.getenv("MONITOR_STATE_FILE", str(DATA_DIR / "website_monitor_multi_state.json"))

SMTP_HOST = os.getenv("MONITOR_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("MONITOR_SMTP_PORT", "587"))
SMTP_USER = os.getenv("MONITOR_SMTP_USER", "")
SMTP_PASS = os.getenv("MONITOR_SMTP_PASS", "")
EMAIL_FROM = os.getenv("MONITOR_EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.getenv("MONITOR_EMAIL_TO", EMAIL_FROM)  # comma separated list

SEND_ON_EVERY_FAILURE = os.getenv("MONITOR_SEND_ON_EVERY_FAILURE", "false").lower() in ("1","true","yes")
# if false => only send on state transitions

# Interval for daemon mode
INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", str(30*60)))

# ---------- Logging ----------
# write to stdout (container logs) + file inside DATA_DIR
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

# stdout handler
sh = logging.StreamHandler()
sh.setFormatter(formatter)
logger.addHandler(sh)

# file handler
try:
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
except Exception:
    logger.exception("Unable to create file handler for log; continuing with stdout only")

# alias logging
logging = logger


# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)

# ---------- Helpers ----------
def load_urls(path: str) -> List[str]:
    if not os.path.exists(path):
        logging.error("URLs file not found: %s", path)
        return []
    with open(path, "r") as f:
        lines = [l.strip() for l in f.readlines()]
    # remove blank lines and comments
    return [l for l in lines if l and not l.startswith("#")]

def icmp_ping(host: str, count: int = 1, timeout: int = 2) -> bool:
    try:
        import platform
        plat = platform.system().lower()
        if plat == "windows":
            cmd = ["ping", "-n", str(count), "-w", str(timeout * 1000), host]
        else:
            cmd = ["ping", "-c", str(count), "-W", str(timeout), host]
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False

def http_check(url: str, timeout: int = CHECK_TIMEOUT, retries: int = HTTP_RETRIES) -> Tuple[bool,int,str]:
    last_status = 0
    last_err = ""
    for attempt in range(1, retries+1):
        try:
            resp = requests.get(url, timeout=timeout)
            last_status = resp.status_code
            if 200 <= resp.status_code < 300:
                return True, resp.status_code, ""
            else:
                return False, resp.status_code, f"HTTP {resp.status_code}"
        except (Timeout, ConnectionError) as e:
            last_err = str(e)
        except RequestException as e:
            last_err = str(e)
        if attempt < retries:
            time.sleep(RETRY_DELAY)
    return False, last_status, last_err or "Unknown error"

def send_email(subject: str, body: str):
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]
    if not SMTP_USER or not SMTP_PASS or not EMAIL_FROM or not recipients:
        logging.error("SMTP not properly configured; skipping email")
        return
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            if SMTP_PORT in (587, 25):
                smtp.starttls()
                smtp.ehlo()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        logging.info("Email sent: %s", subject)
    except Exception:
        logging.exception("Failed to send email")

# ---------- State persistence ----------
def load_state(path: str) -> Dict[str,str]:
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        logging.exception("Failed to read state file")
    return {}

def save_state(path: str, state: Dict[str,str]):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f)
    except Exception:
        logging.exception("Failed to write state file")

# ---------- Per-URL worker ----------
def check_url(url: str) -> Tuple[str, dict]:
    """
    Returns (url, result_dict)
    result_dict: {ok:bool, status_code:int, error:str, ping:bool}
    """
    url = url.strip()
    host = requests.utils.urlparse(url).hostname or url
    ping_ok = icmp_ping(host)
    ok, status_code, err = http_check(url)
    return url, {"ok": ok, "status_code": status_code, "error": err, "ping": ping_ok}

# ---------- Orchestration ----------
def perform_checks(urls: List[str], last_state: Dict[str,str]) -> Dict[str,str]:
    updated_state = last_state.copy()
    failures = []
    recoveries = []
    details = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        future_to_url = {ex.submit(check_url, u): u for u in urls}
        for fut in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[fut]
            try:
                u, res = fut.result()
            except Exception as e:
                logging.exception("Worker failure for %s", url)
                res = {"ok": False, "status_code": 0, "error": str(e), "ping": False}
                u = url

            status_text = f"OK: HTTP {res['status_code']}" if res["ok"] else f"FAIL: {res['error'] or ('HTTP '+str(res['status_code']))}"
            details[u] = {"status": status_text, "ping": res["ping"], "raw": res}

            prev = last_state.get(u)
            if res["ok"]:
                if prev and prev.startswith("FAIL"):
                    # transitioned to OK -> RECOVERY
                    recoveries.append((u, prev, status_text))
                updated_state[u] = "OK"
            else:
                # failure
                send_alert = False
                if SEND_ON_EVERY_FAILURE:
                    send_alert = True
                else:
                    if not prev:
                        send_alert = True
                    elif prev.startswith("OK"):
                        send_alert = True
                    elif prev.startswith("FAIL") and prev != status_text:
                        send_alert = True
                if send_alert:
                    failures.append((u, status_text))
                updated_state[u] = f"FAIL::{status_text}"

    # Build aggregated email(s)
    now = datetime.utcnow().isoformat() + "Z"
    if failures or recoveries:
        body_lines = [f"Monitor run time (UTC): {now}", "", "Summary:"]
        if failures:
            body_lines.append(f"\nFailures ({len(failures)}):")
            for u, st in failures:
                d = details.get(u, {})
                ping = d.get("ping")
                body_lines.append(f"- {u} -> {st} (ping={'OK' if ping else 'NO'})")
        if recoveries:
            body_lines.append(f"\nRecoveries ({len(recoveries)}):")
            for u, prev, cur in recoveries:
                d = details.get(u, {})
                ping = d.get("ping")
                body_lines.append(f"- {u} -> RECOVERED (now {cur}, prev {prev}, ping={'OK' if ping else 'NO'})")

        # include brief footer with instructions
        body_lines.append("\nFull details (per-URL):")
        for u, d in details.items():
            body_lines.append(f"- {u} : {d['status']} (ping={'OK' if d['ping'] else 'NO'})")

        subject = f"[ALERT] Monitor: {len(failures)} failures, {len(recoveries)} recoveries"
        send_email(subject, "\n".join(body_lines))
    else:
        logging.info("No failures or recoveries in this run.")

    return updated_state

# ---------- Main loop ----------
def main(run_once=False):
    urls = load_urls(URLS_FILE)
    if not urls:
        logging.error("No URLs to monitor; exiting")
        return

    state = load_state(STATE_FILE)
    state = perform_checks(urls, state)
    save_state(STATE_FILE, state)

    if run_once:
        return

    logging.info("Daemon mode: sleeping %s seconds", INTERVAL_SECONDS)
    while True:
        time.sleep(INTERVAL_SECONDS)
        try:
            urls = load_urls(URLS_FILE)
            state = perform_checks(urls, state)
            save_state(STATE_FILE, state)
        except Exception:
            logging.exception("Error during monitor loop")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="run once and exit (good for cron/systemd timer)")
    args = p.parse_args()
    main(run_once=args.once)
