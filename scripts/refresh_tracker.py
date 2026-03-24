#!/usr/bin/env python3
"""
Refresh Tsetemi's Slack Mention Action Tracker canvas.

Searches for new @Tsetemi mentions, classifies actionable ones using Claude,
and prepends them into the right priority section of the canvas.
"""

import argparse
import datetime
import json
import os
import sys
import time

import anthropic
import requests

SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
CANVAS_ID = "F0APFE4JLEL"
USER_ID = "U04Q38S5UN7"

PRIORITY_HEADER_TEXT = {
    "critical":       ":fire: Critical Priority",
    "high":           ":warning: High Priority",
    "medium_access":  ":gear: Medium Priority",
    "medium_process": ":bulb: Medium Priority",
    "low":            ":speech_balloon: Low Priority",
}

SLACK_HEADERS = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}


# ── Slack helpers ──────────────────────────────────────────────────────────────

def slack_get(endpoint: str, params: dict) -> dict:
    r = requests.get(f"https://slack.com/api/{endpoint}",
                     headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                     params=params)
    r.raise_for_status()
    return r.json()


def slack_post(endpoint: str, payload: dict) -> dict:
    r = requests.post(f"https://slack.com/api/{endpoint}",
                      headers=SLACK_HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


# ── Search ─────────────────────────────────────────────────────────────────────

def search_mentions(hours_back: int) -> list[dict]:
    cutoff = time.time() - hours_back * 3600
    after_date = datetime.datetime.utcfromtimestamp(cutoff).strftime("%Y-%m-%d")

    data = slack_get("search.messages", {
        "query": f"<@{USER_ID}> after:{after_date}",
        "sort": "timestamp",
        "sort_dir": "desc",
        "count": 100,
    })
    if not data.get("ok"):
        print(f"[ERROR] Slack search failed: {data.get('error')}", file=sys.stderr)
        return []

    results = []
    for msg in data.get("messages", {}).get("matches", []):
        ts = float(msg.get("ts", 0))
        if ts < cutoff:
            continue
        results.append({
            "channel":   msg.get("channel", {}).get("name", "unknown"),
            "user_id":   msg.get("user", ""),
            "from":      msg.get("username") or msg.get("user", "unknown"),
            "text":      msg.get("text", ""),
            "permalink": msg.get("permalink", ""),
            "ts":        ts,
            "date":      datetime.datetime.utcfromtimestamp(ts).strftime("%b %d"),
        })
    return results


# ── Classify with Claude ───────────────────────────────────────────────────────

def classify_mentions(mentions: list[dict]) -> list[dict]:
    if not mentions:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    payload = [{"i": i, "channel": m["channel"], "from": m["from"],
                "text": m["text"][:400]} for i, m in enumerate(mentions)]

    prompt = f"""You are reviewing Slack messages mentioning @Tsetemi (Josephine Tuoyo, HR Experience Operations Manager at Deel).

For each message decide:
1. Does it require action from Tsetemi? An action means: she must answer a question, approve something, investigate an issue, provide input, or do a task.
   Skip: thank-yous, FYIs with no ask, messages she sent herself (user_id {USER_ID}), CC-only mentions with no direct ask.
2. If actionable, assign a priority:
   - critical  → urgent/broken systems, mass-impact issues
   - high      → important issues needing timely response (same day)
   - medium_access → access requests, permission approvals
   - medium_process → process decisions, policy questions
   - low       → input/feedback, low-urgency asks
3. Write a description ≤12 words of exactly what action is needed.

Messages:
{json.dumps(payload, indent=2)}

Reply with ONLY a JSON array, one object per message:
  actionable=true:  {{"i":<int>,"actionable":true,"priority":"critical|high|medium_access|medium_process|low","description":"..."}}
  actionable=false: {{"i":<int>,"actionable":false}}"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        classifications = json.loads(msg.content[0].text)
    except json.JSONDecodeError:
        # Try to extract JSON array from response
        text = msg.content[0].text
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            classifications = json.loads(text[start:end+1])
        else:
            print("[ERROR] Could not parse Claude response.", file=sys.stderr)
            return []

    actionable = []
    for item in classifications:
        if item.get("actionable"):
            m = mentions[item["i"]].copy()
            m["priority"] = item["priority"]
            m["description"] = item["description"]
            actionable.append(m)
    return actionable


# ── Canvas helpers ─────────────────────────────────────────────────────────────

def get_canvas_sections() -> dict:
    """Return {section_id: markdown_text} for the whole canvas."""
    data = slack_post("canvases.sections.lookup", {
        "canvas_id": CANVAS_ID,
        "criteria": {"section_types": ["any_header", "bulleted_list"]},
    })
    if not data.get("ok"):
        print(f"[WARN] Could not look up canvas sections: {data.get('error')}", file=sys.stderr)
        return {}
    return {s["id"]: s.get("content", "") for s in data.get("sections", [])}


def find_section_id(sections: dict, header_fragment: str) -> str | None:
    for sid, content in sections.items():
        if header_fragment.lower() in content.lower():
            return sid
    return None


def canvas_edit(changes: list[dict]) -> bool:
    data = slack_post("canvases.edit", {"canvas_id": CANVAS_ID, "changes": changes})
    if not data.get("ok"):
        print(f"[ERROR] Canvas edit failed: {data.get('error')}", file=sys.stderr)
        return False
    return True


# ── Duplicate detection ────────────────────────────────────────────────────────

def get_tracked_permalinks() -> set[str]:
    """Pull all content from canvas and extract permalinks already tracked."""
    data = slack_post("canvases.sections.lookup", {
        "canvas_id": CANVAS_ID,
        "criteria": {"section_types": ["bulleted_list"]},
    })
    tracked = set()
    for section in data.get("sections", []):
        content = section.get("content", "")
        for part in content.split("]("):
            url = part.split(")")[0]
            if "slack.com/archives" in url:
                tracked.add(url.strip())
    return tracked


# ── Main update logic ──────────────────────────────────────────────────────────

def update_canvas(actionable: list[dict]) -> None:
    tracked = get_tracked_permalinks()
    new_items = [m for m in actionable if m["permalink"] not in tracked]

    if not new_items:
        print("No new items to add to canvas.")
        return

    sections = get_canvas_sections()

    # Group by priority
    by_priority: dict[str, list] = {p: [] for p in PRIORITY_HEADER_TEXT}
    for item in new_items:
        by_priority.setdefault(item["priority"], []).append(item)

    changes = []
    for priority, items in by_priority.items():
        if not items:
            continue
        header_fragment = PRIORITY_HEADER_TEXT[priority]
        section_id = find_section_id(sections, header_fragment)

        markdown_lines = "\n".join(
            f"- [ ] **#{m['channel']}** · {m['from']} · {m['date']} — {m['description']} [→ Thread]({m['permalink']})"
            for m in items
        )

        if section_id:
            changes.append({
                "operation": "insert_after",
                "section_id": section_id,
                "document_content": {"type": "markdown", "markdown": markdown_lines},
            })
        else:
            # Fallback: append to end of canvas
            changes.append({
                "operation": "insert_at_end",
                "document_content": {"type": "markdown", "markdown": f"## {header_fragment}\n{markdown_lines}"},
            })

    if not canvas_edit(changes):
        sys.exit(1)

    print(f"Added {len(new_items)} new item(s) to canvas.")


def update_last_refreshed() -> None:
    """Update the intro paragraph with today's date and current time."""
    now_utc = datetime.datetime.utcnow()
    date_str = now_utc.strftime("%B %d, %Y")
    time_str = now_utc.strftime("%H:%M UTC")

    sections = get_canvas_sections()
    sid = find_section_id(sections, "Last refreshed")
    if not sid:
        print("[WARN] Could not find 'Last refreshed' section.", file=sys.stderr)
        return

    canvas_edit([{
        "operation": "replace",
        "section_id": sid,
        "document_content": {
            "type": "markdown",
            "markdown": (
                f"**Last refreshed:** {date_str} at {time_str} — "
                "Auto-refreshes daily at 8 AM CET and hourly Mon–Fri 8 AM–6 PM CET. "
                "Check off items by clicking the checkbox once actioned."
            ),
        },
    }])


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh Slack mention tracker canvas.")
    parser.add_argument("--hours", type=int, default=1,
                        help="How many hours back to search (default: 1)")
    args = parser.parse_args()

    print(f"[{datetime.datetime.utcnow().isoformat()}] Searching past {args.hours}h for @{USER_ID} mentions...")
    mentions = search_mentions(args.hours)
    print(f"  Found {len(mentions)} mention(s).")

    if mentions:
        print("  Classifying with Claude...")
        actionable = classify_mentions(mentions)
        print(f"  {len(actionable)} actionable item(s).")
        update_canvas(actionable)

    update_last_refreshed()
    print("Done.")


if __name__ == "__main__":
    main()
