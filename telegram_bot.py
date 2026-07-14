import asyncio
import os
import logging
from datetime import datetime, date, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from supabase import create_client, Client
from geopy.distance import geodesic

from geocoding import get_address
from crew_presence import classify_crew_presence

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_INVITE_CODE = os.getenv("TELEGRAM_INVITE_CODE")
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

TZ = timezone(timedelta(hours=5))

def now_local():
    return datetime.now(TZ)

def utc_to_local(dt_str):
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt.astimezone(TZ)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    full_name = update.effective_user.full_name

    if TELEGRAM_INVITE_CODE:
        provided = context.args[0] if context.args else None
        if provided != TELEGRAM_INVITE_CODE:
            await update.message.reply_text(
                "Доступ по приглашению. Уточните ссылку-приглашение у администратора."
            )
            return

    existing = supabase.table("tg_subscribers").select("*").eq("chat_id", chat_id).execute()
    if not existing.data:
        supabase.table("tg_subscribers").insert({"chat_id": chat_id, "full_name": full_name}).execute()
        await update.message.reply_text(
            f"*{full_name}*, вы подписаны на уведомления Hard Collection.\n\n"
            "Вы будете получать:\n"
            "- Смена не начата до 09:01\n"
            "- Смена завершена раньше 19:00\n"
            "- Экипаж без движения 60+ мин\n"
            "- Неполный состав экипажа\n"
            "- Итог завершённой смены",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"Вы уже подписаны, *{full_name}*", parse_mode="Markdown")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    supabase.table("tg_subscribers").delete().eq("chat_id", chat_id).execute()
    await update.message.reply_text("Вы отписались от уведомлений.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = now_local().date().isoformat()
    crews = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("is_active", True).execute().data
    text = f"*Статус экипажей на {today}*\n\n"
    for crew in crews:
        shifts = supabase.table("shifts").select("*").eq("crew_id", crew["id"]).eq("date", today).execute().data
        active = [s for s in shifts if s["status"] == "active"]
        member_count = len(crew.get("crew_members", []))
        online_count = len(shifts)
        if not shifts:
            emoji, status_text = "\u26ab", "Не вышли"
        elif len(active) == member_count:
            emoji, status_text = "\U0001f7e2", "На линии"
        elif active:
            emoji, status_text = "\U0001f7e1", f"Неполный ({online_count}/{member_count})"
        else:
            emoji, status_text = "\U0001f7e0", "На паузе"
        total_km = sum(float(s.get("total_km") or 0) for s in shifts)
        crew_name = crew["name"]
        text += f"{emoji} *{crew_name}* — {status_text}\n"
        text += f"   Пробег: {total_km:.1f} км\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("ack_"):
        notif_id = data.replace("ack_", "")
        chat_id = update.effective_chat.id
        supabase.table("notifications").update({
            "is_acknowledged": True,
            "acknowledged_by": chat_id,
            "acknowledged_at": datetime.utcnow().isoformat()
        }).eq("id", notif_id).execute()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Принято. Ситуация под контролем.")

async def send_notification(app, message: str, notification_id: str = None):
    subscribers = supabase.table("tg_subscribers").select("chat_id").execute().data
    keyboard = None
    if notification_id:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Под контролем", callback_data=f"ack_{notification_id}"),
        ]])
    for sub in subscribers:
        try:
            await app.bot.send_message(
                chat_id=sub["chat_id"],
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка отправки {sub['chat_id']}: {e}")

async def check_shift_not_started(app):
    now = now_local()
    if now.weekday() == 6:
        return
    today = now.date().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data
    for crew in crews:
        shifts = supabase.table("shifts").select("*").eq("crew_id", crew["id"]).eq("date", today).execute().data
        active_shifts = [s for s in shifts if s["status"] != "finished"]
        if not active_shifts:
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"]).eq("type", "no_shift").gte("created_at", today).execute()
            if not existing.data:
                notif = supabase.table("notifications").insert({
                    "type": "no_shift",
                    "crew_id": crew["id"],
                    "message": f"Экипаж не начал смену"
                }).execute().data[0]
                crew_name = crew["name"]
                time_str = now.strftime("%H:%M")
                msg = f"\U0001f534 *Смена не начата*\n\nЭкипаж *{crew_name}* не вышел на линию.\nВремя: {time_str}"
                await send_notification(app, msg, notif["id"])

async def notify_shift_started(app):
    now = now_local()
    today = now.date().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data
    for crew in crews:
        active_shifts = supabase.table("shifts").select("*")\
            .eq("crew_id", crew["id"]).eq("date", today)\
            .eq("status", "active").execute().data
        for shift in active_shifts:
            if not shift.get("started_at"):
                continue
            started_local = utc_to_local(shift["started_at"])
            minutes_ago = (now - started_local).total_seconds() / 60
            if minutes_ago > 6:
                continue
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"]).eq("type", "shift_started")\
                .gte("created_at", today).execute()
            already_sent = False
            if existing.data:
                for notif in existing.data:
                    notif_time = datetime.fromisoformat(notif["created_at"].replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - notif_time).total_seconds() < 600:
                        already_sent = True
                        break
            if already_sent:
                continue
            supabase.table("notifications").insert({
                "type": "shift_started",
                "crew_id": crew["id"],
                "message": "Экипаж начал смену"
            }).execute()
            crew_name = crew["name"]
            time_str = started_local.strftime("%H:%M")
            msg = f"\U0001f7e2 *Смена начата*\n\nЭкипаж *{crew_name}* вышел на линию в *{time_str}*"
            await send_notification(app, msg)

async def check_shift_summary(app):
    now = now_local()
    today = now.date().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data
    for crew in crews:
        finished_shifts = supabase.table("shifts").select("*")\
            .eq("crew_id", crew["id"]).eq("date", today)\
            .eq("status", "finished").execute().data
        if not finished_shifts:
            continue
        active_shifts = supabase.table("shifts").select("*")\
            .eq("crew_id", crew["id"]).eq("date", today)\
            .in_("status", ["active", "break", "tech"]).execute().data
        if active_shifts:
            continue
        existing = supabase.table("notifications").select("*")\
            .eq("crew_id", crew["id"]).eq("type", "shift_summary")\
            .gte("created_at", today).execute()
        if existing.data:
            continue
        total_km = sum(float(s.get("total_km") or 0) for s in finished_shifts)
        total_fuel = sum(float(s.get("fuel_used") or 0) for s in finished_shifts)
        total_cost = sum(float(s.get("fuel_cost") or 0) for s in finished_shifts)
        stops = supabase.table("stop_points").select("id").eq("crew_id", crew["id"]).execute().data
        last_shift = max(finished_shifts, key=lambda s: s.get("ended_at") or "")
        time_str = "—"
        early_warning = ""
        if last_shift.get("ended_at"):
            ended_local = utc_to_local(last_shift["ended_at"])
            time_str = ended_local.strftime("%H:%M")
            if ended_local.hour < 19:
                early_warning = "\n_Смена завершена раньше 19:00_"
        notif = supabase.table("notifications").insert({
            "type": "shift_summary",
            "crew_id": crew["id"],
            "message": "Экипаж завершил смену"
        }).execute().data[0]
        crew_name = crew["name"]
        msg = (
            f"\u2705 *Смена завершена*{early_warning}\n\n"
            f"Экипаж *{crew_name}* закончил работу в *{time_str}*\n\n"
            f"Точек: *{len(stops)}*\n"
            f"Пробег: *{total_km:.1f} км*\n"
            f"Расход: *{total_fuel:.1f} л*\n"
            f"Стоимость: *{total_cost:,.0f} \u20b8*"
        )
        await send_notification(app, msg, notif["id"])

async def auto_finish_shifts(app):
    now = now_local()
    today = now.date().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data
    for crew in crews:
        active_shifts = supabase.table("shifts").select("*")\
            .eq("crew_id", crew["id"]).eq("date", today)\
            .in_("status", ["active", "break", "tech"]).execute().data
        for shift in active_shifts:
            try:
                supabase.table("shifts").update({
                    "status": "finished",
                    "ended_at": datetime.utcnow().isoformat()
                }).eq("id", shift["id"]).execute()
            except Exception as e:
                logger.error(f"Ошибка автозавершения: {e}")
        if active_shifts:
            all_shifts = supabase.table("shifts").select("*")\
                .eq("crew_id", crew["id"]).eq("date", today).execute().data
            total_km = sum(float(s.get("total_km") or 0) for s in all_shifts)
            total_fuel = sum(float(s.get("fuel_used") or 0) for s in all_shifts)
            total_cost = sum(float(s.get("fuel_cost") or 0) for s in all_shifts)
            stops = supabase.table("stop_points").select("id").eq("crew_id", crew["id"]).execute().data
            crew_name = crew["name"]
            msg = (
                f"\U0001f319 *Смена автозавершена*\n\n"
                f"Экипаж *{crew_name}* завершён автоматически в *23:50*\n\n"
                f"Точек: *{len(stops)}*\n"
                f"Пробег: *{total_km:.1f} км*\n"
                f"Расход: *{total_fuel:.1f} л*\n"
                f"Стоимость: *{total_cost:,.0f} \u20b8*"
            )
            await send_notification(app, msg)

GPS_TRACK_RETENTION_DAYS = 40

async def cleanup_old_gps_tracks(app):
    # Сырые GPS-точки — главный пожиратель места в базе при большом парке
    # машин. Итоги смены (пробег/расход/стоимость) хранятся отдельно в
    # shifts и не зависят от наличия старых точек — можно спокойно чистить.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=GPS_TRACK_RETENTION_DAYS)).isoformat()
    try:
        result = supabase.table("gps_tracks").delete().lt("recorded_at", cutoff).execute()
        deleted = len(result.data or [])
        logger.info(f"Очистка GPS-точек старше {GPS_TRACK_RETENTION_DAYS} дней: удалено {deleted}")
    except Exception as e:
        logger.error(f"Ошибка очистки старых GPS-точек: {e}")

async def check_long_stops(app):
    now = now_local()
    today = now.date().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data
    for crew in crews:
        active_shifts = supabase.table("shifts").select("*")\
            .eq("crew_id", crew["id"]).eq("date", today)\
            .in_("status", ["active", "break", "tech"]).execute().data
        if not active_shifts:
            continue

        # Не проверяем если смена началась менее 70 минут назад
        earliest_start = min(
            (s["started_at"] for s in active_shifts if s.get("started_at")),
            default=None
        )
        if earliest_start:
            started_utc = datetime.fromisoformat(earliest_start.replace("Z", "+00:00"))
            minutes_since_start = (datetime.now(timezone.utc) - started_utc).total_seconds() / 60
            if minutes_since_start < 70:
                continue

        last_points = supabase.table("gps_tracks")\
            .select("lat,lng,speed,recorded_at")\
            .eq("crew_id", crew["id"])\
            .order("recorded_at", desc=True)\
            .limit(30).execute().data
        if len(last_points) < 2:
            continue
        latest_time = datetime.fromisoformat(last_points[0]["recorded_at"].replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        minutes_since_last = (now_utc - latest_time).total_seconds() / 60
        if minutes_since_last > 120:
            continue
        one_hour_ago = now_utc.timestamp() - 3600
        recent_hour = [
            p for p in last_points
            if datetime.fromisoformat(p["recorded_at"].replace("Z", "+00:00")).timestamp() > one_hour_ago
        ]
        if len(recent_hour) < 3:
            continue
        first = recent_hour[-1]
        last = recent_hour[0]
        moved_meters = geodesic((first["lat"], first["lng"]), (last["lat"], last["lng"])).meters
        if moved_meters < 150:
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"]).eq("type", "long_stop")\
                .gte("created_at", today).execute()
            should_notify = True
            if existing.data:
                last_notif_time = datetime.fromisoformat(existing.data[-1]["created_at"].replace("Z", "+00:00"))
                if (now_utc - last_notif_time).total_seconds() < 7200:
                    should_notify = False
            if should_notify:
                address = get_address(last["lat"], last["lng"])
                notif = supabase.table("notifications").insert({
                    "type": "long_stop",
                    "crew_id": crew["id"],
                    "message": "Экипаж без движения 60+ мин"
                }).execute().data[0]
                crew_name = crew["name"]
                msg = f"\U0001f6d1 *Долгая остановка*\n\nЭкипаж *{crew_name}* не двигается более *60 минут*.\n{address}"
                await send_notification(app, msg, notif["id"])

async def check_incomplete_crews(app):
    now = now_local()
    today = now.date().isoformat()
    crews = supabase.table("crews").select("*, crew_members(*)").eq("is_active", True).execute().data
    for crew in crews:
        member_count = len(crew.get("crew_members", []))
        if member_count < 2:
            continue
        shifts = supabase.table("shifts").select("*")\
            .eq("crew_id", crew["id"]).eq("date", today)\
            .in_("status", ["active", "break", "tech"]).execute().data
        if 0 < len(shifts) < member_count:
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"]).eq("type", "incomplete_crew")\
                .gte("created_at", today).execute()
            if not existing.data:
                notif = supabase.table("notifications").insert({
                    "type": "incomplete_crew",
                    "crew_id": crew["id"],
                    "message": "Неполный состав"
                }).execute().data[0]
                crew_name = crew["name"]
                msg = f"\u26a0\ufe0f *Неполный состав*\n\nЭкипаж *{crew_name}* движется, но не все нажали Начать смену.\nНа линии: {len(shifts)} из {member_count}"
                await send_notification(app, msg, notif["id"])

# crew_id -> когда впервые замечено расхождение (UTC). Хранится в памяти
# процесса — при рестарте бота обнуляется, это ок: просто ещё раз подождём
# DIVERGENCE_DEBOUNCE_MINUTES перед первым уведомлением после рестарта.
_pending_divergence = {}
DIVERGENCE_DEBOUNCE_MINUTES = 8  # ~2 проверки шедулера подряд — не поднимаем тревогу на единичный GPS-скачок

async def check_crew_diverged(app):
    now_utc = datetime.now(timezone.utc)
    today = now_local().date().isoformat()
    crews = supabase.table("crews").select("*, crew_members(*)").eq("is_active", True).execute().data
    seen_crew_ids = set()
    for crew in crews:
        member_count = len(crew.get("crew_members", []))
        if member_count < 2:
            continue
        seen_crew_ids.add(crew["id"])

        shifts = supabase.table("shifts").select("*, employees(full_name)")\
            .eq("crew_id", crew["id"]).eq("date", today)\
            .in_("status", ["active", "break", "tech"]).execute().data
        if len(shifts) < 2:
            _pending_divergence.pop(crew["id"], None)
            continue

        shift_ids = [s["id"] for s in shifts]
        recent_points = supabase.table("gps_tracks").select("shift_id,lat,lng,recorded_at")\
            .in_("shift_id", shift_ids).order("recorded_at", desc=True)\
            .limit(len(shift_ids) * 20).execute().data
        last_point_by_shift = {}
        for p in recent_points:
            last_point_by_shift.setdefault(p["shift_id"], p)

        active_with_points = [{"shift": s, "last_point": last_point_by_shift.get(s["id"])} for s in shifts]
        presence = classify_crew_presence(active_with_points)

        if presence["state"] != "divergent":
            _pending_divergence.pop(crew["id"], None)
            continue

        first_seen = _pending_divergence.get(crew["id"])
        if first_seen is None:
            _pending_divergence[crew["id"]] = now_utc
            continue  # первое обнаружение — ждём подтверждения на следующей проверке, не поднимаем тревогу сразу
        if (now_utc - first_seen).total_seconds() / 60 < DIVERGENCE_DEBOUNCE_MINUTES:
            continue

        existing = supabase.table("notifications").select("*")\
            .eq("crew_id", crew["id"]).eq("type", "crew_diverged")\
            .gte("created_at", today).execute()
        if existing.data:
            last_notif_time = datetime.fromisoformat(existing.data[-1]["created_at"].replace("Z", "+00:00"))
            if (now_utc - last_notif_time).total_seconds() < 7200:  # не чаще раза в 2 часа, пока не сошлись обратно
                continue

        lines = []
        for cluster in presence["clusters"]:
            shift = next(s for s in shifts if s["id"] == cluster[0])
            point = last_point_by_shift.get(shift["id"])
            name = (shift.get("employees") or {}).get("full_name") or "?"
            addr = get_address(point["lat"], point["lng"]) if point else "координаты неизвестны"
            lines.append(f"\U0001f4cd {name}: {addr}")

        notif = supabase.table("notifications").insert({
            "type": "crew_diverged", "crew_id": crew["id"], "message": "Экипаж разошёлся"
        }).execute().data[0]
        crew_name = crew["name"]
        msg = (
            f"⚠️ *Экипаж разошёлся*\n\n"
            f"Экипаж *{crew_name}* — сотрудники в разных местах. Возможно, кто-то не на рабочем месте.\n\n"
            + "\n".join(lines)
        )
        await send_notification(app, msg, notif["id"])

    # Экипажи, которых не проверяли в этот раз (архивированы и т.п.) — чистим debounce-словарь
    for crew_id in list(_pending_divergence.keys()):
        if crew_id not in seen_crew_ids:
            _pending_divergence.pop(crew_id, None)

async def scheduler(app):
    logger.info("Планировщик запущен")
    last_minute = -1
    while True:
        try:
            now = now_local()
            if now.minute != last_minute:
                last_minute = now.minute
                logger.info(f"Планировщик: {now.strftime('%H:%M')} (UTC+5)")
                if now.hour == 9 and now.minute == 1 and now.weekday() < 6:
                    await check_shift_not_started(app)
                if now.minute % 5 == 0:
                    await notify_shift_started(app)
                if now.minute % 5 == 0:
                    await check_shift_summary(app)
                if now.minute % 10 == 0:
                    await check_long_stops(app)
                if now.minute % 5 == 0:
                    await check_incomplete_crews(app)
                if now.minute % 5 == 0:
                    await check_crew_diverged(app)
                if now.hour == 23 and now.minute == 50:
                    await auto_finish_shifts(app)
                if now.hour == 4 and now.minute == 0:
                    await cleanup_old_gps_tracks(app)
        except Exception as e:
            logger.error(f"Ошибка планировщика: {e}")
        await asyncio.sleep(30)

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Telegram бот запускается...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Бот запущен, стартуем планировщик...")
        await scheduler(app)

if __name__ == "__main__":
    asyncio.run(main())