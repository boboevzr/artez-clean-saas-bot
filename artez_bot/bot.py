import os
import logging
import aiohttp
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    WebAppInfo
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import database as db
from database import init_db, upsert_client, save_order, update_order_status, get_client_orders, get_stats, get_next_order_num, get_all_prices, get_price, add_staff, remove_staff, get_staff_by_role, get_client_lang, set_client_lang, get_all_units, get_unit, add_unit, delete_unit, upsert_crm_client, get_client_by_tg_id, update_client_tg_phone, get_client_tg_phone, get_staff_by_tg_id_for_lead, take_lead, is_client_blocked, get_order_by_id, update_order_status_by_id, get_order_activity_by_id, get_route_delivery_info, get_prices_for_services, create_pickup_items, delete_order_items, set_route_stop_status, add_order_activity, get_services, get_order_debt, add_payment_by_driver, save_payment_receipt_file, get_route_channel_info_for_order, get_debt_approvers_bot, approve_debt_close, get_staff_id_by_tg, set_driver_confirmed, get_order_items_for_driver, create_discount_request, get_managers_with_push, apply_auto_discount, resolve_discount_request, reject_discount_request, get_order_full_for_debt, create_debt_approval_db, mark_debt_approval_resolved_by_order, confirm_cash_handover_bot, reject_cash_handover_bot, get_site_user_by_tg_id, check_promo_eligibility, get_live_promo_id_for_user, set_lead_promo

logging.basicConfig(level=logging.INFO)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
ADMIN_ID    = int(os.getenv("ADMIN_ID") or "0")
GROUP_ID    = int(os.getenv("GROUP_ID") or "0")          # общая группа (fallback)
GROUP_SMS_ID         = int(os.getenv("GROUP_SMS_ID") or "0")
GROUP_NEW_CLIENTS_ID = int(os.getenv("GROUP_NEW_CLIENTS_ID") or "0")
SHEETS_URL  = os.getenv("SHEETS_URL", "")
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://artez.uz")
API_URL     = os.getenv("API_URL", "")

# ── SaaS: идентификатор компании ─────────────────────────────────────────
# Устанавливается в Railway env при деплое для каждой компании отдельно.
COMPANY_ID  = int(os.getenv("COMPANY_ID", "1"))

# ── Динамические данные филиалов (загружаются при старте из БД) ───────────
# Структура: [{"slug": "zarafshan", "name_ru": "Зарафшан",
#              "tg_delivery_group_id": -100..., "phones": [...], ...}, ...]
BRANCHES: list[dict] = []

# Настройки сайта — загружаются при старте из API, используются во всех сообщениях
SITE = {
    "contact_short":      "1221",
    "contact_main":       "+998 79 222-12-21",
    "contact_zarafshan_1": "+998 88 200-12-21",
    "contact_zarafshan_2": "+998 94 738-04-44",
    "contact_navoi_1":    "+998 99 750-00-20",
    "contact_navoi_2":    "+998 99 112-48-48",
    "social_tg_group":    "https://t.me/artez_gilam_yuvish",
    "social_tg_bot":      "https://t.me/artez_orders_bot",
    "social_instagram":   "https://www.instagram.com/ziyoboboev/",
}

async def load_branches():
    """Загружает список филиалов компании из БД и кеширует в BRANCHES."""
    global BRANCHES
    try:
        rows = await db.get_branches(COMPANY_ID)
        if rows:
            BRANCHES = [dict(r) for r in rows]
            logging.info(f"✅ Branches loaded: {[b.get('slug') for b in BRANCHES]}")
        else:
            logging.warning("⚠️ No branches found for COMPANY_ID=%s", COMPANY_ID)
    except Exception as e:
        logging.warning(f"load_branches error: {e}")


async def load_site_settings():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API_URL}/settings/site", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    s = data.get("settings", {})
                    for k, v in s.items():
                        if v:
                            SITE[k] = v
                    _rebuild_dynamic_texts()
                    logging.info("✅ Site settings loaded from API")
    except Exception as e:
        logging.warning(f"Could not load site settings: {e}")

def _rebuild_dynamic_texts():
    """Обновляет строки в TEXTS на основе SITE и динамических данных BRANCHES."""
    sh  = SITE.get("contact_short", "")
    mn  = SITE.get("contact_main", "")
    company_name = SITE.get("company_name", "")
    website = WEBSITE_URL

    # Строка контактов для меню — из динамических филиалов
    branches_ru = "\n\n".join(
        f"*{b.get('name_ru', b.get('slug',''))}*\n" +
        "\n".join(f"📱 {p}" for p in (b.get("phones") or []))
        for b in BRANCHES if b.get("phones")
    )
    branches_uz = "\n\n".join(
        f"*{b.get('name_uz', b.get('slug',''))}*\n" +
        "\n".join(f"📱 {p}" for p in (b.get("phones") or []))
        for b in BRANCHES if b.get("phones")
    )

    TEXTS["ru"]["menu_title"] = (
        f"🏠 Главное меню\n\n{company_name}\n"
        f"🌐 [{website}]({website})\n\n"
        f"{'☎️ Короткий номер: ' + sh + chr(10) if sh else ''}"
        f"{'📞 Оператор: ' + mn + chr(10) if mn else ''}"
        + (f"\n{branches_ru}" if branches_ru else "")
    )
    TEXTS["uz"]["menu_title"] = (
        f"🏠 Asosiy menyu\n\n{company_name}\n"
        f"🌐 [{website}]({website})\n\n"
        f"{'☎️ Qisqa raqam: ' + sh + chr(10) if sh else ''}"
        f"{'📞 Operator: ' + mn + chr(10) if mn else ''}"
        + (f"\n{branches_uz}" if branches_uz else "")
    )
    TEXTS["ru"]["order_done"] = (
        f"✅ *Заявка принята!*\n\nМы перезвоним вам в течение 30 минут.\n\n"
        f"Номер заявки: *#{{num}}*\n\n"
        f"{'☎️ Короткий номер: *' + sh + '*' + chr(10) if sh else ''}"
        f"{'📞 ' + mn if mn else ''}"
        f"{{branch_phones}}"
    )
    TEXTS["uz"]["order_done"] = (
        f"✅ *Ariza qabul qilindi!*\n\n30 daqiqa ichida qayta qo'ng'iroq qilamiz.\n\n"
        f"Ariza raqami: *#{{num}}*\n\n"
        f"{'☎️ Qisqa raqam: *' + sh + '*' + chr(10) if sh else ''}"
        f"{'📞 ' + mn if mn else ''}"
        f"{{branch_phones}}"
    )
    TEXTS["ru"]["quick_done"] = (
        f"✅ *Заявка принята!*\n\nМы свяжемся с вами в ближайшее время.\n\n"
        f"{'☎️ Короткий номер: *' + sh + '*' + chr(10) if sh else ''}"
        f"{'📞 ' + mn if mn else ''}"
        f"{{branch_phones}}"
    )
    TEXTS["uz"]["quick_done"] = (
        f"✅ *Ariza qabul qilindi!*\n\nTez orada siz bilan bog'lanamiz.\n\n"
        f"{'☎️ Qisqa raqam: *' + sh + '*' + chr(10) if sh else ''}"
        f"{'📞 ' + mn if mn else ''}"
        f"{{branch_phones}}"
    )
    TEXTS["ru"]["order_rejected"] = (
        f"❌ К сожалению, заявка *{{num}}* не может быть выполнена.\n\n"
        f"Позвоните нам:\n"
        f"{'☎️ ' + sh + chr(10) if sh else ''}{'📞 ' + mn if mn else ''}"
    )
    TEXTS["uz"]["order_rejected"] = (
        f"❌ Afsuski, *{{num}}* arizasi bajarilishi mumkin emas.\n\n"
        f"Bizga qo'ng'iroq qiling:\n"
        f"{'☎️ ' + sh + chr(10) if sh else ''}{'📞 ' + mn if mn else ''}"
    )
    # Текст "Наши филиалы" — динамически из BRANCHES
    branches_detail_ru = "\n\n".join(
        f"🏢 *{b.get('name_ru', b.get('slug',''))}*\n" +
        "\n".join(f"📱 {p}" for p in (b.get("phones") or []))
        for b in BRANCHES
    ) or "Информация о филиалах недоступна"
    branches_detail_uz = "\n\n".join(
        f"🏢 *{b.get('name_uz', b.get('name_ru', b.get('slug','')))}*\n" +
        "\n".join(f"📱 {p}" for p in (b.get("phones") or []))
        for b in BRANCHES
    ) or "Filiallar haqida ma'lumot mavjud emas"
    TEXTS["ru"]["branches_text"] = f"📍 *Наши филиалы*\n\n{branches_detail_ru}"
    TEXTS["uz"]["branches_text"] = f"📍 *Filiallarimiz*\n\n{branches_detail_uz}"

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ── Часовой пояс ──
TASHKENT_TZ = ZoneInfo("Asia/Tashkent")

def now_local():
    return datetime.now(TASHKENT_TZ)

def md_escape(text):
    """Экранирует символы, которые ломают Telegram Markdown-разметку"""
    if not text:
        return ""
    text = str(text)
    for ch in ['_', '*', '[', ']', '`']:
        text = text.replace(ch, f"\\{ch}")
    return text

# ══════════════════════════════════════
#  ПЕРЕВОДЫ
# ══════════════════════════════════════
T = {
    "ru": {
        "choose_lang":    "👋 Добро пожаловать в ARTEZ!\n\nВыберите язык:",
        "lang_set":       "🇷🇺 Выбран русский язык",
        "menu_title":     "🏠 Главное меню\n\nООО «ARTEZ» — профессиональная чистка ковров\n📍 Зарафшан и Навои\n🌐 [artez.uz](https://artez.uz)\n\n☎️ Короткий номер: 1221\n📞 Оператор:\n+998 79 222 12 21\n\n*г. Зарафшан*\n📱 +998 88 200 12 21\n📱 +998 94 738 04 44\n\n*г. Навои*\n📱 +998 99 750 00 20\n📱 +998 99 112 48 48",
        "btn_webapp":     "🌐 Открыть приложение",
        "btn_order":      "📋 Оставить заявку",
        "btn_calc":       "🧮 Калькулятор",
        "btn_prices":     "💰 Цены",
        "btn_branches":   "📍 Филиалы",
        "btn_promo":      "🎁 Акции",
        "btn_status":     "📦 Статус заказа",
        "btn_operator":   "👨‍💼 Оператор",
        "btn_info":       "ℹ️ О компании",
        "btn_profile":    "👤 Мой профиль",
        "profile_text":   "👤 *Ваш профиль*\n\n📛 Имя: {name}\n📞 Телефон: {phone}\n🆔 ID: {uid}\n\n📊 Заявок всего: *{total}*\n✅ Выполнено: *{done}*\n{last}",
        "profile_last":   "📅 Последняя заявка: {date}\n",
        "profile_nophone":"Не указан",
        "profile_link_phone": "📞 Привязать номер",
        "btn_use_saved_phone": "✅ Использовать {phone}",
        "btn_enter_other_phone": "⌨️ Ввести другой номер",
        "ask_phone_saved":"Шаг 2 из 6\n📞 Использовать сохранённый номер?",
        "btn_help":       "🆘 Помощь",
        "btn_settings":   "⚙️ Настройки",
        "btn_change_lang": "🌐 Сменить язык",
        "settings_text":  "⚙️ *Настройки*\n\nЗдесь вы можете изменить язык или открыть справку.",
        "choose_lang_text": "🌐 Выберите язык:",
        "btn_back":       "◀️ Назад",
        "btn_menu":       "🏠 Меню",
        "btn_zarafshan":  "📍 Зарафшан",
        "btn_navoi":      "📍 Навои",
        "ask_name":       "📋 *Оформление заявки*\n\nШаг 1 из 6\n👤 Введите ваше имя:",
        "ask_phone":      "Шаг 2 из 6\n📞 Поделитесь номером или введите вручную:\n\nФормат: +998XXXXXXXXX",
        "btn_share_phone":"📱 Поделиться номером",
        "btn_enter_phone":"⌨️ Ввести другой номер",
        "link_phone_prompt": (
            "🔗 *Привязка номера к сайту ARTEZ*\n\n"
            "Нажмите кнопку ниже, чтобы поделиться своим номером телефона.\n"
            "После этого при регистрации на сайте *artez.uz* вы сможете получить код через Telegram вместо SMS."
        ),
        "link_phone_ok": (
            "✅ *Номер привязан!*\n\n"
            "📱 {phone}\n\n"
            "Теперь зайдите на сайт *artez.uz*, выберите «Регистрация» и нажмите «Получить код в Telegram».\n\n"
            "Если вы уже зарегистрированы — просто войдите в личный кабинет."
        ),
        "link_phone_ok_registered": (
            "✅ *Телефон привязан!*\n\n"
            "📱 {phone}\n\n"
            "Вы уже зарегистрированы на сайте — просто войдите на *artez.uz*."
        ),
        "ask_phone_manual":"✏️ Введите номер в формате:\n+998XXXXXXXXX\n\nПример: +998901234567",
        "phone_invalid":  "⚠️ Неверный формат!\n\nВведите номер строго в формате:\n*+998XXXXXXXXX*\n\nПример: +998901234567",
        "ask_branch":     "Шаг 3 из 6\n🏢 Выберите филиал:",
        "ask_address":    "Шаг 4 из 6\n🏠 Введите адрес вывоза ковра:",
        "ask_location":   "Шаг 5 из 6\n📍 Отправьте локацию места вывоза\n\n_(необязательно — нажмите «Пропустить» если не нужно)_",
        "btn_send_loc":   "📍 Отправить локацию",
        "btn_skip_loc":   "⏭ Пропустить",
        "ask_service":    "Шаг 6 из 6\n🧺 Выберите услугу:",
        "ask_date":       "📅 Выберите дату вывоза:",
        "btn_today":      "📅 Сегодня",
        "btn_tomorrow":   "📅 Завтра",
        "btn_pick_date":  "🗓 Указать дату",
        "ask_date_manual":"✏️ Введите дату в формате ДД.ММ.ГГГГ\n\nПример: 20.06.2026",
        "date_invalid":   "⚠️ Неверный формат даты!\n\nВведите в формате ДД.ММ.ГГГГ\nПример: 20.06.2026",
        "ask_time":       "🕐 Выберите удобное время:",
        "btn_morning":    "🌅 До обеда (08:00-13:00)",
        "btn_evening":    "🌆 После обеда (13:00-20:00)",
        "btn_custom_time":"⏰ Указать время",
        "ask_time_from":  "🕐 Выберите время *начала* вывоза:",
        "ask_time_to":    "Теперь выберите время *окончания*:",
        "btn_use_saved_name":  "👤 {name}",
        "btn_enter_other_name":"✏️ Другое имя",
        "btn_use_saved_addr":  "🏠 Этот адрес",
        "btn_enter_other_addr":"✏️ Другой адрес",
        "order_done":     "✅ *Заявка принята!*\n\nМы перезвоним вам в течение 30 минут.\n\nНомер заявки: *#{num}*\n\n☎️ Короткий номер: *1221*\n📞 +998 79 222-12-21\n\n*Зарафшан:*\n+998 88 200-12-21\n+998 94 738-04-44\n\n*Навои:*\n+998 99 750-00-20\n+998 99 112-48-48",
        "order_rejected": "❌ К сожалению, заявка *{num}* не может быть выполнена.\n\nПозвоните нам:\n☎️ 1221\n📞 +998 79 222-12-21",
        "order_summary":  "📋 *Новая заявка #{num}* (бот)\n━━━━━━━━━━━━━━━\n👤 {name}\n📞 {phone}\n🏢 {branch}\n📍 {city}\n🏠 {address}\n🗺 {location}\n🧺 {service}\n📅 {date}\n🕐 {time}\n━━━━━━━━━━━━━━━\n🕒 {dt}",
        "prices_text":    "💰 *Прайс-лист ARTEZ*\n\n🧺 Стандартная чистка — 12 000 сум/м²\n✨ Глубокая химчистка — 16 000 сум/м²\n🛋 Бытовая техника/Понка — от 16 000 сум/шт\n🌿 Сухая чистка — 14 000 сум/м²\n\n📦 Минимальный заказ — 10 м²\n🚚 Вывоз и доставка — *бесплатно*",
        "calc_selected_header": "🧮 *Калькулятор стоимости*\n\n🧺 Услуга: {svc}",
        "calc_ask_w":     "Введите ширину в сантиметрах:\n\nПример: 200 (= 2 метра)",
        "calc_ask_l":     "Теперь введите длину в сантиметрах:\n\nПример: 300 (= 3 метра)",
        "calc_ask_svc":   "🧮 *Калькулятор стоимости*\n\nВыберите услугу:",
        "calc_result_below_min": "🧮 *Расчёт стоимости*\n\n📐 Размер: {w} × {l} см = {sqm} {unit}\n🧺 {svc}\n💰 {price} сум/{unit}\n\n⚠️ Ваш размер {sqm} {unit} — меньше мин. заказа ({min_order} {unit})\n💵 *Итого: {total} сум* _(за {min_order} {unit})_",
        "calc_result_no_min": "🧮 *Расчёт стоимости*\n\n📐 Размер: {w} × {l} см = {sqm} {unit}\n🧺 {svc}\n💰 {price} сум/{unit}\n\n💵 *Итого: {total} сум*",
        "branches_text":  "📍 *Наши филиалы*\n\n🏢 *Филиал Зарафшан*\nОбслуживает: Зарафшан, Учкудук, Тамдинский район\n📞 1221\n📱 +998 79 222-12-21\n📱 +998 88 200-12-21\n📱 +998 94 738-04-44\n\n🏢 *Филиал Навои*\nОбслуживает: Навои и все остальные районы области\n📞 1221\n📱 +998 79 222-12-21\n📱 +998 99 750-00-20\n📱 +998 99 112-48-48",
        "promo_text":     "🎁 *Акции и скидки*\n\n🔥 При заказе от 3 ковров — скидка до 20%\n🚚 На все заказы — бесплатная доставка и забор\n🚗 Если у вас свой автомобиль — скидка до 20% на страховой полис ОСАГО\n📢 Подписчикам нашей Telegram-группы и Instagram — скидка до 30%\n\nПодпишитесь и получите скидку 👇",
        "btn_promo_telegram": "📢 Telegram-группа",
        "btn_promo_instagram": "📸 Instagram",
        "promo_campaign_full": "🎉🔥 *{title}* 🔥🎉\n\n{text}\n\n💥 Скидка *{pct:.0f}%* на первый заказ!\n⏳ Успейте оформить заявку до *{deadline}* — предложение действует всего 48 часов!",
        "promo_campaign_silent": "🎁 Напоминаем: скидка *{pct:.0f}%* на первый заказ ещё действует ⏳ осталось ~{hours} ч. Успейте оформить заявку!",
        "info_text":      "ℹ️ *О компании ARTEZ*\n\nООО «ARTEZ» — профессиональная чистка ковров в Навоийской области.\n\n🏢 Два филиала: Зарафшан и Навои\n🚚 Бесплатный вывоз и доставка\n⚡ Срок чистки от 24 часов\n🛡 Бережное отношение к коврам\n\n🌐 [artez.uz](https://artez.uz)\n📢 Telegram-группа: [artez_gilam_yuvish](https://t.me/artez_gilam_yuvish)\n📸 Instagram: [@ziyoboboev](https://www.instagram.com/ziyoboboev/)\n\n☎️ Короткий номер: 1221\n📞 Оператор:\n+998 79 222 12 21\n\n*г. Зарафшан*\n📱 +998 88 200 12 21\n📱 +998 94 738 04 44\n\n*г. Навои*\n📱 +998 99 750 00 20\n📱 +998 99 112 48 48",
        "help_text":      "🆘 *Помощь*\n\n/start — Главное меню\n/order — Оставить заявку\n/calc — Калькулятор\n/prices — Цены\n/branches — Филиалы\n\nПо всем вопросам: 📞 1221",
        "status_text":    "📦 *Статус заказа*\n\nДля проверки статуса заказа позвоните нам:\n📞 1221\n📱 +998 79 222-12-21\n\nИли напишите оператору 👇",
        "status_menu_title": "📦 *Статус заказа*\n\nВыберите категорию:",
        "status_btn_new":       "🆕 Новые",
        "status_btn_progress":  "🔄 В работе",
        "status_btn_done":      "✅ Выполнено",
        "status_btn_cancelled": "❌ Отказано",
        "status_empty":   "📦 *Статус заказа*\n\nУ вас пока нет заявок.",
        "status_group_empty": "В этой категории заявок нет.",
        "status_order_line":  "📋 *{num}*\n🧺 {service}\n📅 {date}\n📍 Статус: {status}",
        "btn_back_to_status": "◀️ К категориям",
        "operator_text":  "👨‍💼 Соединяю с оператором...\n\nНапишите ваш вопрос — оператор ответит в ближайшее время.",
        "operator_msg":   "💬 *Сообщение клиенту*\n\n👤 {name}\n💬 {msg}\n🆔 Chat: {cid}",
        "cancel":         "❌ Заявка отменена. Возвращаемся в меню.",
        "btn_cancel":     "❌ Отмена",
        "ask_order_type": "📋 Выберите тип заявки:",
        "btn_order_quick":"⚡ Быстрая заявка",
        "btn_order_full": "📋 Подробная заявка",
        "quick_ask_name": "⚡ *Быстрая заявка*\n\nШаг 1 из 3\n👤 Введите ваше имя:",
        "quick_ask_phone":"Шаг 2 из 3\n📞 Поделитесь номером или введите вручную:\n\nФормат: +998XXXXXXXXX",
        "quick_ask_branch":"Шаг 3 из 3\n🏢 Выберите филиал:",
        "quick_done":     "✅ *Заявка принята!*\n\nМы свяжемся с вами в ближайшее время.\n\n☎️ Короткий номер: *1221*\n📞 +998 79 222-12-21",
        "btn_svc_carpet":      "🧺 Чистка ковра",
        "btn_svc_carpet_home": "🏠 Чистка ковра на дому",
        "btn_svc_sofa":        "🛋 Чистка диван, кресло",
        "btn_svc_mattress":    "🛏 Чистка матрас, одеяло",
        "btn_svc_curtains":    "🪟 Чистка штор",
        "ask_service_type":    "Тип услуги:",
        "btn_type_standard":   "🧺 Стандартный",
        "btn_type_express":    "⚡ Быстрый",
        "invalid_num":    "⚠️ Пожалуйста, введите число. Например: 200",
        "operator_fwd":   "✅ Ваше сообщение передано оператору. Ожидайте ответа.",
    },
    "uz": {
        "choose_lang":    "👋 ARTEZ ga xush kelibsiz!\n\nTilni tanlang:",
        "lang_set":       "🇺🇿 O'zbek tili tanlandi",
        "menu_title":     "🏠 Asosiy menyu\n\nARTEZ MChJ — professional gilam tozalash\n📍 Zarafshon va Navoiy\n🌐 [artez.uz](https://artez.uz)\n\n☎️ Qisqa raqam: 1221\n📞 Operator:\n+998 79 222 12 21\n\n*Zarafshon shahri*\n📱 +998 88 200 12 21\n📱 +998 94 738 04 44\n\n*Navoiy shahri*\n📱 +998 99 750 00 20\n📱 +998 99 112 48 48",
        "btn_webapp":     "🌐 Ilovani ochish",
        "btn_order":      "📋 Ariza qoldirish",
        "btn_calc":       "🧮 Kalkulyator",
        "btn_prices":     "💰 Narxlar",
        "btn_branches":   "📍 Filiallar",
        "btn_promo":      "🎁 Aksiyalar",
        "btn_status":     "📦 Buyurtma holati",
        "btn_operator":   "👨‍💼 Operator",
        "btn_info":       "ℹ️ Kompaniya haqida",
        "btn_profile":    "👤 Mening profilim",
        "profile_text":   "👤 *Profilingiz*\n\n📛 Ism: {name}\n📞 Telefon: {phone}\n🆔 ID: {uid}\n\n📊 Jami buyurtmalar: *{total}*\n✅ Bajarildi: *{done}*\n{last}",
        "profile_last":   "📅 Oxirgi buyurtma: {date}\n",
        "profile_nophone":"Ko'rsatilmagan",
        "profile_link_phone": "📞 Raqam ulash",
        "btn_use_saved_phone": "✅ {phone} dan foydalanish",
        "btn_enter_other_phone": "⌨️ Boshqa raqam kiritish",
        "ask_phone_saved":"2-qadam (6 dan)\n📞 Saqlangan raqamdan foydalanasizmi?",
        "btn_help":       "🆘 Yordam",
        "btn_settings":   "⚙️ Sozlamalar",
        "btn_change_lang": "🌐 Tilni o'zgartirish",
        "settings_text":  "⚙️ *Sozlamalar*\n\nBu yerda tilni o'zgartirishingiz yoki yordam bo'limini ochishingiz mumkin.",
        "choose_lang_text": "🌐 Tilni tanlang:",
        "btn_back":       "◀️ Orqaga",
        "btn_menu":       "🏠 Menyu",
        "btn_zarafshan":  "📍 Zarafshon",
        "btn_navoi":      "📍 Navoiy",
        "ask_name":       "📋 *Ariza rasmiylashtirish*\n\n1-qadam (6 dan)\n👤 Ismingizni kiriting:",
        "ask_phone":      "2-qadam (6 dan)\n📞 Raqamingizni ulashing yoki qo'lda kiriting:\n\nFormat: +998XXXXXXXXX",
        "btn_share_phone":"📱 Raqamni ulashish",
        "btn_enter_phone":"⌨️ Boshqa raqam kiritish",
        "link_phone_prompt": (
            "🔗 *Sayt raqamini bog'lash*\n\n"
            "Quyidagi tugmani bosing va raqamingizni ulashing.\n"
            "Keyin *artez.uz* saytida ro'yxatdan o'tishda kodni SMS o'rniga Telegram orqali olishingiz mumkin."
        ),
        "link_phone_ok": (
            "✅ *Raqam bog'landi!*\n\n"
            "📱 {phone}\n\n"
            "*artez.uz* saytiga o'ting, «Ro'yxatdan o'tish» ni tanlang va «Telegram orqali kod olish» tugmasini bosing."
        ),
        "link_phone_ok_registered": (
            "✅ *Raqam bog'landi!*\n\n"
            "📱 {phone}\n\n"
            "Siz allaqachon saytda ro'yxatdan o'tgansiz — *artez.uz* ga kiring."
        ),
        "ask_phone_manual":"✏️ Raqamni quyidagi formatda kiriting:\n+998XXXXXXXXX\n\nMisol: +998901234567",
        "phone_invalid":  "⚠️ Noto'g'ri format!\n\nRaqamni qat'iy formatda kiriting:\n*+998XXXXXXXXX*\n\nMisol: +998901234567",
        "ask_branch":     "3-qadam (6 dan)\n🏢 Filialni tanlang:",
        "ask_address":    "4-qadam (6 dan)\n🏠 Gilamni olib ketish manzilini kiriting:",
        "ask_location":   "5-qadam (6 dan)\n📍 Olib ketish joylashuvini yuboring\n\n_(ixtiyoriy — kerak bo'lmasa «O'tkazib yuborish» tugmasini bosing)_",
        "btn_send_loc":   "📍 Joylashuvni yuborish",
        "btn_skip_loc":   "⏭ O'tkazib yuborish",
        "ask_service":    "6-qadam (6 dan)\n🧺 Xizmatni tanlang:",
        "ask_date":       "📅 Olib ketish sanasini tanlang:",
        "btn_today":      "📅 Bugun",
        "btn_tomorrow":   "📅 Ertaga",
        "btn_pick_date":  "🗓 Sanani kiritish",
        "ask_date_manual":"✏️ Sanani KK.OO.YYYY formatida kiriting\n\nMisol: 20.06.2026",
        "date_invalid":   "⚠️ Sana formati noto'g'ri!\n\nKK.OO.YYYY formatida kiriting\nMisol: 20.06.2026",
        "ask_time":       "🕐 Qulay vaqtni tanlang:",
        "btn_morning":    "🌅 Tushgacha (08:00-13:00)",
        "btn_evening":    "🌆 Tushdan keyin (13:00-20:00)",
        "btn_custom_time":"⏰ Vaqtni ko'rsatish",
        "ask_time_from":  "🕐 Olib ketish *boshlanish* vaqtini tanlang:",
        "ask_time_to":    "Endi olib ketish *tugash* vaqtini tanlang:",
        "btn_use_saved_name":  "👤 {name}",
        "btn_enter_other_name":"✏️ Boshqa ism",
        "btn_use_saved_addr":  "🏠 Shu manzil",
        "btn_enter_other_addr":"✏️ Boshqa manzil",
        "order_done":     "✅ *Ariza qabul qilindi!*\n\n30 daqiqa ichida qayta qo'ng'iroq qilamiz.\n\nAriza raqami: *#{num}*\n\n☎️ Qisqa raqam: *1221*\n📞 +998 79 222-12-21\n\n*Zarafshon:*\n+998 88 200-12-21\n+998 94 738-04-44\n\n*Navoiy:*\n+998 99 750-00-20\n+998 99 112-48-48",
        "order_rejected": "❌ Afsuski, *{num}* arizasi bajarilishi mumkin emas.\n\nBizga qo'ng'iroq qiling:\n☎️ 1221\n📞 +998 79 222-12-21",
        "order_summary":  "📋 *Yangi ariza #{num}* (bot)\n━━━━━━━━━━━━━━━\n👤 {name}\n📞 {phone}\n🏢 {branch}\n📍 {city}\n🏠 {address}\n🗺 {location}\n🧺 {service}\n📅 {date}\n🕐 {time}\n━━━━━━━━━━━━━━━\n🕒 {dt}",
        "prices_text":    "💰 *ARTEZ narx-navo*\n\n🧺 Standart tozalash — 12 000 so'm/m²\n✨ Chuqur kimyoviy — 16 000 so'm/m²\n🛋 Maishiy texnika/Ponka — 16 000 so'mdan/dona\n🌿 Quruq tozalash — 14 000 so'm/m²\n\n📦 Minimal buyurtma — 10 m²\n🚚 Olib ketish va yetkazish — *bepul*",
        "calc_selected_header": "🧮 *Narx kalkulyatori*\n\n🧺 Xizmat: {svc}",
        "calc_ask_w":     "Enini santimetrda kiriting:\n\nMisol: 200 (= 2 metr)",
        "calc_ask_l":     "Endi bo'yini santimetrda kiriting:\n\nMisol: 300 (= 3 metr)",
        "calc_ask_svc":   "🧮 *Narx kalkulyatori*\n\nXizmatni tanlang:",
        "calc_result_below_min": "🧮 *Narx hisobi*\n\n📐 O'lcham: {w} × {l} sm = {sqm} {unit}\n🧺 {svc}\n💰 {price} so'm/{unit}\n\n⚠️ Sizning o'lchamingiz {sqm} {unit} — minimal buyurtmadan kam ({min_order} {unit})\n💵 *Jami: {total} so'm* _({min_order} {unit} uchun)_",
        "calc_result_no_min": "🧮 *Narx hisobi*\n\n📐 O'lcham: {w} × {l} sm = {sqm} {unit}\n🧺 {svc}\n💰 {price} so'm/{unit}\n\n💵 *Jami: {total} so'm*",
        "branches_text":  "📍 *Filiallarimiz*\n\n🏢 *Zarafshon filiali*\nXizmat ko'rsatadi: Zarafshon, Uchquduq, Tomdi tumani\n📞 1221\n📱 +998 79 222-12-21\n📱 +998 88 200-12-21\n📱 +998 94 738-04-44\n\n🏢 *Navoiy filiali*\nXizmat ko'rsatadi: Navoiy va viloyatning boshqa tumanlari\n📞 1221\n📱 +998 79 222-12-21\n📱 +998 99 750-00-20\n📱 +998 99 112-48-48",
        "promo_text":     "🎁 *Aksiyalar va chegirmalar*\n\n🔥 3 ta va undan ko'p gilam buyurtma qilsangiz — 20% gacha chegirma\n🚚 Barcha buyurtmalar uchun — bepul olib ketish va yetkazish\n🚗 Agar shaxsiy avtomobilingiz bo'lsa — OSAGO sug'urta polisiga 20% gacha chegirma\n📢 Telegram-guruhimiz va Instagram'ga obuna bo'lganlar uchun — 30% gacha chegirma\n\nObuna bo'ling va chegirma oling 👇",
        "btn_promo_telegram": "📢 Telegram-guruh",
        "btn_promo_instagram": "📸 Instagram",
        "promo_campaign_full": "🎉🔥 *{title}* 🔥🎉\n\n{text}\n\n💥 Birinchi buyurtmaga *{pct:.0f}%* chegirma!\n⏳ *{deadline}* gacha ulgurib qoling — taklif atigi 48 soat amal qiladi!",
        "promo_campaign_silent": "🎁 Eslatma: birinchi buyurtmaga *{pct:.0f}%* chegirma hali amal qilmoqda ⏳ ~{hours} soat qoldi. Ulgurib qoling!",
        "info_text":      "ℹ️ *ARTEZ haqida*\n\nARTEZ MChJ — Navoiy viloyatida professional gilam tozalash.\n\n🏢 Ikki filial: Zarafshon va Navoiy\n🚚 Bepul olib ketish va yetkazish\n⚡ Tozalash muddati 24 soatdan\n🛡 Gilamlarga ehtiyotkorona munosabat\n\n🌐 [artez.uz](https://artez.uz)\n📢 Telegram-guruh: [artez_gilam_yuvish](https://t.me/artez_gilam_yuvish)\n📸 Instagram: [@ziyoboboev](https://www.instagram.com/ziyoboboev/)\n\n☎️ Qisqa raqam: 1221\n📞 Operator:\n+998 79 222 12 21\n\n*Zarafshon shahri*\n📱 +998 88 200 12 21\n📱 +998 94 738 04 44\n\n*Navoiy shahri*\n📱 +998 99 750 00 20\n📱 +998 99 112 48 48",
        "help_text":      "🆘 *Yordam*\n\n/start — Asosiy menyu\n/order — Ariza qoldirish\n/calc — Kalkulyator\n/prices — Narxlar\n/branches — Filiallar\n\nBarcha savollar uchun: 📞 1221",
        "status_text":    "📦 *Buyurtma holati*\n\nBuyurtma holatini tekshirish uchun qo'ng'iroq qiling:\n📞 1221\n📱 +998 79 222-12-21\n\nYoki operatorga yozing 👇",
        "status_menu_title": "📦 *Buyurtma holati*\n\nKategoriyani tanlang:",
        "status_btn_new":       "🆕 Yangi",
        "status_btn_progress":  "🔄 Bajarilmoqda",
        "status_btn_done":      "✅ Bajarildi",
        "status_btn_cancelled": "❌ Bekor qilindi",
        "status_empty":   "📦 *Buyurtma holati*\n\nSizda hali buyurtmalar yo'q.",
        "status_group_empty": "Bu kategoriyada buyurtmalar yo'q.",
        "status_order_line":  "📋 *{num}*\n🧺 {service}\n📅 {date}\n📍 Holat: {status}",
        "btn_back_to_status": "◀️ Kategoriyalarga",
        "operator_text":  "👨‍💼 Operator bilan bog'lanmoqda...\n\nSavolingizni yozing — operator tez orada javob beradi.",
        "operator_msg":   "💬 *Mijozdan xabar*\n\n👤 {name}\n💬 {msg}\n🆔 Chat: {cid}",
        "cancel":         "❌ Ariza bekor qilindi. Menyuga qaytamiz.",
        "btn_cancel":     "❌ Bekor qilish",
        "ask_order_type": "📋 Ariza turini tanlang:",
        "btn_order_quick":"⚡ Tezkor ariza",
        "btn_order_full": "📋 Batafsil ariza",
        "quick_ask_name": "⚡ *Tezkor ariza*\n\n1-qadam (3 dan)\n👤 Ismingizni kiriting:",
        "quick_ask_phone":"2-qadam (3 dan)\n📞 Raqamingizni ulashing yoki qo'lda kiriting:\n\nFormat: +998XXXXXXXXX",
        "quick_ask_branch":"3-qadam (3 dan)\n🏢 Filialni tanlang:",
        "quick_done":     "✅ *Ariza qabul qilindi!*\n\nTez orada siz bilan bog'lanamiz.\n\n☎️ Qisqa raqam: *1221*\n📞 +998 79 222-12-21",
        "btn_svc_carpet":      "🧺 Gilam tozalash",
        "btn_svc_carpet_home": "🏠 Gilamni uyda tozalash",
        "btn_svc_sofa":        "🛋 Divan, kreslo tozalash",
        "btn_svc_mattress":    "🛏 Matras, ko'rpa tozalash",
        "btn_svc_curtains":    "🪟 Parda tozalash",
        "ask_service_type":    "Xizmat turi:",
        "btn_type_standard":   "🧺 Standart",
        "btn_type_express":    "⚡ Tezkor",
        "invalid_num":    "⚠️ Iltimos, son kiriting. Masalan: 200",
        "operator_fwd":   "✅ Xabaringiz operatorga yuborildi. Javob kuting.",
    }
}

