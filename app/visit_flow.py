from __future__ import annotations

import asyncio
import logging
import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from publisher import (
    build_channel_post_url,
    build_publish_text,
    get_destination_channel,
    get_property_for_publish,
)

from phone_utils import normalize_iran_phone

from visit_ops_group import (
    get_visit_ops_target,
    telegram_thread_kwargs,
)

from visit_leads import (
    clear_pending_action,
    create_or_get_visit_request,
    get_customer_by_telegram_id,
    get_pending_visit,
    get_primary_property_source,
    get_operator_source_context,
    get_visit_operator_telegram_ids,
    get_visit_request_detail,
    get_unnotified_visit_request_ids,
    get_visitable_property,
    mark_visit_notified,
    save_customer_phone,
    set_pending_visit,
    upsert_customer_profile,
    visit_request_is_notified,
)


def _phone_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 ارسال شماره تماس من",
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=(
            "برای ثبت درخواست، شماره تماس را ارسال کنید"
        ),
    )


def _visit_property_id_from_start(context):
    args = list(
        context.args or []
    )

    if len(args) != 1:
        return None

    value = str(
        args[0]
    ).strip()

    if not value.startswith(
        "visit_"
    ):
        return None

    raw_id = value.replace(
        "visit_",
        "",
        1,
    )

    if not raw_id.isdigit():
        return None

    property_id = int(
        raw_id
    )

    if property_id <= 0:
        return None

    return property_id


def _customer_name(detail):
    first_name = detail[8] or ""
    last_name = detail[9] or ""

    name = (
        f"{first_name} {last_name}"
        .strip()
    )

    return name or "بدون نام"


def _format_price(detail):
    price_billion = detail[16]
    price_toman = detail[17]

    if price_billion is not None:
        return (
            f"{price_billion} میلیارد تومان"
        )

    if price_toman is not None:
        return (
            f"{int(price_toman):,} تومان"
        )

    return "ثبت نشده"


CHANNEL_POST_BUTTON_TEXT = "📢 مشاهده فایل در کانال ما"


def _our_channel_post_url(property_data, destination):
    """Link to the listing's post in our own channel, if it is still live."""
    if not property_data or not property_data[10]:
        return None

    return (
        build_channel_post_url(
            destination[2] if destination else None,
            property_data[10],
        )
        or property_data[9]
    )


def _build_operator_lead_text(detail, channel_post_url=None):
    request_id = detail[0]
    property_id = detail[2]
    unit_code = detail[3] or "-"

    telegram_user_id = detail[6]
    telegram_username = detail[7]
    phone_number = normalize_iran_phone(
        detail[10]
    )

    address = (
        detail[11]
        or detail[12]
        or "-"
    )

    area = detail[13]
    bedrooms = detail[14]
    floor_text = detail[15]

    username_text = (
        f"@{telegram_username}"
        if telegram_username
        else "ندارد"
    )

    source = (
        get_operator_source_context(
            property_id
        )
    )

    lines = [
        "🔔 درخواست جدید بازدید",
        "",
        f"🆔 شماره درخواست: {request_id}",
        f"🔢 کد واحد: {unit_code}",
    ]

    if channel_post_url:
        lines.append(
            f"📢 فایل در کانال ما: {channel_post_url}"
        )

    lines.append(
        f"📍 موقعیت: {address}"
    )

    if area is not None:
        lines.append(
            f"📐 متراژ: {area} متر"
        )

    if bedrooms is not None:
        lines.append(
            f"🛏 خواب: {bedrooms}"
        )

    if floor_text:
        lines.append(
            f"🏢 طبقه: {floor_text}"
        )

    lines.extend(
        [
            f"💰 قیمت: {_format_price(detail)}",
            "",
            "👤 اطلاعات مشتری",
            f"نام: {_customer_name(detail)}",
            (
                "📱 شماره تماس: "
                f"{phone_number or '-'}"
            ),
            f"💬 تلگرام: {username_text}",
            (
                "Telegram ID: "
                f"{telegram_user_id}"
            ),
            "",
            "📌 منبع اصلی فایل",
        ]
    )

    if source:
        lines.append(
            "🏢 منبع: "
            + str(
                source.get(
                    "source_name"
                )
                or "ثبت نشده"
            )
        )

        source_code = (
            source.get(
                "source_unit_code"
            )
        )

        lines.append(
            "🔢 کد فایل منبع: "
            + (
                str(source_code)
                if source_code
                else "ثبت نشده"
            )
        )

        direct_url = source.get(
            "direct_post_url"
        )

        source_is_active = source.get(
            "source_is_active"
        )

        if (
            direct_url
            and source_is_active
        ):
            lines.extend(
                [
                    "🔗 پست اصلی منبع:",
                    direct_url,
                ]
            )

        else:
            lines.append(
                "🔗 پست اصلی منبع: "
                "از دکمه زیر بررسی کنید"
            )

    else:
        lines.append(
            "منبع: ثبت نشده"
        )

    return "\n".join(lines)

