#!/usr/bin/env python3
"""GitHub Actions version of the WU scraper — requests only, no Playwright."""

import os, sys, json, re, base64
from datetime import datetime
import requests as rq

WU_URL   = "https://www.wunderground.com/hourly/us/ca/los-angeles/KLAX/date/{date}"
GH_FILE  = "weather.json"
UA       = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
             "S","SSW","SW","WSW","W","WNW","NW","NNW"]
CLOUD_MAP = [("clear",5),("sunny",5),("fair",15),("mostly sunny",20),
             ("mostly clear",15),("partly cloudy",40),("partly sunny",40),
             ("mostly cloudy",75),("cloudy",90),("overcast",95),
             ("fog",85),("haze",50),("smoke",50),("drizzle",90),
             ("rain",90),("shower",85),("thunder",95),("snow",90)]

def deg_to_dir(deg):
    return WIND_DIRS[round(int(deg) / 22.5) % 16]

def cloud_from_phrase(phrase):
    p = (phrase or "").lower()
    for k, v in CLOUD_MAP:
        if k in p: return v
    return 50

def fmt_hour(iso):
    h = int(iso[11:13]); m = iso[14:16]
    if h == 0:  return f"12:{m} am"
    if h < 12:  return f"{h}:{m} am"
    if h == 12: return f"12:{m} pm"
    return f"{h-12}:{m} pm"

def _time_sort_key(h):
    mm = re.match(r"(\d+):(\d+)\s*(am|pm)", h["time"])
    if not mm: return 9999
    hr, mn, p = int(mm.group(1)), int(mm.group(2)), mm.group(3)
    return ((0 if hr==12 else hr)*60+mn) if p=="am" else ((12 if hr==12 else hr+12)*60+mn)

def scrape(date_str):
    url = WU_URL.format(date=date_str)
    print(f"Fetching {url}")
    resp = rq.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US"}, timeout=30)
    resp.raise_for_status()
    html = resp.text
    print(f"HTML {len(html):,} bytes")

    def extract_array(key, text):
        idx = text.find(f'"{key}"')
        while idx >= 0:
            arr_start = text.find('[', idx)
            if arr_start < 0 or arr_start - idx > 50:
                idx = text.find(f'"{key}"', idx+1); continue
            depth, end = 0, arr_start
            for i in range(arr_start, min(len(text), arr_start+200_000)):
                if text[i]=='[': depth+=1
                elif text[i]==']':
                    depth-=1
                    if depth==0: end=i+1; break
            try:
                arr = json.loads(text[arr_start:end])
                if arr: return arr, idx
            except Exception: pass
            idx = text.find(f'"{key}"', idx+1)
        return [], -1

    best_times, best_pos, best_len = [], -1, 0
    search_from = 0
    while True:
        arr, pos = extract_array("validTimeLocal", html[search_from:])
        if pos < 0: break
        abs_pos = search_from + pos
        if len(arr) > best_len:
            best_len = len(arr)
            best_times = [t for t in arr if isinstance(t,str) and t.startswith(date_str)]
            best_pos = abs_pos
        search_from = abs_pos + 1

    if not best_times:
        raise ValueError("No hourly data found in WU HTML")
    print(f"{len(best_times)} hours for {date_str} in {best_len}-entry block")

    w0 = max(0, best_pos-150_000); w1 = min(len(html), best_pos+50_000)
    window = html[w0:w1]

    def get_longest(key):
        best, i2 = [], 0
        while True:
            arr, pos = extract_array(key, window[i2:])
            if pos < 0: break
            if len(arr)>len(best): best=arr
            i2 += pos+1
        return best

    times_all = get_longest("validTimeLocal")
    offset = next((i for i,t in enumerate(times_all) if isinstance(t,str) and t.startswith(date_str)), 0)
    times  = [t for t in times_all if isinstance(t,str) and t.startswith(date_str)]
    temps   = get_longest("temperature")
    hums    = get_longest("relativeHumidity")
    winds   = get_longest("windSpeed")
    wdirs   = get_longest("windDirection")
    clouds  = get_longest("cloudCover")
    phrases = get_longest("wxPhraseLong") or get_longest("wxPhraseShort")

    def safe(arr, i, default=None): return arr[i] if i<len(arr) else default

    hourly = []
    for i, t in enumerate(times):
        ai = offset+i
        try:
            temp=safe(temps,ai); hum=safe(hums,ai)
            if temp is None or hum is None: continue
            wind=safe(winds,ai,0); wdir=safe(wdirs,ai,270)
            cloud=safe(clouds,ai); phrase=safe(phrases,ai,"")
            cloud_val = int(cloud) if cloud is not None else cloud_from_phrase(phrase)
            cond = str(phrase).split(".")[0][:25].strip() if phrase else "Cloudy"
            hourly.append(dict(time=fmt_hour(t), temp=int(temp), cloud=cloud_val,
                               hum=int(hum), wind=int(wind) if wind else 0,
                               dir=deg_to_dir(wdir), cond=cond))
        except Exception: continue

    hourly.sort(key=_time_sort_key)
    return hourly

def commit(payload, token, repo):
    api  = f"https://api.github.com/repos/{repo}/contents/{GH_FILE}"
    hdrs = {"Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "klax-scraper/1.0"}
    content = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()
    sha = None
    r = rq.get(api, headers=hdrs, timeout=15)
    if r.status_code == 200: sha = r.json().get("sha")
    body = {"message": f"wx: KLAX {payload['date']} {payload['scrapedAt']}", "content": content}
    if sha: body["sha"] = sha
    r2 = rq.put(api, headers=hdrs, json=body, timeout=15)
    r2.raise_for_status()
    return r2.json().get("commit",{}).get("sha","")[:8]

def main():
    token = os.environ.get("GITHUB_TOKEN","")
    repo  = os.environ.get("GITHUB_REPO","lukasbadaro1/klax-bets")
    if not token: sys.exit("ERROR: GITHUB_TOKEN not set")

    date_str = datetime.now().strftime("%Y-%m-%d")
    print(f"Scraping KLAX {date_str}")
    hourly = scrape(date_str)
    if not hourly: sys.exit("ERROR: no hours parsed")

    wu_high = max(h["temp"] for h in hourly)
    peak    = next(h for h in hourly if h["temp"]==wu_high)
    print(f"High {wu_high}°F @ {peak['time']}  wind={peak['wind']}mph {peak['dir']}")

    now = datetime.now()
    payload = {"station":"KLAX", "date": now.strftime("%B %-d, %Y"),
               "scrapedAt": now.strftime("%-I:%M %p")+" · WU",
               "ts": int(now.timestamp()*1000), "source":"WU live", "hourly": hourly}

    with open(GH_FILE,"w") as f: json.dump(payload,f,indent=2)
    sha = commit(payload, token, repo)
    print(f"Committed {sha}")

if __name__ == "__main__":
    main()
