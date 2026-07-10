import logging
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, date, timezone, timedelta
import os, hashlib, secrets

from geocoding import get_address

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hard_collection")

app = FastAPI(title="Hard Collection API")

# Дашборд и локальная разработка — единственные легитимные браузерные клиенты.
# Мобильные приложения не отправляют Origin, так что CORS их не касается.
ALLOWED_ORIGINS = [
    "https://hard-collection-dashboard-production.up.railway.app",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_unhandled_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception:
        logger.exception(f"Unhandled error on {request.method} {request.url.path}")
        raise


KZ_TZ = timezone(timedelta(hours=5))


def today_kz() -> str:
    """Календарная дата в казахстанском времени (+5), а не по UTC/времени сервера."""
    return datetime.now(KZ_TZ).date().isoformat()


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def generate_password(length=8) -> str:
    chars = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(chars) for _ in range(length))


def safe_employee(emp: dict) -> dict:
    """Убирает password_hash перед тем как отдать сотрудника клиенту."""
    if not emp:
        return emp
    return {k: v for k, v in emp.items() if k != "password_hash"}


def sanitize_crews(crews: list) -> list:
    for crew in crews:
        for cm in (crew.get("crew_members") or []):
            if cm.get("employees"):
                cm["employees"] = safe_employee(cm["employees"])
    return crews


def get_current_employee(x_employee_id: str = Header(...)):
    result = supabase.table("employees").select("*").eq("id", x_employee_id).execute()
    if not result.data:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return result.data[0]


def require_admin(employee=Depends(get_current_employee)):
    if employee.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return employee


supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

STOP_LABELS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P"]


def stop_label(index: int) -> str:
    """A..P, потом A2..P2, и т.д. — без повторов внутри одной смены."""
    cycle, pos = divmod(index, len(STOP_LABELS))
    letter = STOP_LABELS[pos]
    return letter if cycle == 0 else f"{letter}{cycle + 1}"


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
        logger.info(f"Неудачная попытка входа: login={req.login}")
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    emp = result.data[0]
    crew_result = supabase.table("crew_members").select("*, crews(*)").eq("employee_id", emp["id"]).execute()
    crew = crew_result.data[0]["crews"] if crew_result.data else None
    logger.info(f"Вход: employee_id={emp['id']} role={emp.get('role')}")
    return {"employee": safe_employee(emp), "crew": crew}

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
def create_crew(req: CrewCreate, admin=Depends(require_admin)):
    crew_data = {
        "name": req.name, "car_brand": req.car_brand, "car_model": req.car_model,
        "engine_volume": req.engine_volume, "fuel_type": req.fuel_type,
        "fuel_consumption_city": req.fuel_consumption_city,
        "fuel_consumption_highway": req.fuel_consumption_highway, "color": req.color,
    }
    crew = supabase.table("crews").insert(crew_data).execute().data[0]
    prefix = req.name.lower().replace(" ", "_")[:6]
    created_members = []
    for i, _ in enumerate(req.member_logins, 1):
        password = generate_password()
        login_str = f"{prefix}_{i}"
        emp = supabase.table("employees").insert({
            "full_name": req.member_logins[i-1] or f"Сотрудник {i}",
            "login": login_str,
            "password_hash": hash_password(password),
            "role": "agent"
        }).execute().data[0]
        supabase.table("crew_members").insert({
            "crew_id": crew["id"], "employee_id": emp["id"], "is_senior": i == 1
        }).execute()
        created_members.append({"login": login_str, "password": password, "employee_id": emp["id"]})
    logger.info(f"Экипаж создан: {crew['id']} ({req.name}) администратором {admin['id']}")
    return {"crew": crew, "members": created_members}

@app.get("/crews")
def get_crews(admin=Depends(require_admin)):
    crews = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("is_active", True).execute().data
    return sanitize_crews(crews)

