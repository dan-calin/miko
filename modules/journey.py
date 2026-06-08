"""
modules/journey.py — Location-aware Journey Planning for Miko.

When GOOGLE_MAPS_API_KEY is set:
  - search_nearby_places → Google Places API (New) with dynamic SKU-tier field masks
  - calculate_route       → Google Routes API (TRAFFIC_AWARE) + spoken summary

When no API key is configured, both tools fall back to opening Google Maps in the
browser (fully functional, just no spoken distance/duration summary).

Home coordinates are resolved from HOME_COORDS env var, or auto-geocoded from
HOME_POSTCODE via Nominatim (OpenStreetMap, free, no key required) and cached.
"""

import json
import logging
import os
import urllib.parse
import urllib.request
import webbrowser
from functools import lru_cache

logger = logging.getLogger("miko.journey")

_PLACES_URL  = "https://places.googleapis.com/v1/places:searchText"
_ROUTES_URL  = "https://routes.googleapis.com/directions/v2:computeRoutes"
_NOMINATIM   = "https://nominatim.openstreetmap.org/search"

# ── Places API (New) type normalizer ─────────────────────────────────────────
# The new API uses different type names from the old one. Map common words/brands
# to valid includedTypes values so Gemini's free-text answers don't cause 400s.
_TYPE_MAP: dict[str, str] = {
    # Fast food / brands
    "mcdonald's": "fast_food_restaurant", "mcdonalds": "fast_food_restaurant",
    "fast_food":  "fast_food_restaurant", "fast food": "fast_food_restaurant",
    "kfc":        "fast_food_restaurant", "burger king": "fast_food_restaurant",
    "subway":     "fast_food_restaurant", "pizza":       "pizza_restaurant",
    "pizzerie":   "pizza_restaurant",
    # General food
    "restaurant": "restaurant", "restaurante": "restaurant",
    "mancare":    "restaurant", "mâncare":     "restaurant",
    "cafe":       "cafe",       "cafenea":     "cafe",       "coffee": "cafe",
    "bar":        "bar",        "pub":          "bar",
    "club":       "night_club", "nightclub":   "night_club", "discoteca": "night_club",
    # Transport / services
    "benzinarie": "gas_station", "benzinărie": "gas_station",
    "gas_station": "gas_station", "petrol":    "gas_station",
    "supermarket": "supermarket", "grocery":   "supermarket",
    "farmacie":    "pharmacy",    "pharmacy":  "pharmacy",
    "spital":      "hospital",    "hospital":  "hospital",
    # Leisure
    "parc":        "park",        "park":       "park",
    "cinema":      "movie_theater", "movie":    "movie_theater",
    "hotel":       "hotel",       "accommodation": "hotel",
    "atm":         "atm",
    "tourist":     "tourist_attraction", "turistic": "tourist_attraction",
    "nature":      "national_park",      "natura":   "national_park",
    "lake":        "lake",               "lac":      "lake",
}

def _normalize_types(place_types: list) -> list[str]:
    """Map free-text type names to valid Places API (New) type strings."""
    out = []
    for t in place_types:
        key = t.lower().strip()
        out.append(_TYPE_MAP.get(key, key))  # passthrough if not in map
    return out


# ── Field mask tiers (Google Places API SKU pricing) ─────────────────────────
_MASK_ESSENTIALS = "places.id,places.displayName,places.location,places.formattedAddress"
_MASK_PRO_EXTRA  = ",places.rating,places.userRatingCount"
_MASK_ENT_EXTRA  = ",places.currentOpeningHours"