CITIES = {
    "zarafshan": {
        "ru": ["г. Зарафшан","г. Учкудук","Тамдинский район"],
        "uz": ["Zarafshon sh.","Uchquduq sh.","Tomdi tumani"]
    },
    "navoi": {
        "ru": ["г. Навои","Кармана","Навбахор","Хатирчи","Нурата","Конимех","Зафаробод"],
        "uz": ["Navoiy sh.","Karmana","Navbahor","Xatirchi","Nurata","Konimex","Zafarobod"]
    }
}

# Кэш цен из БД: {service_key: {type_key: {"price":.., "unit":.., "unit_key":.., "min_order":..}}}
PRICE_CACHE = {}
# Кэш единиц измерения: {key: {"name_ru":.., "name_uz":.., "symbol_ru":.., "symbol_uz":..}}
UNIT_CACHE = {}

# Дефолты на случай, если БД недоступна или таблица prices пуста
DEFAULT_PRICES = {
    "carpet":      {"standard": {"price": 12000, "unit": "sum/m2", "unit_key": "m2", "min_order": 10.0}, "express": {"price": 16000, "unit": "sum/m2", "unit_key": "m2", "min_order": 10.0}},
    "carpet_home": {"standard": {"price": 14000, "unit": "sum/m2", "unit_key": "m2", "min_order": 10.0}, "express": {"price": 18000, "unit": "sum/m2", "unit_key": "m2", "min_order": 10.0}},
    "sofa":        {"standard": {"price": 16000, "unit": "sum/m2", "unit_key": "m2", "min_order": None}, "express": {"price": 20000, "unit": "sum/m2", "unit_key": "m2", "min_order": None}},
    "mattress":    {"standard": {"price": 16000, "unit": "sum/m2", "unit_key": "m2", "min_order": None}, "express": {"price": 20000, "unit": "sum/m2", "unit_key": "m2", "min_order": None}},
    "curtains":    {"standard": {"price": 14000, "unit": "sum/m2", "unit_key": "m2", "min_order": None}, "express": {"price": 18000, "unit": "sum/m2", "unit_key": "m2", "min_order": None}},
}

DEFAULT_UNITS = {
    "m2":  {"name_ru": "Квадратный метр", "name_uz": "Kvadrat metr", "symbol_ru": "м²", "symbol_uz": "m²"},
    "m":   {"name_ru": "Метр",            "name_uz": "Metr",         "symbol_ru": "м",  "symbol_uz": "m"},
    "pcs": {"name_ru": "Штука",           "name_uz": "Dona",         "symbol_ru": "шт", "symbol_uz": "dona"},
    "cm":  {"name_ru": "Сантиметр",       "name_uz": "Santimetr",    "symbol_ru": "см", "symbol_uz": "sm"},
    "cm2": {"name_ru": "Кв. сантиметр",   "name_uz": "Kv. santimetr","symbol_ru": "см²","symbol_uz": "sm²"},
    "kg":  {"name_ru": "Килограмм",       "name_uz": "Kilogramm",    "symbol_ru": "кг", "symbol_uz": "kg"},
}

import time as _time
_PRICE_CACHE_TS = 0.0
_UNIT_CACHE_TS  = 0.0
PRICE_TTL = 60  # секунд — обновляем кэш цен каждую минуту

async def load_prices():
    """Загружает цены из БД в PRICE_CACHE. При ошибке/пустой БД использует дефолты."""
    global PRICE_CACHE, _PRICE_CACHE_TS
    try:
        data = await get_all_prices()
    except Exception as e:
        logging.warning(f"load_prices error: {e}")
        data = {}
    if not data:
        data = DEFAULT_PRICES
    PRICE_CACHE = data
    _PRICE_CACHE_TS = _time.monotonic()

async def load_units():
    """Загружает единицы измерения из БД в UNIT_CACHE."""
    global UNIT_CACHE, _UNIT_CACHE_TS
    try:
        rows = await get_all_units()
        data = {r["key"]: {
            "name_ru": r["name_ru"], "name_uz": r["name_uz"],
            "symbol_ru": r["symbol_ru"], "symbol_uz": r["symbol_uz"],
        } for r in rows}
    except Exception as e:
        logging.warning(f"load_units error: {e}")
        data = {}
    if not data:
        data = DEFAULT_UNITS
    UNIT_CACHE = data
    _UNIT_CACHE_TS = _time.monotonic()

async def ensure_prices_fresh():
    """Перезагружает кэш если прошло больше PRICE_TTL секунд."""
    if _time.monotonic() - _PRICE_CACHE_TS > PRICE_TTL:
        await load_prices()
    if _time.monotonic() - _UNIT_CACHE_TS > PRICE_TTL:
        await load_units()
    if _time.monotonic() - _SVC_CACHE_TS > PRICE_TTL:
        await load_services()

def get_unit_symbol(unit_key, uid=None):
    is_uz = uid is not None and lang(uid) == "uz"
    entry = UNIT_CACHE.get(unit_key) or DEFAULT_UNITS.get(unit_key, DEFAULT_UNITS["m2"])
    return entry["symbol_uz"] if is_uz else entry["symbol_ru"]

def get_cached_price(service_key: str, type_key: str):
    entry = PRICE_CACHE.get(service_key, {}).get(type_key)
    if entry:
        return entry["price"]
    fallback = DEFAULT_PRICES.get(service_key, {}).get(type_key)
    return fallback["price"] if fallback else 12000

def get_cached_min_order(service_key: str, type_key: str):
    entry = PRICE_CACHE.get(service_key, {}).get(type_key)
    if entry and "min_order" in entry:
        return entry["min_order"]
    fallback = DEFAULT_PRICES.get(service_key, {}).get(type_key)
    return fallback["min_order"] if fallback else None

def get_cached_unit_key(service_key: str, type_key: str):
    entry = PRICE_CACHE.get(service_key, {}).get(type_key)
    if entry and entry.get("unit_key"):
        return entry["unit_key"]
    fallback = DEFAULT_PRICES.get(service_key, {}).get(type_key)
    return fallback["unit_key"] if fallback else "m2"


SVC_KEY_MAP  = {
    "carpet":      "btn_svc_carpet",
    "carpet_home": "btn_svc_carpet_home",
    "sofa":        "btn_svc_sofa",
    "mattress":    "btn_svc_mattress",
    "curtains":    "btn_svc_curtains",
}
TYPE_KEY_MAP = {"standard": "btn_type_standard", "express": "btn_type_express"}

# ── Кэш услуг из БД (название RU/UZ, emoji, порядок) ──
SVC_CACHE: dict = {}  # {key: {name_ru, name_uz, emoji, order_idx}}
_SVC_CACHE_TS = 0.0

async def load_services():
    global SVC_CACHE, _SVC_CACHE_TS
    try:
        rows = await get_services()
        SVC_CACHE = {r["key"]: r for r in rows}
    except Exception as e:
        logging.warning(f"load_services error: {e}")
    _SVC_CACHE_TS = _time.monotonic()

def get_svc_name(svc: str, uid: int) -> str:
    """Название услуги из БД (с emoji), с фолбеком на i18n."""
    entry = SVC_CACHE.get(svc)
    if entry:
        is_uz = lang(uid) == "uz"
        name  = entry.get("name_uz" if is_uz else "name_ru") or svc
        emoji = entry.get("emoji") or ""
        return f"{emoji} {name}".strip()
    i18n_key = SVC_KEY_MAP.get(svc)
    return t(uid, i18n_key) if i18n_key else svc

def svc_display_name(uid, svc, svctype):
    type_name = t(uid, TYPE_KEY_MAP.get(svctype, "btn_type_standard"))
    return f"{get_svc_name(svc, uid)} ({type_name})"

# Услуги, для которых действует минимальный заказ 10 м²
MIN_ORDER_SERVICES = {"carpet", "carpet_home"}

# Группы статусов заказа для раздела «Статус заказа»
STATUS_GROUPS = {
    "new":       ["new", "confirmed"],
    "progress":  ["pickup", "received", "washing", "packing", "ready", "delivery"],
    "done":      ["delivered"],
    "cancelled": ["cancelled"],
}

ORDER_STATUS_NAMES_RU = {
    "new":       "🆕 Новый",
    "confirmed": "✅ Подтверждён",
    "pickup":    "🚗 Вывоз",
    "received":  "📥 В мастерской",
    "washing":   "🧼 Мойка",
    "drying":    "💨 Сушка",
    "packing":   "📦 Упаковка",
    "ready":     "✅ Готов",
    "delivery":  "🚚 Доставка",
    "delivered": "✅ Доставлен",
    "cancelled": "❌ Отменён",
}
ORDER_STATUS_NAMES_UZ = {
    "new":       "🆕 Yangi",
    "confirmed": "✅ Tasdiqlangan",
    "pickup":    "🚗 Olib ketish",
    "received":  "📥 Ustaxonada",
    "washing":   "🧼 Yuvish",
    "drying":    "💨 Quritish",
    "packing":   "📦 Qadoqlash",
    "ready":     "✅ Tayyor",
    "delivery":  "🚚 Yetkazish",
    "delivered": "✅ Yetkazildi",
    "cancelled": "❌ Bekor qilindi",
}

def order_status_name(uid, status):
    names = ORDER_STATUS_NAMES_UZ if lang(uid) == "uz" else ORDER_STATUS_NAMES_RU
    return names.get(status, status)


# Человекочитаемые названия услуг/типов для команд админа
SERVICE_KEYS = ["carpet", "carpet_home", "sofa", "mattress", "curtains"]
TYPE_KEYS    = ["standard", "express"]
SERVICE_NAMES_RU = {
    "carpet":      "Чистка ковра",
    "carpet_home": "Чистка ковра на дому",
    "sofa":        "Чистка диван/кресло",
    "mattress":    "Чистка матрас/одеяло",
    "curtains":    "Чистка штор",
}
TYPE_NAMES_RU = {"standard": "Стандартный", "express": "Быстрый"}

SERVICE_NAMES_UZ = {
    "carpet":      "Gilam tozalash",
    "carpet_home": "Gilamni uyda tozalash",
    "sofa":        "Divan/kreslo tozalash",
    "mattress":    "Matras/ko'rpa tozalash",
    "curtains":    "Parda tozalash",
}
TYPE_NAMES_UZ = {"standard": "Standart", "express": "Tezkor"}

