import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .calendar import get_booked_dates, CALENDARS

load_dotenv()

VENUES = list(CALENDARS.keys())

# Venue lease end dates (availability not shown beyond these dates)
VENUE_LEASE_END = {
    "ballroom": date(2030, 3, 31),
}

# Cache for booked dates per venue
_cache: dict = {
    venue: {"booked_dates": set(), "last_updated": None}
    for venue in VENUES
}

REFRESH_INTERVAL_HOURS = 4


async def refresh_cache():
    """Refresh the cached booked dates for all venues."""
    for venue in VENUES:
        _cache[venue]["booked_dates"] = await get_booked_dates(venue, months_ahead=48)
        _cache[venue]["last_updated"] = datetime.now(timezone.utc)


async def periodic_refresh():
    """Background task to refresh cache every 4 hours."""
    while True:
        await refresh_cache()
        await asyncio.sleep(REFRESH_INTERVAL_HOURS * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    # Initial fetch on startup
    await refresh_cache()
    # Start background refresh task
    task = asyncio.create_task(periodic_refresh())
    yield
    # Cancel background task on shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Midnight Venue Availability", lifespan=lifespan)

# Mount static files
static_path = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_path), name="static")


def get_weekend_days_for_year(venue: str, year: int) -> list[dict]:
    """Generate list of weekend days (Fri/Sat/Sun) for a specific year."""
    today = date.today()
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)

    if year == today.year:
        start_date = today

    # Respect venue lease end date
    lease_end = VENUE_LEASE_END.get(venue)
    if lease_end and end_date > lease_end:
        end_date = lease_end

    days = []
    booked_dates: Set[date] = _cache[venue]["booked_dates"]

    # If start_date is after lease end, return empty list
    if lease_end and start_date > lease_end:
        return days

    current = start_date
    while current <= end_date:
        # Friday = 4, Saturday = 5, Sunday = 6
        if current.weekday() in (4, 5, 6):
            day_name = ["", "", "", "", "friday", "saturday", "sunday"][current.weekday()]
            days.append({
                "date": current.isoformat(),
                "day": day_name,
                "available": current not in booked_dates,
            })
        current += timedelta(days=1)

    return days


@app.get("/health")
async def health_check():
    """Health check endpoint for container orchestration."""
    return {"status": "healthy"}


@app.get("/")
async def root():
    """Redirect to default venue."""
    return RedirectResponse(url="/garden")


@app.get("/api/availability/{venue}")
async def get_availability(venue: str, year: Optional[int] = None):
    """Return weekend availability for a venue and year."""
    if venue not in VENUES:
        raise HTTPException(status_code=404, detail=f"Venue not found. Valid venues: {', '.join(VENUES)}")

    today = date.today()
    if year is None:
        year = today.year

    # Validate year is within range (current year to +4 years, or lease end)
    max_year = today.year + 4
    lease_end = VENUE_LEASE_END.get(venue)
    if lease_end:
        max_year = min(max_year, lease_end.year)

    if year < today.year or year > max_year:
        raise HTTPException(status_code=400, detail=f"Year must be between {today.year} and {max_year}")

    days = get_weekend_days_for_year(venue, year)

    return {
        "venue": venue,
        "year": year,
        "minYear": today.year,
        "maxYear": max_year,
        "leaseEnd": lease_end.isoformat() if lease_end else None,
        "lastUpdated": _cache[venue]["last_updated"].isoformat() if _cache[venue]["last_updated"] else None,
        "days": days,
    }


@app.get("/{venue}")
async def venue_page(venue: str):
    """Serve the calendar page for a specific venue."""
    if venue not in VENUES:
        raise HTTPException(status_code=404, detail=f"Venue not found. Valid venues: {', '.join(VENUES)}")
    return FileResponse(static_path / "index.html")