TOOL_DECLARATIONS = [
    {
        "name": "search_nearby_places",
        "description": (
            "Caută locuri în apropiere (restaurante, cluburi, benzinarii, McDonald's, spitale etc.) "
            "folosind locația de acasă sau coordonate specificate. "
            "Folosește pentru: 'cel mai apropiat McDonald\\'s', 'restaurante bune în zonă', "
            "'cluburi deschise acum', 'benzinărie apropiată', 'supermarket lângă mine', "
            "'unde pot mânca', 'un loc mișto în zona asta'. "
            "Dacă user-ul cere locuri 'bune', 'cu rating' sau 'recomandate' → want_ratings=true. "
            "Dacă user-ul cere locuri 'deschise acum' sau întreabă de program → want_hours=true. "
            "IMPLICIT returnează DOAR lista (cu link), fără a deschide harta. Setează "
            "show_on_map=true DOAR dacă user-ul cere explicit să vadă harta / rezultatele pe "
            "ecran (ex: 'arată-mi pe hartă', 'deschide harta', 'show me on the map', 'bring them "
            "on my screen'). Altfel lasă-l false."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Ce caută user-ul — exact ce a spus: 'McDonald\\'s', 'KFC', "
                        "'benzinărie', 'farmacie', 'restaurant bun', 'club deschis'. "
                        "Folosește cuvântul/brandul exact, nu o categorie generică."
                    ),
                },
                "radius_meters": {
                    "type": "INTEGER",
                    "description": "Raza de căutare în metri (implicit: 3000, max: 50000).",
                },
                "lat": {
                    "type": "NUMBER",
                    "description": "Latitudine punct de căutare. Dacă lipsește → locația de acasă.",
                },
                "lng": {
                    "type": "NUMBER",
                    "description": "Longitudine punct de căutare. Dacă lipsește → locația de acasă.",
                },
                "want_ratings": {
                    "type": "BOOLEAN",
                    "description": "True pentru locuri 'bune', 'cu rating', 'recomandate'.",
                },
                "want_hours": {
                    "type": "BOOLEAN",
                    "description": "True dacă user-ul întreabă dacă sunt deschise acum / program.",
                },
                "max_results": {
                    "type": "INTEGER",
                    "description": "Numărul maxim de rezultate de returnat (implicit: 5, max: 20).",
                },
                "show_on_map": {
                    "type": "BOOLEAN",
                    "description": "True DOAR dacă user-ul cere explicit să vadă harta pe ecran. "
                                   "Implicit false — returnează doar lista + link.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "calculate_route",
        "description": (
            "Calculează o rută cu detalii (durată, distanță) și deschide Google Maps. "
            "Folosește pentru: 'cum ajung la', 'rută spre', 'navigare spre', "
            "'joy ride', 'plimbare cu mașina', 'vreau să mă plimb'. "
            "Pentru joy rides: apelează mai întâi search_nearby_places pentru waypoints "
            "interesante (parcuri, lacuri, autostrăzi panoramice), "
            "apoi calculate_route cu acele waypoints."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "destination": {
                    "type": "STRING",
                    "description": "Destinația finală (adresă, oraș sau loc).",
                },
                "origin": {
                    "type": "STRING",
                    "description": "Punct de plecare. Dacă lipsește → adresa de acasă.",
                },
                "waypoints": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Puncte intermediare pentru rute complexe sau joy rides.",
                },
                "mode": {
                    "type": "STRING",
                    "description": "Mod de transport: driving (implicit), walking, bicycling, transit.",
                    "enum": ["driving", "walking", "bicycling", "transit"],
                },
                "avoid_tolls": {
                    "type": "BOOLEAN",
                    "description": "True pentru a evita taxele de drum.",
                },
                "avoid_highways": {
                    "type": "BOOLEAN",
                    "description": "True pentru a evita autostrăzile (rute prin sate/natura).",
                },
            },
            "required": ["destination"],
        },
    },
    {
        "name": "plan_journey",
        "description": (
            "Deschide o rută simplă A→B în Google Maps (fără API key). "
            "Alternativă rapidă la calculate_route pentru rute simple."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "destination": {"type": "STRING", "description": "Destinația."},
                "origin":      {"type": "STRING", "description": "Punct de start (opțional)."},
                "mode": {
                    "type": "STRING",
                    "enum": ["driving", "walking", "bicycling", "transit"],
                },
            },
            "required": ["destination"],
        },
    },
]


# ── Highway / road queries (not searchable as Places) ────────────────────────
# Motorways are road infrastructure — the Places API has no records for them.
# Detect these queries and open a map view that visually highlights roads instead.
_HIGHWAY_KEYWORDS = {
    "highway", "motorway", "autostrada", "autostradă", "autostrade",
    "freeway", "expressway", "m6", "m42", "m5", "m1", "m40", "m45",
    "drum national", "dn", "autobahn",
}

def _is_highway_query(query: str) -> bool:
    q = query.lower()
    return any(kw in q for kw in _HIGHWAY_KEYWORDS)


