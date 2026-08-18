"""Mayday — flight-disruption rescue agent."""

from google.adk.agents import Agent

# Temporary in-memory data; replaced by the airline backend API.
FAKE_FLIGHTS = {
    "UA482": {
        "flight": "UA482",
        "route": "IAD -> DEN",
        "scheduled_departure": "2026-08-17T18:05:00-04:00",
        "status": "CANCELLED",
        "reason": "crew availability",
        "gate": "C17",
    },
    "UA118": {
        "flight": "UA118",
        "route": "IAD -> DEN",
        "scheduled_departure": "2026-08-17T21:40:00-04:00",
        "status": "ON_TIME",
        "reason": None,
        "gate": "C22",
    },
    "DL201": {
        "flight": "DL201",
        "route": "DCA -> ATL",
        "scheduled_departure": "2026-08-17T19:15:00-04:00",
        "status": "DELAYED",
        "reason": "late inbound aircraft",
        "gate": "B4",
    },
}


def get_flight_status(flight_no: str) -> dict:
    """Look up the current status of a flight by flight number.

    Args:
        flight_no: The flight number, e.g. "UA482". Case-insensitive.

    Returns:
        A dict with flight, route, scheduled_departure, status
        (ON_TIME | DELAYED | CANCELLED), reason, and gate.
        If the flight is not found, returns {"error": ...}.
    """
    flight = FAKE_FLIGHTS.get(flight_no.strip().upper())
    if flight is None:
        return {"error": f"No flight found with number {flight_no!r}."}
    return flight


root_agent = Agent(
    name="mayday",
    model="gemini-3.6-flash",
    description="Flight-disruption rescue agent that helps stranded passengers.",
    instruction=(
        "You are Mayday, a calm, efficient flight-disruption assistant for "
        "passengers whose travel plans just fell apart.\n"
        "\n"
        "Rules:\n"
        "1. When a passenger mentions a flight, ALWAYS call get_flight_status "
        "before saying anything about that flight. Never guess or invent "
        "flight information.\n"
        "2. If a flight is cancelled or delayed, acknowledge the disruption "
        "briefly and empathetically (one sentence, no groveling), then focus "
        "on what you can do next.\n"
        "3. If the tool returns an error, tell the passenger plainly and ask "
        "them to double-check the flight number. Never expose raw errors or "
        "internal details.\n"
        "4. Stay on topic: flights and travel disruption. Politely decline "
        "anything else.\n"
        "\n"
        "You cannot rebook flights yet — if asked, say that rebooking is "
        "coming soon and offer the flight status information you do have."
    ),
    tools=[get_flight_status],
)
