from __future__ import annotations

import asyncio
import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from publisher import (
    build_publish_text,
    get_destination_channel,
    get_property_for_publish,
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


def _build_operator_lead_text(detail):
    request_id = detail[0]
    property_id = detail[2]
    unit_code = detail[3] or "-"

    telegram_user_id = detail[6]
    telegram_username = detail[7]
    phone_number = detail[10]

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
        f"📍 موقعیت: {address}",
    ]

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

    return InlineKeyboardMarkup(
        [[button]]
    )


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
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔗 مشاهده پست اصلی منبع",
                                url=direct_url,
                            )
                        ]
                    ]
                )
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

    if not operator_ids:
        logging.warning(
            "No visit operators available: request_id=%s",
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

    lead_text = (
        _build_operator_lead_text(
            detail
        )
    )

    successful_operators = 0

    for operator_chat_id in operator_ids:
        try:
            await bot.send_message(
                chat_id=operator_chat_id,
                text=lead_text,
                disable_web_page_preview=True,
                reply_markup=(
                    _build_operator_source_keyboard(
                        property_id
                    )
                    if property_id
                    else None
                ),
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
        "از طریق دکمه زیر ارسال کنید.",
        reply_markup=_phone_keyboard(),
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