def _highway_route(query: str) -> str:
    """Route to the nearest motorway via Routes API — roads aren't searchable Places."""
    q = query.lower()

    # Pick a specific motorway if named, otherwise use the nearest motorway services
    # (which sit at junction entries — perfect proxy for motorway access point)
    if "m6" in q:
        destination = "M6 Motorway, Birmingham"
    elif "m42" in q:
        destination = "M42 Motorway"
    elif "m5" in q:
        destination = "M5 Motorway"
    elif "m1" in q:
        destination = "M1 Motorway"
    elif "m40" in q:
        destination = "M40 Motorway"
    else:
        # Nearest motorway services = closest real place at a motorway junction
        destination = "nearest motorway services"

    logger.info(f"_highway_route: query='{query}' → routing to '{destination}'")
    return calculate_route(destination=destination, mode="driving")


# ── Location resolution ───────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _geocode_address(address: str) -> tuple | None:
    """Geocode an address via Nominatim (OSM). Cached — only runs once per session."""
    try:
        encoded = urllib.parse.quote_plus(address)
        url = f"{_NOMINATIM}?q={encoded}&format=json&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Miko/2.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        if data:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            logger.info(f"Geocoded '{address}' → ({lat}, {lon})")
            return lat, lon
    except Exception as e:
        logger.warning(f"Nominatim geocode failed for '{address}': {e}")
    return None


def _get_home_coords() -> tuple | None:
    """Return (lat, lng) from HOME_COORDS, or geocode HOME_POSTCODE as fallback."""
    raw = os.getenv("HOME_COORDS", "").strip()
    if raw:
        try:
            parts = raw.split(",")
            return float(parts[0].strip()), float(parts[1].strip())
        except Exception:
            pass
    postcode = os.getenv("HOME_POSTCODE", "").strip()
    if postcode:
        return _geocode_address(postcode)
    return None


def _get_home_address() -> str:
    """Return home address string used in browser URL fallbacks."""
    return os.getenv("HOME_POSTCODE", os.getenv("HOME_COORDS", "")).strip()


def _mobile_maps_link(query_or_dest: str, origin: str = "") -> str:
    """Return a tappable Google Maps URL that opens navigation on mobile."""
    if origin:
        enc_o = urllib.parse.quote_plus(origin)
        enc_d = urllib.parse.quote_plus(query_or_dest)
        return f"https://www.google.com/maps/dir/?api=1&origin={enc_o}&destination={enc_d}&travelmode=driving"
    else:
        enc = urllib.parse.quote_plus(query_or_dest)
        return f"https://www.google.com/maps/search/?api=1&query={enc}"


def _api_key() -> str:
    return os.getenv("GOOGLE_MAPS_API_KEY", "").strip()


# ── search_nearby_places ──────────────────────────────────────────────────────