@app.get("/crews/{crew_id}")
def get_crew(crew_id: str, admin=Depends(require_admin)):
    result = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("id", crew_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Экипаж не найден")
    return sanitize_crews(result.data)[0]

@app.delete("/crews/{crew_id}")
def delete_crew(crew_id: str, admin=Depends(require_admin)):
    members = supabase.table("crew_members").select("employee_id").eq("crew_id", crew_id).execute().data
    # Чистим всё, что ссылается на экипаж, иначе остаются висячие записи
    supabase.table("gps_tracks").delete().eq("crew_id", crew_id).execute()
    supabase.table("stop_points").delete().eq("crew_id", crew_id).execute()
    supabase.table("shifts").delete().eq("crew_id", crew_id).execute()
    supabase.table("crew_members").delete().eq("crew_id", crew_id).execute()
    for m in members:
        supabase.table("employees").delete().eq("id", m["employee_id"]).execute()
    supabase.table("crews").delete().eq("id", crew_id).execute()
    logger.info(f"Экипаж удалён: {crew_id} администратором {admin['id']}")
    return {"message": "Экипаж удалён"}

class CrewUpdate(BaseModel):
    name: str
    car_brand: str
    car_model: str
    engine_volume: float
    fuel_type: str
    fuel_consumption_city: float
    fuel_consumption_highway: float
    color: str = "#3B82F6"

@app.put("/crews/{crew_id}")
def update_crew(crew_id: str, req: CrewUpdate, admin=Depends(require_admin)):
    supabase.table("crews").update({
        "name": req.name, "car_brand": req.car_brand, "car_model": req.car_model,
        "engine_volume": req.engine_volume, "fuel_type": req.fuel_type,
        "fuel_consumption_city": req.fuel_consumption_city,
        "fuel_consumption_highway": req.fuel_consumption_highway, "color": req.color,
    }).eq("id", crew_id).execute()
    return {"message": "Экипаж обновлён"}

@app.post("/employees/{employee_id}/reset-password")
def reset_password(employee_id: str, admin=Depends(require_admin)):
    new_password = generate_password()
    supabase.table("employees").update({"password_hash": hash_password(new_password)}).eq("id", employee_id).execute()
    logger.info(f"Пароль сброшен: employee_id={employee_id} администратором {admin['id']}")
    return {"password": new_password}

# ─── СМЕНЫ ───────────────────────────────────────────────────────

class ShiftAction(BaseModel):
    action: str

@app.post("/shifts/action")
def shift_action(req: ShiftAction, employee=Depends(get_current_employee)):
    today = today_kz()
    shift_result = supabase.table("shifts").select("*").eq("employee_id", employee["id"]).eq("date", today).execute()

    if req.action == "start":
        active_shifts = [s for s in shift_result.data if s["status"] != "finished"]
        if active_shifts:
            raise HTTPException(status_code=400, detail="Смена уже начата")
        crew_result = supabase.table("crew_members").select("crew_id").eq("employee_id", employee["id"]).execute()
        if not crew_result.data:
            raise HTTPException(status_code=400, detail="Сотрудник не в экипаже")
        crew_id = crew_result.data[0]["crew_id"]
        shift = supabase.table("shifts").insert({
            "employee_id": employee["id"], "crew_id": crew_id, "date": today,
            "started_at": datetime.utcnow().isoformat(), "status": "active"
        }).execute().data[0]
        return {"shift": shift, "message": "Смена начата"}

    if not shift_result.data:
        raise HTTPException(status_code=400, detail="Смена не найдена")
    active = [s for s in shift_result.data if s["status"] != "finished"]
    if not active:
        raise HTTPException(status_code=400, detail="Нет активной смены")
    shift = active[0]

    status_map = {"pause_break": "break", "pause_tech": "tech", "resume": "active", "end": "finished"}
    new_status = status_map.get(req.action)
    if not new_status:
        raise HTTPException(status_code=400, detail="Неизвестное действие")

    update_data = {"status": new_status}
    if req.action == "end":
        update_data["ended_at"] = datetime.utcnow().isoformat()
    supabase.table("shifts").update(update_data).eq("id", shift["id"]).execute()
    return {"message": f"Статус обновлён: {new_status}"}

@app.get("/shifts/today/{crew_id}")
def get_today_shifts(crew_id: str, admin=Depends(require_admin)):
    today = today_kz()
    result = supabase.table("shifts").select("*, employees(full_name)").eq("crew_id", crew_id).eq("date", today).execute()
    return result.data

# ─── GPS ТРЕКИ ───────────────────────────────────────────────────
# Вся логика фильтрации и пробега на мобилке.
# Бэкенд просто сохраняет точку и прибавляет distance_km.

class GpsPoint(BaseModel):
    lat: float
    lng: float
    speed: float = 0.0
    distance_km: float = 0.0  # считается на мобилке, только при speed >= 15 км/ч

def _fuel_rate_for(crew: dict, speed: float) -> float:
    # Расход: трасса если > 80 км/ч, иначе город
    return crew["fuel_consumption_highway"] if speed > 80 else crew["fuel_consumption_city"]

@app.post("/gps/track")
def add_gps_point(point: GpsPoint, employee=Depends(get_current_employee)):
    today = today_kz()
    shift_result = supabase.table("shifts").select("*")\
        .eq("employee_id", employee["id"]).eq("date", today)\
        .in_("status", ["active", "break", "tech"]).execute()

    if not shift_result.data:
        raise HTTPException(status_code=400, detail="Нет активной смены")

    shift = shift_result.data[0]

    # Сохраняем точку
    supabase.table("gps_tracks").insert({
        "shift_id": shift["id"], "crew_id": shift["crew_id"],
        "lat": point.lat, "lng": point.lng, "speed": point.speed,
        "recorded_at": datetime.utcnow().isoformat()
    }).execute()

    # Обновляем пробег только если мобилка прислала реальное расстояние
    if point.distance_km > 0:
        new_km = float(shift.get("total_km") or 0) + point.distance_km
        crew = supabase.table("crews").select(
            "fuel_consumption_city,fuel_consumption_highway,fuel_type"
        ).eq("id", shift["crew_id"]).execute().data[0]

        rate = _fuel_rate_for(crew, point.speed)
        fuel_used = float(shift.get("fuel_used") or 0) + (point.distance_km * rate / 100)

        price_result = supabase.table("fuel_prices").select("price_per_liter")\
            .eq("fuel_type", crew["fuel_type"]).order("valid_from", desc=True).limit(1).execute()
        price = float(price_result.data[0]["price_per_liter"]) if price_result.data else 245.0
        fuel_cost = fuel_used * price

        supabase.table("shifts").update({
            "total_km": round(new_km, 3),
            "fuel_used": round(fuel_used, 3),
            "fuel_cost": round(fuel_cost, 2)
        }).eq("id", shift["id"]).execute()

    return {"status": "ok"}

@app.post("/gps/batch")
def add_gps_batch(points: list[GpsPoint], employee=Depends(get_current_employee)):
    """Батч-эндпоинт для отправки накопленных офлайн точек"""
    today = today_kz()
    shift_result = supabase.table("shifts").select("*")\
        .eq("employee_id", employee["id"]).eq("date", today)\
        .in_("status", ["active", "break", "tech"]).execute()

    if not shift_result.data:
        raise HTTPException(status_code=400, detail="Нет активной смены")

    shift = shift_result.data[0]
    total_dist = 0.0
    total_fuel = 0.0
    crew = None

    rows = []
    for point in points:
        rows.append({
            "shift_id": shift["id"], "crew_id": shift["crew_id"],
            "lat": point.lat, "lng": point.lng, "speed": point.speed,
            "recorded_at": datetime.utcnow().isoformat()
        })
        if point.distance_km > 0:
            total_dist += point.distance_km
            if crew is None:
                crew = supabase.table("crews").select(
                    "fuel_consumption_city,fuel_consumption_highway,fuel_type"
                ).eq("id", shift["crew_id"]).execute().data[0]
            rate = _fuel_rate_for(crew, point.speed)
            total_fuel += point.distance_km * rate / 100

    if rows:
        supabase.table("gps_tracks").insert(rows).execute()

    if total_dist > 0:
        new_km = float(shift.get("total_km") or 0) + total_dist
        fuel_used = float(shift.get("fuel_used") or 0) + total_fuel
        price_result = supabase.table("fuel_prices").select("price_per_liter")\
            .eq("fuel_type", crew["fuel_type"]).order("valid_from", desc=True).limit(1).execute()
        price = float(price_result.data[0]["price_per_liter"]) if price_result.data else 245.0
        fuel_cost = fuel_used * price
        supabase.table("shifts").update({
            "total_km": round(new_km, 3),
            "fuel_used": round(fuel_used, 3),
            "fuel_cost": round(fuel_cost, 2)
        }).eq("id", shift["id"]).execute()

    return {"status": "ok", "saved": len(rows)}

@app.get("/gps/track/{crew_id}")
def get_crew_track(crew_id: str, shift_date: str = None, admin=Depends(require_admin)):
    target_date = shift_date or today_kz()
    shift = supabase.table("shifts").select("id").eq("crew_id", crew_id).eq("date", target_date).limit(1).execute()
    if not shift.data:
        return []
    tracks = supabase.table("gps_tracks").select("lat,lng,speed,recorded_at")\
        .eq("shift_id", shift.data[0]["id"]).order("recorded_at").execute()
    return tracks.data

# ─── ТОЧКИ ОСТАНОВОК ─────────────────────────────────────────────

@app.get("/stops/{crew_id}")
def get_stops(crew_id: str, shift_date: str = None, admin=Depends(require_admin)):
    target_date = shift_date or today_kz()
    shift = supabase.table("shifts").select("id").eq("crew_id", crew_id).eq("date", target_date).limit(1).execute()
    if not shift.data:
        return []
    stops = supabase.table("stop_points").select("*")\
        .eq("shift_id", shift.data[0]["id"]).order("arrived_at").execute()
    return stops.data

class StopPoint(BaseModel):
    lat: float
    lng: float
    address: str = ""
    arrived_at: str
    duration_minutes: int = 0

@app.post("/stops/add")
def add_stop(req: StopPoint, employee=Depends(get_current_employee)):
    today = today_kz()
    shift_result = supabase.table("shifts").select("*")\
        .eq("employee_id", employee["id"]).eq("date", today)\
        .in_("status", ["active", "break", "tech"]).execute()
    if not shift_result.data:
        raise HTTPException(status_code=400, detail="Нет активной смены")
    shift = shift_result.data[0]

    stop_count = supabase.table("stop_points").select("id").eq("shift_id", shift["id"]).execute()
    label = stop_label(len(stop_count.data))
    address = req.address if req.address else get_address(req.lat, req.lng)

    supabase.table("stop_points").insert({
        "shift_id": shift["id"], "crew_id": shift["crew_id"],
        "lat": req.lat, "lng": req.lng, "address": address,
        "point_label": label, "arrived_at": req.arrived_at,
        "duration_minutes": req.duration_minutes,
    }).execute()
    return {"status": "ok", "label": label}

# ─── ДАШБОРД ─────────────────────────────────────────────────────

@app.get("/dashboard/live")
def get_live_dashboard(admin=Depends(require_admin)):
    today = today_kz()
    crews = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("is_active", True).execute().data
    crews = sanitize_crews(crews)
    result = []
    for crew in crews:
        shifts = supabase.table("shifts").select("*, employees(full_name)").eq("crew_id", crew["id"]).eq("date", today).execute().data
        last_point = supabase.table("gps_tracks").select("lat,lng,recorded_at").eq("crew_id", crew["id"]).order("recorded_at", desc=True).limit(1).execute().data
        stops = supabase.table("stop_points").select("*").eq("crew_id", crew["id"]).order("arrived_at", desc=True).limit(1).execute().data
        result.append({
            "crew": crew, "shifts": shifts,
            "last_position": last_point[0] if last_point else None,
            "current_stop": stops[0] if stops else None,
            "total_km": sum(float(s.get("total_km") or 0) for s in shifts),
            "total_fuel": sum(float(s.get("fuel_used") or 0) for s in shifts),
            "total_cost": sum(float(s.get("fuel_cost") or 0) for s in shifts),
        })
    return result

# ─── ОТЧЁТЫ ──────────────────────────────────────────────────────

@app.get("/reports/summary")
def get_report(date_from: str, date_to: str, crew_id: str = None, admin=Depends(require_admin)):
    query = supabase.table("shifts").select("*, crews(name, car_brand, car_model), employees(full_name)")\
        .gte("date", date_from).lte("date", date_to)
    if crew_id:
        query = query.eq("crew_id", crew_id)
    return query.execute().data

# ─── ЦЕНЫ ТОПЛИВА ────────────────────────────────────────────────

class FuelPriceUpdate(BaseModel):
    fuel_type: str
    price_per_liter: float

@app.post("/fuel-prices")
def update_fuel_price(req: FuelPriceUpdate, admin=Depends(require_admin)):
    supabase.table("fuel_prices").insert({
        "fuel_type": req.fuel_type, "price_per_liter": req.price_per_liter,
        "valid_from": today_kz()
    }).execute()
    return {"message": "Цена обновлена"}

@app.get("/fuel-prices")
def get_fuel_prices(admin=Depends(require_admin)):
    return supabase.table("fuel_prices").select("*").order("valid_from", desc=True).execute().data

@app.get("/")
def root():
    return {"status": "ok", "service": "Hard Collection API"}