def build_prices_text(uid):
    is_uz = lang(uid) == "uz"
    title = "💰 ARTEZ narx-navo" if is_uz else "💰 Прайс-лист ARTEZ"
    currency = "so'm" if is_uz else "сум"
    lines = [title, ""]
    min_groups: dict = {}  # {(min_val, unit_sym): [svc_name,...]}

    active_svcs = sorted(SVC_CACHE.keys(), key=lambda k: SVC_CACHE[k].get("order_idx", 0)) if SVC_CACHE else SERVICE_KEYS
    for svc in active_svcs:
        svc_name = get_svc_name(svc, uid)
        prices = PRICE_CACHE.get(svc, DEFAULT_PRICES.get(svc, {}))
        std = prices.get("standard")
        exp = prices.get("express")
        if not std and not exp:
            continue
        entry = std or exp
        unit_sym = get_unit_symbol(entry.get("unit_key", "m2"), uid)
        price_parts = []
        if std:
            price_parts.append(f"{std['price']:,}".replace(",", " "))
        if exp:
            price_parts.append(f"{exp['price']:,}".replace(",", " "))
        lines.append(f"🔹 {svc_name} ")
        lines.append(f"— {' / '.join(price_parts)} {currency}/{unit_sym}")
        if std and std.get("min_order"):
            key = (std["min_order"], unit_sym)
            min_groups.setdefault(key, []).append(svc_name)

    lines.append("")
    if min_groups:
        if is_uz:
            lines.append("📦 Min buyurtma: ")
            for (mo, unit_sym), svc_names in min_groups.items():
                mo_str = int(mo) if mo == int(mo) else mo
                lines.append(f"{mo_str} {unit_sym} ({', '.join(svc_names)}) ")
            lines.append("Standart / Ekspress")
            lines.append("🚚 Olib ketish va yetkazish — bepul")
        else:
            lines.append("📦 Мин. заказ: ")
            for (mo, unit_sym), svc_names in min_groups.items():
                mo_str = int(mo) if mo == int(mo) else mo
                lines.append(f"{mo_str} {unit_sym} ({', '.join(svc_names)}) ")
            lines.append("Стандарт / Экспресс")
            lines.append("🚚 Вывоз и доставка — бесплатно")
    else:
        if is_uz:
            lines.append("Standart / Ekspress")
            lines.append("🚚 Olib ketish va yetkazish — bepul")
        else:
            lines.append("Стандарт / Экспресс")
            lines.append("🚚 Вывоз и доставка — бесплатно")
    return "\n".join(lines)


# ── Хранилище языков и данных ──
user_lang    = {}
user_data_db = {}

def lang(uid): return user_lang.get(uid, "ru")
def t(uid, key): return T[lang(uid)].get(key, key)

# ══════════════════════════════════════
#  FSM STATES
# ══════════════════════════════════════
class OrderForm(StatesGroup):
    name        = State()
    phone       = State()
    branch      = State()
    address     = State()
    location    = State()
    service     = State()
    service_type = State()
    date        = State()
    time        = State()
    time_from   = State()   # выбор начала (grid) после «Указать время»
    time_to     = State()   # выбор конца (grid)

class QuickForm(StatesGroup):
    name   = State()
    phone  = State()
    branch = State()

class CalcForm(StatesGroup):
    width   = State()
    length  = State()
    service = State()
    service_type = State()

class OperatorForm(StatesGroup):
    message = State()

class AdminReply(StatesGroup):
    waiting_reply = State()   # оператор пишет ответ клиенту

class AgentForm(StatesGroup):
    waiting_contact = State()  # ожидаем контакт для регистрации агента

class LinkPhoneForm(StatesGroup):
    waiting_contact = State()  # ожидаем контакт для привязки к сайту

# ══════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════
def lang_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский язык", callback_data="lang_ru"),
        InlineKeyboardButton(text="🇺🇿 O'zbek tili",  callback_data="lang_uz"),
    ]])

def menu_kb(uid):
    rows = [
        [InlineKeyboardButton(text=t(uid,"btn_webapp"), web_app=WebAppInfo(url=WEBSITE_URL))],
        [InlineKeyboardButton(text=t(uid,"btn_order"),    callback_data="menu_order")],
        [InlineKeyboardButton(text=t(uid,"btn_status"),   callback_data="menu_status"),
         InlineKeyboardButton(text=t(uid,"btn_prices"),   callback_data="menu_prices")],
        [InlineKeyboardButton(text=t(uid,"btn_calc"),     callback_data="menu_calc"),
         InlineKeyboardButton(text=t(uid,"btn_profile"),  callback_data="menu_profile")],
        [InlineKeyboardButton(text=t(uid,"btn_operator"), callback_data="menu_operator")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def settings_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(uid,"btn_change_lang"), callback_data="settings_lang")],
        [InlineKeyboardButton(text=t(uid,"btn_help"),        callback_data="menu_help")],
        [InlineKeyboardButton(text=t(uid,"btn_menu"),        callback_data="go_menu")],
    ])

def back_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(uid,"btn_menu"), callback_data="go_menu")
    ]])

def phone_kb(uid):
    """ReplyKeyboard с кнопкой Поделиться номером"""
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text=t(uid,"btn_share_phone"), request_contact=True),
            KeyboardButton(text=t(uid,"btn_enter_phone")),
        ]],
        resize_keyboard=True, one_time_keyboard=True
    )

LOCATION_PICKER_URL = "https://artez.uz/location_picker.html"

def location_kb(uid):
    """ReplyKeyboard: GPS / выбрать на карте / пропустить"""
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text=t(uid,"btn_send_loc"), request_location=True),
            KeyboardButton(text="🗺 Выбрать на карте", web_app=WebAppInfo(url=LOCATION_PICKER_URL)),
        ],[
            KeyboardButton(text=t(uid,"btn_skip_loc")),
        ]],
        resize_keyboard=True, one_time_keyboard=True
    )

def branch_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(uid,"btn_zarafshan"), callback_data="branch_zarafshan"),
        InlineKeyboardButton(text=t(uid,"btn_navoi"),     callback_data="branch_navoi"),
    ],[
        InlineKeyboardButton(text=t(uid,"btn_cancel"), callback_data="cancel_order"),
    ]])

def city_kb(uid, branch):
    cities = CITIES[branch][lang(uid)]
    rows = [[InlineKeyboardButton(text=c, callback_data=f"city_{i}")] for i,c in enumerate(cities)]
    rows.append([InlineKeyboardButton(text=t(uid,"btn_cancel"), callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def service_kb(uid):
    if SVC_CACHE:
        keys = sorted(SVC_CACHE.keys(), key=lambda k: SVC_CACHE[k].get("order_idx", 0))
        rows = [[InlineKeyboardButton(text=get_svc_name(k, uid), callback_data=f"svc_{k}")] for k in keys]
    else:
        rows = [
            [InlineKeyboardButton(text=t(uid,"btn_svc_carpet"),      callback_data="svc_carpet")],
            [InlineKeyboardButton(text=t(uid,"btn_svc_carpet_home"), callback_data="svc_carpet_home")],
            [InlineKeyboardButton(text=t(uid,"btn_svc_sofa"),        callback_data="svc_sofa")],
            [InlineKeyboardButton(text=t(uid,"btn_svc_mattress"),    callback_data="svc_mattress")],
            [InlineKeyboardButton(text=t(uid,"btn_svc_curtains"),    callback_data="svc_curtains")],
        ]
    rows.append([InlineKeyboardButton(text=t(uid,"btn_cancel"), callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def service_type_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(uid,"btn_type_standard"), callback_data="svctype_standard")],
        [InlineKeyboardButton(text=t(uid,"btn_type_express"),  callback_data="svctype_express")],
        [InlineKeyboardButton(text=t(uid,"btn_cancel"),        callback_data="cancel_order")],
    ])

_WD_RU = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
_WD_UZ = ["Du","Se","Ch","Pa","Ju","Sh","Ya"]

def date_kb(uid):
    from datetime import date, timedelta
    today = date.today()
    rows, row = [], []
    for i in range(7):
        d = today + timedelta(days=i)
        date_str = d.strftime("%d.%m.%Y")
        if i == 0:
            label = t(uid,"btn_today") + f" ({d.strftime('%d.%m')})"
        elif i == 1:
            label = t(uid,"btn_tomorrow") + f" ({d.strftime('%d.%m')})"
        else:
            wd = (_WD_UZ if lang(uid) == "uz" else _WD_RU)[d.weekday()]
            label = f"{wd} {d.strftime('%d.%m')}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"date_{date_str}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=t(uid,"btn_pick_date"), callback_data="date_pick")])
    rows.append([InlineKeyboardButton(text=t(uid,"btn_cancel"),    callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def time_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(uid,"btn_morning"),     callback_data="time_morning")],
        [InlineKeyboardButton(text=t(uid,"btn_evening"),     callback_data="time_evening")],
        [InlineKeyboardButton(text=t(uid,"btn_custom_time"), callback_data="time_custom")],
        [InlineKeyboardButton(text=t(uid,"btn_cancel"),      callback_data="cancel_order")],
    ])

_TIME_SLOTS = [f"{h:02d}:00" for h in range(8, 20)]  # 08:00..19:00

def time_from_kb(uid):
    rows = []
    for i in range(0, 12, 3):
        rows.append([InlineKeyboardButton(text=_TIME_SLOTS[j], callback_data=f"tslot_from_{j+8}") for j in range(i, i+3)])
    rows.append([InlineKeyboardButton(text=t(uid,"btn_cancel"), callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def time_to_kb(uid, from_h: int):
    slots = [h for h in range(8, 20) if h > from_h]
    rows, row = [], []
    for h in slots:
        row.append(InlineKeyboardButton(text=f"{h:02d}:00", callback_data=f"tslot_to_{h}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=t(uid,"btn_cancel"), callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cancel_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(uid,"btn_cancel"), callback_data="cancel_order")
    ]])

# ══════════════════════════════════════
#  ОТПРАВКА ДАННЫХ
# ══════════════════════════════════════
async def send_to_sheets(data: dict):
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(SHEETS_URL, json=data, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        logging.warning(f"Sheets error: {e}")

def _group_id_for_branch(branch: str) -> int:
    """Возвращает tg_orders_group_id филиала из BRANCHES. Fallback — GROUP_ID."""
    for b in BRANCHES:
        if b.get("slug") == branch:
            gid = b.get("tg_orders_channel_id") or b.get("tg_delivery_group_id")
            if gid:
                return int(gid)
    return GROUP_ID

async def _notify_new_bot_client(uid: int, first_name: str, last_name: str, phone: str, username: str):
    """Уведомление о новом клиенте из бота в группу новых клиентов."""
    if not GROUP_NEW_CLIENTS_ID:
        return
    from datetime import datetime
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    name = f"{first_name or ''} {last_name or ''}".strip() or "—"
    tg_link = f'<a href="tg://user?id={uid}">{uid}</a>'
    text = (
        f"👤 {name}, 📞 <code>{phone}</code>, ✈️ {tg_link}, 🤖\n"
        f"📅 {now}"
    )
    try:
        await bot.send_message(GROUP_NEW_CLIENTS_ID, text, parse_mode="HTML")
    except Exception as e:
        logging.warning(f"_notify_new_bot_client error: {e}")


async def notify_group(text: str, order_num: int = None, client_id: int = None, phone: str = None, username: str = None, location_url: str = None, branch: str = ""):
    """Отправляет заявку в группу сотрудников с кнопками действий"""
    kb_rows = []
    if location_url:
        kb_rows.append([InlineKeyboardButton(text="🗺 Открыть на карте", url=location_url)])
    if order_num and client_id:
        if username:
            msg_button = InlineKeyboardButton(text="✉️ Написать", url=f"https://t.me/{username}")
        else:
            msg_button = InlineKeyboardButton(text="✉️ Написать", url=f"tg://user?id={client_id}")
        kb_rows.extend([
            [
                InlineKeyboardButton(text="✅ Принять заказ",  callback_data=f"accept_{order_num}_{client_id}"),
                msg_button,
            ],
            [
                InlineKeyboardButton(text="🚗 Назначить водителя", callback_data=f"driver_{order_num}_{client_id}"),
                InlineKeyboardButton(text="❌ Отклонить",          callback_data=f"reject_{order_num}_{client_id}"),
            ],
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    target_group = _group_id_for_branch(branch)
    try:
        await bot.send_message(target_group, text, reply_markup=kb)
    except Exception as e:
        logging.warning(f"Group notify error: {e}")
        # Если не получилось в группу — отправляем лично
        try:
            await bot.send_message(ADMIN_ID, text, reply_markup=kb)
        except Exception as e2:
            logging.warning(f"Admin notify error: {e2}")

async def notify_admin(text: str):
    """Личные сообщения администратору (от оператора)"""
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
    except Exception as e:
        logging.warning(f"Admin notify error: {e}")

# ── ПРОМО-АКЦИЯ ──────────────────────────────────────────────────────
async def _maybe_send_promo(uid: int, msg: Message):
    """Если uid — зарегистрированный клиент сайта (users.tg_id, is_verified=TRUE) с активным
    окном акции — шлёт доп. сообщение (full/silent). Ничего не делает для незарегистрированных
    и не блокирует основной хендлер (вызывается после основного ответа)."""
    try:
        site_user = await get_site_user_by_tg_id(uid)
        if not site_user or not site_user.get("phone"):
            return
        promo = await check_promo_eligibility(site_user["id"], site_user["phone"], channel="bot")
        if not promo or promo.get("mode") not in ("full", "silent"):
            return
        pct = promo.get("discount_pct") or 0
        expires_at_raw = promo.get("expires_at")
        if promo["mode"] == "full":
            title = promo.get("title_ru") if lang(uid) == "ru" else promo.get("title_uz")
            text  = promo.get("text_ru")  if lang(uid) == "ru" else promo.get("text_uz")
            deadline = "—"
            if expires_at_raw:
                try:
                    dt = datetime.fromisoformat(expires_at_raw).astimezone(ZoneInfo("Asia/Tashkent"))
                    deadline = dt.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    pass
            await msg.answer(
                t(uid, "promo_campaign_full").format(title=title or "", text=text or "", pct=pct, deadline=deadline),
                parse_mode="Markdown"
            )
        else:  # silent
            hours_left = 0
            if expires_at_raw:
                try:
                    dt = datetime.fromisoformat(expires_at_raw)
                    hours_left = max(0, int((dt - datetime.now(timezone.utc)).total_seconds() // 3600))
                except Exception:
                    pass
            await msg.answer(
                t(uid, "promo_campaign_silent").format(pct=pct, hours=hours_left),
                parse_mode="Markdown"
            )
    except Exception as e:
        logging.warning(f"promo check error: {e}")

# ══════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id

    # Проверка блокировки
    try:
        if await is_client_blocked(uid):
            await msg.answer("🚫 Ваш аккаунт заблокирован. Обратитесь в поддержку.")
            return
    except Exception:
        pass

    # Если язык ещё не известен в этой сессии — пробуем подгрузить из БД
    if uid not in user_lang:
        try:
            saved_lang = await get_client_lang(uid)
        except Exception as e:
            logging.warning(f"get_client_lang error: {e}")
            saved_lang = None
        if saved_lang in ("ru", "uz"):
            user_lang[uid] = saved_lang

    # Сохраняем/обновляем клиента в БД
    await upsert_client(
        tg_id=uid,
        username=msg.from_user.username,
        first_name=msg.from_user.first_name,
        last_name=msg.from_user.last_name,
        lang=user_lang.get(uid,"ru")
    )

    # Deep link: /start tglink_{user_id} — привязка аккаунта сайта
    args = msg.text.split(maxsplit=1)[1] if msg.text and " " in msg.text else ""
    if args.startswith("tglink_"):
        try:
            site_user_id = int(args.split("_", 1)[1])
            async with aiohttp.ClientSession() as s:
                r = await s.post(f"{API_URL}/user/link-tg",
                                 json={"user_id": site_user_id, "tg_id": uid,
                                       "tg_username": msg.from_user.username},
                                 timeout=aiohttp.ClientTimeout(total=8))
                data = await r.json()
            if data.get("ok"):
                name = data.get("name") or "друг"
                await msg.answer(
                    f"✅ Telegram успешно привязан к вашему аккаунту на сайте!\n\n"
                    f"Теперь вернитесь на сайт artez.uz и нажмите *Стать Агентом*.",
                    parse_mode="Markdown")
            else:
                await msg.answer("❌ Не удалось привязать аккаунт. Попробуйте ещё раз.")
        except Exception as e:
            logging.warning(f"tglink error: {e}")
            await msg.answer("❌ Ошибка привязки. Обратитесь к администратору.")
        return

    # Deep link: /start link_phone — привязка телефона к сайту для регистрации
    if args == "link_phone":
        # Проверяем — вдруг пользователь уже делился номером раньше
        saved_phone = await get_client_tg_phone(uid)
        if saved_phone:
            # Есть сохранённый номер — сразу привязываем без повторного шаринга
            registered = False
            try:
                async with aiohttp.ClientSession() as s:
                    r = await s.post(f"{API_URL}/tg-phone-link",
                                     json={"phone": saved_phone, "tg_id": uid},
                                     timeout=aiohttp.ClientTimeout(total=8))
                    data = await r.json()
                    registered = data.get("registered", False)
            except Exception as e:
                logging.warning(f"tg-phone-link (saved) error: {e}")
            key = "link_phone_ok_registered" if registered else "link_phone_ok"
            await msg.answer(
                t(uid, key).format(phone=saved_phone),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌐 artez.uz", url="https://artez.uz")],
                    [InlineKeyboardButton(text=t(uid,"btn_menu"), callback_data="go_menu")],
                ]),
                parse_mode="Markdown"
            )
            return
        # Номера нет — просим поделиться
        share_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=t(uid,"btn_share_phone"), request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
        await state.set_state(LinkPhoneForm.waiting_contact)
        await msg.answer(t(uid,"link_phone_prompt"), reply_markup=share_kb, parse_mode="Markdown")
        return

    if uid in user_lang:
        await msg.answer(t(uid,"menu_title"), reply_markup=menu_kb(uid), parse_mode="Markdown")
        await _maybe_send_promo(uid, msg)
    else:
        await msg.answer("👋", reply_markup=lang_kb())

@dp.callback_query(F.data.in_({"lang_ru","lang_uz"}))
async def set_language(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    user_lang[uid] = "ru" if cb.data == "lang_ru" else "uz"
    try:
        await set_client_lang(uid, user_lang[uid])
    except Exception as e:
        logging.warning(f"set_client_lang error: {e}")
    await cb.message.edit_text(t(uid,"lang_set"))
    await cb.message.answer(t(uid,"menu_title"), reply_markup=menu_kb(uid), parse_mode="Markdown")

@dp.callback_query(F.data == "go_menu")
async def go_menu(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await state.clear()
    if uid not in user_lang:
        try:
            saved_lang = await get_client_lang(uid)
        except Exception as e:
            logging.warning(f"get_client_lang error: {e}")
            saved_lang = None
        if saved_lang in ("ru", "uz"):
            user_lang[uid] = saved_lang
        else:
            await cb.message.answer("👋", reply_markup=lang_kb())
            return
    await cb.message.answer(t(uid,"menu_title"), reply_markup=menu_kb(uid), parse_mode="Markdown")

# ── МЕНЮ ПУНКТЫ ──
@dp.callback_query(F.data == "menu_prices")
async def menu_prices(cb: CallbackQuery):
    uid = cb.from_user.id
    await ensure_prices_fresh()
    await cb.message.answer(build_prices_text(uid), reply_markup=back_kb(uid), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_branches")
async def menu_branches(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.message.answer(t(uid,"branches_text"), reply_markup=back_kb(uid), parse_mode="Markdown")

def promo_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(uid,"btn_promo_telegram"),  url=SITE["social_tg_group"])],
        [InlineKeyboardButton(text=t(uid,"btn_promo_instagram"), url=SITE["social_instagram"])],
        [InlineKeyboardButton(text=t(uid,"btn_menu"), callback_data="go_menu")],
    ])

@dp.callback_query(F.data == "menu_promo")
async def menu_promo(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.message.answer(t(uid,"promo_text"), reply_markup=promo_kb(uid), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_info")
async def menu_info(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.message.answer(t(uid,"info_text"), reply_markup=back_kb(uid), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_help")
async def menu_help(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.message.answer(t(uid,"help_text"), reply_markup=back_kb(uid), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_settings")
async def menu_settings(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.message.answer(t(uid,"settings_text"), reply_markup=settings_kb(uid), parse_mode="Markdown")

@dp.callback_query(F.data == "settings_lang")
async def settings_lang(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.message.answer(t(uid,"choose_lang_text"), reply_markup=lang_kb())

# ── АГЕНТ ─────────────────────────────────────────────────────────────
async def _do_agent_check(uid: int, phone: str | None, answer_fn):
    """Общая логика проверки/регистрации агента. answer_fn(text, kb, parse_mode)."""
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{API_URL}/agent/status-by-tg/{uid}",
                            params={"phone": phone} if phone else {},
                            timeout=aiohttp.ClientTimeout(total=6))
            data = await r.json()
    except Exception:
        data = {}

    if data.get("is_agent"):
        await answer_fn(
            "✅ *Вы уже являетесь Агентом ARTEZ\\!*\n\n"
            "Войдите в кабинет агента:\n🔗 artez\\.uz/staff\\.html\n\n"
            "Логин: ваш номер телефона\n_Забыли пароль? Нажмите кнопку ниже_",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎯 Открыть кабинет агента", url="https://artez.uz/staff.html")],
                [InlineKeyboardButton(text="🔑 Сбросить пароль", callback_data="agent_reset_pass")],
                [InlineKeyboardButton(text="← Назад", callback_data="go_menu")],
            ]), "MarkdownV2")
        return

    if data.get("has_site_account"):
        # Регистрируем
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.post(f"{API_URL}/agent/apply-by-tg",
                                 json={"tg_id": uid, "phone": phone},
                                 timeout=aiohttp.ClientTimeout(total=8))
                result = await r.json()
        except Exception:
            result = {}

        if result.get("ok"):
            p = result.get("phone", "")
            already = result.get("already", False)
            txt = (f"✅ *Вы уже являетесь Агентом ARTEZ\\!*\n\nЛогин: `{p}`\nПароль: как на сайте artez\\.uz\n\n🔗 artez\\.uz/staff\\.html"
                   if already else
                   f"🎉 *Ура\\! Вы стали Агентом ARTEZ\\!*\n\nЛогин: `{p}`\nПароль: как на сайте artez\\.uz\n\nВойдите в кабинет:\n🔗 artez\\.uz/staff\\.html")
            await answer_fn(txt, InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎯 Открыть кабинет агента", url="https://artez.uz/staff.html")],
                [InlineKeyboardButton(text="← Назад", callback_data="go_menu")],
            ]), "MarkdownV2")
        else:
            await answer_fn("❌ Не удалось зарегистрировать\\. Попробуйте через сайт artez\\.uz",
                            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data="go_menu")]]),
                            "MarkdownV2")
        return

    # Аккаунт на сайте не найден — просим поделиться НАСТОЯЩИМ номером
    await answer_fn(
        "🤝 Стать Агентом ARTEZ\n\n"
        "Аккаунт на сайте не найден.\n\n"
        "Нажмите кнопку ниже — бот получит ваш реальный номер Telegram и найдёт ваш аккаунт на artez.uz",
        ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
        ], resize_keyboard=True, one_time_keyboard=True),
        None)

@dp.callback_query(F.data == "menu_agent")
async def menu_agent(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    lang_u = user_lang.get(uid, "ru")
    if lang_u == "uz":
        info_text = (
            "🤝 *ARTEZ Agenti bo'lish*\n\n"
            "Agentlar mijozlarni jalb qilish orqali har bir buyurtmadan *komissiya* oladi\\.\n\n"
            "📋 *Shartlar:*\n"
            "• artez\\.uz saytida ro'yxatdan o'tgan bo'lish\n"
            "• Referral havola orqali mijoz topib kelish\n"
            "• Komissiya miqdori: buyurtma summasiga qarab\n\n"
            "🔒 *Maxfiylik siyosati:* artez\\.uz/privacy\n\n"
            "Davom etish uchun tasdiqlang:"
        )
        btn_confirm = "✅ Tasdiqlash — Agent bo'lish"
        btn_cancel  = "❌ Bekor qilish"
    else:
        info_text = (
            "🤝 *Стать Агентом ARTEZ*\n\n"
            "Агенты привлекают клиентов и получают *комиссию* с каждого заказа\\.\n\n"
            "📋 *Условия:*\n"
            "• Быть зарегистрированным на artez\\.uz\n"
            "• Приводить клиентов по реферальной ссылке\n"
            "• Размер комиссии: зависит от суммы заказа\n\n"
            "🔒 *Политика конфиденциальности:* artez\\.uz/privacy\n\n"
            "Нажмите «Подтвердить» чтобы продолжить:"
        )
        btn_confirm = "✅ Подтвердить — Стать Агентом"
        btn_cancel  = "❌ Отмена"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_confirm, callback_data="agent_confirm")],
        [InlineKeyboardButton(text=btn_cancel,  callback_data="go_menu")],
    ])
    await cb.message.answer(info_text, reply_markup=kb, parse_mode="MarkdownV2")

