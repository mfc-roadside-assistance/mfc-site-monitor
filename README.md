# mfc-site-monitor

Watches **mfcroadsideassistance.com** from GitHub Actions (not from the office Mac)
and posts to the private **Roadside Website Alerts** Telegram chat.

## What it checks
- **Every run (~15 min):** homepage, one service hub, a made-up URL (must be a real
  404 with the MFC 404 page), and `reviews.json` freshness (`googleTotal` in range,
  `lastUpdated` within 26 h).
- **Every ~4 h (full sweep):** the 10 Google Ads final URLs, all 6 service hubs, a city
  page, a combo page, http→https (query kept), `/gps`, www→apex, and GTM on a
  WordPress page.
- Any check that is failing is re-checked every run.

## Alert rules
- A check must fail **2 runs in a row** before it alerts (one blip never alerts).
- Alerts are grouped into one message; a still-failing incident is repeated at most
  every **2 h**; **✅ RECOVERED** after 2 passing runs.
- SiteGround's bot challenge (HTTP 202 + `sg-captcha`) is **BLIND**, never "down".
  If every check is BLIND for 2 h, one low-priority "monitor can't see the site" note.
  The monitor uses an honest user agent and never tries to get past the challenge.
- Daily heartbeat after ~8:30 AM Pacific; silence means the monitor is broken.
- Announces once when the nightly reviews run on the header key has passed.

## Security
No secrets in this repo. The bot token and chat id are encrypted Actions secrets
(`WEBSITE_TELEGRAM_BOT_TOKEN`, `WEBSITE_TELEGRAM_CHAT_ID`). Logs print check names,
statuses and HTTP codes only — never page bodies or the token. State (statuses and
timestamps only) is kept on the `monitor-state` branch.

Manual run: Actions → site-monitor → Run workflow (optional TEST probe URL, which is
labelled TEST in every message).
