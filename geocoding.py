import httpx


def get_address(lat: float, lng: float) -> str:
    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {"lat": lat, "lon": lng, "format": "json", "accept-language": "ru"}
        headers = {"User-Agent": "HardCollectionApp/1.0"}
        r = httpx.get(url, params=params, headers=headers, timeout=5)
        data = r.json()
        addr = data.get("address", {})
        parts = []
        if addr.get("road"): parts.append(addr["road"])
        if addr.get("house_number"): parts.append(addr["house_number"])
        if addr.get("city") or addr.get("town") or addr.get("village"):
            parts.append(addr.get("city") or addr.get("town") or addr.get("village"))
        return ", ".join(parts) if parts else data.get("display_name", f"{lat:.4f}, {lng:.4f}")
    except Exception:
        return f"{lat:.4f}, {lng:.4f}"
