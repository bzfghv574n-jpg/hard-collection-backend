import logging
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, date, timezone, timedelta
import os, hashlib, secrets

from geocoding import get_address
from crew_presence import classify_crew_presence

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

def generate_unique_login(prefix: str, index: int) -> str:
    # Экипажи архивируются, а не удаляются (см. delete_crew) — значит старые
    # логины вида "prefix_1" остаются в базе навсегда, и при пересоздании
    # экипажа с тем же названием они бы столкнулись с уникальным индексом
    # на employees.login (500 Internal Server Error). Проверяем занятость
    # и добавляем суффикс, пока не найдём свободный вариант.
    base = f"{prefix}_{index}"
    login_str = base
    suffix = 1
    while supabase.table("employees").select("id").eq("login", login_str).execute().data:
        suffix += 1
        login_str = f"{base}{suffix}"
    return login_str

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
        login_str = generate_unique_login(prefix, i)
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
    # Архивируем, а не удаляем физически — иначе весь пробег/расход/GPS за
    # прошлые смены этого экипажа пропал бы из отчётов навсегда. Экипаж с тем
    # же названием можно пересоздать сразу же: create_crew всегда генерирует
    # новый id, так что он не пересечётся с архивным.

    # Принудительно завершаем смену, если она прямо сейчас активна —
    # иначе она осталась бы "активной" навечно без возможности её закрыть.
    supabase.table("shifts").update({
        "status": "finished",
        "ended_at": datetime.utcnow().isoformat()
    }).eq("crew_id", crew_id).in_("status", ["active", "break", "tech"]).execute()

    # Отвязываем сотрудников от экипажа — без этого /shifts/action("start")
    # пустил бы их работать под уже архивным экипажем.
    supabase.table("crew_members").delete().eq("crew_id", crew_id).execute()

    # Экипаж пропадает из /crews и /dashboard/live (там фильтр is_active=True),
    # но сама запись и вся его история (shifts/gps_tracks/stop_points) остаются
    # в базе — /reports/summary их не фильтрует по is_active, так что архивные
    # экипажи по-прежнему видны в выгрузках за прошлые периоды.
    supabase.table("crews").update({"is_active": False}).eq("id", crew_id).execute()

    logger.info(f"Экипаж архивирован: {crew_id} администратором {admin['id']}")
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
def get_today_shifts(crew_id: str):
    # Мобилка вызывает это без заголовка авторизации, чтобы восстановить
    # статус смены при открытии приложения — не требуем auth (только для
    # дашборда используется require_admin, но здесь дашборд не ходит).
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
    recorded_at: str = None  # реальное время фиксации на телефоне (не время получения сервером!)
                              # — критично при батч-отправке: без этого все точки батча
                              # получают ПОЧТИ ОДИНАКОВЫЙ recorded_at (время INSERT в базу),
                              # и любой анализ по интервалам между точками (например, фильтр
                              # GPS-скачков в дашборде, считающий скорость=расстояние/время)
                              # ломается — секунды между точками ≈0 → скорость ≈∞.

def _fuel_rate_for(crew: dict, speed: float) -> float:
    # Расход: трасса если > 80 км/ч, иначе город
    return crew["fuel_consumption_highway"] if speed > 80 else crew["fuel_consumption_city"]

def ensure_start_point(shift: dict, lat: float, lng: float, recorded_at: str = None):
    # Единообразно для ВСЕХ экипажей: самая первая GPS-точка смены отмечается
    # как "Точка A" — начало маршрута, а не только точки реальных остановок
    # (те приходят отдельно через /stops/add). Проверяем по наличию хоть одной
    # stop_points у этой смены — если уже есть (в т.ч. эта же "A"), ничего не
    # делаем, так что срабатывает ровно один раз за смену.
    existing = supabase.table("stop_points").select("id").eq("shift_id", shift["id"]).limit(1).execute()
    if existing.data:
        return
    address = get_address(lat, lng)
    supabase.table("stop_points").insert({
        "shift_id": shift["id"], "crew_id": shift["crew_id"],
        "lat": lat, "lng": lng, "address": address,
        "point_label": "A", "arrived_at": recorded_at or datetime.utcnow().isoformat(),
        "duration_minutes": 0,
    }).execute()