@dp.callback_query(F.data == "agent_confirm")
async def agent_confirm(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    await cb.message.answer("⏳ Проверяем…" if user_lang.get(uid,"ru") == "ru" else "⏳ Tekshirilmoqda…")

    bot_client = await get_client_by_tg_id(uid)
    bot_phone = (bot_client.get("tg_phone") or bot_client.get("phone")) if bot_client else None

    async def reply(text, kb, pm):
        if pm:
            await cb.message.answer(text, reply_markup=kb, parse_mode=pm)
        else:
            await cb.message.answer(text, reply_markup=kb)
            await state.set_state(AgentForm.waiting_contact)

    await _do_agent_check(uid, bot_phone, reply)

@dp.message(AgentForm.waiting_contact, F.contact)
async def agent_contact_received(msg: Message, state: FSMContext):
    """Пользователь поделился контактом — сохраняем как tg_phone и ищем аккаунт."""
    await state.clear()
    await msg.answer("⏳ Проверяем…", reply_markup=ReplyKeyboardRemove())
    uid = msg.from_user.id
    phone = msg.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    # Сохраняем в clients: phone (для заявок) и tg_phone (верифицированный)
    await upsert_client(tg_id=uid, username=msg.from_user.username,
                        first_name=msg.from_user.first_name,
                        last_name=msg.from_user.last_name,
                        phone=phone, lang=user_lang.get(uid, "ru"))
    await update_client_tg_phone(uid, phone)

    kb_back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="go_menu")]
    ])

    async def reply(text, kb, pm):
        # После получения контакта — не показываем кнопку контакта снова
        if pm:
            await msg.answer(text, reply_markup=kb, parse_mode=pm)
        else:
            # "не найден" — показываем сообщение со ссылкой на сайт
            await msg.answer(
                f"❌ Номер `{phone}` не найден на сайте artez\\.uz\n\n"
                "Зарегистрируйтесь на сайте с этим номером, затем снова нажмите «Стать Агентом»",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌐 Зарегистрироваться", url="https://artez.uz")],
                    [InlineKeyboardButton(text="← Назад", callback_data="go_menu")],
                ]), parse_mode="MarkdownV2")

    await _do_agent_check(uid, phone, reply)

@dp.message(LinkPhoneForm.waiting_contact, F.contact)
async def link_phone_contact_received(msg: Message, state: FSMContext):
    """Пользователь поделился номером для привязки к сайту."""
    await state.clear()
    uid = msg.from_user.id
    phone = msg.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone

    # Принимаем только собственный контакт
    if msg.contact.user_id and int(msg.contact.user_id) != uid:
        await msg.answer("❌ " + ("Поделитесь своим номером." if user_lang.get(uid,"ru") == "ru" else "O'z raqamingizni ulashing."),
                         reply_markup=ReplyKeyboardRemove())
        return

    await msg.answer("⏳", reply_markup=ReplyKeyboardRemove())

    # Сохраняем номер в профиль клиента (чтобы отображался в «Мой профиль»)
    await upsert_client(tg_id=uid, username=msg.from_user.username,
                        first_name=msg.from_user.first_name,
                        last_name=msg.from_user.last_name,
                        phone=phone, lang=user_lang.get(uid, "ru"))
    await update_client_tg_phone(uid, phone)

    registered = False
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"{API_URL}/tg-phone-link",
                             json={"phone": phone, "tg_id": uid},
                             timeout=aiohttp.ClientTimeout(total=8))
            data = await r.json()
            registered = data.get("registered", False)
    except Exception as e:
        logging.warning(f"tg-phone-link error: {e}")

    key = "link_phone_ok_registered" if registered else "link_phone_ok"
    await msg.answer(
        t(uid, key).format(phone=phone),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 artez.uz", url="https://artez.uz")],
            [InlineKeyboardButton(text=t(uid,"btn_menu"), callback_data="go_menu")],
        ]),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("take_lead_"))
async def cb_take_lead(cb: CallbackQuery):
    """Сотрудник нажал 'Взять лид' в групповом чате."""
    try:
        tg_user_id = cb.from_user.id
        cq_data    = cb.data
        orig_text  = cb.message.text or ""

        try:
            lead_id = int(cq_data.split("_")[2])
        except (IndexError, ValueError):
            await cb.answer("❌ Неверный формат данных", show_alert=True)
            return

        staff = await get_staff_by_tg_id_for_lead(tg_user_id)
        if not staff:
            await cb.answer(
                "❌ Ваш Telegram не привязан к аккаунту сотрудника ARTEZ.\nОбратитесь к администратору.",
                show_alert=True)
            return
        if staff.get("role") == "agent":
            await cb.answer("❌ Агенты не могут брать лиды через Telegram.\nЛиды берут только сотрудники.", show_alert=True)
            return

        staff_id   = staff["id"]
        staff_name = f"{staff.get('first_name') or ''} {staff.get('last_name') or ''}".strip() or staff.get("login", "")
        took_verb  = "Взяла" if staff.get("gender") == "F" else "Взял"

        result, taker_name, taker_verb = await take_lead(lead_id, staff_id, staff_name)

        if result == 'not_found':
            await cb.answer("❌ Лид не найден", show_alert=True)
        elif result == 'already_mine':
            await cb.answer("✅ Этот лид уже ваш!")
        elif result == 'taken':
            await cb.answer(f"❌ Лид уже взят: {taker_name or 'другой сотрудник'}", show_alert=True)
            new_text = orig_text.rstrip("━" * 10).rstrip() + f"\n{'━'*10}\n✅ {taker_verb}: {taker_name or 'другой сотрудник'}"
            try:
                await cb.message.edit_text(new_text)
            except Exception:
                pass
        elif result == 'ok':
            await cb.answer("✅ Лид взят! Откройте приложение.")
            new_text = orig_text.rstrip("━" * 10).rstrip() + f"\n{'━'*10}\n✅ {took_verb}: {staff_name}"
            try:
                await cb.message.edit_text(new_text)
            except Exception:
                pass
        else:
            await cb.answer("❌ Ошибка базы данных", show_alert=True)
    except Exception as e:
        logging.warning(f"cb_take_lead error: {e}")
        try:
            await cb.answer("❌ Ошибка сервера. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass


@dp.callback_query(F.data == "agent_reset_pass")
async def agent_reset_pass(cb: CallbackQuery):
    uid = cb.from_user.id
    API = os.getenv("WEBSITE_API", "https://artez-api.railway.app/api")
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"{API}/agent/reset-password-by-tg",
                             json={"tg_id": uid},
                             timeout=aiohttp.ClientTimeout(total=8))
            data = await r.json()
        if data.get("ok"):
            await cb.message.answer("🔑 Временный пароль отправлен выше.\n⏰ Действует 10 минут.\nПосле входа сразу смените пароль.")
        else:
            await cb.message.answer("❌ Ошибка: " + data.get("detail",""))
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка соединения: {e}")
    await cb.answer()

def status_menu_kb(uid, counts):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{t(uid,'status_btn_new')} ({counts['new']})",       callback_data="status_new"),
         InlineKeyboardButton(text=f"{t(uid,'status_btn_progress')} ({counts['progress']})", callback_data="status_progress")],
        [InlineKeyboardButton(text=f"{t(uid,'status_btn_done')} ({counts['done']})",     callback_data="status_done"),
         InlineKeyboardButton(text=f"{t(uid,'status_btn_cancelled')} ({counts['cancelled']})", callback_data="status_cancelled")],
        [InlineKeyboardButton(text=t(uid,"btn_menu"), callback_data="go_menu")],
    ])

@dp.callback_query(F.data == "menu_status")
async def menu_status(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        orders = await get_client_orders(uid)
    except Exception as e:
        logging.warning(f"get_client_orders error: {e}")
        orders = []

    counts = {"new": 0, "progress": 0, "done": 0, "cancelled": 0}
    for o in orders:
        for group, statuses in STATUS_GROUPS.items():
            if o["status"] in statuses:
                counts[group] += 1
                break

    if not orders:
        kb_empty = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(uid,"btn_order"), callback_data="menu_order")],
            [InlineKeyboardButton(text=t(uid,"btn_menu"),  callback_data="go_menu")],
        ])
        await cb.message.answer(t(uid,"status_empty"), reply_markup=kb_empty, parse_mode="Markdown")
        return

    await cb.message.answer(t(uid,"status_menu_title"), reply_markup=status_menu_kb(uid, counts), parse_mode="Markdown")

def back_to_status_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(uid,"btn_back_to_status"), callback_data="menu_status")],
        [InlineKeyboardButton(text=t(uid,"btn_menu"), callback_data="go_menu")],
    ])

@dp.callback_query(F.data.in_({"status_new","status_progress","status_done","status_cancelled"}))
async def show_status_group(cb: CallbackQuery):
    uid   = cb.from_user.id
    group = cb.data.replace("status_","")
    statuses = STATUS_GROUPS.get(group, [])

    try:
        orders = await get_client_orders(uid)
    except Exception as e:
        logging.warning(f"get_client_orders error: {e}")
        orders = []

    filtered = [o for o in orders if o["status"] in statuses]

    group_titles = {
        "new": "status_btn_new", "progress": "status_btn_progress",
        "done": "status_btn_done", "cancelled": "status_btn_cancelled",
    }
    title = t(uid, group_titles.get(group, "status_btn_new"))

    if not filtered:
        text = f"{title}\n\n" + t(uid,"status_group_empty")
    else:
        lines = [f"{title}\n"]
        for o in filtered:
            lines.append(
                t(uid,"status_order_line").format(
                    num=o["order_num"],
                    service=o["service"] or "",
                    date=o["pickup_date"] or "",
                    status=order_status_name(uid, o["status"]),
                )
            )
        text = "\n\n".join(lines)

    await cb.message.answer(text, reply_markup=back_to_status_kb(uid), parse_mode="Markdown")


# ── ОПЕРАТОР ──
@dp.callback_query(F.data == "menu_profile")
async def menu_profile(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        client = await get_client_by_tg_id(uid)
        orders = await get_client_orders(uid)
        total  = len(orders)
        done   = sum(1 for o in orders if o.get("status") in ("done","completed"))
        last_d = ""
        if orders:
            ts = orders[0].get("created_at")
            if ts:
                last_d = ts.strftime("%d.%m.%Y") if hasattr(ts, "strftime") else str(ts)[:10]
        name_parts = [cb.from_user.first_name or "", cb.from_user.last_name or ""]
        name  = " ".join(p for p in name_parts if p) or "—"
        phone_raw = (client or {}).get("phone")
        phone = phone_raw or t(uid, "profile_nophone")
        last_line = t(uid, "profile_last").format(date=last_d) if last_d else ""
        text = t(uid, "profile_text").format(
            name=name, phone=phone, uid=uid,
            total=total, done=done, last=last_line
        )
    except Exception as e:
        logging.warning(f"menu_profile error: {e}")
        phone_raw = None
        text = t(uid, "profile_text").format(name="—", phone="—", uid=uid, total=0, done=0, last="")

    # Проверяем статус агента
    is_agent = False
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{API_URL}/agent/status-by-tg/{uid}",
                            timeout=aiohttp.ClientTimeout(total=4))
            is_agent = (await r.json()).get("is_agent", False)
    except Exception:
        pass

    kb_rows = []
    if not phone_raw:
        kb_rows.append([InlineKeyboardButton(text=t(uid,"profile_link_phone"), callback_data="link_phone_from_profile")])
    if is_agent:
        kb_rows.append([InlineKeyboardButton(text="✅ Агент ARTEZ", url="https://artez.uz/staff.html")])
    else:
        kb_rows.append([InlineKeyboardButton(text="🤝 Стать Агентом", callback_data="menu_agent")])
    kb_rows.append([InlineKeyboardButton(text=t(uid,"btn_settings"), callback_data="menu_settings")])
    kb_rows.append([InlineKeyboardButton(text=t(uid,"btn_menu"), callback_data="go_menu")])
    await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="Markdown")

@dp.callback_query(F.data == "link_phone_from_profile")
async def link_phone_from_profile(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    saved_phone = await get_client_tg_phone(uid)
    if saved_phone:
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"{API_URL}/tg-phone-link",
                             json={"phone": saved_phone, "tg_id": uid},
                             timeout=aiohttp.ClientTimeout(total=8))
        except Exception as e:
            logging.warning(f"link_phone_from_profile error: {e}")
        await cb.message.answer(t(uid, "link_phone_ok").format(phone=saved_phone),
                                reply_markup=back_kb(uid), parse_mode="Markdown")
        return
    share_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(uid,"btn_share_phone"), request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await state.set_state(LinkPhoneForm.waiting_contact)
    await cb.message.answer(t(uid,"link_phone_prompt"), reply_markup=share_kb, parse_mode="Markdown")

@dp.callback_query(F.data == "menu_operator")
async def menu_operator(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await state.set_state(OperatorForm.message)
    await cb.message.answer(t(uid,"operator_text"), reply_markup=cancel_kb(uid), parse_mode="Markdown")

@dp.message(OperatorForm.message)
async def operator_message(msg: Message, state: FSMContext):
    uid      = msg.from_user.id
    username = msg.from_user.username or ""
    fname    = msg.from_user.first_name or ""
    lname    = msg.from_user.last_name  or ""
    fullname = f"{fname} {lname}".strip()

    # Формируем сообщение для оператора с кнопкой «Ответить»
    tg_link = f"tg://user?id={uid}"
    text = (
        f"💬 *Сообщение от клиента*\n"
        f"━━━━━━━━━━\n"
        f"👤 {md_escape(fullname)}" + (f" | @{md_escape(username)}" if username else "") + "\n"
        f"🆔 `{uid}`\n"
        f"━━━━━━━━━━\n"
        f"📝 {md_escape(msg.text)}\n"
        f"━━━━━━━━━━"
    )
    reply_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="↩️ Ответить клиенту",
            callback_data=f"reply_to_{uid}"
        )],
        [InlineKeyboardButton(
            text="📱 Открыть чат",
            url=tg_link
        )],
    ])
    # Отправляем в группу сообщений от клиентов
    try:
        await bot.send_message(GROUP_SMS_ID, text, parse_mode="Markdown", reply_markup=reply_kb)
    except Exception as e:
        logging.warning(f"Group SMS notify error (operator msg): {e}")
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown", reply_markup=reply_kb)
    # Подтверждение клиенту
    await msg.answer(t(uid,"operator_fwd"), reply_markup=back_kb(uid))
    await state.clear()

# ── ОПЕРАТОР НАЖАЛ «ОТВЕТИТЬ» ──
@dp.callback_query(F.data.startswith("reply_to_"))
async def admin_reply_start(cb: CallbackQuery, state: FSMContext):
    client_id = int(cb.data.replace("reply_to_",""))
    await state.set_state(AdminReply.waiting_reply)
    await state.update_data(reply_to_client=client_id)
    await cb.message.answer(
        f"✏️ Напишите ответ клиенту `{client_id}`:\n_(следующее сообщение будет отправлено ему)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_admin_reply")
        ]])
    )
    await cb.answer()

@dp.callback_query(F.data == "cancel_admin_reply")
async def admin_reply_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Отменено.")
    await cb.answer()

@dp.message(AdminReply.waiting_reply)
async def admin_reply_send(msg: Message, state: FSMContext):
    data      = await state.get_data()
    client_id = data.get("reply_to_client")
    sender    = msg.from_user
    sname     = f"{sender.first_name or ''} {sender.last_name or ''}".strip()

    try:
        is_uz = lang(client_id) == "uz"
        client_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Yozish" if is_uz else "✍️ Написать оператору",
                                  callback_data="menu_operator")],
            [InlineKeyboardButton(text="🏠 Menyu" if is_uz else "🏠 Меню",
                                  callback_data="go_menu")],
        ])
        await bot.send_message(
            client_id,
            f"📩 *Сообщение от оператора ARTEZ*\n\n{md_escape(msg.text)}",
            parse_mode="Markdown",
            reply_markup=client_kb
        )
        await msg.answer(
            f"✅ Ответ отправлен клиенту `{client_id}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.answer(f"⚠️ Не удалось отправить: {e}")

    await state.clear()

# ── ЗАЯВКА ──
@dp.callback_query(F.data == "menu_order")
async def menu_order(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(uid,"btn_order_quick"), callback_data="order_type_quick")],
        [InlineKeyboardButton(text=t(uid,"btn_order_full"),  callback_data="order_type_full")],
        [InlineKeyboardButton(text=t(uid,"btn_cancel"),      callback_data="cancel_order")],
    ])
    await cb.message.answer(t(uid,"ask_order_type"), reply_markup=kb)

