#!/usr/bin/env python3
"""
Zendesk First Response Time (FRT) Breach Headsup Notifier.

Fetches tickets from the "First response delayed" Zendesk view and sends
Headsup notifications to assignees (and their managers) to prompt a public response.

Zendesk View: https://letsdeel.zendesk.com/agent/filters/44845984680465
"""

import datetime
import json
import os
import sys
from typing import Any

import requests

# Environment variables
ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "letsdeel")
ZENDESK_EMAIL = os.environ["ZENDESK_EMAIL"]
ZENDESK_API_TOKEN = os.environ["ZENDESK_API_TOKEN"]
HEADSUP_WEBHOOK_URL = os.environ["HEADSUP_WEBHOOK_URL"]

# Zendesk View ID for "First response delayed"
FRT_BREACH_VIEW_ID = "44845984680465"

# Custom field IDs (update these based on your Zendesk configuration)
DIRECT_MANAGER_FIELD_ID = os.environ.get("ZENDESK_DIRECT_MANAGER_FIELD_ID")


def zendesk_get(endpoint: str, params: dict | None = None) -> dict:
    """Make authenticated GET request to Zendesk API."""
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/{endpoint}"
    auth = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)
    r = requests.get(url, auth=auth, params=params)
    r.raise_for_status()
    return r.json()


def get_view_tickets() -> list[dict]:
    """Fetch all tickets from the FRT breach view."""
    data = zendesk_get(f"views/{FRT_BREACH_VIEW_ID}/tickets.json")
    return data.get("tickets", [])


def get_user_info(user_id: int) -> dict[str, Any]:
    """Get user details from Zendesk."""
    if not user_id:
        return {}
    try:
        data = zendesk_get(f"users/{user_id}.json")
        return data.get("user", {})
    except requests.exceptions.HTTPError:
        return {}


def get_assignee_manager(ticket: dict) -> str | None:
    """Extract the direct manager from ticket custom fields or user fields."""
    # Try to get manager from custom fields on the ticket
    if DIRECT_MANAGER_FIELD_ID:
        for field in ticket.get("custom_fields", []):
            if str(field.get("id")) == str(DIRECT_MANAGER_FIELD_ID):
                return field.get("value")

    # Fallback: try to get manager from assignee's user profile
    assignee_id = ticket.get("assignee_id")
    if assignee_id:
        user = get_user_info(assignee_id)
        # Check user_fields for manager info
        user_fields = user.get("user_fields", {})
        manager = user_fields.get("direct_manager") or user_fields.get("manager")
        if manager:
            return manager

    return None


def build_ticket_url(ticket_id: int) -> str:
    """Build the Zendesk ticket URL."""
    return f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{ticket_id}"


def send_headsup(ticket: dict, assignee_name: str, manager_name: str | None) -> bool:
    """Send a Headsup notification for an FRT breach."""
    ticket_id = ticket.get("id")
    ticket_url = build_ticket_url(ticket_id)

    payload = {
        "headsup_type": "Zendesk",
        "headsup_link": ticket_url,
        "description": "FRT breach - Provide a public first response",
        "team_member": assignee_name,
        "manager": manager_name,
        "ticket_id": ticket_id,
        "ticket_subject": ticket.get("subject", ""),
        "created_at": ticket.get("created_at"),
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }

    try:
        r = requests.post(
            HEADSUP_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to send headsup for ticket {ticket_id}: {e}", file=sys.stderr)
        return False


CACHE_FILE = os.path.join(os.path.dirname(__file__), ".frt_headsup_cache.json")


def get_processed_tickets_cache() -> set[int]:
    """Load previously processed ticket IDs to avoid duplicate notifications within 24 hours."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
                cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
                cutoff_ts = cutoff.timestamp()
                return {
                    int(k) for k, v in data.items()
                    if v > cutoff_ts
                }
        except (json.JSONDecodeError, IOError):
            pass
    return set()


def save_processed_ticket(ticket_id: int) -> None:
    """Save ticket ID to persistent cache with current timestamp."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                data = json.load(f)
        else:
            data = {}
    except (json.JSONDecodeError, IOError):
        data = {}

    # Prune entries older than 24 hours to keep the file small
    cutoff_ts = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).timestamp()
    data = {k: v for k, v in data.items() if v > cutoff_ts}

    data[str(ticket_id)] = datetime.datetime.utcnow().timestamp()

    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)


def main() -> None:
    print(f"[{datetime.datetime.utcnow().isoformat()}] Checking FRT breach view...")

    tickets = get_view_tickets()
    print(f"  Found {len(tickets)} ticket(s) in view.")

    if not tickets:
        print("No FRT breaches to process.")
        return

    processed = get_processed_tickets_cache()
    sent_count = 0
    skipped_count = 0

    for ticket in tickets:
        ticket_id = ticket.get("id")

        # Skip already processed tickets
        if ticket_id in processed:
            skipped_count += 1
            continue

        assignee_id = ticket.get("assignee_id")
        if not assignee_id:
            print(f"  Ticket {ticket_id}: No assignee, skipping.")
            continue

        # Get assignee info
        assignee = get_user_info(assignee_id)
        assignee_name = assignee.get("name") or assignee.get("email") or f"User {assignee_id}"

        # Get manager info
        manager_name = get_assignee_manager(ticket)

        # Send headsup
        if send_headsup(ticket, assignee_name, manager_name):
            print(f"  Ticket {ticket_id}: Headsup sent to {assignee_name}")
            save_processed_ticket(ticket_id)
            processed.add(ticket_id)
            sent_count += 1
        else:
            print(f"  Ticket {ticket_id}: Failed to send headsup")

    print(f"Done. Sent {sent_count} headsup(s), skipped {skipped_count} already processed.")


if __name__ == "__main__":
    main()