def _build_operator_source_keyboard(
    property_id: int,
    channel_post_url=None,
):
    source = get_operator_source_context(
        property_id
    )

    direct_url = (
        source.get("direct_post_url")
        if source
        else None
    )

    source_is_active = (
        bool(source.get("source_is_active"))
        if source
        else False
    )

    if (
        source_is_active
        and direct_url
    ):
        button = InlineKeyboardButton(
            "🔗 مشاهده پست اصلی منبع",
            url=direct_url,
        )

    else:
        button = InlineKeyboardButton(
            "🔗 مشاهده پست اصلی منبع",
            callback_data=(
                f"visitsrc_{int(property_id)}"
            ),
        )

    rows = [[button]]

    if channel_post_url:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    CHANNEL_POST_BUTTON_TEXT,
                    url=channel_post_url,
                )
            ],
        )

    return InlineKeyboardMarkup(rows)


async def handle_visit_source_callback(
    update,
    context,
):
    query = update.callback_query

    if not query:
        return

    action = query.data or ""

    if not action.startswith(
        "visitsrc_"
    ):
        return

    try:
        property_id = int(
            action.replace(
                "visitsrc_",
                "",
                1,
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        await query.answer(
            "لینک منبع نامعتبر است.",
            show_alert=True,
        )
        return

    source = get_operator_source_context(
        property_id
    )

    direct_url = (
        source.get("direct_post_url")
        if source
        else None
    )

    source_is_active = (
        bool(source.get("source_is_active"))
        if source
        else False
    )

    if (
        source_is_active
        and direct_url
    ):
        rows = [
            [
                InlineKeyboardButton(
                    "🔗 مشاهده پست اصلی منبع",
                    url=direct_url,
                )
            ]
        ]

        channel_post_url = _our_channel_post_url(
            get_visitable_property(property_id),
            get_destination_channel(),
        )

        if channel_post_url:
            rows.insert(
                0,
                [
                    InlineKeyboardButton(
                        CHANNEL_POST_BUTTON_TEXT,
                        url=channel_post_url,
                    )
                ],
            )

        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(rows)
            )

            await query.answer(
                "لینک پست منبع بازیابی شد؛ "
                "دوباره دکمه را بزنید."
            )

        except Exception:
            logging.exception(
                "Could not restore visit source button: "
                "property_id=%s",
                property_id,
            )

            await query.answer(
                "لینک پست منبع بازیابی شد؛ "
                "اعلان را دوباره باز کنید.",
                show_alert=True,
            )

        return

    await query.answer(
        "این واحد از کانال منبع حذف شده.",
        show_alert=True,
    )


async def _notify_visit_operators(
    bot,
    request_id: int,
):
    detail = get_visit_request_detail(
        request_id
    )

    if not detail:
        logging.error(
            "Visit request detail not found: request_id=%s",
            request_id,
        )
        return 0

    property_id = detail[2]

    operator_ids = (
        get_visit_operator_telegram_ids()
    )
    visit_group = get_visit_ops_target()

    if visit_group:
        delivery_targets = [
            (
                int(visit_group[0]),
                telegram_thread_kwargs(visit_group),
            )
        ]
    else:
        delivery_targets = [
            (int(operator_id), {})
            for operator_id in operator_ids
        ]

    if not delivery_targets:
        logging.warning(
            "No visit destination available: request_id=%s",
            request_id,
        )
        return 0

    destination = (
        get_destination_channel()
    )

    destination_chat_id = (
        destination[0]
        if destination
        else None
    )

    property_row = (
        get_property_for_publish(
            property_id
        )
        if property_id
        else None
    )

    published_message_id = None
    property_data = None

    if property_id:
        property_data = (
            get_visitable_property(
                property_id
            )
        )

        if property_data:
            published_message_id = (
                property_data[10]
            )

    channel_post_url = _our_channel_post_url(
        property_data,
        destination,
    )

    lead_text = (
        _build_operator_lead_text(
            detail,
            channel_post_url,
        )
    )

    successful_operators = 0

    for operator_chat_id, thread_kwargs in delivery_targets:
        try:
            await bot.send_message(
                chat_id=operator_chat_id,
                text=lead_text,
                disable_web_page_preview=True,
                reply_markup=(
                    _build_operator_source_keyboard(
                        property_id,
                        channel_post_url,
                    )
                    if property_id
                    else None
                ),
                **thread_kwargs,
            )

            file_sent = False

            if (
                destination_chat_id
                and published_message_id
            ):
                try:
                    await bot.copy_message(
                        chat_id=operator_chat_id,
                        from_chat_id=destination_chat_id,
                        message_id=published_message_id,
                        **thread_kwargs,
                    )

                    file_sent = True

                except Exception:
                    logging.exception(
                        "Could not copy original property post "
                        "to operator: request_id=%s "
                        "operator=%s",
                        request_id,
                        operator_chat_id,
                    )

            if (
                not file_sent
                and property_row
            ):
                try:
                    full_property_text = (
                        build_publish_text(
                            property_row
                        )
                    )

                    await bot.send_message(
                        chat_id=operator_chat_id,
                        text=full_property_text,
                        disable_web_page_preview=True,
                        **thread_kwargs,
                    )

                except Exception:
                    logging.exception(
                        "Could not send fallback property text "
                        "to operator: request_id=%s "
                        "operator=%s",
                        request_id,
                        operator_chat_id,
                    )

            successful_operators += 1

        except Exception:
            logging.exception(
                "Visit operator notification failed: "
                "request_id=%s operator=%s",
                request_id,
                operator_chat_id,
            )

    if successful_operators > 0:
        mark_visit_notified(
            request_id
        )

    return successful_operators


async def run_pending_visit_notifications(
    application,
):
    request_ids = get_unnotified_visit_request_ids(
        limit=50
    )

    for request_id in request_ids:
        if visit_request_is_notified(request_id):
            continue

        delivered = await _notify_visit_operators(
            application.bot,
            request_id,
        )

        logging.info(
            "Visit notification retry processed: "
            "request_id=%s operators=%s",
            request_id,
            delivered,
        )


async def _visit_notification_retry_loop(
    application,
):
    logging.info(
        "Visit request notification retry worker started."
    )

    while True:
        try:
            await run_pending_visit_notifications(
                application
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception(
                "Visit notification retry worker error"
            )

        await asyncio.sleep(30)


def start_visit_notification_worker(
    application,
):
    existing = application.bot_data.get(
        "visit_notification_retry_task"
    )

    if existing and not existing.done():
        return existing

    task = asyncio.create_task(
        _visit_notification_retry_loop(
            application
        ),
        name="visit_notification_retry",
    )

    application.bot_data[
        "visit_notification_retry_task"
    ] = task

    return task


async def _complete_visit_request(
    update,
    context,
    *,
    customer_id: int,
    property_id: int,
):
    visitable = get_visitable_property(
        property_id
    )

    if not visitable:
        clear_pending_action(
            customer_id
        )

        await update.effective_message.reply_text(
            "این فایل در حال حاضر برای درخواست بازدید فعال نیست.",
            reply_markup=ReplyKeyboardRemove(),
        )

        return

    request_id, created = (
        create_or_get_visit_request(
            customer_id=customer_id,
            property_id=property_id,
        )
    )

    clear_pending_action(
        customer_id
    )

    should_notify = (
        created
        or not visit_request_is_notified(
            request_id
        )
    )

    if should_notify:
        operator_count = (
            await _notify_visit_operators(
                context.bot,
                request_id,
            )
        )

        logging.info(
            "Visit lead processed: request_id=%s "
            "created=%s operators=%s",
            request_id,
            created,
            operator_count,
        )

    if created:
        text = (
            "✅ درخواست بازدید شما ثبت شد.\n\n"
            "همکاران ما برای هماهنگی با شما تماس می‌گیرند."
        )
    else:
        text = (
            "✅ درخواست بازدید این واحد قبلاً ثبت شده.\n\n"
            "همکاران ما برای هماهنگی با شما تماس می‌گیرند."
        )

    await update.effective_message.reply_text(
        text,
        reply_markup=ReplyKeyboardRemove(),
    )

    # Ask this qualification question only on the customer's first visit.
    if update.effective_user:
        from shahfile_smart import ask_finance_once
        await ask_finance_once(
            context.bot,
            update.effective_user.id,
            customer_id,
        )


async def handle_visit_start(
    update,
    context,
):
    property_id = (
        _visit_property_id_from_start(
            context
        )
    )

    if property_id is None:
        return False

    if (
        not update.effective_user
        or not update.effective_message
    ):
        return True

    return await start_visit_request(
        update,
        context,
        property_id,
    )


async def start_visit_request(
    update,
    context,
    property_id,
):
    """File a visit request for property_id, asking for the phone when it is missing.

    The channel post's visit link and the bot's own visit offer («آره،
    بازدید می‌خوام») both land here.
    """
    visitable = get_visitable_property(
        property_id
    )

    if not visitable:
        await update.effective_message.reply_text(
            "این فایل در حال حاضر برای درخواست بازدید فعال نیست."
        )
        return True

    user = update.effective_user

    customer = upsert_customer_profile(
        telegram_user_id=user.id,
        telegram_username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    customer_id = customer[0]
    phone_number = customer[5]

    set_pending_visit(
        customer_id=customer_id,
        property_id=property_id,
    )

    if phone_number:
        await _complete_visit_request(
            update,
            context,
            customer_id=customer_id,
            property_id=property_id,
        )

        return True

    await update.effective_message.reply_text(
        "لطفاً برای ثبت درخواست بازدید، شماره تماس خودتون رو "
        "از طریق دکمه زیر ارسال کنید یا شماره موبایل‌تون رو بنویسید.\n"
        "مثال: 09121234567",
        reply_markup=_phone_keyboard(),
    )

    return True


_MOBILE_IN_TEXT = re.compile(r"(?<!\d)(?:\+?98|0098|0)?9\d{9}(?!\d)")


def typed_mobile_number(text):
    """The one mobile number a short message carries (09xxxxxxxxx), or None."""
    raw = str(text or "").translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )
    if len(raw) > 60:
        return None
    # «0914 413 9040» and «0914-413-9040» are one number.
    raw = re.sub(r"(?<=\d)[ \t\-]+(?=\d)", "", raw)
    numbers = _MOBILE_IN_TEXT.findall(raw)
    if len(numbers) != 1:
        return None
    phone = normalize_iran_phone(numbers[0])
    return phone if re.fullmatch(r"09\d{9}", phone) else None


async def handle_visit_phone_text(
    update,
    context,
):
    """A typed mobile number completes a waiting visit request.

    Many customers type their number instead of pressing «ارسال شماره تماس
    من»; without this the number went to an advisor as a question and the
    visit request was never sent.
    """
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return False

    phone = typed_mobile_number(
        message.text
    )

    if not phone:
        return False

    customer = get_customer_by_telegram_id(
        user.id
    )

    if not customer:
        return False

    pending = get_pending_visit(
        customer[0]
    )

    if not pending:
        return False

    save_customer_phone(
        telegram_user_id=user.id,
        phone_number=phone,
    )

    await _complete_visit_request(
        update,
        context,
        customer_id=customer[0],
        property_id=pending[2],
    )

    return True


async def handle_visit_contact(
    update,
    context,
):
    if (
        not update.effective_user
        or not update.effective_message
        or not update.effective_message.contact
    ):
        return

    user = update.effective_user
    contact = (
        update.effective_message.contact
    )

    customer = (
        get_customer_by_telegram_id(
            user.id
        )
    )

    if not customer:
        await update.effective_message.reply_text(
            "درخواست فعالی برای ثبت شماره ندارید.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    pending = get_pending_visit(
        customer[0]
    )

    if not pending:
        await update.effective_message.reply_text(
            "درخواست فعالی برای ثبت شماره ندارید.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if (
        contact.user_id is None
        or contact.user_id != user.id
    ):
        await update.effective_message.reply_text(
            "لطفاً شماره خودتون رو فقط از دکمه "
            "«ارسال شماره تماس من» ارسال کنید.",
            reply_markup=_phone_keyboard(),
        )
        return

    save_customer_phone(
        telegram_user_id=user.id,
        phone_number=contact.phone_number,
    )

    property_id = pending[2]

    await _complete_visit_request(
        update,
        context,
        customer_id=customer[0],
        property_id=property_id,
    )