@app.post("/gps/track")
def add_gps_point(point: GpsPoint, employee=Depends(get_current_employee)):
    today = today_kz()
    shift_result = supabase.table("shifts").select("*")\
        .eq("employee_id", employee["id"]).eq("date", today)\
        .in_("status", ["active", "break", "tech"]).execute()

    if not shift_result.data:
        raise HTTPException(status_code=400, detail="Нет активной смены")

    shift = shift_result.data[0]

    # Сохраняем точку — recorded_at берём с телефона, если он его прислал
    # (см. комментарий в GpsPoint), иначе — время сервера как раньше.
    supabase.table("gps_tracks").insert({
        "shift_id": shift["id"], "crew_id": shift["crew_id"],
        "lat": point.lat, "lng": point.lng, "speed": point.speed,
        "recorded_at": point.recorded_at or datetime.utcnow().isoformat()
    }).execute()

    ensure_start_point(shift, point.lat, point.lng, point.recorded_at)

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
            # Критично для батчей: без recorded_at с телефона все точки одного
            # батча получили бы почти одинаковое время (момент INSERT), а не
            # реальные интервалы между GPS-фиксами.
            "recorded_at": point.recorded_at or datetime.utcnow().isoformat()
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
        # Первая точка батча — самая ранняя (мобилка копит их по порядку) —
        # если у смены ещё нет ни одной stop_points, отмечаем её как "Точка A".
        ensure_start_point(shift, points[0].lat, points[0].lng, points[0].recorded_at)

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
    # Экипаж может начать/закончить/начать смену заново в один день (пауза на
    # обед и т.п.) — если брать только одну смену (.limit(1), без сортировки),
    # можно тихо показать трек СТАРОЙ смены и потерять текущую. Объединяем
    # точки со всех смен этого дня.
    shifts = supabase.table("shifts").select("id, employee_id, employees(full_name)")\
        .eq("crew_id", crew_id).eq("date", target_date).execute().data
    if not shifts:
        return []
    shift_ids = [s["id"] for s in shifts]
    # employee_id/full_name на каждую точку — нужно дашборду, чтобы разделить
    # треки по сотрудникам, когда экипаж "разошёлся" (см. crew_presence.py).
    employee_by_shift = {
        s["id"]: {"employee_id": s["employee_id"], "full_name": (s.get("employees") or {}).get("full_name")}
        for s in shifts
    }
    # У PostgREST есть ЖЁСТКИЙ серверный потолок в 1000 строк на запрос
    # (db-max-rows) — обычный .limit() с клиента его не обходит. При долгой
    # смене (14ч, точка каждые 5-8 сек) один экипаж легко даёт больше 1000
    # точек в день, и без пагинации терялся именно конец трека (самые свежие
    # точки!). Обнаружено на тесте с 2 одновременными сменами, где суммарно
    # вышло 1000+ точек за день. Тянем страницами по .range(), пока страница
    # не окажется неполной.
    tracks = []
    PAGE = 1000
    offset = 0
    while True:
        page = supabase.table("gps_tracks").select("lat,lng,speed,recorded_at,shift_id")\
            .in_("shift_id", shift_ids).order("recorded_at")\
            .range(offset, offset + PAGE - 1).execute().data
        tracks.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    for t in tracks:
        emp = employee_by_shift.get(t["shift_id"], {})
        t["employee_id"] = emp.get("employee_id")
        t["full_name"] = emp.get("full_name")
    return tracks

# ─── ТОЧКИ ОСТАНОВОК ─────────────────────────────────────────────

