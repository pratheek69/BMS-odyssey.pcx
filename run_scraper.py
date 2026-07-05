import requests
from curl_cffi import requests as cffi_requests
import time
import json
import os
import re
import subprocess
from datetime import datetime

# --- CONFIGURATION ---
DATES = ["20260718", "20260719"]
VENUE_CODE = "PRHN"
EVENT_CODE = "ET00452034"
STATE_FILE = "seats.json"
MAX_RUNTIME_SECONDS = (5 * 3600) + (55 * 60) # 5 hours 55 mins

# Track WARP State natively
USE_WARP = False

# Cloudflare WARP local proxy
PROXIES = {
    "http": "socks5://127.0.0.1:40000",
    "https": "socks5://127.0.0.1:40000"
}

GET_HEADERS = {
    "Host": "in.bookmyshow.com",
    "Content-Type": "application/json",
    "X-Latitude": "17.385044",
    "X-Subregion-Code": "HYD",
    "X-App-Code": "MOBAND2",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 10; Android SDK built for x86_64 Build/QSR1.211112.011)",
    "X-App-Version": "18.2.3",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive"
}

POST_HEADERS = {
    "Host": "services-in.bookmyshow.com",
    "X-Timeout": "10",
    "X-Latitude": "17.385044",
    "X-Subregion-Code": "HYD",
    "X-App-Code": "MOBAND2",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 10; Android SDK built for x86_64 Build/QSR1.211112.011)",
    "X-App-Version": "18.2.3",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept-Encoding": "gzip, deflate"
}

def humanize_date(date_str):
    dt = datetime.strptime(date_str, "%Y%m%d")
    day = dt.day

    if 11 <= (day % 100) <= 13:
        suffix = 'th'
    else:
        suffix = ['th', 'st', 'nd', 'rd', 'th'][min(day % 10, 4)]
        
    month_name = dt.strftime("%B")
    return f"{day}{suffix} {month_name}"

def quiet_git_pull():
    """Fetches and hard resets to exactly match remote. Wipes any failed local commits to prevent JSON merge conflicts."""
    subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, check=False)
    subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, check=False)

def quiet_git_push():
    res = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True, check=False)
    return res.returncode == 0

