import asyncio
import os
import logging
from datetime import datetime, time, date
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from supabase import create_client, Client

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# ─── СТАРТ / ПОДПИСКА ────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    full_name = update.effective_user.full_name

    # Проверяем подписан ли уже
    existing = supabase.table("tg_subscribers").select("*").eq("chat_id", chat_id).execute()
    if not existing.data:
        supabase.table("tg_subscribers").insert({
            "chat_id": chat_id,
            "full_name": full_name,
        }).execute()
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
        await update.message.reply_text(
            f"Вы уже подписаны, *{full_name}* 👍",
            parse_mode="Markdown"
        )

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    supabase.table("tg_subscribers").delete().eq("chat_id", chat_id).execute()
    await update.message.reply_text("❌ Вы отписались от уведомлений.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    crews = supabase.table("crews").select("*, crew_members(*, employees(*))").eq("is_active", True).execute().data
    
    text = f"📊 *Статус экипажей на {today}*\n\n"
    for crew in crews:
        shifts = supabase.table("shifts").select("*").eq("crew_id", crew["id"]).eq("date", today).execute().data
        active = [s for s in shifts if s["status"] == "active"]
        member_count = len(crew.get("crew_members", []))
        online_count = len(shifts)
        
        if not shifts:
            emoji = "⚫"
            status_text = "Не вышли"
        elif len(active) == member_count:
            emoji = "🟢"
            status_text = "На линии"
        elif active:
            emoji = "🟡"
            status_text = f"Неполный состав ({online_count}/{member_count})"
        else:
            emoji = "🟠"
            status_text = "На паузе"
        
        total_km = sum(float(s.get("total_km") or 0) for s in shifts)
        text += f"{emoji} *«{crew['name']}»* — {status_text}\n"
        text += f"   Пробег: {total_km:.1f} км\n\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── КНОПКА "ПОД КОНТРОЛЕМ" ──────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data  # формат: "ack_{notification_id}"
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

async def send_notification(app, message: str, notification_id: str = None, crew_name: str = None):
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

# ─── ПРОВЕРКИ (запускаются по расписанию) ────────────────────────

async def check_shift_not_started(app):
    """Проверяем что все экипажи начали смену до 09:00"""
    now = datetime.now()
    if now.weekday() == 6:  # Воскресенье — не проверяем (только если вышли сами)
        return
    
    today = date.today().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data
    
    for crew in crews:
        shifts = supabase.table("shifts").select("*").eq("crew_id", crew["id"]).eq("date", today).execute().data
        if not shifts:
            # Проверяем не отправляли ли уже уведомление сегодня
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"])\
                .eq("type", "no_shift")\
                .gte("created_at", today)\
                .execute()
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
                await send_notification(app, msg, notif["id"], crew["name"])

async def check_long_stops(app):
    """Проверяем экипажи без движения 60+ минут"""
    today = date.today().isoformat()
    crews = supabase.table("crews").select("*").eq("is_active", True).execute().data
    
    for crew in crews:
        last_points = supabase.table("gps_tracks")\
            .select("lat,lng,recorded_at")\
            .eq("crew_id", crew["id"])\
            .order("recorded_at", desc=True)\
            .limit(10)\
            .execute().data
        
        if len(last_points) < 2:
            continue
        
        # Проверяем двигался ли экипаж последние 60 минут
        latest_time = datetime.fromisoformat(last_points[0]["recorded_at"].replace("Z", ""))
        minutes_since = (datetime.utcnow() - latest_time).seconds // 60
        
        # Проверяем смещение координат
        from geopy.distance import geodesic
        p1 = (last_points[-1]["lat"], last_points[-1]["lng"])
        p2 = (last_points[0]["lat"], last_points[0]["lng"])
        moved_meters = geodesic(p1, p2).meters
        
        if moved_meters < 50 and minutes_since >= 60:
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"])\
                .eq("type", "long_stop")\
                .gte("created_at", today)\
                .execute()
            
            # Не спамим — максимум 1 раз в 2 часа
            if not existing.data or (datetime.utcnow() - datetime.fromisoformat(existing.data[-1]["created_at"].replace("Z", ""))).seconds > 7200:
                notif = supabase.table("notifications").insert({
                    "type": "long_stop",
                    "crew_id": crew["id"],
                    "message": f"Экипаж «{crew['name']}» без движения {minutes_since} мин"
                }).execute().data[0]
                
                msg = (
                    f"🛑 *Долгая остановка*\n\n"
                    f"Экипаж *«{crew['name']}»* без движения *{minutes_since} минут*.\n"
                    f"Координаты: {last_points[0]['lat']:.4f}, {last_points[0]['lng']:.4f}"
                )
                await send_notification(app, msg, notif["id"], crew["name"])

async def check_incomplete_crews(app):
    """Проверяем неполный состав экипажей"""
    today = date.today().isoformat()
    crews = supabase.table("crews").select("*, crew_members(*)").eq("is_active", True).execute().data
    
    for crew in crews:
        member_count = len(crew.get("crew_members", []))
        if member_count < 2:
            continue
        
        shifts = supabase.table("shifts").select("*")\
            .eq("crew_id", crew["id"])\
            .eq("date", today)\
            .in_("status", ["active", "break", "tech"])\
            .execute().data
        
        if 0 < len(shifts) < member_count:
            existing = supabase.table("notifications").select("*")\
                .eq("crew_id", crew["id"])\
                .eq("type", "incomplete_crew")\
                .gte("created_at", today)\
                .execute()
            if not existing.data:
                notif = supabase.table("notifications").insert({
                    "type": "incomplete_crew",
                    "crew_id": crew["id"],
                    "message": f"Экипаж «{crew['name']}» неполный состав"
                }).execute().data[0]
                
                msg = (
                    f"⚠️ *Неполный состав*\n\n"
                    f"Экипаж *«{crew['name']}»* движется, но не все сотрудники нажали «Начать смену».\n"
                    f"На линии: {len(shifts)} из {member_count}"
                )
                await send_notification(app, msg, notif["id"], crew["name"])

async def notify_shift_finished(app, crew_name: str, crew_id: str):
    """Уведомление о завершении смены"""
    today = date.today().isoformat()
    shifts = supabase.table("shifts").select("*").eq("crew_id", crew_id).eq("date", today).execute().data
    
    total_km = sum(float(s.get("total_km") or 0) for s in shifts)
    total_fuel = sum(float(s.get("fuel_used") or 0) for s in shifts)
    total_cost = sum(float(s.get("fuel_cost") or 0) for s in shifts)
    stops = supabase.table("stop_points").select("*").eq("crew_id", crew_id).execute().data
    
    msg = (
        f"✅ *Смена завершена*\n\n"
        f"Экипаж *«{crew_name}»* закончил работу.\n\n"
        f"📍 Точек посещено: *{len(stops)}*\n"
        f"🛣 Пробег: *{total_km:.1f} км*\n"
        f"⛽ Расход: *{total_fuel:.1f} л*\n"
        f"💰 Стоимость топлива: *{total_cost:,.0f} ₸*"
    )
    await send_notification(app, msg)

# ─── ПЛАНИРОВЩИК ─────────────────────────────────────────────────

async def scheduler(app):
    """Запускает проверки по расписанию"""
    while True:
        now = datetime.now()
        
        # Проверка незапущенных смен — в 09:05 по рабочим дням
        if now.hour == 9 and now.minute == 5 and now.weekday() < 6:
            await check_shift_not_started(app)
        
        # Проверка долгих остановок — каждые 10 минут
        if now.minute % 10 == 0:
            await check_long_stops(app)
        
        # Проверка неполного состава — каждые 5 минут
        if now.minute % 5 == 0:
            await check_incomplete_crews(app)
        
        await asyncio.sleep(60)  # проверяем каждую минуту

# ─── ЗАПУСК ──────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Запускаем планировщик в фоне
    async def post_init(application):
        asyncio.create_task(scheduler(application))
    
    app.post_init = post_init
    
    logger.info("Telegram бот запущен...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()