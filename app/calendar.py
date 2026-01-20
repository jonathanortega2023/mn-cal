import httpx
from datetime import date, datetime, timedelta, timezone
from typing import Set
import logging
import os

logger = logging.getLogger(__name__)

# Venue names (used by main.py)
CALENDARS = {
    "garden": "garden",
    "ballroom": "ballroom",
}

TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Token cache
_cached_token: str | None = None
_token_expires_at: datetime | None = None


def _get_config():
    """Get configuration from environment at runtime."""
    return {
        "client_id": os.environ.get("MS_CLIENT_ID", ""),
        "client_secret": os.environ.get("MS_CLIENT_SECRET", ""),
        "refresh_token": os.environ.get("MS_REFRESH_TOKEN", ""),
        "calendar_ids": {
            "garden": os.environ.get("CALENDAR_ID_GARDEN", ""),
            "ballroom": os.environ.get("CALENDAR_ID_BALLROOM", ""),
        },
    }


async def get_access_token() -> str | None:
    """Get a valid access token, refreshing if necessary."""
    global _cached_token, _token_expires_at

    config = _get_config()
    if not config["refresh_token"]:
        logger.warning("No MS_REFRESH_TOKEN configured")
        return None

    # Check if cached token is still valid (with 5 min buffer)
    if _cached_token and _token_expires_at:
        if datetime.now(timezone.utc) < _token_expires_at - timedelta(minutes=5):
            return _cached_token

    # Refresh the token
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                    "refresh_token": config["refresh_token"],
                    "grant_type": "refresh_token",
                    "scope": "offline_access Calendars.Read",
                },
            )
            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.status_code} - {response.text}")
                return None

            data = response.json()
            _cached_token = data["access_token"]
            _token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))

            return _cached_token
    except Exception as e:
        logger.error(f"Failed to refresh access token: {e}")
        return None


async def fetch_calendar_events_graph(calendar_id: str, start_date: date, end_date: date) -> Set[date]:
    """Fetch calendar events using Microsoft Graph API."""
    access_token = await get_access_token()
    if not access_token:
        return set()

    booked_dates: Set[date] = set()

    # Use /events endpoint to get all events (no date range limit)
    if calendar_id:
        url = f"{GRAPH_API_BASE}/me/calendars/{calendar_id}/events"
    else:
        url = f"{GRAPH_API_BASE}/me/calendar/events"

    params = {
        "$select": "subject,start,end,isAllDay,showAs",
        "$top": "500",
        "$orderby": "start/dateTime",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while url:
                response = await client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                params = None  # Clear params after first request (nextLink includes them)

                if response.status_code != 200:
                    logger.error(f"Graph API error: {response.status_code} - {response.text}")
                    break

                data = response.json()
                events = data.get("value", [])

                for event in events:
                    # Process all all-day events (any all-day event means the venue is booked)
                    if not event.get("isAllDay", False):
                        continue

                    # Parse start and end dates
                    start_str = event.get("start", {}).get("dateTime", "")
                    end_str = event.get("end", {}).get("dateTime", "")

                    if not start_str or not end_str:
                        continue

                    # All-day events come as date strings like "2026-01-20T00:00:00.0000000"
                    event_start = datetime.fromisoformat(start_str.replace("Z", "")).date()
                    event_end = datetime.fromisoformat(end_str.replace("Z", "")).date()

                    # Add all dates in the range (end date is exclusive for all-day events)
                    current = event_start
                    while current < event_end:
                        if start_date <= current <= end_date:
                            booked_dates.add(current)
                        current += timedelta(days=1)

                # Handle pagination
                url = data.get("@odata.nextLink")

    except Exception as e:
        logger.error(f"Failed to fetch events from Graph API: {e}")

    return booked_dates


async def get_booked_dates(venue: str, months_ahead: int = 36) -> Set[date]:
    """Fetch calendar for a venue and return set of booked dates."""
    if venue not in CALENDARS:
        logger.error(f"Unknown venue: {venue}")
        return set()

    today = date.today()
    end_date = today + timedelta(days=months_ahead * 30)

    config = _get_config()

    if not config["refresh_token"]:
        logger.error("No MS_REFRESH_TOKEN configured")
        return set()

    calendar_id = config["calendar_ids"].get(venue, "")
    booked = await fetch_calendar_events_graph(calendar_id, today, end_date)
    logger.info(f"Fetched {len(booked)} booked dates for {venue} via Graph API")
    return booked
