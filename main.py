from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from geopy.distance import geodesic
from datetime import datetime, date
import os, hashlib, secrets, httpx

load_dotenv()

app = FastAPI(title="Hard Collection API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# ─── HELPERS ─────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def generate_password(length=8) -> str:
    chars = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(chars) for _ in range(length))

def get_current_employee(x_employee_id: str = Header(...)):
    result = supabase.table("employees").select("*").eq("id", x_employee_id).execute()
    if not result.data:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return result.data[0]

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

STOP_LABELS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P"]

# ─── АВТОРИЗАЦИЯ ─────────────────────────────────────────────────

class LoginRequest(BaseModel):
    login: str
    password: str

@app.post("/auth/login")
def login(req: LoginRequest):
    result = supabase.table("employees").select("*")\
        .eq("login", req.login)\
        .eq("password_hash", hash_password(req.password))\
        .execute()
    if not result.data:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    emp = result.data[0]
    crew_result = supabase.table("crew_members").select("*, crews(*)").eq("employee_id", emp["id"]).execute()
    crew = crew_result.data[0]["crews"] if crew_result.data else None
    return {"employee": emp, "crew": crew}

# ─── ЭКИПАЖИ ─────────────────────────────────────────────────────

class CrewCreate(BaseModel):
    name: str
    car_brand: str
    car_model: str
    engine_volume: float
    fuel_type: str
    fuel_consumption_city: float
    fuel_consumption_highway: float
    color: str = "#3B82F6"
    member_logins: list[str]

@app.post("/crews")
def create_crew(req: CrewCreate):
    crew_data = {
        "name": req.name,
        "car_brand": req.car_brand,
        "car_model": req.car_model,
        "engine_volume": req.engine_volume,
        "fuel_type": req.fuel_type,
        "fuel_consumption_city": req.fuel_consumption_city,
        "fuel_consumption_highway": req.fuel_consumption_highway,
        "color": req.color,
    }
    crew = supabase.table("crews").insert(crew_data).execute().data[0]

    prefix = req.name.lower().replace(" ", "_")[:6]
    created_members = []
    for i, _ in enumerate(req.member_logins, 1):
        password = generate_password()
        login = f"{prefix}_{i}"
        emp = supabase.table("employees").insert({
            "full_name": req.member_logins[i-1] if req.member_logins[i-1] else f"Сотрудник {i} · {req.name}",
            "login": login,
            "password_hash": hash_password(password),
            "role": "agent"
        }).execute().data[0]
        supabase.table("crew_members").insert({
            "crew_id": crew["id"],
            "employee_id": emp["id"],
            "is_senior": i == 1
        }).execute()
        created_members.append({"login": login, "password": password, "employee_id": emp["id"]})

    return {"crew": crew, "members": created_members}

@app.get("/crews")
def get_crews():
    crews = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("is_active", True).execute()
    return crews.data

