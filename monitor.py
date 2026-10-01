#!/usr/bin/env python3
"""MFC site monitor: runs on GitHub Actions, alerts via the website alerts Telegram bot.

Standard library only. Never prints secrets or page bodies: logs show check ids,
statuses and HTTP codes only. Honest user agent; a SiteGround challenge (HTTP 202 +
sg-captcha) is treated as BLIND (cannot see), never as DOWN, and is never bypassed.

Tiers: CORE checks every run (~15 min); FULL sweep every ~4 h. Any check that is
failing or alerted is re-checked every run. A check must fail on 2 consecutive runs
before it alerts; alerts are grouped; a still-failing incident is re-announced at most
every 2 h; a check that passes 2 consecutive runs after an alert sends RECOVERED.
"""
import json, os, random, re, string, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SITE = "https://mfcroadsideassistance.com"
UA = "MFC-SiteMonitor/1.0 (+https://github.com/mfc-roadside-assistance/mfc-site-monitor)"
PT = ZoneInfo("America/Los_Angeles")
STATE_PATH = os.environ.get("STATE_PATH", "state/state.json")
CONFIRM_FAILS, CONFIRM_OKS = 2, 2
REPEAT_EVERY = timedelta(hours=2)
FULL_EVERY = timedelta(hours=4) - timedelta(minutes=5)   # full sweep every ~4 h (lighter load on SiteGround)
BLIND_ALERT_AFTER, BLIND_REPEAT = timedelta(hours=2), timedelta(hours=6)
REVIEWS_MIN = int(os.environ.get("REVIEWS_MIN", "1300"))
REVIEWS_MAX = int(os.environ.get("REVIEWS_MAX", "1999"))
REVIEWS_MAX_AGE = timedelta(hours=26)
GATE_AFTER = datetime.fromisoformat(os.environ.get("GATE_AFTER", "2026-10-01T18:29:14+00:00"))
HEARTBEAT_PT = (8, 30)
PHONE, GTM = "(323) 365-3538", "GTM-KQTCCWB"
ADS_URLS = [
    "https://mfcroadsideassistance.com", "https://mfcroadsideassistance.com/",
    "https://mfcroadsideassistance.com/#reviews", "https://mfcroadsideassistance.com/#services",
    "https://mfcroadsideassistance.com/battery-service.html", "https://mfcroadsideassistance.com/car-lockout.html",
    "https://mfcroadsideassistance.com/flat-tire-change.html", "https://mfcroadsideassistance.com/fuel-delivery.html",
    "https://mfcroadsideassistance.com/jump-start.html", "https://mfcroadsideassistance.com/tire-replacement.html",
]
HUBS = ["tire-replacement", "battery-service", "flat-tire-change", "jump-start", "car-lockout", "fuel-delivery"]

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
_opener = urllib.request.build_opener(_NoRedirect)

def fetch(url, accept="text/html,application/json;q=0.9,*/*;q=0.8"):
    """GET without following redirects. Returns code/headers/body; never raises."""
    req = urllib.request.Request(url.split("#")[0], headers={"User-Agent": UA, "Accept": accept})
    try:
        r = _opener.open(req, timeout=20)
        code, hdr, body = r.status, r.headers, r.read(800_000)
    except urllib.error.HTTPError as e:
        code, hdr = e.code, e.headers
        try: body = e.read(800_000)
        except Exception: body = b""
    except Exception as e:
        return {"code": None, "hdr": {}, "body": "", "err": type(e).__name__}
    finally:
        time.sleep(0.7)   # be gentle: sequential requests, small gap
    return {"code": code, "hdr": hdr, "body": body.decode("utf-8", "replace"), "err": None}

def challenged(r):
    return r["code"] == 202 and (bool(r["hdr"].get("sg-captcha")) or "sgcaptcha" in r["body"])