@dp.callback_query(F.data == "order_type_full")
async def menu_order_full(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    user_data_db[uid] = {}
    await state.set_state(OrderForm.name)
    await _ask_name_step(cb.message, uid)

@dp.callback_query(F.data == "order_type_quick")
async def menu_order_quick(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    user_data_db[uid] = {"_quick": True}
    await state.set_state(QuickForm.name)
    await cb.message.answer(t(uid,"quick_ask_name"), reply_markup=cancel_kb(uid), parse_mode="Markdown")

_PHONE_RE_BOT = re.compile(r"^\+998\d{9}$")

def normalize_phone_bot(raw: str) -> str:
    """Normalize phone to +998XXXXXXXXX, return empty string if invalid."""
    v = raw.strip().replace(" ","").replace("-","").replace("(","").replace(")","")
    if v.startswith("998") and not v.startswith("+"):
        v = "+" + v
    return v if _PHONE_RE_BOT.match(v) else ""

async def _submit_bot_lead(uid: int, d: dict, is_quick: bool = False, user_from=None) -> str | None:
    """POST lead to /api/bot/lead. Returns lead_code on success, None on failure."""
    try:
        user_obj   = user_from
        first_name = getattr(user_obj, 'first_name', '') or d.get("name", "")
        last_name  = getattr(user_obj, 'last_name',  '') or ''
        username   = getattr(user_obj, 'username',   '') or ''
        client_name = d.get("name") or f"{first_name} {last_name}".strip() or f"TG {uid}"

        note_parts = []
        if username: note_parts.append(f"@{username}")
        if d.get("service_type"): note_parts.append(f"Тип: {d['service_type']}")
        if d.get("date"):         note_parts.append(f"Дата: {d['date']}")

        payload = {
            "client_name":    client_name,
            "client_phone":   d.get("phone",""),
            "branch":         d.get("branch",""),
            "city":           d.get("city",""),
            "address":        d.get("address",""),
            "service":        d.get("service",""),
            "service_type":   d.get("service_type",""),
            "pickup_date":    d.get("date",""),
            "pickup_time":    d.get("time",""),
            "note":           " · ".join(note_parts) if note_parts else "",
            "location":       d.get("location",""),
            "location_address": d.get("location_address",""),
            "client_tg_id":   uid,
            "is_quick":       is_quick,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_URL}/bot/lead",
                json=payload,
                headers={"X-Bot-Token": BOT_TOKEN or ""},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    lead_code = data.get("lead_code", "")
                    if lead_code:
                        await _tag_lead_promo(uid, lead_code)
                    return lead_code
                return None
    except Exception as e:
        logging.warning(f"_submit_bot_lead error: {e}")
        return None

async def _tag_lead_promo(uid: int, lead_code: str):
    """Если у пользователя есть живое окно акции — помечает созданный лид (leads.promo_id).
    Не потребляет окно, только визуальный тег для сотрудников."""
    try:
        site_user = await get_site_user_by_tg_id(uid)
        if not site_user:
            return
        promo_id = await get_live_promo_id_for_user(site_user["id"])
        if promo_id:
            await set_lead_promo(lead_code, promo_id)
    except Exception as e:
        logging.warning(f"_tag_lead_promo error: {e}")

# ── БЫСТРАЯ ЗАЯВКА ──
@dp.message(QuickForm.name)
async def quick_name(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    user_data_db.setdefault(uid, {})["name"] = msg.text.strip()
    await state.set_state(QuickForm.phone)
    try:
        client = await get_client_by_tg_id(uid)
        saved_phone = (client or {}).get("phone") or ""
    except Exception:
        saved_phone = ""
    if saved_phone:
        user_data_db[uid]["_saved_phone"] = saved_phone
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=t(uid,"btn_use_saved_phone").format(phone=saved_phone),
                callback_data="qphone_use_saved"
            ),
            InlineKeyboardButton(text=t(uid,"btn_enter_other_phone"), callback_data="qphone_enter_other"),
        ],[
            InlineKeyboardButton(text=t(uid,"btn_cancel"), callback_data="cancel_order"),
        ]])
        saved_txt = t(uid,"ask_phone_saved") if lang(uid)=="ru" else "2-qadam (3 dan)\n📞 Saqlangan raqamdan foydalanasizmi?"
        await msg.answer(saved_txt, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg.answer(t(uid,"quick_ask_phone"), reply_markup=phone_kb(uid), parse_mode="Markdown")

@dp.callback_query(QuickForm.phone, F.data == "qphone_use_saved")
async def quick_phone_use_saved(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    user_data_db[uid]["phone"] = user_data_db[uid].get("_saved_phone","")
    await state.set_state(QuickForm.branch)
    await cb.message.answer(t(uid,"quick_ask_branch"), reply_markup=branch_kb_quick(uid))

@dp.callback_query(QuickForm.phone, F.data == "qphone_enter_other")
async def quick_phone_enter_other(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(t(uid,"quick_ask_phone"), reply_markup=phone_kb(uid), parse_mode="Markdown")

async def _maybe_notify_new_client(uid: int, phone: str, user_from):
    """Отправляет уведомление если клиент впервые даёт номер."""
    try:
        existing = await get_client_tg_phone(uid)
        if not existing:
            asyncio.create_task(_notify_new_bot_client(
                uid, getattr(user_from, "first_name", "") or "",
                getattr(user_from, "last_name", "") or "",
                phone, getattr(user_from, "username", "") or ""))
    except Exception as e:
        logging.warning(f"_maybe_notify_new_client error: {e}")

@dp.message(QuickForm.phone, F.contact)
async def quick_phone_contact(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    raw = msg.contact.phone_number or ""
    norm = normalize_phone_bot(raw)
    if not norm:
        await msg.answer(t(uid,"phone_invalid"), reply_markup=phone_kb(uid), parse_mode="Markdown")
        return
    await _maybe_notify_new_client(uid, norm, msg.from_user)
    user_data_db.setdefault(uid, {})["phone"] = norm
    await state.set_state(QuickForm.branch)
    await msg.answer("✅", reply_markup=ReplyKeyboardRemove())
    await msg.answer(t(uid,"quick_ask_branch"), reply_markup=branch_kb_quick(uid))

@dp.message(QuickForm.phone, F.text)
async def quick_phone_text(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    raw = (msg.text or "").strip()
    if raw == t(uid,"btn_enter_phone"):
        await msg.answer(t(uid,"ask_phone_manual"), reply_markup=cancel_kb(uid))
        return
    norm = normalize_phone_bot(raw)
    if not norm:
        await msg.answer(t(uid,"phone_invalid"), reply_markup=phone_kb(uid), parse_mode="Markdown")
        return
    await _maybe_notify_new_client(uid, norm, msg.from_user)
    user_data_db.setdefault(uid, {})["phone"] = norm
    await state.set_state(QuickForm.branch)
    await msg.answer("✅", reply_markup=ReplyKeyboardRemove())
    await msg.answer(t(uid,"quick_ask_branch"), reply_markup=branch_kb_quick(uid))

@dp.callback_query(QuickForm.branch, F.data.in_({"qbranch_zarafshan","qbranch_navoi"}))
async def quick_branch(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    branch = cb.data.replace("qbranch_","")
    user_data_db[uid]["branch"]      = branch
    user_data_db[uid]["branch_name"] = t(uid,"btn_zarafshan") if branch=="zarafshan" else t(uid,"btn_navoi")
    await finish_quick(cb.message, uid, state, user_from=cb.from_user)

def branch_kb_quick(uid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(uid,"btn_zarafshan"), callback_data="qbranch_zarafshan"),
        InlineKeyboardButton(text=t(uid,"btn_navoi"),     callback_data="qbranch_navoi"),
    ],[
        InlineKeyboardButton(text=t(uid,"btn_cancel"), callback_data="cancel_order"),
    ]])

def _branch_phones_str(branch: str) -> str:
    """Строка с номерами выбранного филиала из настроек сайта."""
    if branch == "zarafshan":
        phones = [SITE.get("contact_zarafshan_1",""), SITE.get("contact_zarafshan_2","")]
    else:
        phones = [SITE.get("contact_navoi_1",""), SITE.get("contact_navoi_2","")]
    lines = "\n".join(f"📱 {p}" for p in phones if p)
    return f"\n\n{lines}" if lines else ""

async def finish_quick(msg, uid: int, state: FSMContext, user_from=None):
    d = user_data_db.get(uid, {})
    user_obj = user_from or getattr(msg, 'from_user', None)
    first_name = getattr(user_obj, 'first_name', '') or ''
    last_name  = getattr(user_obj, 'last_name',  '') or ''
    username   = getattr(user_obj, 'username',   '') or ''

    await upsert_client(tg_id=uid, username=username,
                        first_name=first_name, last_name=last_name,
                        phone=d.get("phone",""), lang=lang(uid))

    await _submit_bot_lead(uid, d, is_quick=True, user_from=user_from)
    await state.clear()
    branch_phones = _branch_phones_str(d.get("branch",""))
    done_msg = t(uid,"quick_done").format(branch_phones=branch_phones)
    await msg.answer(done_msg, reply_markup=back_kb(uid), parse_mode="Markdown")

async def _ask_name_step(message, uid: int):
    """Shows name prompt; if DB has a previous name — suggest it with buttons."""
    try:
        last = await db.get_last_lead_info(uid)
    except Exception:
        last = None
    saved_name = (last or {}).get("client_name", "").strip()
    saved_addr = (last or {}).get("address", "").strip()
    user_data_db.setdefault(uid, {})["_saved_name"] = saved_name
    user_data_db[uid]["_saved_addr"] = saved_addr
    if saved_name:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(uid,"btn_use_saved_name").format(name=saved_name),
                                  callback_data="name_use_saved")],
            [InlineKeyboardButton(text=t(uid,"btn_enter_other_name"), callback_data="name_enter_other")],
        ])
        await message.answer(t(uid,"ask_name"), reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(t(uid,"ask_name"), reply_markup=cancel_kb(uid), parse_mode="Markdown")

async def _proceed_after_name(uid: int, state: FSMContext, message):
    """After name is set — ask for phone (with saved phone suggestion if any)."""
    await state.set_state(OrderForm.phone)
    try:
        client = await get_client_by_tg_id(uid)
        saved_phone = (client or {}).get("phone") or ""
    except Exception:
        saved_phone = ""
    if saved_phone:
        user_data_db[uid]["_saved_phone"] = saved_phone
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t(uid,"btn_use_saved_phone").format(phone=saved_phone),
                                  callback_data="phone_use_saved")],
            [InlineKeyboardButton(text=t(uid,"btn_enter_other_phone"), callback_data="phone_enter_other")],
        ])
        await message.answer(t(uid,"ask_phone_saved"), reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(t(uid,"ask_phone"), reply_markup=phone_kb(uid), parse_mode="Markdown")

@dp.callback_query(OrderForm.name, F.data == "name_use_saved")
async def order_name_saved(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    user_data_db[uid]["name"] = user_data_db[uid].get("_saved_name", "")
    await cb.answer()
    try: await cb.message.edit_reply_markup(reply_markup=None)
    except: pass
    await _proceed_after_name(uid, state, cb.message)

@dp.callback_query(OrderForm.name, F.data == "name_enter_other")
async def order_name_enter_other(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    try: await cb.message.edit_reply_markup(reply_markup=None)
    except: pass
    await cb.message.answer(t(uid,"ask_name"), reply_markup=cancel_kb(uid), parse_mode="Markdown")

@dp.message(OrderForm.name)
async def order_name(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    user_data_db[uid]["name"] = msg.text
    await _proceed_after_name(uid, state, msg)

# Клиент выбрал «Использовать сохранённый номер»
@dp.callback_query(OrderForm.phone, F.data == "phone_use_saved")
async def order_phone_use_saved(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    phone = user_data_db[uid].get("_saved_phone", "")
    if not phone:
        await cb.answer()
        await cb.message.answer(t(uid, "ask_phone"), reply_markup=phone_kb(uid), parse_mode="Markdown")
        return
    user_data_db[uid]["phone"] = phone
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(OrderForm.branch)
    await cb.message.answer(f"✅ {phone}")
    await cb.message.answer(t(uid, "ask_branch"), reply_markup=branch_kb(uid))

# Клиент выбрал «Ввести другой номер» из inline-меню
@dp.callback_query(OrderForm.phone, F.data == "phone_enter_other")
async def order_phone_enter_other(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(t(uid, "ask_phone"), reply_markup=phone_kb(uid), parse_mode="Markdown")

# Клиент нажал «Поделиться номером» — Telegram прислал contact
@dp.message(OrderForm.phone, F.contact)
async def order_phone_contact(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    phone = msg.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    await _maybe_notify_new_client(uid, phone, msg.from_user)
    user_data_db[uid]["phone"] = phone
    await state.set_state(OrderForm.branch)
    await msg.answer("✅", reply_markup=ReplyKeyboardRemove())
    await msg.answer(t(uid,"ask_branch"), reply_markup=branch_kb(uid))

# Клиент нажал «Ввести другой номер»
@dp.message(OrderForm.phone, F.text == "⌨️ Ввести другой номер")
@dp.message(OrderForm.phone, F.text == "⌨️ Boshqa raqam kiritish")
async def order_phone_manual_prompt(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    await msg.answer(
        t(uid,"ask_phone_manual"),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )

# Клиент ввёл номер вручную — валидация
import re
PHONE_RE = re.compile(r"^\+998\d{9}$")

@dp.message(OrderForm.phone, F.text)
async def order_phone_text(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    raw = msg.text.strip().replace(" ","").replace("-","").replace("(","").replace(")","")
    if raw.startswith("998") and not raw.startswith("+"):
        raw = "+" + raw
    if not PHONE_RE.match(raw):
        await msg.answer(t(uid,"phone_invalid"), parse_mode="Markdown")
        return
    await _maybe_notify_new_client(uid, raw, msg.from_user)
    user_data_db.setdefault(uid, {})["phone"] = raw
    await state.set_state(OrderForm.branch)
    await msg.answer("✅", reply_markup=ReplyKeyboardRemove())
    await msg.answer(t(uid,"ask_branch"), reply_markup=branch_kb(uid))

async def _ask_address_step(message, uid: int):
    """Shows address prompt; if we have a saved address from DB — suggest it."""
    saved_addr = user_data_db.get(uid, {}).get("_saved_addr", "")
    if saved_addr:
        display = saved_addr[:60] + ("…" if len(saved_addr) > 60 else "")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🏠 {display}", callback_data="addr_use_saved")],
            [InlineKeyboardButton(text=t(uid,"btn_enter_other_addr"), callback_data="addr_enter_other")],
        ])
        await message.answer(t(uid,"ask_address"), reply_markup=kb)
    else:
        await message.answer(t(uid,"ask_address"), reply_markup=cancel_kb(uid))

@dp.callback_query(OrderForm.address, F.data == "addr_use_saved")
async def order_addr_saved(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    user_data_db[uid]["address"] = user_data_db[uid].get("_saved_addr", "")
    await state.set_state(OrderForm.location)
    await cb.answer()
    try: await cb.message.edit_reply_markup(reply_markup=None)
    except: pass
    await cb.message.answer(t(uid,"ask_location"), reply_markup=location_kb(uid), parse_mode="Markdown")

@dp.callback_query(OrderForm.address, F.data == "addr_enter_other")
async def order_addr_enter_other(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await cb.answer()
    try: await cb.message.edit_reply_markup(reply_markup=None)
    except: pass
    await cb.message.answer(t(uid,"ask_address"), reply_markup=cancel_kb(uid))

@dp.callback_query(F.data.in_({"branch_zarafshan","branch_navoi"}))
async def order_branch(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    branch = cb.data.replace("branch_","")
    user_data_db[uid]["branch"] = branch
    user_data_db[uid]["branch_name"] = t(uid,"btn_zarafshan") if branch=="zarafshan" else t(uid,"btn_navoi")
    user_data_db[uid]["city"] = user_data_db[uid]["branch_name"]
    await state.set_state(OrderForm.address)
    await _ask_address_step(cb.message, uid)

@dp.message(OrderForm.address)
async def order_address(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    user_data_db[uid]["address"] = msg.text
    await state.set_state(OrderForm.location)
    await msg.answer(
        t(uid,"ask_location"),
        reply_markup=location_kb(uid),
        parse_mode="Markdown"
    )

# Клиент отправил GPS-локацию (нативная кнопка Telegram)
@dp.message(OrderForm.location, F.location)
async def order_location_geo(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    lat = msg.location.latitude
    lon = msg.location.longitude
    user_data_db[uid]["location"]         = f"{lat:.5f}, {lon:.5f}"
    user_data_db[uid]["location_address"] = ""
    await state.set_state(OrderForm.service)
    await msg.answer("📍 ✅", reply_markup=ReplyKeyboardRemove())
    await msg.answer(t(uid,"ask_service"), reply_markup=service_kb(uid))

# Клиент выбрал точку на карте (Telegram Mini App)
@dp.message(OrderForm.location, F.web_app_data)
async def order_location_webapp(msg: Message, state: FSMContext):
    import json as _json
    uid = msg.from_user.id
    try:
        data = _json.loads(msg.web_app_data.data)
        la, lo = float(data["lat"]), float(data["lon"])
        user_data_db[uid]["location"]         = f"{la:.5f}, {lo:.5f}"
        user_data_db[uid]["location_address"] = data.get("address", "")
    except Exception as e:
        logging.warning(f"WebApp location parse error: {e}")
        user_data_db[uid]["location"]         = ""
        user_data_db[uid]["location_address"] = ""
    await state.set_state(OrderForm.service)
    addr_txt = user_data_db[uid].get("location_address") or user_data_db[uid].get("location") or ""
    await msg.answer(f"📍 ✅ {addr_txt}", reply_markup=ReplyKeyboardRemove())
    await msg.answer(t(uid,"ask_service"), reply_markup=service_kb(uid))

# Клиент нажал «Пропустить»
@dp.message(OrderForm.location, F.text)
async def order_location_skip(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    user_data_db[uid]["location"]         = ""
    user_data_db[uid]["location_address"] = ""
    await state.set_state(OrderForm.service)
    await msg.answer("⏭", reply_markup=ReplyKeyboardRemove())
    await msg.answer(t(uid,"ask_service"), reply_markup=service_kb(uid))

@dp.callback_query(OrderForm.service, F.data.startswith("svc_"))
async def order_service(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    svc = cb.data.replace("svc_","")
    user_data_db[uid]["service"] = get_svc_name(svc, uid)
    await state.set_state(OrderForm.service_type)
    await cb.message.answer(t(uid,"ask_service_type"), reply_markup=service_type_kb(uid))

@dp.callback_query(OrderForm.service_type, F.data.startswith("svctype_"))
async def order_service_type(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    svctype = cb.data.replace("svctype_","")
    type_map = {
        "standard": t(uid,"btn_type_standard"),
        "express":  t(uid,"btn_type_express"),
    }
    user_data_db[uid]["service_type"] = type_map.get(svctype, svctype)
    await state.set_state(OrderForm.date)
    await cb.message.answer(t(uid,"ask_date"), reply_markup=date_kb(uid))

# ── ДАТА — кнопки Сегодня/Завтра ──
@dp.callback_query(F.data.startswith("date_") & (F.data != "date_pick"))
async def order_date_btn(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if cb.data == "date_pick":
        return await order_date_pick(cb, state)
    date_val = cb.data.replace("date_","")
    user_data_db[uid]["date"] = date_val
    await state.set_state(OrderForm.time)
    await cb.message.answer(t(uid,"ask_time"), reply_markup=time_kb(uid))

# ── ДАТА — кнопка «Указать дату» (ручной ввод) ──
@dp.callback_query(F.data == "date_pick")
async def order_date_pick(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    from datetime import date as _dt, timedelta
    example = (_dt.today() + timedelta(days=7)).strftime("%d.%m.%Y")
    txt_ru = f"✏️ Введите дату в формате ДД.ММ.ГГГГ\n\nПример: {example}"
    txt_uz = f"✏️ Sanani KK.OO.YYYY formatida kiriting\n\nMisol: {example}"
    await state.set_state(OrderForm.date)
    await cb.message.answer(txt_uz if lang(uid) == "uz" else txt_ru,
                            reply_markup=cancel_kb(uid))
    await cb.answer()

@dp.message(OrderForm.date)
async def order_date_manual(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    text = (msg.text or "").strip()
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    valid = False
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            from datetime import date as dt_date
            d = dt_date(year, month, day)
            if d >= dt_date.today():
                valid = True
        except ValueError:
            valid = False
    if not valid:
        await msg.answer(t(uid,"date_invalid"), reply_markup=cancel_kb(uid), parse_mode="Markdown")
        return
    user_data_db[uid]["date"] = text
    await state.set_state(OrderForm.time)
    await msg.answer(t(uid,"ask_time"), reply_markup=time_kb(uid))


# ── ВРЕМЯ — 3-кнопочное меню ──
@dp.callback_query(OrderForm.time, F.data.in_({"time_morning","time_evening","time_custom"}))
async def order_time_choice(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if cb.data == "time_morning":
        await cb.answer()
        await finish_order(cb.message, uid, "08:00 — 13:00", state, user_from=cb.from_user)
    elif cb.data == "time_evening":
        await cb.answer()
        await finish_order(cb.message, uid, "13:00 — 20:00", state, user_from=cb.from_user)
    else:  # time_custom
        await state.set_state(OrderForm.time_from)
        await cb.message.answer(t(uid,"ask_time_from"), parse_mode="Markdown",
                                reply_markup=time_from_kb(uid))
        await cb.answer()

# ── ВРЕМЯ — сетка «с» ──
@dp.callback_query(OrderForm.time_from, F.data.startswith("tslot_from_"))
async def order_time_from_cb(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    from_h = int(cb.data.split("_")[-1])
    user_data_db[uid]["time_from_h"] = from_h
    await state.set_state(OrderForm.time_to)
    await cb.message.answer(t(uid,"ask_time_to"), parse_mode="Markdown",
                            reply_markup=time_to_kb(uid, from_h))
    await cb.answer()

# ── ВРЕМЯ — сетка «до» ──
@dp.callback_query(OrderForm.time_to, F.data.startswith("tslot_to_"))
async def order_time_to_cb(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    from_h = user_data_db[uid].get("time_from_h", 8)
    to_h   = int(cb.data.split("_")[-1])
    time_txt = f"{from_h:02d}:00 — {to_h:02d}:00"
    await finish_order(cb.message, uid, time_txt, state, user_from=cb.from_user)
    await cb.answer()

async def finish_order(msg_or_cb, uid: int, time_txt: str, state: FSMContext, user_from=None):
    d = user_data_db.get(uid, {})
    answer_fn = msg_or_cb.answer

    user_obj   = user_from or getattr(msg_or_cb, 'from_user', None)
    first_name = getattr(user_obj, 'first_name', '') or ''
    last_name  = getattr(user_obj, 'last_name',  '') or ''
    username   = getattr(user_obj, 'username',   '') or ''
    tg_name    = f"{first_name} {last_name}".strip() or f"@{username}"

    d["time"] = time_txt

    await upsert_client(tg_id=uid, username=username,
                        first_name=first_name, last_name=last_name,
                        phone=d.get("phone",""), lang=lang(uid))
    if d.get("phone",""):
        await upsert_crm_client(phone=d["phone"], first_name=first_name, last_name=last_name,
                                tg_id=uid, tg_username=username, source="bot")

    lead_code = await _submit_bot_lead(uid, d, is_quick=False, user_from=user_from)

    branch_phones = _branch_phones_str(d.get("branch",""))
    done_msg = t(uid,"order_done").format(num=lead_code or "?", branch_phones=branch_phones)
    await answer_fn(done_msg, reply_markup=back_kb(uid), parse_mode="Markdown")

    # В Google Таблицу
    await send_to_sheets({
        "name":        d.get("name",""),
        "tg_id":       str(uid),
        "tg_username": f"@{username}" if username else "",
        "tg_name":     tg_name,
        "phone":       d.get("phone",""),
        "branch":      d.get("branch_name",""),
        "city":        d.get("city",""),
        "address":     d.get("address",""),
        "location":    d.get("location",""),
        "service":     d.get("service",""),
        "service_type": d.get("service_type",""),
        "date":        d.get("date",""),
        "time":        time_txt,
        "note":        f"Telegram (бот, подробная заявка)",
        "status":      "Новый",
    })
    await state.clear()

# ── ОТМЕНА ──
@dp.callback_query(F.data == "cancel_order")
async def cancel_order(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    await state.clear()
    await cb.message.answer(t(uid,"cancel"), reply_markup=back_kb(uid))

# ── КАЛЬКУЛЯТОР ──
@dp.callback_query(F.data == "menu_calc")
async def menu_calc(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    user_data_db[uid] = {}
    await ensure_prices_fresh()
    await state.set_state(CalcForm.service)
    await cb.message.answer(t(uid,"calc_ask_svc"), reply_markup=service_kb(uid), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(CalcForm.service, F.data.startswith("svc_"))
async def calc_service(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    svc = cb.data.replace("svc_","")
    user_data_db[uid]["calc_svc"] = svc
    await state.set_state(CalcForm.service_type)
    await cb.message.answer(t(uid,"ask_service_type"), reply_markup=service_type_kb(uid))

@dp.callback_query(CalcForm.service_type, F.data.startswith("svctype_"))
async def calc_service_type(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    svctype = cb.data.replace("svctype_","")
    user_data_db[uid]["calc_svctype"] = svctype
    svc = user_data_db[uid].get("calc_svc","carpet")
    await state.set_state(CalcForm.width)
    header = t(uid,"calc_selected_header").format(svc=svc_display_name(uid, svc, svctype))
    await cb.message.answer(header + "\n\n" + t(uid,"calc_ask_w"), reply_markup=cancel_kb(uid), parse_mode="Markdown")

@dp.message(CalcForm.width)
async def calc_width(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    try:
        w = float(msg.text.replace(",","."))
        user_data_db[uid]["calc_w"] = w
        await state.set_state(CalcForm.length)
        d       = user_data_db.get(uid,{})
        svc     = d.get("calc_svc","carpet")
        svctype = d.get("calc_svctype","standard")
        header  = t(uid,"calc_selected_header").format(svc=svc_display_name(uid, svc, svctype))
        await msg.answer(header + "\n\n" + t(uid,"calc_ask_l"), reply_markup=cancel_kb(uid), parse_mode="Markdown")
    except:
        await msg.answer(t(uid,"invalid_num"))

@dp.message(CalcForm.length)
async def calc_length(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    try:
        l = float(msg.text.replace(",","."))
        user_data_db[uid]["calc_l"] = l
    except:
        await msg.answer(t(uid,"invalid_num"))
        return

    d   = user_data_db.get(uid,{})
    svc     = d.get("calc_svc","carpet")
    svctype = d.get("calc_svctype","standard")
    w       = d.get("calc_w",200)
    sqm_real = (w/100) * (l/100)
    min_order = get_cached_min_order(svc, svctype)
    if min_order:
        sqm_bill = max(sqm_real, min_order)
    else:
        sqm_bill = sqm_real
    price     = get_cached_price(svc, svctype)
    total     = int(sqm_bill * price)
    unit_sym  = get_unit_symbol(get_cached_unit_key(svc, svctype), uid)

    fmt_args = dict(
        w=int(w), l=int(l), sqm=round(sqm_real, 2), unit=unit_sym,
        svc=svc_display_name(uid, svc, svctype),
        price=f"{price:,}".replace(",", " "),
        total=f"{total:,}".replace(",", " "),
        min_order=min_order,
    )
    if min_order and sqm_real < min_order:
        result = t(uid, "calc_result_below_min").format(**fmt_args)
    else:
        result = t(uid, "calc_result_no_min").format(**fmt_args)
    await msg.answer(result, reply_markup=back_kb(uid), parse_mode="Markdown")
    await state.clear()

# ── КНОПКИ В ГРУППЕ ──
@dp.callback_query(F.data.startswith("accept_"))
async def group_accept(cb: CallbackQuery):
    parts     = cb.data.split("_")
    order_num = parts[1]
    client_id = int(parts[2])
    w = cb.from_user
    wname = f"{w.first_name or ''} {w.last_name or ''}".strip()
    await update_order_status(
        order_num=order_num, new_status="confirmed",
        by_tg_id=w.id, by_name=wname,
        note=f"Принял оператор {wname}",
        extra={
            "operator_tg_id": w.id,
            "operator_username": w.username or "",
            "operator_first_name": w.first_name or "",
            "operator_last_name": w.last_name or "",
            "accepted_at": now_local().replace(tzinfo=None),
        }
    )
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ Принял: {wname}" + (f" @{w.username}" if w.username else ""),
            callback_data="done"
        )],
        [InlineKeyboardButton(text="🚗 Назначить водителя", callback_data=f"driver_{order_num}_{client_id}")]
    ]))
    try:
        await bot.send_message(client_id,
            f"✅ Ваша заявка *{order_num}* принята!\nМенеджер свяжется с вами в ближайшее время.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.warning(f"Client notify error: {e}")
    await cb.answer(f"Вы приняли заказ {order_num}")

@dp.callback_query(F.data.startswith("driver_"))
async def group_driver(cb: CallbackQuery):
    parts     = cb.data.split("_")
    order_num = parts[1]
    client_id = parts[2]

    drivers = await get_staff_by_role("driver")
    if not drivers:
        await cb.answer("⚠️ Список водителей пуст. Добавьте их командой /add_driver", show_alert=True)
        return

    rows = list(cb.message.reply_markup.inline_keyboard) if cb.message.reply_markup else []
    # Убираем строку с кнопкой "Назначить водителя" / "Отклонить", оставляем остальное (например "Принял")
    rows = [r for r in rows if not any(
        (btn.callback_data or "").startswith(("driver_", "reject_")) for btn in r
    )]
    for d in drivers:
        fname = f"{d['first_name'] or ''} {d['last_name'] or ''}".strip() or f"id{d['tg_id']}"
        rows.append([InlineKeyboardButton(
            text=f"🚗 {fname}",
            callback_data=f"setdriver_{order_num}_{client_id}_{d['tg_id']}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"backdriver_{order_num}_{client_id}")])

    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@dp.callback_query(F.data.startswith("backdriver_"))
async def group_driver_back(cb: CallbackQuery):
    parts     = cb.data.split("_")
    order_num = parts[1]
    client_id = parts[2]

    rows = list(cb.message.reply_markup.inline_keyboard) if cb.message.reply_markup else []
    # Убираем строки с выбором водителя и "Назад"
    rows = [r for r in rows if not any(
        (btn.callback_data or "").startswith(("setdriver_", "backdriver_")) for btn in r
    )]
    already_accepted = any(
        (btn.callback_data or "") == "done" and "Принял" in (btn.text or "")
        for r in rows for btn in r
    )
    if already_accepted:
        rows.append([InlineKeyboardButton(text="🚗 Назначить водителя", callback_data=f"driver_{order_num}_{client_id}")])
    else:
        rows.append([
            InlineKeyboardButton(text="🚗 Назначить водителя", callback_data=f"driver_{order_num}_{client_id}"),
            InlineKeyboardButton(text="❌ Отклонить",          callback_data=f"reject_{order_num}_{client_id}"),
        ])
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@dp.callback_query(F.data.startswith("setdriver_"))
async def group_set_driver(cb: CallbackQuery):
    parts        = cb.data.split("_")
    order_num    = parts[1]
    client_id    = parts[2]
    driver_tg_id = int(parts[3])

    drivers = await get_staff_by_role("driver")
    driver  = next((d for d in drivers if d["tg_id"] == driver_tg_id), None)
    if not driver:
        await cb.answer("⚠️ Водитель не найден", show_alert=True)
        return

    dname = f"{driver['first_name'] or ''} {driver['last_name'] or ''}".strip() or f"id{driver_tg_id}"
    chooser = cb.from_user
    chooser_name = f"{chooser.first_name or ''} {chooser.last_name or ''}".strip()

    await update_order_status(
        order_num=order_num, new_status="pickup",
        by_tg_id=chooser.id, by_name=chooser_name,
        note=f"{chooser_name} назначил водителем: {dname}",
        extra={
            "driver_pickup_tg_id": driver["tg_id"],
            "driver_pickup_username": driver["tg_username"] or "",
            "driver_pickup_first_name": driver["first_name"] or "",
            "driver_pickup_last_name": driver["last_name"] or "",
            "pickup_at": now_local().replace(tzinfo=None),
        }
    )

    rows = list(cb.message.reply_markup.inline_keyboard) if cb.message.reply_markup else []
    rows = [r for r in rows if not any(
        (btn.callback_data or "").startswith(("setdriver_", "backdriver_")) for btn in r
    )]
    rows.append([InlineKeyboardButton(
        text=f"🚗 Водитель: {dname}" + (f" @{driver['tg_username']}" if driver["tg_username"] else ""),
        callback_data="done"
    )])
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    try:
        await bot.send_message(driver["tg_id"],
            f"🚗 Вам назначен заказ *{order_num}* на вывоз/доставку.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.warning(f"Driver notify error: {e}")

    await cb.answer(f"Водитель {dname} назначен на заказ {order_num}", show_alert=True)


@dp.callback_query(F.data.startswith("reject_"))
async def group_reject(cb: CallbackQuery):
    parts     = cb.data.split("_")
    order_num = parts[1]
    client_id = int(parts[2])
    w = cb.from_user
    wname = f"{w.first_name or ''} {w.last_name or ''}".strip()
    await update_order_status(
        order_num=order_num, new_status="cancelled",
        by_tg_id=w.id, by_name=wname,
        note=f"Отклонил {wname}"
    )
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ Отклонил: {wname}", callback_data="done")]
    ]))
    try:
        await bot.send_message(client_id,
            t(client_id, "order_rejected").format(num=order_num),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.warning(f"Client notify error: {e}")
    await cb.answer(f"Заказ {order_num} отклонён")

# ── МАРШРУТ: ВОДИТЕЛЬ ЗАБИРАЕТ / СДАЁТ (rp:) ──
_ROUTE_STATUS_RU = {
    "confirmed": "Подтверждён", "pickup": "Вывоз", "received": "В мастерской",
    "washing": "Стирка", "ready": "Готов", "delivery": "Доставка", "delivered": "Доставлен",
}
_STAFF_CONFIRM_ROLES = {"admin", "manager"}
_PICKUP_SVCS = [                          # (service_key, emoji_label)
    ("carpet",      "🏠 Ковёр"),
    ("carpet_home", "🏡 Ковёр (на дому)"),
    ("sofa",        "🛋 Диван"),
    ("mattress",    "🛏 Матрас"),
    ("curtains",    "🪟 Шторы"),
]
_pickup_carts: dict = {}                  # {(user_id, order_id): {svc_idx: qty}}
_pending_receipt: dict[int, dict] = {}    # {tg_user_id: {order_id,amount,method,wname}} — ждём фото чека
_pending_payment: dict[int, dict] = {}    # {tg_user_id: {order_id, method}} — ждём сумму от водителя
_pending_debt_approvals: dict[int, dict] = {}  # {order_id: {driver_tg_id, order_num, debt, mgr_msgs: {tg_id: msg_id}}}
_pending_discount: dict[int, dict] = {}   # {tg_user_id: {order_id, order_num, chat_id, msg_id}}

_history_orig: dict = {}   # {(chat_id, msg_id): (html_text, status, ts)}

def _history_fmt(activity: list, order_num: str) -> list:
    """Возвращает список строк-страниц истории (по 4 записи)."""
    from datetime import timezone, timedelta
    UZ = timezone(timedelta(hours=5))
    PER = 4
    entries = []
    for a in activity:
        dt = a.get("created_at")
        if dt and hasattr(dt, "astimezone"):
            t = dt.astimezone(UZ).strftime("%d.%m %H:%M")
        else:
            t = str(dt or "")[:16].replace("T", " ")
        d = a.get("details","") or a.get("action","")
        for k, v in _ROUTE_STATUS_RU.items():
            d = d.replace(k, v)
        name = (a.get("staff_name") or "").strip()
        if not name or name == "Водитель (TG)":
            name = "Водитель"
        entries.append(f"🕐 {t}\n{d}\n👤 {name}")
    if not entries:
        return [f"📋 {order_num}\n(история пуста)"]
    total = (len(entries) + PER - 1) // PER
    pages = []
    for i in range(0, len(entries), PER):
        pg = i // PER + 1
        header = f"📋 {order_num}  ({pg}/{total})"
        sep = "─" * 20
        pages.append(header + f"\n{sep}\n" + f"\n{sep}\n".join(entries[i:i+PER]))
    return pages

def _history_nav_kb(order_id: int, page: int, total: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"rp:{order_id}:history:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1} / {total}", callback_data=f"rp:{order_id}:histnoop"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"rp:{order_id}:history:{page+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="✖ Закрыть историю", callback_data=f"rp:{order_id}:history:close")],
    ])

# Причины пропуска (забор — клиента не было и т.п.)
_SKIP_REASONS = [
    ("no_client",  "🚪 Клиента не было"),
    ("refused",    "🚫 Отказался"),
    ("reschedule", "📅 Перенос"),
    ("no_answer",  "📵 Не дозвонился"),
    ("wrong_addr", "📍 Неверный адрес"),
    ("other",      "✏️ Другое"),
]

# Причины «Не забрал» (доставка — не взял из мастерской)
_NOT_TAKEN_REASONS = [
    ("no_space",   "🚗 Нет места в машине"),
    ("not_ready",  "⏳ Заказ ещё не готов"),
    ("reschedule", "📅 Клиент перенёс"),
    ("fragile",    "📦 Хрупкий/негабаритный"),
    ("other",      "✏️ Другое"),
]

# Причины «Вернул в мастерскую» (доставка — не смог доставить)
_RETURNED_REASONS = [
    ("no_client",  "🚪 Клиента нет дома"),
    ("no_addr",    "📍 Не нашёл адрес"),
    ("no_answer",  "📵 Не дозвонился"),
    ("refused",    "🚫 Клиент отказался"),
    ("reschedule", "📅 Клиент перенёс"),
    ("road",       "🚧 Дорога недоступна"),
    ("other",      "✏️ Другое"),
]

def _skip_reason_kb(order_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rp:{order_id}:skipr:{i}")]
            for i, (_, label) in enumerate(_SKIP_REASONS)]
    rows.append([InlineKeyboardButton(text="↩️ Отмена", callback_data=f"rp:{order_id}:skipcancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _not_taken_reason_kb(order_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rp:{order_id}:ntakenr:{i}")]
            for i, (_, label) in enumerate(_NOT_TAKEN_REASONS)]
    rows.append([InlineKeyboardButton(text="↩️ Отмена", callback_data=f"rp:{order_id}:ntakenc")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _returned_reason_kb(order_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rp:{order_id}:retbackr:{i}")]
            for i, (_, label) in enumerate(_RETURNED_REASONS)]
    rows.append([InlineKeyboardButton(text="↩️ Отмена", callback_data=f"rp:{order_id}:retbackc")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _skipped_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Отменить пропуск", callback_data=f"rp:{order_id}:unskip")],
        [InlineKeyboardButton(text="📋 История", callback_data=f"rp:{order_id}:history"),
         InlineKeyboardButton(text="🔄 Обновить", callback_data=f"rp:{order_id}:refresh")],
    ])

def _fmt_stop_text(stop: dict, num: int) -> str:
    import html as _h
    def h(s): return _h.escape(str(s)) if s else ""
    order_num = (stop.get("order_num") or "").replace("ARTEZ-", "")
    item_count = stop.get("item_count", 0) or 0
    addr  = stop.get("short_address") or stop.get("address") or stop.get("location_address") or "—"
    first = (stop.get("client_first_name") or "").strip()
    last  = (stop.get("client_last_name")  or "").strip()
    client = f"{first} {last}".strip() or "—"
    phone  = stop.get("client_phone") or ""
    total  = float(stop.get("items_total") or stop.get("total_price") or 0)
    disc   = (float(stop.get("discount_sum") or 0) + float(stop.get("delivery_discount") or 0)
              + float(stop.get("manual_discount") or 0))
    net    = max(0.0, total - disc)
    paid   = float(stop.get("paid_amount") or 0)
    debt   = max(0.0, net - paid)
    def _f(n): return f"{int(n):,}".replace(",", " ") + " с" if n > 0 else "—"
    pay_line = f"💰 {_f(net)} · Опл: {_f(paid)} · Долг: {_f(debt)}"
    contact = f"👤 {h(client)}"
    if phone: contact += f" 📞{h(phone)}"
    count_str = f" / {item_count}" if item_count else ""
    return f"📦 #{num}·{h(order_num)}{count_str} 📍{h(addr)}\n{contact}\n{pay_line}"


async def _update_channel_stop(order_id: int, order_id_for_kb: int = None):
    """Обновить сообщение заказа в канале водителей после изменения оплаты."""
    info = await get_route_channel_info_for_order(order_id)
    if not info or not info.get("msg_id") or not info.get("channel_id"):
        logging.warning(f"_update_channel_stop: no channel info for order {order_id} info={info}")
        return
    logging.info(f"_update_channel_stop order={order_id} channel_id={info['channel_id']} msg_id={info['msg_id']}")
    try:
        new_text = _fmt_stop_text(info, info["stop_num"])
        kb_order_id = order_id_for_kb if order_id_for_kb is not None else order_id
        order = await get_order_by_id(kb_order_id)
        status = order.get("status", "delivery") if order else "delivery"
        kb = _route_pickup_kb(kb_order_id, status)
        payload = {
            "chat_id": info["channel_id"],
            "message_id": info["msg_id"],
            "text": new_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": kb.model_dump(exclude_none=True),
        }
        async with aiohttp.ClientSession() as sess:
            resp = await sess.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
                json=payload, timeout=aiohttp.ClientTimeout(total=6))
            res = await resp.json()
            if not res.get("ok"):
                logging.warning(f"_update_channel_stop TG error order={order_id}: {res}")
    except Exception as e:
        logging.warning(f"_update_channel_stop exception order={order_id}: {e}", exc_info=True)


def _route_pickup_kb(order_id: int, status: str) -> InlineKeyboardMarkup:
    h = InlineKeyboardButton(text="📋 История", callback_data=f"rp:{order_id}:history")
    r = InlineKeyboardButton(text="🔄 Обновить", callback_data=f"rp:{order_id}:refresh")
    p = InlineKeyboardButton(text="📦 Позиции", callback_data=f"rp:{order_id}:items")
    if status == "confirmed":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Забрал", callback_data=f"rp:{order_id}:take"),
             InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"rp:{order_id}:skip")],
            [p, h, r],
        ])
    elif status == "pickup":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏭 Сдал в мастерскую", callback_data=f"rp:{order_id}:deliver")],
            [InlineKeyboardButton(text="↩️ Не забирал", callback_data=f"rp:{order_id}:undo")],
            [p, h, r],
        ])
    elif status == "ready":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚗 Везу клиенту", callback_data=f"rp:{order_id}:take_delivery"),
             InlineKeyboardButton(text="❌ Не забрал", callback_data=f"rp:{order_id}:ntaken")],
            [p, h, r],
        ])
    elif status == "delivery":
        disc = InlineKeyboardButton(text="💸 Скидка", callback_data=f"rp:{order_id}:disc_init")
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Доставил клиенту", callback_data=f"rp:{order_id}:mark_delivered")],
            [InlineKeyboardButton(text="💳 Оплата", callback_data=f"rp:{order_id}:pay_init"), disc],
            [InlineKeyboardButton(text="🔙 Вернул в мастерскую", callback_data=f"rp:{order_id}:retback")],
            [p, h, r],
        ])
    elif status == "delivered":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отменить «Доставлен»", callback_data=f"rp:{order_id}:undo_delivered")],
            [p, h, r],
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[[p, h, r]])

def _deliver_pending_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить приём", callback_data=f"rp:{order_id}:confirm_receive")],
        [InlineKeyboardButton(text="↩️ Не сдавал", callback_data=f"rp:{order_id}:undo_deliver"),
         InlineKeyboardButton(text="📋 История", callback_data=f"rp:{order_id}:history"),
         InlineKeyboardButton(text="🔄 Обновить", callback_data=f"rp:{order_id}:refresh")],
    ])

def _svc_kb(order_id: int, user_id: int) -> InlineKeyboardMarkup:
    cart  = _pickup_carts.get((user_id, order_id), {})
    total = sum(cart.values())
    rows, row = [], []
    for i, (_, label) in enumerate(_PICKUP_SVCS):
        qty  = cart.get(i, 0)
        text = f"{label} ✓{qty}" if qty > 0 else label
        row.append(InlineKeyboardButton(text=text, callback_data=f"rp:{order_id}:svc:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    bottom = [InlineKeyboardButton(text="↩️ Отменить", callback_data=f"rp:{order_id}:undo")]
    if total > 0:
        bottom.append(InlineKeyboardButton(
            text=f"✅ Подтвердить ({total} изд.)", callback_data=f"rp:{order_id}:sconfirm"))
    rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _sqty_kb(order_id: int, svc_idx: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=str(n), callback_data=f"rp:{order_id}:sqty:{svc_idx}:{n}")
         for n in range(s, min(s + 5, 21))]
        for s in range(1, 21, 5)
    ]
    rows.append([
        InlineKeyboardButton(text="← Назад", callback_data=f"rp:{order_id}:svc"),
        InlineKeyboardButton(text="✖ Убрать",  callback_data=f"rp:{order_id}:sqty:{svc_idx}:0"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data.startswith("rp:"))
async def route_pickup_cb(cb: CallbackQuery):
    try:
        parts    = cb.data.split(":")
        order_id = int(parts[1])
        action   = parts[2]

        staff = await get_staff_by_tg_id_for_lead(cb.from_user.id)
        if not staff:
            await cb.answer("❌ Доступ только для сотрудников ARTEZ", show_alert=True)
            return

        order = await get_order_by_id(order_id)
        if not order:
            await cb.answer("❌ Заказ не найден", show_alert=True)
            return

        cur   = order["status"]
        w     = cb.from_user
        wname = f"{w.first_name or ''} {w.last_name or ''}".strip()
        orig  = cb.message.html_text or cb.message.text or ""

        # ── История (с пагинацией) ──────────────────────────────────
        if action == "histnoop":
            await cb.answer()
            return

        if action == "history":
            page_str = parts[3] if len(parts) > 3 else "0"
            key = (cb.message.chat.id, cb.message.message_id)

            if page_str == "close":
                saved = _history_orig.pop(key, None)
                if saved:
                    orig_text, orig_status = saved[0], saved[1]
                    await cb.message.edit_text(
                        orig_text, reply_markup=_route_pickup_kb(order_id, orig_status),
                        parse_mode="HTML", disable_web_page_preview=True)
                else:
                    order2 = await get_order_by_id(order_id)
                    st2 = (order2 or {}).get("status", "confirmed")
                    await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, st2))
                await cb.answer()
                return

            page = int(page_str) if page_str.isdigit() else 0
            activity = await get_order_activity_by_id(order_id)
            pages = _history_fmt(activity, order.get("order_num",""))
            total = len(pages)
            page  = max(0, min(page, total - 1))

            if key not in _history_orig:
                import time as _t
                now = _t.time()
                # Чистим записи старше 2 часов
                stale = [k for k, v in _history_orig.items() if now - v[2] > 7200]
                for k in stale: del _history_orig[k]
                _history_orig[key] = (orig, cur, now)

            await cb.message.edit_text(
                pages[page],
                reply_markup=_history_nav_kb(order_id, page, total),
                disable_web_page_preview=True)
            await cb.answer()
            return

        # ── Позиции заказа ──────────────────────────────────────────
        if action == "items":
            if parts[3:4] == ["close"]:
                saved = _history_orig.pop((cb.message.chat.id, cb.message.message_id), None)
                if saved:
                    await cb.message.edit_text(
                        saved[0], reply_markup=_route_pickup_kb(order_id, saved[1]),
                        parse_mode="HTML", disable_web_page_preview=True)
                else:
                    order2 = await get_order_by_id(order_id)
                    st2 = (order2 or {}).get("status", cur)
                    await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, st2))
                await cb.answer()
                return
            items = await get_order_items_for_driver(order_id)
            order_num = (order.get("order_num") or f"#{order_id}").replace("ARTEZ-", "")
            if not items:
                await cb.answer("Позиции не найдены", show_alert=True)
                return
            import html as _ih
            def _fmt_n(n): return f"{int(n):,}".replace(",", " ")
            lines = []
            grand_total = 0.0
            for i, it in enumerate(items, 1):
                name    = _ih.escape(str(it.get("name") or "—"))
                w       = float(it.get("width_cm") or 0)
                l       = float(it.get("length_cm") or 0)
                sqm     = float(it.get("sqm") or 0)
                price   = float(it.get("price_per_sqm") or 0)
                total_i = float(it.get("line_total") or 0)
                grand_total += total_i
                # Строка размеров
                if w and l:
                    dim_line = f"   {int(w)}×{int(l)} см · {sqm:g} м²"
                elif sqm:
                    dim_line = f"   {sqm:g} м²"
                else:
                    dim_line = ""
                # Строка цены и суммы
                price_line = ""
                if price and total_i:
                    price_line = f"   {_fmt_n(price)} с/м² · {_fmt_n(total_i)} с"
                elif total_i:
                    price_line = f"   {_fmt_n(total_i)} с"
                block = f"{i}. {name}"
                if dim_line:   block += f"\n{dim_line}"
                if price_line: block += f"\n{price_line}"
                lines.append(block)
            total_line = f"\n💰 Итого: <b>{_fmt_n(grand_total)} с</b>"
            text = (f"📦 <b>Заказ {_ih.escape(order_num)}</b> — {len(items)} поз.\n\n"
                    + "\n\n".join(lines) + total_line)
            key = (cb.message.chat.id, cb.message.message_id)
            if key not in _history_orig:
                import time as _t
                now = _t.time()
                stale = [k for k, v in _history_orig.items() if now - v[2] > 7200]
                for k in stale: del _history_orig[k]
                _history_orig[key] = (orig, cur, now)
            close_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Закрыть", callback_data=f"rp:{order_id}:items:close")]
            ])
            await cb.message.edit_text(text, reply_markup=close_kb, parse_mode="HTML")
            await cb.answer()
            return

        # ── Подтвердить приём (только admin/manager) ────────────────
        if action == "confirm_receive":
            staff = await get_staff_by_tg_id_for_lead(w.id)
            if not staff or staff.get("role") not in _STAFF_CONFIRM_ROLES:
                await cb.answer("❌ Только менеджеры и администраторы", show_alert=True)
                return
            staff_name = f"{staff.get('first_name','')} {staff.get('last_name','')}".strip() or wname
            await update_order_status_by_id(order_id, "received", by_tg_id=w.id, by_name=staff_name,
                                            note="Подтверждён приём в мастерской")
            await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 История", callback_data=f"rp:{order_id}:history")
            ]]))
            await cb.answer("✅ Принято! Статус → В мастерской")
            return

        # ── Выбор типа услуги (после «Забрал») ──────────────────────
        if action == "take":
            if cur != "confirmed":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            _pickup_carts[(w.id, order_id)] = {}
            await cb.message.edit_reply_markup(reply_markup=_svc_kb(order_id, w.id))
            await cb.answer("Выберите тип изделий")
            return

        # ── Пропустить точку маршрута: показать причины ─────────────
        if action == "skip":
            if cur not in ("confirmed", "ready"):
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            await cb.message.edit_reply_markup(reply_markup=_skip_reason_kb(order_id))
            await cb.answer("Выберите причину пропуска")
            return

        if action == "skipcancel":
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, cur))
            await cb.answer("↩️ Отменено")
            return

        # ── Причина выбрана → stop_status=skipped + запись в историю ─
        if action == "skipr":
            if cur not in ("confirmed", "ready"):
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
            if idx < 0 or idx >= len(_SKIP_REASONS):
                await cb.answer("❌ Причина не найдена", show_alert=True)
                return
            reason = _SKIP_REASONS[idx][1]
            await set_route_stop_status(order_id, "skipped")
            await add_order_activity(order_id, "route_skip", f"⏭ Пропущено: {reason}",
                                     staff_name=wname or "Водитель (TG)")
            await cb.message.edit_reply_markup(reply_markup=_skipped_kb(order_id))
            await cb.answer(f"⏭ Пропущено: {reason}")
            return

        if action == "unskip":
            await set_route_stop_status(order_id, "pending")
            await add_order_activity(order_id, "route_unskip", "↩️ Пропуск отменён",
                                     staff_name=wname or "Водитель (TG)")
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, cur))
            await cb.answer("↩️ Пропуск отменён")
            return

        # ── Не забрал из мастерской (доставка, ready) ───────────────
        if action == "ntaken":
            if cur != "ready":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            await cb.message.edit_reply_markup(reply_markup=_not_taken_reason_kb(order_id))
            await cb.answer("Выберите причину")
            return

        if action == "ntakenc":
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "ready"))
            await cb.answer("↩️ Отменено")
            return

        if action == "ntakenr":
            if cur != "ready":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
            if idx < 0 or idx >= len(_NOT_TAKEN_REASONS):
                await cb.answer("❌ Причина не найдена", show_alert=True)
                return
            reason = _NOT_TAKEN_REASONS[idx][1]
            await set_route_stop_status(order_id, "skipped")
            await set_driver_confirmed(order_id, confirmed=False)
            await add_order_activity(order_id, "not_taken", f"❌ Не забрал: {reason}",
                                     staff_name=wname or "Водитель (TG)")
            await cb.message.edit_reply_markup(reply_markup=_skipped_kb(order_id))
            await cb.answer(f"❌ {reason}")
            return

        # ── Вернул в мастерскую (доставка, delivery) ────────────────
        if action == "retback":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            await cb.message.edit_reply_markup(reply_markup=_returned_reason_kb(order_id))
            await cb.answer("Выберите причину")
            return

        if action == "retbackc":
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "delivery"))
            await cb.answer("↩️ Отменено")
            return

        if action == "retbackr":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
            if idx < 0 or idx >= len(_RETURNED_REASONS):
                await cb.answer("❌ Причина не найдена", show_alert=True)
                return
            reason = _RETURNED_REASONS[idx][1]
            await update_order_status_by_id(order_id, "ready", by_tg_id=w.id, by_name=wname,
                                            note=f"Маршрут: вернул в мастерскую — {reason}")
            await set_route_stop_status(order_id, "skipped")
            await set_driver_confirmed(order_id, confirmed=False)
            await add_order_activity(order_id, "returned", f"🔙 Вернул в мастерскую: {reason}",
                                     staff_name=wname or "Водитель (TG)")
            await cb.message.edit_reply_markup(reply_markup=_skipped_kb(order_id))
            await cb.answer(f"🔙 {reason}")
            return

        if action == "svc":
            sub = parts[3] if len(parts) > 3 else None
            if sub and sub.isdigit():
                await cb.message.edit_reply_markup(reply_markup=_sqty_kb(order_id, int(sub)))
                await cb.answer(f"Кол-во: {_PICKUP_SVCS[int(sub)][1]}")
            else:
                await cb.message.edit_reply_markup(reply_markup=_svc_kb(order_id, w.id))
                await cb.answer()
            return

        if action == "sqty":
            svc_idx = int(parts[3])
            qty     = int(parts[4])
            cart    = _pickup_carts.setdefault((w.id, order_id), {})
            if qty == 0:
                cart.pop(svc_idx, None)
                await cb.answer("Убрано")
            else:
                cart[svc_idx] = qty
                await cb.answer(f"{_PICKUP_SVCS[svc_idx][1]}: {qty}")
            await cb.message.edit_reply_markup(reply_markup=_svc_kb(order_id, w.id))
            return

        if action == "sconfirm":
            cart = _pickup_carts.pop((w.id, order_id), {})
            if not cart:
                await cb.answer("❌ Выберите хотя бы один тип изделий", show_alert=True)
                return
            service_type = order.get("service_type") or "standard"
            type_label   = "Экспресс" if service_type == "express" else "Стандарт"
            svc_keys     = [_PICKUP_SVCS[i][0] for i in cart]
            price_map    = await get_prices_for_services(svc_keys, service_type)
            items = [(_PICKUP_SVCS[i][0], qty, f"{_PICKUP_SVCS[i][1]} ({type_label})")
                     for i, qty in cart.items()]
            await create_pickup_items(order_id, items, price_map)
            note = ", ".join(f"{_PICKUP_SVCS[i][1]}:{q}" for i, q in cart.items())
            await update_order_status_by_id(order_id, "pickup", by_tg_id=w.id, by_name=wname,
                                            note=f"Забрал: {note}")
            await set_route_stop_status(order_id, "done")
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "pickup"))
            await cb.answer(f"✅ Забрал {sum(cart.values())} изд. → Вывоз")
            return

        # ── «Сдал» — показываем кнопки подтверждения (статус не меняем) ─
        if action == "deliver":
            if cur != "pickup":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            await cb.message.edit_reply_markup(reply_markup=_deliver_pending_kb(order_id))
            await cb.answer("🏭 Ожидает подтверждения менеджера")
            return

        if action == "undo_deliver":
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "pickup"))
            await cb.answer("↩️ Отменено")
            return

        # ── ДОСТАВКА КЛИЕНТУ ────────────────────────────────────────
        if action == "take_delivery":
            if cur not in ("ready", "delivery"):
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            if cur == "ready":
                await update_order_status_by_id(order_id, "delivery", by_tg_id=w.id, by_name=wname,
                                                note="Маршрут: забрал для доставки клиенту")
            await set_driver_confirmed(order_id)
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "delivery"))
            await cb.answer("🚗 В пути → Доставка")
            return

        if action == "mark_delivered":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, доставил клиенту", callback_data=f"rp:{order_id}:deliver_confirm")],
                [InlineKeyboardButton(text="❌ Отмена",               callback_data=f"rp:{order_id}:deliver_cancel")],
            ])
            await cb.message.edit_reply_markup(reply_markup=confirm_kb)
            await cb.answer("Подтвердите доставку")
            return

        if action == "deliver_cancel":
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "delivery"))
            await cb.answer("Отменено")
            return

        if action == "deliver_confirm":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            debt = await get_order_debt(order_id)
            if debt <= 0:
                await update_order_status_by_id(order_id, "delivered", by_tg_id=w.id, by_name=wname,
                                                note="Маршрут: доставлен клиенту")
                await set_route_stop_status(order_id, "done")
                await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "delivered"))
                await cb.answer("✅ Доставлен клиенту!", show_alert=True)
                return
            debt_str = f"{int(debt):,}".replace(",", " ") + " сум"
            pay_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💵 Наличные ({debt_str})",  callback_data=f"rp:{order_id}:pay:cash")],
                [InlineKeyboardButton(text=f"💳 Картой ({debt_str})",    callback_data=f"rp:{order_id}:pay:card")],
                [InlineKeyboardButton(text=f"📱 Перевод ({debt_str})",   callback_data=f"rp:{order_id}:pay:transfer")],
                [InlineKeyboardButton(text="✓ Уже оплачен / без оплаты", callback_data=f"rp:{order_id}:pay:none")],
                [InlineKeyboardButton(text="← Назад",                    callback_data=f"rp:{order_id}:deliver_cancel")],
            ])
            await cb.message.edit_reply_markup(reply_markup=pay_kb)
            await cb.answer("💰 Выберите способ оплаты")
            return

        if action == "pay_init":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            order_num = order.get("order_num", f"#{order_id}")
            pay_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💵 Наличные",  callback_data=f"rp:{order_id}:pay_only:cash")],
                [InlineKeyboardButton(text="💳 Картой",    callback_data=f"rp:{order_id}:pay_only:card")],
                [InlineKeyboardButton(text="📱 Перевод",   callback_data=f"rp:{order_id}:pay_only:transfer")],
                [InlineKeyboardButton(text="❌ Отмена",    callback_data=f"rp:{order_id}:deliver_cancel")],
            ])
            await cb.message.edit_reply_markup(reply_markup=pay_kb)
            await cb.answer("💰 Выберите способ оплаты")
            return

        if action == "disc_init":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            order_num = order.get("order_num", f"#{order_id}")
            ch_str = str(cb.message.chat.id)
            ch_abs = ch_str[4:] if ch_str.startswith("-100") else (ch_str[1:] if ch_str.startswith("-") else ch_str)
            _pending_discount[cb.from_user.id] = {
                "order_id": order_id, "order_num": order_num,
                "chat_id": cb.message.chat.id, "msg_id": cb.message.message_id,
            }
            cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data=f"disc_cancel:{order_id}")]
            ])
            try:
                await bot.send_message(
                    cb.from_user.id,
                    f"💸 Скидка — {order_num}\n\n"
                    f"Введите сумму скидки (например: 5000)\nОтмена: /cancel_discount",
                    reply_markup=cancel_kb)
                await cb.answer("✉️ Написал в личку!\nОткройте бот и введите сумму скидки.", show_alert=True)
            except Exception:
                _pending_discount.pop(cb.from_user.id, None)
                await cb.answer("❌ Напишите боту /start чтобы открыть личку", show_alert=True)
            return

        if action == "pay_only":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            method = parts[3] if len(parts) > 3 else ""
            if not method:
                await cb.answer("❌ Ошибка")
                return
            _METHOD_LABELS = {"cash": "💵 Наличные", "card": "💳 Картой", "transfer": "📱 Перевод"}
            method_label = _METHOD_LABELS.get(method, method)
            order_num = order.get("order_num", f"#{order_id}")
            debt = await get_order_debt(order_id)
            debt_hint = f"\nДолг: {int(debt):,} сум".replace(",", " ") if debt > 0 else ""
            ch_str = str(cb.message.chat.id)
            ch_abs = ch_str[4:] if ch_str.startswith("-100") else (ch_str[1:] if ch_str.startswith("-") else ch_str)
            ch_url = f"https://t.me/c/{ch_abs}/{cb.message.message_id}"
            _pending_payment[cb.from_user.id] = {"order_id": order_id, "method": method,
                                                   "chat_id": cb.message.chat.id,
                                                   "msg_id":  cb.message.message_id,
                                                   "ch_url":  ch_url}
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "delivery"))
            cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить оплату", callback_data=f"cpay_cancel:{order_id}")]
            ])
            try:
                await bot.send_message(
                    cb.from_user.id,
                    f"💳 Оплата {method_label} — {order_num}{debt_hint}\n\n"
                    f"Введите сумму (например: 315000)\n"
                    f"Отмена: /cancel_payment",
                    reply_markup=cancel_kb)
                await cb.answer("✉️ Написал в личку!\nОткройте бот и введите сумму.", show_alert=True)
            except Exception:
                _pending_payment.pop(cb.from_user.id, None)
                await cb.answer("❌ Напишите боту /start чтобы открыть личку", show_alert=True)
            return

        if action == "pay":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            method = parts[3] if len(parts) > 3 else "none"
            debt = await get_order_debt(order_id)
            order_num = order.get("order_num", f"#{order_id}")
            if method == "none" and debt > 0:
                sent = await _send_debt_approval(order_id, order_num, debt, cb.from_user.id, cb.message)
                if not sent:
                    await update_order_status_by_id(order_id, "delivered", by_tg_id=w.id, by_name=wname,
                                                    note="Маршрут: доставлен клиенту (без оплаты, нет менеджеров)")
                    await set_route_stop_status(order_id, "done")
                    await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "delivered"))
                    await cb.answer("✅ Доставлен (нет менеджеров для подтверждения долга)", show_alert=True)
                else:
                    await cb.answer("⏳ Запрос отправлен менеджеру. Ожидайте решения.", show_alert=True)
                return
            ch_str2 = str(cb.message.chat.id)
            ch_abs2 = ch_str2[4:] if ch_str2.startswith("-100") else (ch_str2[1:] if ch_str2.startswith("-") else ch_str2)
            ch_url2 = f"https://t.me/c/{ch_abs2}/{cb.message.message_id}"
            if method not in ("none", "cash") and debt > 0:
                _pending_receipt[cb.from_user.id] = {
                    "order_id": order_id, "amount": debt, "method": method, "wname": wname,
                    "ch_url": ch_url2, "staff_id": staff["id"],
                }
            elif method == "cash" and debt > 0:
                await add_payment_by_driver(order_id, debt, method, wname,
                                            driver_tg_id=cb.from_user.id, driver_staff_id=staff["id"])
            await update_order_status_by_id(order_id, "delivered", by_tg_id=w.id, by_name=wname,
                                            note="Маршрут: доставлен клиенту")
            await set_route_stop_status(order_id, "done")
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "delivered"))
            if method == "cash" and debt > 0:
                try:
                    await _update_channel_stop(order_id)
                except Exception as e:
                    logging.warning(f"_update_channel_stop cash pay: {e}")
            if method in ("card", "transfer") and debt > 0:
                method_label = "💳 Картой" if method == "card" else "📱 Переводом"
                cancel_kb2 = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отменить оплату", callback_data=f"cpay_cancel:{order_id}")]
                ])
                try:
                    await bot.send_message(
                        cb.from_user.id,
                        f"✅ Доставлен!\n\n"
                        f"📎 Загрузите фото чека {method_label} или нажмите /skip_receipt",
                        reply_markup=cancel_kb2)
                    await cb.answer("✅ Доставлен! Отправьте фото чека в личку", show_alert=True)
                except Exception:
                    await cb.answer("✅ Доставлен! Отправьте фото чека в боте", show_alert=True)
            elif method == "none":
                await cb.answer("✅ Доставлен клиенту!", show_alert=True)
            else:
                await cb.answer("✅ Доставлен! Оплата наличными записана", show_alert=True)
            return

        if action == "undo_delivery":
            if cur != "delivery":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            await update_order_status_by_id(order_id, "ready", by_tg_id=w.id, by_name=wname,
                                            note="Маршрут: не забирал для доставки")
            await set_driver_confirmed(order_id, confirmed=False)
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "ready"))
            await cb.answer("↩️ Не забирал")
            return

        if action == "undo_delivered":
            if cur != "delivered":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, отменить «Доставлен»", callback_data=f"rp:{order_id}:undo_delivered_yes")],
                [InlineKeyboardButton(text="❌ Нет, оставить", callback_data=f"rp:{order_id}:undo_delivered_no")],
            ])
            await cb.message.edit_reply_markup(reply_markup=confirm_kb)
            await cb.answer("Подтвердите отмену")
            return

        if action == "undo_delivered_yes":
            if cur != "delivered":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            await update_order_status_by_id(order_id, "delivery", by_tg_id=w.id, by_name=wname,
                                            note="Маршрут: отменён статус «Доставлен», возврат в Доставку")
            await set_route_stop_status(order_id, "pending")
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "delivery"))
            await cb.answer("↩️ Возврат в Доставку", show_alert=True)
            return

        if action == "undo_delivered_no":
            await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, cur))
            await cb.answer()
            return

        if action == "undo":
            if cur == "confirmed":
                _pickup_carts.pop((w.id, order_id), None)
                await cb.message.edit_reply_markup(reply_markup=_route_pickup_kb(order_id, "confirmed"))
                await cb.answer("Отменено")
                return
            if cur != "pickup":
                await cb.answer(f"ℹ️ {_ROUTE_STATUS_RU.get(cur, cur)}")
                return
            await delete_order_items(order_id)
            await update_order_status_by_id(order_id, "confirmed", by_tg_id=w.id, by_name=wname,
                                            note="Маршрут: не забирал — позиции удалены")
            await set_route_stop_status(order_id, "pending")
            await cb.message.edit_text(orig, reply_markup=_route_pickup_kb(order_id, "confirmed"),
                                       parse_mode="HTML", disable_web_page_preview=True)
            await cb.answer("↩️ Не забирал — позиции удалены")
            return

        if action == "refresh":
            await _update_channel_stop(order_id)
            await cb.answer("🔄 Обновлено")
            return

        await cb.answer()

    except Exception as e:
        logging.warning(f"route_pickup_cb error: {e}", exc_info=True)
        try: await cb.answer("❌ Ошибка сервера", show_alert=True)
        except Exception: pass


