"""Send email alert when high-risk or Israel-relevant alerts are found today.

Usage (called by GitHub Actions after daily pipeline):
  python3 scripts/notify.py --date 2026-06-16
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from scripts.generate_dashboard import _absolute_score

SCORE_THRESHOLD = 8.5
RECIPIENTS = [
    "yuvalreisner96@gmail.com",  # TODO: change to matan.shiner@moh.gov.il for production
]
SENDER = "FoodSafe Alerts <onboarding@resend.dev>"
RESEND_API_URL = "https://api.resend.com/emails"
DASHBOARD_URL = "https://yuvalreisner.github.io/foodalert-risk-monitoring-system/"


def fetch_todays_alerts(conn, ref_date: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.source_id, a.hazard_specific, a.hazard_category,
               a.severity_normalized, a.origin_country, a.distribution_countries,
               a.israel_relevance_flag, a.record_url,
               s.bi_encoder_score
        FROM alerts a JOIN alert_scores s ON s.alert_id = a.id
        WHERE a.source_published_date = ?
        ORDER BY s.bi_encoder_score DESC
        """,
        (ref_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def should_notify(alert: dict) -> tuple[bool, list[str]]:
    reasons = []
    score = _absolute_score(alert["bi_encoder_score"])
    if score >= SCORE_THRESHOLD:
        reasons.append(f"Score {score:.1f}/10 ≥ {SCORE_THRESHOLD}")
    if alert["israel_relevance_flag"]:
        reasons.append("Israel relevance")
    return bool(reasons), reasons


def build_html(alerts_to_send: list[tuple[dict, list[str]]], ref_date: str) -> str:
    rows_html = ""
    for alert, reasons in alerts_to_send:
        score = _absolute_score(alert["bi_encoder_score"])
        score_color = "#c0392b" if score >= 9 else "#d35400" if score >= 8 else "#7f8c8d"
        israel_tag = (
            '<span style="background:#1a5276;color:#fff;border-radius:4px;'
            'padding:1px 6px;font-size:11px;margin-left:4px">🇮🇱 Israel</span>'
            if alert["israel_relevance_flag"] else ""
        )
        reason_tags = " · ".join(reasons)
        url = alert.get("record_url") or ""
        card_id = "alert-" + alert["id"].replace("::", "-").replace("/", "-").replace(" ", "-")
        import re
        card_id = "alert-" + re.sub(r'[^a-zA-Z0-9]', '-', alert["id"])
        dashboard_link = f'{DASHBOARD_URL}#{card_id}'
        link = (
            f'<a href="{dashboard_link}" style="color:#2471a3;margin-right:12px">View in dashboard ↗</a>'
            + (f'<a href="{url}" style="color:#888;font-size:11px">Source ↗</a>' if url else "")
        )
        hazard = alert.get("hazard_specific") or alert.get("hazard_category") or "—"
        origin = alert.get("origin_country") or "—"

        rows_html += f"""
        <tr>
          <td style="padding:14px 16px;border-bottom:1px solid #eee;vertical-align:top">
            <div style="margin-bottom:6px">
              <span style="font-size:18px;font-weight:700;color:{score_color};
                font-family:monospace">{score:.1f}/10</span>
              {israel_tag}
            </div>
            <div style="font-weight:600;font-size:14px;margin-bottom:4px;color:#1a1f2e">
              {alert['title'] or alert['id']}
            </div>
            <div style="font-size:12px;color:#5a6478;margin-bottom:4px">
              <b>Hazard:</b> {hazard} &nbsp;·&nbsp; <b>Origin:</b> {origin}
            </div>
            <div style="font-size:11px;color:#888;margin-bottom:4px">
              Trigger: {reason_tags}
            </div>
            <div style="font-size:12px">{link}</div>
          </td>
        </tr>"""

    n = len(alerts_to_send)
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f5f7fa;margin:0;padding:20px">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:10px;
    box-shadow:0 2px 12px rgba(0,0,0,.08);overflow:hidden">

    <div style="background:#1a1f2e;padding:20px 24px">
      <div style="color:#fff;font-size:20px;font-weight:700">🍽️ FoodSafe Intelligence</div>
      <div style="color:#aab;font-size:13px;margin-top:4px">
        Daily Alert — {ref_date} &nbsp;·&nbsp; {n} alert{"s" if n != 1 else ""} require attention
      </div>
    </div>

    <table style="width:100%;border-collapse:collapse">
      {rows_html}
    </table>

    <div style="padding:16px 24px;background:#f8f9fb;border-top:1px solid #eee">
      <a href="https://yuvalreisner.github.io/foodalert-risk-monitoring-system/"
         style="color:#2471a3;font-size:13px">
        View full dashboard ↗
      </a>
      <span style="color:#bbb;font-size:11px;margin-left:16px">
        Alerts with score ≥ {SCORE_THRESHOLD}/10 or Israel relevance
      </span>
    </div>
  </div>
</body>
</html>"""


def send_email(api_key: str, subject: str, html: str) -> None:
    payload = json.dumps({
        "from": SENDER,
        "to": RECIPIENTS,
        "subject": subject,
        "html": html,
    }).encode()

    req = urllib.request.Request(
        RESEND_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
        print(f"Email sent. Response: {body}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today()))
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent without actually sending")
    args = parser.parse_args()

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key and not args.dry_run:
        print("RESEND_API_KEY not set — skipping email notification.")
        sys.exit(0)

    conn = db.connect()
    alerts = fetch_todays_alerts(conn, args.date)

    if not alerts:
        print(f"No alerts found for {args.date} — nothing to send.")
        return

    to_send = [(a, reasons) for a in alerts if (should_notify(a)[0]) for _, reasons in [should_notify(a)]]
    # rebuild cleanly
    to_send = [(a, should_notify(a)[1]) for a in alerts if should_notify(a)[0]]

    if not to_send:
        print(f"No alerts above threshold on {args.date} — no email sent.")
        return

    print(f"Found {len(to_send)} alert(s) to notify:")
    for a, reasons in to_send:
        score = _absolute_score(a["bi_encoder_score"])
        print(f"  {score:.1f}/10  [{', '.join(reasons)}]  {a['title'][:70]}")

    subject = f"🚨 FoodSafe Alert — {len(to_send)} high-risk alert(s) on {args.date}"
    html = build_html(to_send, args.date)

    if args.dry_run:
        print("\n-- DRY RUN: email not sent --")
        print(f"Subject: {subject}")
        print(f"To: {RECIPIENTS}")
        return

    send_email(api_key, subject, html)


if __name__ == "__main__":
    main()