def page_check(url):
    def run():
        r = fetch(url)
        if challenged(r): return "BLIND", "sg-captcha 202"
        if r["err"]: return "FAIL", f"no response ({r['err']})"
        if r["code"] != 200: return "FAIL", f"HTTP {r['code']}"
        if PHONE not in r["body"]: return "FAIL", "HTTP 200 but phone number missing"
        if GTM not in r["body"]: return "FAIL", "HTTP 200 but GTM-KQTCCWB missing"
        return "OK", "HTTP 200"
    return run

def notfound_check():
    slug = "mfc-monitor-404-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    r = fetch(f"{SITE}/{slug}")
    if challenged(r): return "BLIND", "sg-captcha 202"
    if r["err"]: return "FAIL", f"no response ({r['err']})"
    if r["code"] != 404: return "FAIL", f"missing URL returned HTTP {r['code']}, expected 404"
    if "Page Not Found" not in r["body"]: return "FAIL", "HTTP 404 but not the MFC 404 page"
    return "OK", "HTTP 404 + MFC 404 page"

def redirect_check(url, code, target):
    def run():
        r = fetch(url)
        if challenged(r): return "BLIND", "sg-captcha 202"
        if r["err"]: return "FAIL", f"no response ({r['err']})"
        loc = (r["hdr"].get("Location") or "").strip()
        if loc.startswith("/"): loc = SITE + loc
        if r["code"] != code: return "FAIL", f"HTTP {r['code']}, expected {code}"
        if loc != target: return "FAIL", f"{code} to the wrong place"
        return "OK", f"{code} -> expected target"
    return run

REVIEWS_SEEN = {}
def reviews_check():
    r = fetch(f"{SITE}/reviews.json?v={datetime.now(timezone.utc):%Y-%m-%d}", accept="application/json")
    if challenged(r): return "BLIND", "sg-captcha 202"
    if r["err"]: return "FAIL", f"no response ({r['err']})"
    if r["code"] != 200: return "FAIL", f"HTTP {r['code']}"
    try:
        d = json.loads(r["body"])
        total = d.get("googleTotal")
        lu = datetime.fromisoformat(str(d.get("lastUpdated")).replace("Z", "+00:00"))
    except Exception:
        return "FAIL", "reviews.json unreadable"
    age = datetime.now(timezone.utc) - lu
    REVIEWS_SEEN.update(total=total, last_updated=lu, age_h=round(age.total_seconds() / 3600, 1))
    if not (isinstance(total, int) and REVIEWS_MIN <= total <= REVIEWS_MAX):
        return "FAIL", f"googleTotal {total!r} outside {REVIEWS_MIN}-{REVIEWS_MAX}"
    if age > REVIEWS_MAX_AGE:
        return "FAIL", f"stale: lastUpdated {REVIEWS_SEEN['age_h']} h ago"
    return "OK", f"googleTotal {total}, updated {REVIEWS_SEEN['age_h']} h ago"

def wp_gtm_check():
    r = fetch(f"{SITE}/wp-json/wp/v2/pages?per_page=1&_fields=link", accept="application/json")
    if challenged(r): return "BLIND", "sg-captcha 202"
    try:
        link = json.loads(r["body"])[0]["link"]
    except Exception:
        return "FAIL", f"could not list WordPress pages (HTTP {r['code']})"
    if not link.startswith(SITE + "/"): return "FAIL", "WordPress page link not on the site"
    p = fetch(link)
    if challenged(p): return "BLIND", "sg-captcha 202"
    if p["code"] != 200: return "FAIL", f"WordPress page HTTP {p['code']}"
    if GTM not in p["body"]: return "FAIL", "WordPress page loads but GTM-KQTCCWB missing"
    return "OK", "WordPress page has GTM"