# ── КВИТАНЦИИ: фото от водителя в личку ──
@dp.message(F.photo & F.chat.type.in_({"private"}))
async def handle_receipt_photo(msg: Message):
    receipt_info = _pending_receipt.pop(msg.from_user.id, None)
    if not receipt_info:
        return
    file_id = msg.photo[-1].file_id
    order_id = receipt_info["order_id"]
    await add_payment_by_driver(order_id, receipt_info["amount"], receipt_info["method"],
                                receipt_info["wname"], file_id,
                                driver_tg_id=msg.from_user.id,
                                driver_staff_id=receipt_info.get("staff_id"))
    try:
        await _update_channel_stop(order_id)
    except Exception as e:
        logging.warning(f"_update_channel_stop failed for order {order_id}: {e}")
    ch_url = receipt_info.get("ch_url", "")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Перейти в канал", url=ch_url)]]) if ch_url else None
    await msg.reply("✅ Чек и платёж сохранены! Менеджер проверит и подтвердит.", reply_markup=kb)

@dp.message(Command("skip_receipt"))
async def cmd_skip_receipt(msg: Message):
    receipt_info = _pending_receipt.pop(msg.from_user.id, None)
    if receipt_info:
        await add_payment_by_driver(receipt_info["order_id"], receipt_info["amount"],
                                    receipt_info["method"], receipt_info["wname"],
                                    driver_tg_id=msg.from_user.id,
                                    driver_staff_id=receipt_info.get("staff_id"))
        try:
            await _update_channel_stop(receipt_info["order_id"])
        except Exception as e:
            logging.warning(f"_update_channel_stop failed: {e}")
        ch_url = receipt_info.get("ch_url", "")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Перейти в канал", url=ch_url)]]) if ch_url else None
        await msg.reply("✅ Платёж записан без фото чека.", reply_markup=kb)
    else:
        await msg.reply("Нет активного ожидания чека.")