@app.get("/crews/{crew_id}")
def get_crew(crew_id: str):
    result = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("id", crew_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Экипаж не найден")
    return result.data[0]

@app.delete("/crews/{crew_id}")
def delete_crew(crew_id: str):
    supabase.table("crew_members").delete().eq("crew_id", crew_id).execute()
    supabase.table("crews").update({"is_active": False}).eq("id", crew_id).execute()
    return {"message": "Экипаж деактивирован"}

@app.post("/employees/{employee_id}/reset-password")
def reset_password(employee_id: str):
    new_password = generate_password()
    supabase.table("employees").update({
        "password_hash": hash_password(new_password)
    }).eq("id", employee_id).execute()
    return {"password": new_password}

# ─── СМЕНЫ ───────────────────────────────────────────────────────

class ShiftAction(BaseModel):
    action: str

@app.post("/shifts/action")
def shift_action(req: ShiftAction, employee=Depends(get_current_employee)):
    today = date.today().isoformat()

    shift_result = supabase.table("shifts")\
        .select("*")\
        .eq("employee_id", employee["id"])\
        .eq("date", today)\
        .execute()

    if req.action == "start":
        active_shifts = [s for s in shift_result.data if s["status"] != "finished"]
        if active_shifts:
            raise HTTPException(status_code=400, detail="Смена уже начата")
        crew_result = supabase.table("crew_members").select("crew_id").eq("employee_id", employee["id"]).execute()
        if not crew_result.data:
            raise HTTPException(status_code=400, detail="Сотрудник не в экипаже")
        crew_id = crew_result.data[0]["crew_id"]
        shift = supabase.table("shifts").insert({
            "employee_id": employee["id"],
            "crew_id": crew_id,
            "date": today,
            "started_at": datetime.utcnow().isoformat(),
            "status": "active"
        }).execute().data[0]
        return {"shift": shift, "message": "Смена начата"}

    if not shift_result.data:
        raise HTTPException(status_code=400, detail="Смена не найдена")

    active = [s for s in shift_result.data if s["status"] != "finished"]
    if not active:
        raise HTTPException(status_code=400, detail="Нет активной смены")
    shift = active[0]
    shift_id = shift["id"]

    status_map = {
        "pause_break": "break",
        "pause_tech": "tech",
        "resume": "active",
        "end": "finished"
    }
    new_status = status_map.get(req.action)
    if not new_status:
        raise HTTPException(status_code=400, detail="Неизвестное действие")

    update_data = {"status": new_status}
    if req.action == "end":
        update_data["ended_at"] = datetime.utcnow().isoformat()

    supabase.table("shifts").update(update_data).eq("id", shift_id).execute()
    return {"message": f"Статус обновлён: {new_status}"}

@app.get("/shifts/today/{crew_id}")
def get_today_shifts(crew_id: str):
    today = date.today().isoformat()
    result = supabase.table("shifts")\
        .select("*, employees(full_name)")\
        .eq("crew_id", crew_id)\
        .eq("date", today)\
        .execute()
    return result.data

# ─── GPS ТРЕКИ ───────────────────────────────────────────────────

class GpsPoint(BaseModel):
    lat: float
    lng: float
    speed: float = 0.0

@app.post("/gps/track")
def add_gps_point(point: GpsPoint, employee=Depends(get_current_employee)):
    today = date.today().isoformat()
    shift_result = supabase.table("shifts")\
        .select("*")\
        .eq("employee_id", employee["id"])\
        .eq("date", today)\
        .in_("status", ["active", "break", "tech"])\
        .execute()

    if not shift_result.data:
        raise HTTPException(status_code=400, detail="Нет активной смены")

    shift = shift_result.data[0]

    # Фильтруем GPS шум
    if point.speed < 5:
        last_point = supabase.table("gps_tracks")\
            .select("lat,lng")\
            .eq("shift_id", shift["id"])\
            .order("recorded_at", desc=True)\
            .limit(1).execute().data
        if last_point:
            dist = geodesic(
                (last_point[0]["lat"], last_point[0]["lng"]),
                (point.lat, point.lng)
            ).meters
            if dist < 50:
                return {"status": "ok"}

    supabase.table("gps_tracks").insert({
        "shift_id": shift["id"],
        "crew_id": shift["crew_id"],
        "lat": point.lat,
        "lng": point.lng,
        "speed": point.speed,
        "recorded_at": datetime.utcnow().isoformat()
    }).execute()

    prev = supabase.table("gps_tracks")\
        .select("lat,lng")\
        .eq("shift_id", shift["id"])\
        .order("recorded_at", desc=True)\
        .limit(2)\
        .execute()

    if len(prev.data) == 2:
        p1 = (prev.data[1]["lat"], prev.data[1]["lng"])
        p2 = (prev.data[0]["lat"], prev.data[0]["lng"])
        dist_km = geodesic(p1, p2).km
        new_km = float(shift.get("total_km") or 0) + dist_km

        crew = supabase.table("crews").select("fuel_consumption_city,fuel_consumption_highway,fuel_type").eq("id", shift["crew_id"]).execute().data[0]
        rate = crew["fuel_consumption_highway"] if point.speed > 80 else crew["fuel_consumption_city"]
        fuel_used = float(shift.get("fuel_used") or 0) + (dist_km * rate / 100)

        price_result = supabase.table("fuel_prices")\
            .select("price_per_liter")\
            .eq("fuel_type", crew["fuel_type"])\
            .order("valid_from", desc=True)\
            .limit(1).execute()
        price = price_result.data[0]["price_per_liter"] if price_result.data else 245
        fuel_cost = fuel_used * float(price)

        supabase.table("shifts").update({
            "total_km": round(new_km, 2),
            "fuel_used": round(fuel_used, 2),
            "fuel_cost": round(fuel_cost, 2)
        }).eq("id", shift["id"]).execute()

    return {"status": "ok"}

@app.get("/gps/track/{crew_id}")
def get_crew_track(crew_id: str, shift_date: str = None):
    target_date = shift_date or date.today().isoformat()
    shift = supabase.table("shifts")\
        .select("id")\
        .eq("crew_id", crew_id)\
        .eq("date", target_date)\
        .limit(1).execute()
    if not shift.data:
        return []
    shift_id = shift.data[0]["id"]
    tracks = supabase.table("gps_tracks")\
        .select("lat,lng,speed,recorded_at")\
        .eq("shift_id", shift_id)\
        .order("recorded_at")\
        .execute()
    return tracks.data

# ─── ТОЧКИ ОСТАНОВОК ─────────────────────────────────────────────

@app.get("/stops/{crew_id}")
def get_stops(crew_id: str, shift_date: str = None):
    target_date = shift_date or date.today().isoformat()
    shift = supabase.table("shifts").select("id").eq("crew_id", crew_id).eq("date", target_date).limit(1).execute()
    if not shift.data:
        return []
    stops = supabase.table("stop_points")\
        .select("*")\
        .eq("shift_id", shift.data[0]["id"])\
        .order("arrived_at")\
        .execute()
    return stops.data

class StopPoint(BaseModel):
    lat: float
    lng: float
    address: str = ""
    arrived_at: str
    duration_minutes: int = 0

@app.post("/stops/add")
def add_stop(req: StopPoint, employee=Depends(get_current_employee)):
    today = date.today().isoformat()
    shift_result = supabase.table("shifts")\
        .select("*")\
        .eq("employee_id", employee["id"])\
        .eq("date", today)\
        .in_("status", ["active", "break", "tech"])\
        .execute()
    if not shift_result.data:
        raise HTTPException(status_code=400, detail="Нет активной смены")
    shift = shift_result.data[0]

    stop_count = supabase.table("stop_points").select("id").eq("shift_id", shift["id"]).execute()
    label_idx = len(stop_count.data) % len(STOP_LABELS)
    label = STOP_LABELS[label_idx]
    address = req.address if req.address else get_address(req.lat, req.lng)

    supabase.table("stop_points").insert({
        "shift_id": shift["id"],
        "crew_id": shift["crew_id"],
        "lat": req.lat,
        "lng": req.lng,
        "address": address,
        "point_label": label,
        "arrived_at": req.arrived_at,
        "duration_minutes": req.duration_minutes,
    }).execute()
    return {"status": "ok", "label": label}

# ─── ДАШБОРД ─────────────────────────────────────────────────────

@app.get("/dashboard/live")
def get_live_dashboard():
    today = date.today().isoformat()
    crews = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("is_active", True).execute().data
    result = []
    for crew in crews:
        shifts = supabase.table("shifts").select("*, employees(full_name)").eq("crew_id", crew["id"]).eq("date", today).execute().data
        last_point = supabase.table("gps_tracks").select("lat,lng,recorded_at").eq("crew_id", crew["id"]).order("recorded_at", desc=True).limit(1).execute().data
        stops = supabase.table("stop_points").select("*").eq("crew_id", crew["id"]).order("arrived_at", desc=True).limit(1).execute().data
        active_shifts = [s for s in shifts if s["status"] != "finished"] or shifts
        result.append({
            "crew": crew,
            "shifts": shifts,
            "last_position": last_point[0] if last_point else None,
            "current_stop": stops[0] if stops else None,
            "total_km": sum(float(s.get("total_km") or 0) for s in active_shifts),
            "total_fuel": sum(float(s.get("fuel_used") or 0) for s in active_shifts),
            "total_cost": sum(float(s.get("fuel_cost") or 0) for s in active_shifts),
        })
    return result

# ─── ОТЧЁТЫ ──────────────────────────────────────────────────────

@app.get("/reports/summary")
def get_report(date_from: str, date_to: str, crew_id: str = None):
    query = supabase.table("shifts").select("*, crews(name, car_brand, car_model), employees(full_name)").gte("date", date_from).lte("date", date_to)
    if crew_id:
        query = query.eq("crew_id", crew_id)
    result = query.execute()
    return result.data

# ─── ЦЕНЫ ТОПЛИВА ────────────────────────────────────────────────

class FuelPriceUpdate(BaseModel):
    fuel_type: str
    price_per_liter: float

@app.post("/fuel-prices")
def update_fuel_price(req: FuelPriceUpdate):
    supabase.table("fuel_prices").insert({
        "fuel_type": req.fuel_type,
        "price_per_liter": req.price_per_liter,
        "valid_from": date.today().isoformat()
    }).execute()
    return {"message": "Цена обновлена"}

@app.get("/fuel-prices")
def get_fuel_prices():
    result = supabase.table("fuel_prices").select("*").order("valid_from", desc=True).execute()
    return result.data

@app.get("/")
def root():
    return {"status": "ok", "service": "Hard Collection API"}