def read_local_state():
    """Reads the JSON from disk without touching Git."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"[STATE] ⚠️ JSON Error reading state: {e}")
            return {}
    return {}

def load_state():
    """Syncs with remote and loads the freshest state into memory."""
    quiet_git_pull()
    return read_local_state()

def save_state(deltas, commit_msg="Update seat state"):
    """
    Takes a dictionary of local session changes (deltas), cleanly merges them with the 
    absolute latest Git state, and pushes. Retries seamlessly if another runner pushes first.
    Returns the newly merged state so the runner can update its memory.
    """
    for attempt in range(3):
        # 1. Force sync local repo with remote (drops any failed local commits from prior attempts)
        quiet_git_pull()
        
        # 2. Read the newly synced remote state
        latest_state = read_local_state()
        
        # 3. Merge our locally tracked changes (deltas) into this state
        for s_id, s_data in deltas.items():
            latest_state[s_id] = s_data
            
        # 4. Save the merged state to disk
        with open(STATE_FILE, "w") as f:
            json.dump(latest_state, f, indent=2)
            
        # 5. Commit
        subprocess.run(["git", "add", STATE_FILE], capture_output=True, check=False)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        
        if STATE_FILE in status.stdout:
            print(f"[GIT] Committing changes to {STATE_FILE} (Attempt {attempt+1})...")
            subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, check=False)
            
            # 6. Push
            if quiet_git_push():
                print(f"[GIT] Successfully pushed merged state to repository.")
                return latest_state
            else:
                print(f"[GIT] Push attempt {attempt+1} failed (likely concurrent push). Retrying merge...")
                time.sleep(2)
        else:
            print("[GIT] Merged state is identical to remote. Nothing to push.")
            return latest_state
            
    print("[GIT] ❌ Failed to push after 3 attempts. Local memory updated with last known merge.")
    return latest_state

def trigger_ntfy(message, booking_url):
    print(f"\n[!] ALERTING VIA NTFY: {message}")
    for i in range(1):
        try:
            resp = requests.post(
                "https://ntfy.sh/odyssey_pcx",
                data=message.encode("utf-8"),
                headers={
                    "Priority": "urgent",
                    "Title": "🎬 Odyssey Seats Available",
                    "Click": booking_url
                },
                timeout=10
            )
            print(f"    -> Ntfy ping {i+1}/1 sent! Status: {resp.status_code}")
        except Exception as e:
            print(f"    -> Ntfy ping {i+1} failed: {e}")

def toggle_warp():
    global USE_WARP
    if USE_WARP:
        print("    -> 🚨 [IP ROTATION] WARP is currently ON. Disconnecting WARP (Switching to Runner IP)...")
        subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, check=False)
        USE_WARP = False
    else:
        print("    -> 🚨 [IP ROTATION] WARP is currently OFF. Connecting to WARP (Switching to Cloudflare Proxy)...")
        subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, check=False)
        time.sleep(5)
        USE_WARP = True

def make_bms_request(method, url, max_retries=3, **kwargs):
    for attempt in range(1, max_retries + 1):
        current_proxies = PROXIES if USE_WARP else None
        
        try:
            if method.upper() == 'GET':
                resp = cffi_requests.get(url, proxies=current_proxies, impersonate="chrome", timeout=15, **kwargs)
            else:
                resp = cffi_requests.post(url, proxies=current_proxies, impersonate="chrome", timeout=15, **kwargs)
            
            print(f"    -> Status: {resp.status_code} (Using WARP: {USE_WARP})")
            
            if resp.status_code == 429:
                print(f"    -> ⚠️ Rate limited (429) on attempt {attempt}/{max_retries}.")
                if attempt < max_retries:
                    toggle_warp()
                    print("    -> Retrying request...")
                    continue
                else:
                    print("    -> ❌ Max retries reached for this request.")
            
            return resp
            
        except Exception as e:
            print(f"    -> ⚠️ Network exception on attempt {attempt}: {e}")
            if attempt < max_retries:
                time.sleep(3)
                continue
    
    return None

def fetch_sessions():
    sessions = []
    for date_code in DATES:
        print(f"\n[NETWORK] Fetching sessions for Date: {date_code}...")
        url = f"https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary?eventCode={EVENT_CODE}&dateCode={date_code}&regionCode=HYD&venueCode={VENUE_CODE}"
        
        resp = make_bms_request('GET', url, headers=GET_HEADERS)
        if not resp or resp.status_code != 200:
            print(f"    -> Failed fetching {date_code}. Skipping...")
            continue
            
        try:
            data = resp.json()
            shows = data.get("data", {}).get("showTimes", [])
            print(f"    -> Found {len(shows)} total shows for this date. Filtering for PCX SCREEN...")
            
            pcx_count = 0
            for show in shows:
                if show.get("attributes") == "PCX SCREEN":
                    sessions.append({
                        "sessionId": show["sessionId"],
                        "dateCode": show["showDateCode"],
                        "time": show["showTime"]
                    })
                    pcx_count += 1
            print(f"    -> Filtered {pcx_count} PCX SCREEN sessions for {date_code}.")
            
        except Exception as e:
            print(f"    -> JSON Parse error for {date_code}: {e}")
            
    return sessions

def fetch_seat_layout(session_id):
    url = "https://services-in.bookmyshow.com/doTrans.aspx"
    payload = f"strParam4=&strParam5=Y&strParam6=&strParam7=N&strParam1={session_id}&strParam2=WEB&strParam3=&strVenueCode={VENUE_CODE}&lngTransactionIdentifier=0&strAppCode=MOBAND2&strFormat=json&strCommand=GETSEATLAYOUT"
    
    print(f"    -> [POST] {url} (Session: {session_id})")
    resp = make_bms_request('POST', url, headers=POST_HEADERS, data=payload)
    
    if not resp or resp.status_code != 200:
        print(f"    -> Failed layout fetch.")
        return ""
        
    try:
        return resp.json().get("BookMyShow", {}).get("strData", "")
    except Exception as e:
        print(f"    -> Exception during JSON parse for layout {session_id}: {e}")
        return ""

def parse_layout(str_data):
    if not str_data: return {}
    
    parts = str_data.split("||")
    rows_data = parts[1] if len(parts) > 1 else parts[0]
    rows = rows_data.split("|")
    
    available_seats_by_row = {}
    
    for row in rows:
        if not row or ":" not in row: continue
        elements = row.split(":")
        row_letter = elements[1]
        seats = elements[2:]
        
        available_in_row = []
        for seat in seats:
            match = re.search(r"A[^2]\d{2}(\d+)\+", seat)
            if match:
                available_in_row.append(match.group(1))
                
        if available_in_row:
            available_seats_by_row[row_letter] = available_in_row
            
    return available_seats_by_row

def main():
    start_time = time.time()
    
    print("==================================================")
    print("🚀 STARTING BMS SEAT SCRAPER")
    print("==================================================")
    print("Fetching valid sessions...")
    target_sessions = fetch_sessions()
    
    total_sessions = len(target_sessions)
    print(f"\n✅ Found a total of {total_sessions} PCX SCREEN sessions to monitor.")
    print("==================================================")
    
    if total_sessions == 0:
        print("No valid sessions found. Exiting.")
        return

    print("\n[GIT] Loading initial state from repository...")
    state = load_state()
    is_first_run = len(state) == 0
    if is_first_run:
        print("[STATE] Empty state found. Initializing baseline silently...")
    else:
        print(f"[STATE] Loaded existing state for {len(state)} sessions.")

    cycle_count = 1
    
    while (time.time() - start_time) < MAX_RUNTIME_SECONDS:
        print(f"\n==================================================")
        print(f"🔄 STARTING POLLING CYCLE {cycle_count}")
        print(f"==================================================")
        
        # Pull latest state before starting the cycle
        state = load_state() 
        deltas = {} # Track ONLY the sessions that change during this cycle
        
        for index, session in enumerate(target_sessions, 1):
            s_id = session["sessionId"]
            s_date = session["dateCode"]
            s_time = session["time"]
            
            print(f"\n[{index}/{total_sessions}] Checking Session {s_id} (Date: {s_date} Time: {s_time})")
            print("    -> Sleeping for 30 seconds (Rate Limit Prevention)...")
            time.sleep(30) 
            
            str_data = fetch_seat_layout(s_id)
            if not str_data:
                print("    -> Error: Received empty strData.")
                continue
                
            current_seats = parse_layout(str_data)
            current_total = sum(len(seats) for seats in current_seats.values())
            print(f"    -> Parse successful. Current Available Seats: {current_total}")
            
            if s_id not in state:
                state[s_id] = {"date": s_date, "time": s_time, "total": 0, "rows": {}}
            
            previous_total = state[s_id].get("total", 0)
            previous_rows = state[s_id].get("rows", {})
            
            newly_unblocked_count = 0
            unblocked_rows_list = []
            
            for row, seats in current_seats.items():
                old_seats_in_row = previous_rows.get(row, [])
                new_seats = set(seats) - set(old_seats_in_row)
                
                if new_seats:
                    newly_unblocked_count += len(new_seats)
                    unblocked_rows_list.append(row)
            
            if newly_unblocked_count > 0:
                print(f"    -> 🟢 DETECTED UNBLOCKS: +{newly_unblocked_count} new seats!")
                
                if not is_first_run:
                    if newly_unblocked_count >= 6:
                        rows_str = ", ".join(sorted(unblocked_rows_list))
                        human_date = humanize_date(s_date)
                        booking_url = (f"https://in.bookmyshow.com/movies/HYD/seat-layout/"f"{EVENT_CODE}/{VENUE_CODE}/{s_id}/{s_date}")
                        
                        msg = (
                                f"🎬 Odyssey Seats Available\n\n"
                                f"{rows_str} rows unblocked\n"
                                f"{newly_unblocked_count} seats available\n"
                                f"{human_date} • {s_time}\n\n"
                                f"Book now:\n{booking_url}"
                        )
                        trigger_ntfy(msg)
                    else:
                        print(f"    -> 🟡 Less than 6 seats unblocked ({newly_unblocked_count}). Skipping notification to avoid spam.")
                
                # Update memory & Track Delta
                state[s_id]["rows"] = current_seats
                state[s_id]["total"] = current_total
                deltas[s_id] = state[s_id]

            elif current_total < previous_total:
                print(f"    -> 🔴 Seats booked. Total dropped from {previous_total} down to {current_total}.")
                # Update memory & Track Delta
                state[s_id]["rows"] = current_seats
                state[s_id]["total"] = current_total
                deltas[s_id] = state[s_id]
                
            else:
                print("    -> ⚪ No changes detected.")

        if deltas:
            print("\n[STATE] Cycle finished. Changes detected, merging and saving to Git...")
            # Save state will handle merging our deltas with the newest Git data
            # and return the freshly synced state to update our memory
            state = save_state(deltas, f"State update at cycle {cycle_count}")
        else:
            print("\n[STATE] Cycle finished. No changes detected.")
            
        if is_first_run:
            is_first_run = False
            print("[STATE] First run baseline has been successfully established.")
            
        cycle_count += 1
        
    print("\n🏁 Time limit reached (5h 55m). Gracefully shutting down.")

if __name__ == "__main__":
    main()