def search_nearby_places(
    query: str,
    radius_meters: int = 3000,
    lat: float = None,
    lng: float = None,
    want_ratings: bool = False,
    want_hours: bool = False,
    max_results: int = 5,
    show_on_map: bool = False,
    # Legacy compat — ignored if query is provided
    place_types: list = None,
) -> str:
    """Find nearby places via Places API (New) searchText with dynamic field masks."""
    import requests as _req

    # Accept legacy place_types calls (from old sessions / command router redirects)
    if not query and place_types:
        query = place_types[0] if place_types else "restaurant"

    if not query:
        return "Spune-mi ce anume să caut, sefu."

    # Highways/motorways are roads, not places — route to nearest access via Routes API
    if _is_highway_query(query):
        return _highway_route(query)

    # Resolve home coordinates if not provided
    if lat is None or lng is None:
        coords = _get_home_coords()
        if coords:
            lat, lng = coords

    key = _api_key()
    if not key:
        logger.info("No GOOGLE_MAPS_API_KEY — using browser fallback")
        return _places_browser_fallback(query, lat, lng, show_on_map=show_on_map)

    # ── Dynamic field mask (SKU tier selection) ────────────────────────────────
    field_mask = _MASK_ESSENTIALS
    if want_ratings:
        field_mask += _MASK_PRO_EXTRA        # Pro tier: rating + review count
    if want_hours:
        field_mask += _MASK_ENT_EXTRA        # Enterprise tier: current opening hours

    body: dict = {
        "textQuery": query,
        "maxResultCount": min(max(1, max_results), 20),
        "rankPreference": "DISTANCE",
    }

    if lat is not None and lng is not None:
        # searchText supports circle only under locationBias, not locationRestriction
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(min(radius_meters, 50000)),
            }
        }

    logger.info(f"search_nearby_places: query='{query}' coords=({lat},{lng}) r={radius_meters}m mask={field_mask}")

    try:
        resp = _req.post(
            _PLACES_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": field_mask,
            },
            timeout=8,
        )
        if not resp.ok:
            logger.error(f"Places API {resp.status_code}: {resp.text[:500]}")
            return _places_browser_fallback(query, lat, lng, show_on_map=show_on_map)
        data = resp.json()
    except Exception as e:
        logger.error(f"Places API error: {e}")
        return _places_browser_fallback(query, lat, lng, show_on_map=show_on_map)

    places = data.get("places", [])

    if not places:
        return f"N-am găsit niciun '{query}' în raza de {radius_meters // 1000} km, sefu."

    lines = [f"Am găsit {len(places)} rezultate pentru '{query}' în apropiere:\n"]
    for i, p in enumerate(places, 1):
        name = p.get("displayName", {}).get("text", "Necunoscut")
        addr = p.get("formattedAddress", "")
        line = f"{i}. {name}"
        if addr:
            short_addr = addr.split(",")[0]
            line += f" — {short_addr}"
        if want_ratings and "rating" in p:
            count = p.get("userRatingCount", 0)
            line += f" | {p['rating']:.1f}/5 ({count} recenzii)"
        if want_hours and "currentOpeningHours" in p:
            is_open = p["currentOpeningHours"].get("openNow")
            if is_open is True:
                line += " | Deschis acum"
            elif is_open is False:
                line += " | Inchis acum"
        lines.append(line)

    lines.append(f"\n📍 {_mobile_maps_link(query)}")

    # Only pop the map open when the user explicitly asked to see it on screen.
    if show_on_map:
        home = _get_home_address()
        q = urllib.parse.quote_plus(f"{query} near {home}" if home else query)
        webbrowser.open(f"https://www.google.com/maps/search/{q}")

    return "\n".join(lines)


def _places_browser_fallback(
    query: str,
    lat: float = None,
    lng: float = None,
    show_on_map: bool = False,
) -> str:
    home = _get_home_address()
    if lat and lng:
        q   = urllib.parse.quote_plus(query)
        url = f"https://www.google.com/maps/search/{q}/@{lat},{lng},15z"
    elif home:
        q   = urllib.parse.quote_plus(f"{query} near {home}")
        url = f"https://www.google.com/maps/search/{q}"
    else:
        q   = urllib.parse.quote_plus(query)
        url = f"https://www.google.com/maps/search/{q}"
    hint = " (adaugă GOOGLE_MAPS_API_KEY în .env pentru o listă precisă)" if not _api_key() else ""
    if show_on_map:
        webbrowser.open(url)
        return f"Am deschis Google Maps pentru '{query}', sefu.{hint}"
    # No API key → no structured list available; hand back the link, don't pop a window.
    return f"Nu am cheia Google Maps ca să listez locurile, dar uite linkul cu rezultatele pentru '{query}':\n{url}{hint}"


# ── calculate_route ───────────────────────────────────────────────────────────

def calculate_route(
    destination: str,
    origin: str = "",
    waypoints: list = None,
    mode: str = "driving",
    avoid_tolls: bool = False,
    avoid_highways: bool = False,
) -> str:
    """Plan a route, open Google Maps, and return a spoken duration/distance summary."""
    if not destination.strip():
        return "Spune-mi unde vrei să mergi, sefu."

    if not origin.strip():
        origin = _get_home_address()

    valid_modes = {"driving", "walking", "bicycling", "transit"}
    if mode not in valid_modes:
        mode = "driving"

    wps = list(waypoints) if waypoints else []

    # Always open Maps for visual navigation
    enc_origin = urllib.parse.quote_plus(origin)
    enc_dest   = urllib.parse.quote_plus(destination)
    if wps:
        enc_mid = "/".join(urllib.parse.quote_plus(w) for w in wps)
        url = f"https://www.google.com/maps/dir/{enc_origin}/{enc_mid}/{enc_dest}/?travelmode={mode}"
    else:
        url = f"https://www.google.com/maps/dir/{enc_origin}/{enc_dest}/?travelmode={mode}"

    avoid_parts = []
    if avoid_tolls:
        avoid_parts.append("tolls")
    if avoid_highways:
        avoid_parts.append("highways")
    if avoid_parts:
        url += "&avoid=" + "|".join(avoid_parts)

    webbrowser.open(url)
    logger.info(f"calculate_route: {origin} → {destination} [{mode}] — {url}")

    mobile_link = _mobile_maps_link(destination, origin=origin or _get_home_address())

    # Spoken summary via Routes API if key available
    key = _api_key()
    if not key:
        wp_text = f" via {', '.join(wps)}" if wps else ""
        return f"Am deschis ruta{wp_text} spre '{destination}' în Google Maps, sefu.\n📍 {mobile_link}"

    return _routes_api_summary(origin, destination, wps, mode, avoid_tolls, avoid_highways, key, mobile_link)