def registry(test_url):
    core = {"home": page_check(SITE + "/"), "hub flat-tire-change": page_check(SITE + "/flat-tire-change.html"),
            "404 page": notfound_check, "reviews.json freshness": reviews_check}
    full = {}
    for i, u in enumerate(ADS_URLS, 1):
        full[f"Ads URL {i:02d}"] = page_check(u)
    for h in HUBS:
        full[f"hub {h}"] = page_check(f"{SITE}/{h}.html")
    full["city page (Ontario)"] = page_check(SITE + "/roadside-assistance-ontario.html")
    full["combo page (flat tire, Ontario)"] = page_check(SITE + "/flat-tire-change-ontario.html")
    q = "?gclid=mfc-monitor&utm_source=mfc-monitor"
    full["redirect http->https keeps query"] = redirect_check(
        f"http://mfcroadsideassistance.com/flat-tire-change.html{q}", 301, f"{SITE}/flat-tire-change.html{q}")
    full["redirect /gps"] = redirect_check(SITE + "/gps", 302, SITE + "/my-location.html")
    full["redirect www -> apex"] = redirect_check("https://www.mfcroadsideassistance.com/", 301, SITE + "/")
    full["WordPress page GTM"] = wp_gtm_check
    test = {"TEST probe": page_check_status_only(test_url)} if test_url else {}
    return core, full, test

def page_check_status_only(url):
    def run():
        r = fetch(url)
        if challenged(r): return "BLIND", "sg-captcha 202"
        if r["err"]: return "FAIL", f"no response ({r['err']})"
        return ("OK", f"HTTP {r['code']}") if r["code"] == 200 else ("FAIL", f"HTTP {r['code']}")
    return run

def tg(text):
    tok, chat = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", "")
    if not tok or not chat:
        print("telegram: not configured; would send:", text.splitlines()[0])
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=20)
        print("telegram: sent:", text.splitlines()[0])
        return True
    except Exception as e:
        print(f"telegram: send failed ({type(e).__name__})")   # never the URL (it holds the token)
        return False