def _fmt_debt_approval_text(details: dict | None, debt: float, order_num: str) -> str:
    def _f(v): return f"{int(v):,}".replace(",", " ")
    debt_str = _f(debt)
    if not details:
        return (f"⚠️ <b>Запрос: закрыть заказ в долг</b>\n"
                f"Заказ: <b>{order_num}</b>\n"
                f"❗ Долг: <b>{debt_str} сум</b>")
    client = f"{details.get('client_first_name','') or ''} {details.get('client_last_name','') or ''}".strip() or "—"
    phone  = details.get('client_phone') or ''
    addr   = details.get('short_address') or details.get('address') or '—'
    loc    = details.get('location') or ''
    items  = details.get('items', [])
    items_total = float(details.get('items_total', 0))
    total  = float(details.get('total_price', 0)) or items_total
    disc   = (float(details.get('discount_sum', 0)) + float(details.get('delivery_discount', 0))
              + float(details.get('manual_discount', 0)))
    paid   = float(details.get('paid_amount', 0))
    lines  = [f"⚠️ <b>Запрос: закрыть заказ в долг</b>", "",
              f"📋 Заказ: <b>{order_num}</b>",
              f"👤 Клиент: <b>{client}</b>"]
    if phone:
        lines.append(f"📞 {phone}")
    # Адрес + карта
    if loc:
        try:
            la, lo = [p.strip() for p in loc.split(",", 1)]
            map_url = f"https://yandex.uz/maps/?pt={lo},{la}&z=16"
            lines.append(f"📍 <a href=\"{map_url}\">{addr}</a>")
        except Exception:
            lines.append(f"📍 {addr}")
    else:
        lines.append(f"📍 {addr}")
    if items:
        lines.append(f"\n🧾 <b>Позиции ({len(items)} шт.):</b>")
        for i, it in enumerate(items[:8], 1):
            svc = it.get('service') or '—'
            w, ln, sqm = it.get('width_cm'), it.get('length_cm'), it.get('sqm')
            lt = float(it.get('line_total', 0))
            dim = f"{int(w)}×{int(ln)} см" if w and ln else ""
            sqm_s = f" · {float(sqm):.1f} м²" if sqm else ""
            lt_s = f" — {_f(lt)} с" if lt else ""
            lines.append(f"  {i}. {svc} {dim}{sqm_s}{lt_s}")
        if len(items) > 8:
            lines.append(f"  ... (+{len(items)-8} ещё)")
    lines.append("")
    if total > 0:
        lines.append(f"💰 Сумма: <b>{_f(total)} с</b>")
    if disc > 0:
        lines.append(f"🏷 Скидка: <b>-{_f(disc)} с</b>")
    if paid > 0:
        lines.append(f"✅ Оплачено: <b>{_f(paid)} с</b>")
    lines.append(f"❗ <b>Долг: {debt_str} сум</b>")
    return "\n".join(lines)


async def _send_debt_approval(order_id: int, order_num: str, debt: float,
                              driver_tg_id: int, msg) -> bool:
    """Send debt approval request to managers. Returns True if at least one manager notified."""
    approvers = await get_debt_approvers_bot()
    if not approvers:
        return False
    details = await get_order_full_for_debt(order_id)
    text = _fmt_debt_approval_text(details, debt, order_num)
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разрешить закрыть в долг", callback_data=f"debt_approve:{order_id}")],
        [InlineKeyboardButton(text="❌ Отказать",                callback_data=f"debt_reject:{order_id}")],
    ])
    mgr_msgs = {}
    for mgr in approvers:
        try:
            m = await bot.send_message(int(mgr["tg_id"]), text, parse_mode="HTML",
                                       reply_markup=approve_kb, disable_web_page_preview=True)
            mgr_msgs[str(mgr["tg_id"])] = m.message_id
        except Exception as e:
            logging.warning(f"debt_approval send to {mgr['tg_id']}: {e}")
    import json as _json
    req = await create_debt_approval_db(order_id, order_num, driver_tg_id, debt, _json.dumps(mgr_msgs))
    # Мгновенный пуш-уведомление в staff.html для всех менеджеров/админов
    try:
        async with aiohttp.ClientSession() as _s:
            await _s.post(f"{API_URL}/debt-approvals/push-managers",
                          json={"order_num": order_num, "debt_amount": debt,
                                "bot_token_check": BOT_TOKEN},
                          timeout=aiohttp.ClientTimeout(total=5))
    except Exception as _e:
        logging.warning(f"debt push-managers failed: {_e}")
    client_name = ""
    client_phone = ""
    short_addr = ""
    if details:
        fn = details.get("client_first_name") or ""
        ln = details.get("client_last_name") or ""
        client_name = f"{fn} {ln}".strip()
        client_phone = details.get("client_phone") or ""
        short_addr = details.get("short_address") or details.get("address") or ""
    _pending_debt_approvals[order_id] = {
        "driver_tg_id": driver_tg_id,
        "order_num": order_num,
        "debt": debt,
        "mgr_msgs": mgr_msgs,
        "client_name": client_name,
        "client_phone": client_phone,
        "short_addr": short_addr,
    }
    try:
        await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 История", callback_data=f"rp:{order_id}:history"),
             InlineKeyboardButton(text="🔄 Обновить", callback_data=f"rp:{order_id}:refresh")],
        ]))
    except Exception: pass
    return True


@dp.callback_query(F.data.startswith("debt_approve:"))
async def cb_debt_approve(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    pending = _pending_debt_approvals.get(order_id)
    if not pending:
        await cb.answer("ℹ️ Заявка уже обработана", show_alert=True)
        return
    # Show staff selection to pick responsible person
    staff_list = await get_debt_approvers_bot()
    if not staff_list:
        # No staff to choose — use approver as responsible
        staff_id = await get_staff_id_by_tg(cb.from_user.id)
        if not staff_id:
            await cb.answer("❌ Вы не найдены как сотрудник", show_alert=True)
            return
        ok = await approve_debt_close(order_id, staff_id, None)
        if not ok:
            await cb.answer("ℹ️ Заказ уже закрыт или статус изменился", show_alert=True)
            return
        mgr_name = f"{cb.from_user.first_name or ''} {cb.from_user.last_name or ''}".strip()
        order_num = pending["order_num"]
        result_text = f"✅ Закрыт в долг · {order_num}\nОдобрил: {mgr_name}"
        _pending_debt_approvals.pop(order_id, None)
        for tg_id_str, msg_id in pending.get("mgr_msgs", {}).items():
            try:
                await bot.edit_message_text(result_text, chat_id=int(tg_id_str),
                                            message_id=msg_id, reply_markup=None)
            except Exception: pass
        try: await _update_channel_stop(order_id)
        except Exception: pass
        await mark_debt_approval_resolved_by_order(order_id, "approved")
        try:
            await bot.send_message(pending["driver_tg_id"],
                                   f"✅ Запрос на закрытие долга по заказу <b>{order_num}</b> одобрён.",
                                   parse_mode="HTML")
        except Exception: pass
        await cb.answer("✅ Одобрено! Заказ закрыт.", show_alert=True)
        return
    # Show list of responsible staff
    pending["approver_tg_id"] = cb.from_user.id
    buttons = [
        [InlineKeyboardButton(
            text=f"{s['first_name']} {s.get('last_name', '')}".strip(),
            callback_data=f"debt_resp:{order_id}:{s['id']}")]
        for s in staff_list
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"debt_reject:{order_id}")])
    select_kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    order_num = pending["order_num"]
    debt_str = f"{int(pending['debt']):,}".replace(",", " ")
    try:
        await cb.message.edit_text(
            f"⚠️ <b>Запрос: закрыть заказ в долг</b>\n"
            f"Заказ: <b>{order_num}</b>\n"
            f"Долг: <b>{debt_str} сум</b>\n\n"
            f"👤 Выберите ответственного за долг:",
            parse_mode="HTML",
            reply_markup=select_kb)
    except Exception: pass
    await cb.answer("Выберите ответственного")