@app.get("/stops/{crew_id}")
def get_stops(crew_id: str, shift_date: str = None):
    # Тоже вызывается мобилкой без заголовка авторизации (см. /shifts/today).
    # Объединяем все смены дня — см. комментарий в get_crew_track.
    target_date = shift_date or today_kz()
    shifts = supabase.table("shifts").select("id").eq("crew_id", crew_id).eq("date", target_date).execute().data
    if not shifts:
        return []
    shift_ids = [s["id"] for s in shifts]
    stops = supabase.table("stop_points").select("*")\
        .in_("shift_id", shift_ids).order("arrived_at").execute()
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
    crew_ids = [c["id"] for c in crews]
    if not crew_ids:
        return []

    # Раньше это было 3 запроса НА КАЖДЫЙ экипаж в цикле (90 запросов при 30
    # экипажах на один вызов дашборда) — сводим к нескольким пакетным запросам
    # независимо от числа экипажей.
    all_shifts = supabase.table("shifts").select("*, employees(full_name)")\
        .in_("crew_id", crew_ids).eq("date", today).execute().data
    shifts_by_crew = {}
    for s in all_shifts:
        shifts_by_crew.setdefault(s["crew_id"], []).append(s)

    # PostgREST не поддерживает "DISTINCT ON" через клиентскую библиотеку —
    # берём достаточно большое окно последних точек/остановок по всем экипажам
    # и группируем "первую на каждый crew_id" в Python (точки уже отсортированы
    # по убыванию времени, так что первая встреченная — самая свежая).
    # Запасной вариант для метки на карте, если сейчас нет ни одной активной
    # смены (экипаж офлайн или уже завершил день) — тогда сравнивать "вместе
    # или врозь" не с чем, просто показываем где были в последний раз.
    recent_points = supabase.table("gps_tracks").select("crew_id,lat,lng,recorded_at")\
        .in_("crew_id", crew_ids).order("recorded_at", desc=True).limit(len(crew_ids) * 20).execute().data
    last_point_by_crew = {}
    for p in recent_points:
        last_point_by_crew.setdefault(p["crew_id"], p)

    # Последняя точка НА КАЖДУЮ активную смену (не на экипаж целиком) — нужна,
    # чтобы понять, где сейчас каждый конкретный сотрудник экипажа, и сравнить
    # их между собой (см. crew_presence.py: "вместе в одной машине" или
    # "разошлись — один не на рабочем месте").
    active_shift_ids = [s["id"] for s in all_shifts if s["status"] in ("active", "break", "tech")]
    last_point_by_shift = {}
    if active_shift_ids:
        recent_shift_points = supabase.table("gps_tracks").select("shift_id,lat,lng,recorded_at")\
            .in_("shift_id", active_shift_ids).order("recorded_at", desc=True)\
            .limit(len(active_shift_ids) * 20).execute().data
        for p in recent_shift_points:
            last_point_by_shift.setdefault(p["shift_id"], p)

    recent_stops = supabase.table("stop_points").select("*")\
        .in_("crew_id", crew_ids).order("arrived_at", desc=True).limit(len(crew_ids) * 10).execute().data
    last_stop_by_crew = {}
    for s in recent_stops:
        last_stop_by_crew.setdefault(s["crew_id"], s)

    result = []
    for crew in crews:
        shifts = shifts_by_crew.get(crew["id"], [])
        finished = [s for s in shifts if s["status"] == "finished"]
        active = [s for s in shifts if s["status"] in ("active", "break", "tech")]

        # Пробег/расход уже завершённых сегодня смен этого экипажа — это
        # прошлое, они не могут "конфликтовать" ни с кем прямо сейчас,
        # суммируем как раньше.
        finished_km = sum(float(s.get("total_km") or 0) for s in finished)
        finished_fuel = sum(float(s.get("fuel_used") or 0) for s in finished)
        finished_cost = sum(float(s.get("fuel_cost") or 0) for s in finished)

        active_with_points = [{"shift": s, "last_point": last_point_by_shift.get(s["id"])} for s in active]
        presence = classify_crew_presence(active_with_points)

        last_position = last_point_by_crew.get(crew["id"])
        active_detail = None

        if presence["state"] == "divergent":
            # Сотрудники сейчас в разных местах — нет единого "пробега
            # экипажа", который можно было бы честно посчитать одним числом
            # (мы не знаем, кто из них в машине, а кто по своим делам).
            # Отдаём раздельно по сотруднику, дашборд покажет обе линии на
            # карте с предупреждением вместо того, чтобы гадать.
            active_detail = [
                {
                    "shift_id": s["id"],
                    "employee_id": s["employee_id"],
                    "full_name": (s.get("employees") or {}).get("full_name"),
                    "total_km": float(s.get("total_km") or 0),
                    "fuel_used": float(s.get("fuel_used") or 0),
                    "fuel_cost": float(s.get("fuel_cost") or 0),
                    "last_point": last_point_by_shift.get(s["id"]),
                }
                for s in active
            ]
            total_km, total_fuel, total_cost = finished_km, finished_fuel, finished_cost
        else:
            # solo (0-1 активных) или together (несколько, но в одной
            # машине) — берём ОДНУ смену-"победителя" (максимум пробега:
            # см. classify_crew_presence) вместо суммы по всем активным,
            # иначе одновременная работа 2 телефонов задваивала бы пробег
            # и расход топлива на ровном месте.
            primary_shift = None
            if presence["primary_shift_id"]:
                primary_shift = next((s for s in active if s["id"] == presence["primary_shift_id"]), None)
            if primary_shift:
                last_position = last_point_by_shift.get(primary_shift["id"]) or last_position
                total_km = finished_km + float(primary_shift.get("total_km") or 0)
                total_fuel = finished_fuel + float(primary_shift.get("fuel_used") or 0)
                total_cost = finished_cost + float(primary_shift.get("fuel_cost") or 0)
            else:
                total_km, total_fuel, total_cost = finished_km, finished_fuel, finished_cost

        result.append({
            "crew": crew, "shifts": shifts,
            "presence_state": presence["state"],
            # Кого показывать на карте одной линией при solo/together (null
            # при divergent — там дашборд рисует обе линии по active_detail).
            "primary_shift_id": presence["primary_shift_id"] if presence["state"] != "divergent" else None,
            "active_detail": active_detail,
            "last_position": last_position,
            "current_stop": last_stop_by_crew.get(crew["id"]),
            "total_km": round(total_km, 3),
            "total_fuel": round(total_fuel, 3),
            "total_cost": round(total_cost, 2),
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

# ─── УВЕДОМЛЕНИЯ ─────────────────────────────────────────────────
# Диагностика того, что реально отправил (или собирался отправить) Telegram-
# бот — без передачи TELEGRAM_BOT_TOKEN куда-либо. telegram_bot.py пишет сюда
# запись ПЕРЕД отправкой сообщения (см. send_notification в telegram_bot.py),
# так что эта таблица — надёжный журнал сработавших условий, даже если сам
# Telegram недоступен для просмотра.

@app.get("/notifications")
def get_notifications(crew_id: str = None, since: str = None, limit: int = 100, admin=Depends(require_admin)):
    query = supabase.table("notifications").select("*, crews(name)").order("created_at", desc=True)
    if crew_id:
        query = query.eq("crew_id", crew_id)
    if since:
        query = query.gte("created_at", since)
    return query.limit(min(limit, 500)).execute().data

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