def _routes_api_summary(
    origin: str,
    destination: str,
    waypoints: list,
    mode: str,
    avoid_tolls: bool,
    avoid_highways: bool,
    key: str,
    mobile_link: str = "",
) -> str:
    import requests as _req

    travel_mode_map = {
        "driving":   "DRIVE",
        "walking":   "WALK",
        "bicycling": "BICYCLE",
        "transit":   "TRANSIT",
    }
    travel_mode = travel_mode_map.get(mode, "DRIVE")

    body: dict = {
        "origin":      {"address": origin},
        "destination": {"address": destination},
        "travelMode":  travel_mode,
        "routingPreference": "TRAFFIC_AWARE" if travel_mode == "DRIVE" else "ROUTING_PREFERENCE_UNSPECIFIED",
        "routeModifiers": {
            "avoidTolls":    avoid_tolls,
            "avoidHighways": avoid_highways,
        },
    }
    if waypoints:
        body["intermediates"] = [{"address": w} for w in waypoints]

    try:
        resp = _req.post(
            _ROUTES_URL,
            json=body,
            headers={
                "Content-Type":   "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Routes API error: {e}")
        wp_text = f" via {', '.join(waypoints)}" if waypoints else ""
        link_suffix = f"\n📍 {mobile_link}" if mobile_link else ""
        return f"Am deschis ruta{wp_text} spre '{destination}' în Google Maps, sefu.{link_suffix}"

    routes = data.get("routes", [])
    if not routes:
        link_suffix = f"\n📍 {mobile_link}" if mobile_link else ""
        return f"Am deschis ruta spre '{destination}' în Google Maps, sefu.{link_suffix}"

    r          = routes[0]
    dur_raw    = r.get("duration", "0s")
    duration_s = int(dur_raw.rstrip("s")) if dur_raw.endswith("s") else 0
    dist_m     = r.get("distanceMeters", 0)

    total_min = duration_s // 60
    hours     = total_min // 60
    mins      = total_min % 60
    km        = dist_m / 1000

    time_str    = f"{hours}h {mins}min" if hours > 0 else f"{mins} minute"
    wp_text     = f" via {', '.join(waypoints)}" if waypoints else ""
    link_suffix = f"\n📍 {mobile_link}" if mobile_link else ""

    return (
        f"Am deschis ruta{wp_text} spre '{destination}' în Google Maps. "
        f"Durată estimată: {time_str}, distanță: {km:.1f} km, sefu.{link_suffix}"
    )


# ── plan_journey (simple URL fallback, no API needed) ────────────────────────

def plan_journey(
    destination: str,
    origin: str = "",
    mode: str = "driving",
) -> str:
    if not destination.strip():
        return "Spune-mi unde vrei să mergi, sefu."

    valid_modes = {"driving", "walking", "bicycling", "transit"}
    if mode not in valid_modes:
        mode = "driving"

    if not origin.strip():
        origin = _get_home_address()

    enc_origin = urllib.parse.quote_plus(origin)
    enc_dest   = urllib.parse.quote_plus(destination)
    url = f"https://www.google.com/maps/dir/{enc_origin}/{enc_dest}/?travelmode={mode}"

    webbrowser.open(url)
    logger.info(f"plan_journey: {url}")
    return f"Am deschis ruta spre '{destination}' în Google Maps, sefu.\n📍 {_mobile_maps_link(destination, origin=origin)}"