@dp.callback_query(F.data.startswith("debt_resp:"))
async def cb_debt_resp(cb: CallbackQuery):
    parts = cb.data.split(":")
    order_id = int(parts[1])
    resp_staff_id = int(parts[2])
    pending = _pending_debt_approvals.get(order_id)
    ok = await approve_debt_close(order_id, resp_staff_id, None)
    if not ok:
        await cb.answer("ℹ️ Заказ уже закрыт или статус изменился", show_alert=True)
        try: await cb.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        return
    mgr_name = f"{cb.from_user.first_name or ''} {cb.from_user.last_name or ''}".strip()
    order_num = pending["order_num"] if pending else f"#{order_id}"
    # Get responsible name
    resp_name = str(resp_staff_id)
    if pending:
        staff_list = await get_debt_approvers_bot()
        for s in staff_list:
            if s["id"] == resp_staff_id:
                resp_name = f"{s['first_name']} {s.get('last_name', '')}".strip()
                break
    result_text = (f"✅ Закрыт в долг · {order_num}\n"
                   f"Одобрил: {mgr_name}\n"
                   f"Ответственный: {resp_name}")
    if pending:
        _pending_debt_approvals.pop(order_id, None)
        for tg_id_str, msg_id in pending.get("mgr_msgs", {}).items():
            try:
                await bot.edit_message_text(result_text, chat_id=int(tg_id_str),
                                            message_id=msg_id, reply_markup=None)
            except Exception: pass
        order_num_for_notify = pending.get("order_num", f"#{order_id}")
        try:
            await bot.send_message(pending["driver_tg_id"],
                                   f"✅ Запрос на закрытие долга по заказу <b>{order_num_for_notify}</b> одобрён.\n"
                                   f"Заказ закрыт в долг.",
                                   parse_mode="HTML")
        except Exception: pass
    else:
        try: await cb.message.edit_text(result_text, reply_markup=None)
        except Exception: pass
    await mark_debt_approval_resolved_by_order(order_id, "approved")
    try: await _update_channel_stop(order_id)
    except Exception: pass
    drv_tg_approve = pending["driver_tg_id"] if pending else None
    try:
        async with aiohttp.ClientSession() as _s:
            await _s.post(f"{API_URL}/debt-approvals/notify-approved",
                          json={"order_id": order_id, "order_num": order_num_for_notify,
                                "driver_tg_id": drv_tg_approve, "bot_token_check": BOT_TOKEN},
                          timeout=aiohttp.ClientTimeout(total=5))
    except Exception as _e:
        logging.warning(f"notify-approved call failed: {_e}")
    await cb.answer("✅ Одобрено! Заказ закрыт.", show_alert=True)

@dp.callback_query(F.data.startswith("debt_reject:"))
async def cb_debt_reject(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    mgr_name = f"{cb.from_user.first_name or ''} {cb.from_user.last_name or ''}".strip()
    pending = _pending_debt_approvals.pop(order_id, None)
    order_num = pending["order_num"] if pending else f"#{order_id}"
    client_name  = pending.get("client_name", "")  if pending else ""
    client_phone = pending.get("client_phone", "") if pending else ""
    short_addr   = pending.get("short_addr", "")   if pending else ""
    info_line = f"№{order_num}"
    if client_name:  info_line += f" — {client_name}"
    loc_line  = ""
    if short_addr:   loc_line += f"📍 {short_addr}"
    if client_phone: loc_line += f"  📞 {client_phone}"
    result_text = f"❌ Отклонено · #{order_id}\n{info_line}"
    if loc_line:     result_text += f"\n{loc_line}"
    result_text += f"\nОтклонил: {mgr_name}"
    drv_text = f"❌ Отклонено · #{order_id}\n{info_line}"
    if loc_line:     drv_text += f"\n{loc_line}"
    drv_text += f"\n\nНеобходимо принять оплату от клиента."
    if pending:
        for tg_id_str, msg_id in pending.get("mgr_msgs", {}).items():
            try:
                await bot.edit_message_text(result_text, chat_id=int(tg_id_str),
                                            message_id=msg_id, reply_markup=None)
            except Exception: pass
        try:
            await bot.send_message(pending["driver_tg_id"], drv_text, parse_mode="HTML")
        except Exception: pass
    else:
        try: await cb.message.edit_text(result_text, reply_markup=None)
        except Exception: pass
    await mark_debt_approval_resolved_by_order(order_id, "rejected")
    try: await _update_channel_stop(order_id)
    except Exception: pass
    drv_tg = pending["driver_tg_id"] if pending else None
    try:
        async with aiohttp.ClientSession() as _s:
            await _s.post(f"{API_URL}/debt-approvals/notify-rejected",
                          json={"order_id": order_id, "order_num": order_num,
                                "driver_tg_id": drv_tg, "bot_token_check": BOT_TOKEN},
                          timeout=aiohttp.ClientTimeout(total=5))
    except Exception as _e:
        logging.warning(f"notify-rejected call failed: {_e}")
    await cb.answer("Отклонено. Водитель уведомлён.", show_alert=True)

@dp.callback_query(F.data.startswith("disc_cancel:"))
async def cb_disc_cancel(cb: CallbackQuery):
    uid = cb.from_user.id
    info = _pending_discount.pop(uid, None)
    try:
        await cb.message.edit_text("❌ Запрос скидки отменён.")
    except Exception:
        pass
    if info:
        ch_str = str(info["chat_id"])
        ch_abs = ch_str[4:] if ch_str.startswith("-100") else (ch_str[1:] if ch_str.startswith("-") else ch_str)
        ch_url = f"https://t.me/c/{ch_abs}/{info['msg_id']}"
        ch_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Перейти в канал", url=ch_url)]
        ])
        await cb.message.answer("↩️ Возвращайтесь в канал.", reply_markup=ch_kb)
    await cb.answer("Отменено")

@dp.message(Command("cancel_discount"))
async def cmd_cancel_discount(msg: Message):
    info = _pending_discount.pop(msg.from_user.id, None)
    if info:
        ch_str = str(info["chat_id"])
        ch_abs = ch_str[4:] if ch_str.startswith("-100") else (ch_str[1:] if ch_str.startswith("-") else ch_str)
        ch_url = f"https://t.me/c/{ch_abs}/{info['msg_id']}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Перейти в канал", url=ch_url)]
        ])
        await msg.reply("❌ Запрос скидки отменён.", reply_markup=kb)

_pending_disc_approve: dict[int, dict] = {}  # {mgr_tg_id: {req_id, order_id, driver_tg_id, order_num}}

@dp.callback_query(F.data.startswith("disc_approve:"))
async def cb_disc_approve(cb: CallbackQuery):
    parts = cb.data.split(":")
    req_id   = int(parts[1])
    order_id = int(parts[2])
    _pending_disc_approve[cb.from_user.id] = {
        "req_id": req_id, "order_id": order_id,
        "order_num": "", "original_msg_id": cb.message.message_id,
        "original_chat_id": cb.message.chat.id,
    }
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"disc_approve_cancel:{req_id}")]
        ]))
    except Exception:
        pass
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"disc_approve_cancel:{req_id}")]
    ])
    await cb.message.answer(
        "✏️ Введите одобренную сумму скидки (например: 7000):",
        reply_markup=cancel_kb)
    await cb.answer("Введите сумму")

@dp.callback_query(F.data.startswith("disc_approve_cancel:"))
async def cb_disc_approve_cancel(cb: CallbackQuery):
    _pending_disc_approve.pop(cb.from_user.id, None)
    try:
        await cb.message.edit_text("❌ Одобрение отменено.")
    except Exception:
        pass
    await cb.answer("Отменено")

@dp.callback_query(F.data.startswith("disc_reject:"))
async def cb_disc_reject(cb: CallbackQuery):
    parts = cb.data.split(":")
    req_id      = int(parts[1])
    order_id    = int(parts[2])
    driver_tg_id = int(parts[3]) if len(parts) > 3 else 0
    from database import reject_discount_request as _rdr
    staff_id = await get_staff_id_by_tg(cb.from_user.id)
    await _rdr(req_id, staff_id or 0)
    mgr_name = f"{cb.from_user.first_name or ''} {cb.from_user.last_name or ''}".strip()
    try:
        await cb.message.edit_text(f"❌ Скидка отклонена ({mgr_name})", reply_markup=None)
    except Exception:
        pass
    if driver_tg_id:
        try:
            await bot.send_message(driver_tg_id, f"❌ Запрос скидки отклонён менеджером.")
        except Exception:
            pass
    await cb.answer("Отклонено")

@dp.callback_query(F.data.startswith("cpay_cancel:"))
async def cb_cpay_cancel(cb: CallbackQuery):
    uid = cb.from_user.id
    pay  = _pending_payment.pop(uid, None)
    rec  = _pending_receipt.pop(uid, None)
    ch_url = (pay or rec or {}).get("ch_url", "")
    try:
        await cb.message.edit_text("❌ Оплата отменена.")
    except Exception:
        pass
    if ch_url:
        ch_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Перейти в канал", url=ch_url)]
        ])
        await cb.message.answer("↩️ Возвращайтесь в канал.", reply_markup=ch_kb)
    await cb.answer("Отменено")

@dp.message(Command("cancel_payment"))
async def cmd_cancel_payment(msg: Message):
    pay_info = _pending_payment.pop(msg.from_user.id, None)
    if pay_info:
        kb = None
        chat_id = pay_info.get("chat_id")
        m_id    = pay_info.get("msg_id")
        if chat_id and m_id:
            ch_str = str(chat_id)
            if ch_str.startswith("-100"):
                ch_abs = ch_str[4:]
            elif ch_str.startswith("-"):
                ch_abs = ch_str[1:]
            else:
                ch_abs = ch_str
            ch_url = f"https://t.me/c/{ch_abs}/{m_id}"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Перейти в канал", url=ch_url)]
            ])
        await msg.reply("❌ Ввод суммы отменён.", reply_markup=kb)

@dp.message(F.text & F.chat.type.in_({"private"}))
async def handle_payment_amount(msg: Message):
    uid = msg.from_user.id

    # discount input takes priority
    disc_info = _pending_discount.get(uid)
    if disc_info:
        raw = (msg.text or "").strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            amount = float(raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await msg.reply("❌ Введите число больше нуля (например: 5000)")
            return
        _pending_discount.pop(uid)
        order_id   = disc_info["order_id"]
        order_num  = disc_info["order_num"]
        AUTO_THRESHOLD = 1000.0
        ch_str = str(disc_info["chat_id"])
        ch_abs = ch_str[4:] if ch_str.startswith("-100") else (ch_str[1:] if ch_str.startswith("-") else ch_str)
        ch_url = f"https://t.me/c/{ch_abs}/{disc_info['msg_id']}"
        ch_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Перейти в канал", url=ch_url)]
        ])
        if amount < AUTO_THRESHOLD:
            await apply_auto_discount(order_id, amount)
            try:
                await _update_channel_stop(order_id)
            except Exception as e:
                logging.warning(f"disc auto _update_channel_stop: {e}")
            await msg.reply(
                f"✅ Скидка {int(amount):,} сум применена автоматически.".replace(",", " "),
                reply_markup=ch_kb)
        else:
            req = await create_discount_request(order_id, order_num, uid, amount)
            if not req:
                await msg.reply("❌ Ошибка создания запроса. Попробуйте снова.")
                return
            req_id = req["id"]
            amt_str = f"{int(amount):,}".replace(",", " ")
            managers = await get_managers_with_push()
            for mgr in managers:
                try:
                    await bot.send_message(
                        int(mgr["tg_id"]),
                        f"💸 <b>Запрос скидки</b>\n"
                        f"Заказ: {order_num}\n"
                        f"Сумма: {amt_str} сум\n\n"
                        f"Введите одобренную сумму или отклоните:",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"disc_approve:{req_id}:{order_id}"),
                             InlineKeyboardButton(text="❌ Отклонить", callback_data=f"disc_reject:{req_id}:{order_id}:{uid}")]
                        ])
                    )
                except Exception as e:
                    logging.warning(f"disc_request notify mgr {mgr.get('id')}: {e}")
            await msg.reply(
                f"📤 Запрос скидки {amt_str} сум отправлен менеджерам.\nОжидайте ответ.",
                reply_markup=ch_kb)
        return

    # manager approving discount amount
    disc_approve_info = _pending_disc_approve.get(uid)
    if disc_approve_info:
        raw = (msg.text or "").strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            amount = float(raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await msg.reply("❌ Введите число больше нуля (например: 7000)")
            return
        _pending_disc_approve.pop(uid)
        req_id   = disc_approve_info["req_id"]
        order_id = disc_approve_info["order_id"]
        staff_id = await get_staff_id_by_tg(uid)
        result = await resolve_discount_request(req_id, amount, staff_id or 0)
        if not result:
            await msg.reply("❌ Запрос не найден или уже обработан.")
            return
        try:
            await _update_channel_stop(order_id)
        except Exception as e:
            logging.warning(f"disc approve _update_channel_stop: {e}")
        driver_tg_id = result.get("driver_tg_id") or 0
        amt_str = f"{int(amount):,}".replace(",", " ")
        if driver_tg_id:
            try:
                await bot.send_message(driver_tg_id, f"✅ Скидка {amt_str} сум одобрена!")
            except Exception:
                pass
        await msg.reply(f"✅ Скидка {amt_str} сум одобрена и применена.")
        return

    pay_info = _pending_payment.get(uid)
    if not pay_info:
        return
    raw = (msg.text or "").strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await msg.reply("❌ Введите число больше нуля (например: 315000)")
        return
    _pending_payment.pop(uid)
    order_id = pay_info["order_id"]
    method   = pay_info["method"]
    wname    = f"{msg.from_user.first_name or ''} {msg.from_user.last_name or ''}".strip()
    driver_staff_id = await get_staff_id_by_tg(uid)
    _METHOD_LABELS = {"cash": "💵 Наличные", "card": "💳 Картой", "transfer": "📱 Перевод"}
    method_label = _METHOD_LABELS.get(method, method)
    amt_str = f"{int(amount):,}".replace(",", " ")
    if method in ("card", "transfer"):
        ch_url = pay_info.get("ch_url", "")
        _pending_receipt[uid] = {"order_id": order_id, "amount": amount, "method": method,
                                  "wname": wname, "ch_url": ch_url, "staff_id": driver_staff_id}
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить оплату", callback_data=f"cpay_cancel:{order_id}")]
        ])
        await msg.reply(
            f"{method_label} — {amt_str} сум\n\n"
            f"📎 Отправьте фото чека или нажмите /skip_receipt",
            reply_markup=cancel_kb)
    else:
        await add_payment_by_driver(order_id, amount, method, wname,
                                    driver_tg_id=msg.from_user.id, driver_staff_id=driver_staff_id)
        try:
            await _update_channel_stop(order_id)
        except Exception as e:
            logging.warning(f"_update_channel_stop failed for order {order_id}: {e}")
        ch_url = pay_info.get("ch_url", "")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Перейти в канал", url=ch_url)]
        ]) if ch_url else None
        await msg.reply(f"✅ Оплата {method_label} {amt_str} сум записана!", reply_markup=kb)


# ── КОМАНДЫ ──
@dp.message(Command("order"))
async def cmd_order(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if uid not in user_lang:
        await msg.answer("👋", reply_markup=lang_kb()); return
    user_data_db[uid] = {}
    await state.set_state(OrderForm.name)
    await _ask_name_step(msg, uid)

@dp.message(Command("calc"))
async def cmd_calc(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if uid not in user_lang:
        await msg.answer("👋", reply_markup=lang_kb()); return
    user_data_db[uid] = {}
    await state.set_state(CalcForm.service)
    await msg.answer(t(uid,"calc_ask_svc"), reply_markup=service_kb(uid), parse_mode="Markdown")

@dp.message(Command("prices"))
async def cmd_prices(msg: Message):
    uid = msg.from_user.id
    if uid not in user_lang:
        await msg.answer("👋", reply_markup=lang_kb()); return
    await ensure_prices_fresh()
    await msg.answer(build_prices_text(uid), reply_markup=back_kb(uid), parse_mode="Markdown")

@dp.message(Command("branches"))
async def cmd_branches(msg: Message):
    uid = msg.from_user.id
    if uid not in user_lang:
        await msg.answer("👋", reply_markup=lang_kb()); return
    await msg.answer(t(uid,"branches_text"), reply_markup=back_kb(uid), parse_mode="Markdown")

# ── АДМИН: ВОДИТЕЛИ ──
@dp.message(Command("add_driver"))
async def cmd_add_driver(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    args = (msg.text or "").split(maxsplit=2)[1:]
    if len(args) < 2:
        await msg.answer(
            "⚠️ Формат: `/add_driver <tg_id> <Имя> [Фамилия]`\n"
            "Пример: `/add_driver 624826036 Ботир Каримов`",
            parse_mode="Markdown"
        )
        return
    try:
        tg_id = int(args[0])
    except ValueError:
        await msg.answer("⚠️ tg_id должен быть числом.")
        return
    name_parts = args[1].split(maxsplit=1)
    first_name = name_parts[0]
    last_name  = name_parts[1] if len(name_parts) > 1 else ""
    ok = await add_staff(tg_id=tg_id, first_name=first_name, last_name=last_name, role="driver")
    if ok:
        await msg.answer(f"✅ Водитель добавлен: {first_name} {last_name} (id {tg_id})")
    else:
        await msg.answer("⚠️ Не удалось добавить водителя (БД недоступна).")

@dp.message(Command("del_driver"))
async def cmd_del_driver(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    args = (msg.text or "").split()[1:]
    if len(args) != 1:
        await msg.answer("⚠️ Формат: `/del_driver <tg_id>`", parse_mode="Markdown")
        return
    try:
        tg_id = int(args[0])
    except ValueError:
        await msg.answer("⚠️ tg_id должен быть числом.")
        return
    ok = await remove_staff(tg_id)
    if ok:
        await msg.answer(f"✅ Водитель (id {tg_id}) удалён из списка.")
    else:
        await msg.answer("⚠️ Водитель с таким id не найден.")

@dp.message(Command("drivers"))
async def cmd_drivers(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    drivers = await get_staff_by_role("driver")
    if not drivers:
        await msg.answer(
            "📋 Список водителей пуст.\n\n"
            "Добавить: `/add_driver <tg_id> <Имя> [Фамилия]`",
            parse_mode="Markdown"
        )
        return
    lines = ["🚗 *Водители:*", ""]
    for d in drivers:
        uname = f" @{d['tg_username']}" if d["tg_username"] else ""
        lines.append(f"• {d['first_name']} {d['last_name'] or ''} (id `{d['tg_id']}`){uname}".replace("  ", " "))
    lines.append("")
    lines.append("Удалить: `/del_driver <tg_id>`")
    await msg.answer("\n".join(lines), parse_mode="Markdown")


# ── ПОДТВЕРЖДЕНИЕ ПЕРЕДАЧИ НАЛИЧНЫХ ──
@dp.callback_query(F.data.startswith("cash_confirm:"))
async def cash_confirm_cb(cb: CallbackQuery):
    try:
        parts = cb.data.split(":")
        if len(parts) < 2:
            await cb.answer("❌ Некорректные данные", show_alert=True)
            return
        handover_id = int(parts[1])
        staff_id = await get_staff_id_by_tg(cb.from_user.id)
        if not staff_id:
            await cb.answer("❌ Ваш Telegram не привязан к аккаунту сотрудника", show_alert=True)
            return
        row = await confirm_cash_handover_bot(handover_id, staff_id)
        if not row:
            await cb.answer("ℹ️ Уже подтверждено или не найдено", show_alert=True)
            return
        amount = int(float(row.get("amount", 0)))
        confirmer_name = row.get("confirmer_name") or \
            f"{cb.from_user.first_name or ''} {cb.from_user.last_name or ''}".strip() or "сотрудник"
        sender_name = row.get("sender_name", "")
        from_line = f"От: <b>{sender_name}</b>\n" if sender_name else ""
        await cb.message.edit_text(
            f"💵 <b>Вам сдают наличные</b>\n"
            f"{from_line}"
            f"Сумма: <b>{amount:,} сум</b>\n\n"
            f"✅ Подтверждено: <b>{confirmer_name}</b>",
            parse_mode="HTML",
            reply_markup=None,
        )
        await cb.answer("✅ Получение подтверждено!")
    except Exception as e:
        logging.warning(f"cash_confirm_cb error: {e}")
        try:
            await cb.answer("❌ Ошибка при подтверждении", show_alert=True)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("cash_reject:"))
async def cash_reject_cb(cb: CallbackQuery):
    try:
        parts = cb.data.split(":")
        if len(parts) < 2:
            await cb.answer("❌ Некорректные данные", show_alert=True)
            return
        handover_id = int(parts[1])
        staff_id = await get_staff_id_by_tg(cb.from_user.id)
        if not staff_id:
            await cb.answer("❌ Ваш Telegram не привязан к аккаунту сотрудника", show_alert=True)
            return
        row = await reject_cash_handover_bot(handover_id, staff_id)
        if not row:
            await cb.answer("ℹ️ Уже обработано", show_alert=True)
            return
        amount = int(float(row.get("amount", 0)))
        rejector_name = row.get("rejector_name") or \
            f"{cb.from_user.first_name or ''} {cb.from_user.last_name or ''}".strip() or "сотрудник"
        sender_name = row.get("sender_name", "")
        from_line = f"От: <b>{sender_name}</b>\n" if sender_name else ""
        await cb.message.edit_text(
            f"💵 <b>Вам сдают наличные</b>\n"
            f"{from_line}"
            f"Сумма: <b>{amount:,} сум</b>\n\n"
            f"❌ Отклонено: <b>{rejector_name}</b>",
            parse_mode="HTML",
            reply_markup=None,
        )
        await cb.answer("❌ Отклонено")
    except Exception as e:
        logging.warning(f"cash_reject_cb error: {e}")
        try:
            await cb.answer("❌ Ошибка", show_alert=True)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("safe_confirm_"))
async def safe_confirm_cb(cb: CallbackQuery):
    try:
        handover_id = int(cb.data.split("_")[-1])
        staff_id = await get_staff_id_by_tg(cb.from_user.id)
        if not staff_id:
            await cb.answer("❌ Telegram не привязан к аккаунту", show_alert=True)
            return
        row = await confirm_cash_handover_bot(handover_id, staff_id)
        if not row:
            await cb.answer("ℹ️ Уже обработано", show_alert=True)
            return
        amount = int(float(row.get("amount", 0)))
        await cb.message.edit_text(
            f"🔒 <b>Сдача в сейф</b>\n"
            f"Сумма: <b>{amount:,} сум</b>\n\n"
            f"✅ Подтверждено",
            parse_mode="HTML", reply_markup=None,
        )
        await cb.answer("✅ Подтверждено")
    except Exception as e:
        logging.warning(f"safe_confirm_cb error: {e}")
        try: await cb.answer("❌ Ошибка", show_alert=True)
        except Exception: pass


@dp.callback_query(F.data.startswith("safe_reject_"))
async def safe_reject_cb(cb: CallbackQuery):
    try:
        handover_id = int(cb.data.split("_")[-1])
        staff_id = await get_staff_id_by_tg(cb.from_user.id)
        if not staff_id:
            await cb.answer("❌ Telegram не привязан к аккаунту", show_alert=True)
            return
        row = await reject_cash_handover_bot(handover_id, staff_id)
        if not row:
            await cb.answer("ℹ️ Уже обработано", show_alert=True)
            return
        amount = int(float(row.get("amount", 0)))
        await cb.message.edit_text(
            f"🔒 <b>Сдача в сейф</b>\n"
            f"Сумма: <b>{amount:,} сум</b>\n\n"
            f"❌ Отклонено",
            parse_mode="HTML", reply_markup=None,
        )
        await cb.answer("❌ Отклонено")
    except Exception as e:
        logging.warning(f"safe_reject_cb error: {e}")
        try: await cb.answer("❌ Ошибка", show_alert=True)
        except Exception: pass


# ── ЗАПУСК ──
async def main():
    logging.info(f"🚀 Bot starting (COMPANY_ID={COMPANY_ID})...")
    await init_db()
    await load_branches()      # загружаем филиалы до текстов
    await load_prices()
    await load_units()
    await load_services()
    await load_site_settings() # _rebuild_dynamic_texts() вызывается внутри
    # Удаляем webhook если был установлен (artez_api мог его поставить)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("✅ Webhook deleted, switching to polling")
    except Exception as e:
        logging.warning(f"delete_webhook error: {e}")
    logging.info("✅ Bot started, polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
