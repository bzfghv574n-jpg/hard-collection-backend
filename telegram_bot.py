import asyncio
import os
import logging
from datetime import datetime, date
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from supabase import create_client, Client
from geopy.distance import geodesic

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# ─── СТАРТ / ПОДПИСКА ────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    full_name = update.effective_user.full_name
    existing = supabase.table("tg_subscribers").select("*").eq("chat_id", chat_id).execute()
    if not existing.data:
        supabase.table("tg_subscribers").insert({"chat_id": chat_id, "full_name": full_name}).execute()
        await update.message.reply_text(
            f"✅ *{full_name}*, вы подписаны на уведомления Hard Collection.\n\n"
            f"Вы будете получать:\n"
            f"• 🔴 Смена не начата до 09:00\n"
            f"• 🛑 Экипаж без движения 60+ мин\n"
            f"• ⚠️ Неполный состав экипажа\n"
            f"• ✅ Итог завершённой смены",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"Вы уже подписаны, *{full_name}* 👍", parse_mode="Markdown")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    supabase.table("tg_subscribers").delete().eq("chat_id", chat_id).execute()
    await update.message.reply_text("❌ Вы отписались от уведомлений.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    crews = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("is_active", True).execute().data
    text = f"📊 *Статус экипажей на {today}*\n\n"
    for crew in crews:
        shifts = supabase.table("shifts").select("*").eq("crew_id", crew["id"]).eq("date", today).execute().data
        active = [s for s in shifts if s["status"] == "active"]
        member_count = len(crew.get("crew_members", []))
        online_count = len(shifts)
        if not shifts:
            emoji, status_text = "⚫", "Не вышли"
        elif len(active) == member_count:
            emoji, status_text = "🟢", "На линии"
        elif active:
            emoji, status_text = "🟡", f"Неполный состав ({online_count}/{member_count})"
        else:
            emoji, status_text = "🟠", "На паузе"
        total_km = sum(float(s.get("total_km") or 0) for s in shifts)
        text += f"{emoji} *«{crew['name']}»* — {status_text}\n"
        text += f"   Пробег: {total_km:.1f} км\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── КНОПКА "ПОД КОНТРОЛЕМ" ──────────────────────────────────────

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
        await query.message.reply_text("✅ Принято. Ситуация под контролем.")

# ─── ОТПРАВКА УВЕДОМЛЕНИЙ ────────────────────────────────────────

async def send_notification(app, message: str, notification_id: str = None):
    subscribers = supabase.table("tg_subscribers").select("chat_id").execute().data
    keyboard = None
    if notification_id:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Под контролем", callback_data=f"ack_{notification_id}"),
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

# ─── ПРОВЕРКИ ────────────────────────────────────────────────────

async def check_shift_not_started(app):
    now = datetime.now()
    if now.weekday() == 6:
        return
    today = date.today().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data
    for crew in crews:
        shifts = supabase.table("shifts").select("*").eq("crew_id", crew["id"]).eq("date", today).execute().data
        if not shifts:
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"]).eq("type", "no_shift").gte("created_at", today).execute()
            if not existing.data:
                notif = supabase.table("notifications").insert({
                    "type": "no_shift",
                    "crew_id": crew["id"],
                    "message": f"Экипаж «{crew['name']}» не начал смену"
                }).execute().data[0]
                msg = (
                    f"🔴 *Смена не начата*\n\n"
                    f"Экипаж *«{crew['name']}»* не вышел на линию.\n"
                    f"Время: {now.strftime('%H:%M')} · Рабочий день начался в 09:00"
                )
                await send_notification(app, msg, notif["id"])

async def check_long_stops(app):
    """Уведомление если экипаж на одной точке 60+ минут"""
    today = date.today().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data

    for crew in crews:
        # Проверяем только активные смены
        active_shifts = supabase.table("shifts").select("*")\
            .eq("crew_id", crew["id"]).eq("date", today)\
            .in_("status", ["active", "break", "tech"]).execute().data
        if not active_shifts:
            continue

        last_points = supabase.table("gps_tracks")\
            .select("lat,lng,speed,recorded_at")\
            .eq("crew_id", crew["id"])\
            .order("recorded_at", desc=True)\
            .limit(20)\
            .execute().data

        if len(last_points) < 2:
            continue

        # Время последней точки
        latest_time = datetime.fromisoformat(last_points[0]["recorded_at"].replace("Z", "+00:00"))
        now_utc = datetime.utcnow().replace(tzinfo=latest_time.tzinfo)
        minutes_since_last = (now_utc - latest_time).total_seconds() / 60

        # Если последняя точка давно — экипаж офлайн, не спамим
        if minutes_since_last > 120:
            continue

        # Смотрим точки за последний час — двигался ли экипаж
        one_hour_ago = now_utc.timestamp() - 3600
        recent_hour = [
            p for p in last_points
            if datetime.fromisoformat(p["recorded_at"].replace("Z", "+00:00")).timestamp() > one_hour_ago
        ]

        if len(recent_hour) < 2:
            continue

        # Общее смещение за час
        first = recent_hour[-1]
        last = recent_hour[0]
        moved_meters = geodesic(
            (first["lat"], first["lng"]),
            (last["lat"], last["lng"])
        ).meters

        if moved_meters < 100:
            # Стоит 60+ минут — отправляем уведомление (не чаще раза в 2 часа)
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"]).eq("type", "long_stop")\
                .gte("created_at", today).execute()

            should_notify = True
            if existing.data:
                last_notif_time = datetime.fromisoformat(
                    existing.data[-1]["created_at"].replace("Z", "+00:00")
                )
                if (now_utc - last_notif_time).total_seconds() < 7200:
                    should_notify = False

            if should_notify:
                notif = supabase.table("notifications").insert({
                    "type": "long_stop",
                    "crew_id": crew["id"],
                    "message": f"Экипаж «{crew['name']}» без движения 60+ мин"
                }).execute().data[0]
                msg = (
                    f"🛑 *Долгая остановка*\n\n"
                    f"Экипаж *«{crew['name']}»* не двигается более *60 минут*.\n"
                    f"Координаты: {last['lat']:.4f}, {last['lng']:.4f}"
                )
                await send_notification(app, msg, notif["id"])

async def check_incomplete_crews(app):
    today = date.today().isoformat()
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
                    "message": f"Экипаж «{crew['name']}» неполный состав"
                }).execute().data[0]
                msg = (
                    f"⚠️ *Неполный состав*\n\n"
                    f"Экипаж *«{crew['name']}»* движется, но не все нажали «Начать смену».\n"
                    f"На линии: {len(shifts)} из {member_count}"
                )
                await send_notification(app, msg, notif["id"])

# ─── ПЛАНИРОВЩИК ─────────────────────────────────────────────────

async def scheduler(app):
    logger.info("Планировщик запущен")
    last_minute = -1
    while True:
        try:
            now = datetime.now()
            # Запускаем проверки только раз в минуту
            if now.minute != last_minute:
                last_minute = now.minute
                logger.info(f"Планировщик: {now.strftime('%H:%M')}")

                # Незапущенные смены — в 09:05
                if now.hour == 9 and now.minute == 5 and now.weekday() < 6:
                    await check_shift_not_started(app)

                # Долгие остановки — каждые 10 минут
                if now.minute % 10 == 0:
                    await check_long_stops(app)

                # Неполный состав — каждые 5 минут
                if now.minute % 5 == 0:
                    await check_incomplete_crews(app)

        except Exception as e:
            logger.error(f"Ошибка планировщика: {e}")

        await asyncio.sleep(30)

# ─── ЗАПУСК ──────────────────────────────────────────────────────

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