def iso(d): return d.isoformat(timespec="seconds")
def since(s, now): return int((now - datetime.fromisoformat(s)).total_seconds() // 60)

def main():
    now = datetime.now(timezone.utc)
    test_url = os.environ.get("TEST_URL", "").strip()
    force_full = os.environ.get("FORCE_FULL", "").lower() == "true"
    force_hb = os.environ.get("FORCE_HEARTBEAT", "").lower() == "true"
    try:
        st = json.load(open(STATE_PATH))
    except Exception:
        st = {}
    st.setdefault("checks", {})
    core, full, test = registry(test_url)
    every = {**core, **full, **test}
    do_full = force_full or not st.get("last_full") or now - datetime.fromisoformat(st["last_full"]) >= FULL_EVERY
    ids = list(core) + (list(full) if do_full else []) + list(test)
    ids += [i for i, c in st["checks"].items() if i in every and i not in ids and (c.get("fails") or c.get("alerted"))]
    print(f"run {iso(now)} | tier {'FULL' if do_full else 'CORE'} | checks {len(ids)}")
    results = {}
    for i in ids:
        try:
            results[i] = every[i]()
        except Exception as e:
            results[i] = ("FAIL", f"check error ({type(e).__name__})")
        print(f"  {results[i][0]:5s} {i} | {results[i][1]}")
    if do_full:
        st["last_full"] = iso(now)

    confirmed, recovered = {"real": [], "test": []}, {"real": [], "test": []}
    for i, (status, detail) in results.items():
        c = st["checks"].setdefault(i, {"fails": 0, "oks": 0, "alerted": False})
        c["last"], c["detail"], c["at"] = status, detail, iso(now)
        if status == "BLIND":
            continue
        group = "test" if i.startswith("TEST") else "real"
        if status == "FAIL":
            c["fails"] += 1; c["oks"] = 0
            c.setdefault("first_fail", iso(now))
            if c["fails"] >= CONFIRM_FAILS and not c["alerted"]:
                c["alerted"] = True; confirmed[group].append(i)
        else:
            c["oks"] += 1; c["fails"] = 0
            if c["alerted"] and c["oks"] >= CONFIRM_OKS:
                recovered[group].append((i, since(c["first_fail"], now)))
                c["alerted"] = False; c.pop("first_fail", None)
            elif not c["alerted"]:
                c.pop("first_fail", None)
    for i in [i for i, c in st["checks"].items() if i.startswith("TEST") and not c["alerted"] and not c.get("fails")]:
        if i not in test: st["checks"].pop(i, None)

    for group, prefix in (("real", ""), ("test", "🧪 TEST — ")):
        alerted = [i for i, c in st["checks"].items() if c.get("alerted") and (i.startswith("TEST") == (group == "test"))]
        key = "last_alert_" + group
        if confirmed[group]:
            lines = [f"• {i}: {st['checks'][i]['detail']} (failing {since(st['checks'][i]['first_fail'], now)} min)" for i in alerted]
            if tg(f"{prefix}🔴 MFC SITE PROBLEM — {len(alerted)} check(s) failing\n" + "\n".join(lines)):
                st[key] = iso(now)
        elif alerted and (not st.get(key) or now - datetime.fromisoformat(st[key]) >= REPEAT_EVERY):
            lines = [f"• {i}: {st['checks'][i]['detail']} (since {since(st['checks'][i]['first_fail'], now)} min)" for i in alerted]
            if tg(f"{prefix}🔴 STILL FAILING — {len(alerted)} check(s)\n" + "\n".join(lines)):
                st[key] = iso(now)
        if recovered[group]:
            tg(f"{prefix}✅ RECOVERED\n" + "\n".join(f"• {i} (was failing ~{m} min)" for i, m in recovered[group]))
        if not alerted:
            st.pop(key, None)

    real = {i: r for i, r in results.items() if not i.startswith("TEST")}
    if real and all(s == "BLIND" for s, _ in real.values()):
        st.setdefault("blind_since", iso(now))
        b = datetime.fromisoformat(st["blind_since"])
        if now - b >= BLIND_ALERT_AFTER and (not st.get("blind_alert") or now - datetime.fromisoformat(st["blind_alert"]) >= BLIND_REPEAT):
            if tg(f"🟠 Monitor can't see the site — every check got SiteGround's challenge (HTTP 202) since {b.astimezone(PT):%b %d %H:%M} PT. "
                  "This is the monitor being blocked, not the site being down. Fix: ask SiteGround to allowlist the monitor."):
                st["blind_alert"] = iso(now)
    elif real:
        if st.get("blind_alert"):
            tg("👁️ Monitor can see the site again.")
        st.pop("blind_since", None); st.pop("blind_alert", None)

    lu = REVIEWS_SEEN.get("last_updated")
    if lu and results.get("reviews.json freshness", ("",))[0] == "OK" and not st.get("gate_announced") \
            and lu > GATE_AFTER and 7 <= lu.hour <= 10:
        if tg(f"🟢 Nightly header gate PASSED — reviews.json updated {iso(lu)} by the scheduled Make run "
              f"(X-MFC-Reviews-Key header), googleTotal {REVIEWS_SEEN['total']}. Removing ?key= support is now unblocked "
              "(it still needs its own approved change)."):
            st["gate_announced"] = iso(now)

    now_pt = now.astimezone(PT)
    due = (now_pt.hour, now_pt.minute) >= HEARTBEAT_PT and st.get("last_heartbeat") != f"{now_pt:%Y-%m-%d}"
    if due or force_hb:
        failing = sum(1 for c in st["checks"].values() if c.get("alerted"))
        rv = REVIEWS_SEEN
        rtxt = f"googleTotal {rv['total']}, updated {rv['age_h']} h ago" if rv else "not checked this run"
        gate = "PASSED" if st.get("gate_announced") else "pending"
        ok = sum(1 for s, _ in real.values() if s == "OK")
        if tg(f"💚 MFC site monitor alive{' (manual check)' if force_hb and not due else ''} — {now_pt:%a %b %d}\n"
              f"Last run: {ok}/{len(real)} checks OK, {failing} alerting. Reviews: {rtxt}. Nightly header gate: {gate}.") and due:
            st["last_heartbeat"] = f"{now_pt:%Y-%m-%d}"

    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    json.dump(st, open(STATE_PATH, "w"), indent=2, sort_keys=True)
    print("state saved")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"monitor crashed: {type(e).__name__}")
        tg(f"🟠 MFC site monitor crashed ({type(e).__name__}) — checks did not run. See the GitHub Actions log.")
        raise
