import hmac
import json
import os
import re
import secrets
import time
from datetime import date, datetime, time as wall_time, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)

# Все слоты показываются по местному времени Барсука.
BUSINESS_TIME_ZONE = ZoneInfo("Europe/Moscow")
BUSINESS_START_HOUR = 10
BUSINESS_END_HOUR = 18
MEETING_MINUTES = 60
SLOT_STEP_MINUTES = 30
BOOKING_DAYS_AHEAD = 30
ACTIVE_STATUSES = ("pending", "confirmed")

# Постоянный ключ подписи сохраняет сессию владельца после перезапуска.
secret_key = os.environ.get("FLASK_SECRET_KEY")
if not secret_key:
    if os.environ.get("RENDER") == "true":
        raise RuntimeError("Set FLASK_SECRET_KEY in the Render environment.")
    secret_key = secrets.token_urlsafe(32)
    app.logger.warning("FLASK_SECRET_KEY is temporary; local sessions reset on restart.")
if len(secret_key) < 32:
    raise RuntimeError("FLASK_SECRET_KEY must contain at least 32 characters.")
app.secret_key = secret_key

# Эти параметры cookie защищают сессию владельца на опубликованном сайте.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true"
    or os.environ.get("RENDER") == "true",
)

# Ограничиваем число неудачных входов: пять попыток с одного IP за 15 минут.
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_SECONDS = 15 * 60
login_failures = {}


class BookingServiceError(Exception):
    """Ошибка возникает, когда сервер не может обратиться к закрытому API записей."""


class SlotUnavailableError(BookingServiceError):
    """Запрошенное время уже занято другой заявкой."""


class EmailDeliveryError(Exception):
    """Почтовый сервис не принял уведомление."""


def csrf_token():
    # Токен сессии мешает постороннему сайту отправить форму от имени посетителя.
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    # Передаём один и тот же токен во все HTML-формы.
    return {"csrf_token": csrf_token}


def require_csrf_token():
    # Любая отправка формы должна содержать токен, выданный этой страницей.
    submitted_token = request.form.get("_csrf_token", "")
    submitted_bytes = submitted_token.encode("utf-8")
    expected_bytes = csrf_token().encode("utf-8")
    if not hmac.compare_digest(submitted_bytes, expected_bytes):
        abort(400, description="Форма устарела. Обновите страницу и попробуйте ещё раз.")


def supabase_request(method, path, payload=None):
    # Секретный ключ базы хранится на сервере Flask и не попадает в браузер.
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url.startswith("https://") or not service_key:
        raise BookingServiceError("Supabase server credentials are not configured.")

    url = f"{base_url}/rest/v1/{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    api_request = Request(
        url,
        data=body,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation",
        },
        method=method,
    )

    try:
        # Тайм-аут не даёт зависшему запросу надолго занять серверный процесс.
        with urlopen(api_request, timeout=10) as response:
            response_body = response.read()
    except HTTPError as error:
        if error.code == 409:
            raise SlotUnavailableError("The requested appointment time is already reserved.") from error
        # Записываем только код ошибки: ответ базы может содержать личные данные.
        app.logger.error("Supabase request failed with HTTP status %s.", error.code)
        raise BookingServiceError("Supabase rejected the request.") from error
    except URLError as error:
        # Сообщаем о проблеме связи, не записывая пароль или данные клиента в журнал.
        app.logger.error("Could not reach Supabase: %s", error.reason)
        raise BookingServiceError("Supabase is unreachable.") from error
    except TimeoutError as error:
        app.logger.error("Supabase request timed out.")
        raise BookingServiceError("Supabase request timed out.") from error

    if not response_body:
        return []
    try:
        return json.loads(response_body)
    except ValueError as error:
        app.logger.error("Supabase returned an invalid JSON response.")
        raise BookingServiceError("Supabase returned an invalid response.") from error


def send_email(recipient, subject, text):
    # В тестовом режиме письма уходят только на специальный адрес Resend.
    test_mode_setting = os.environ.get("EMAIL_TEST_MODE", "true").strip().lower()
    if test_mode_setting not in ("true", "false"):
        raise EmailDeliveryError("EMAIL_TEST_MODE must be true or false.")
    test_mode = test_mode_setting == "true"
    api_key = os.environ.get("RESEND_API_KEY", "")
    sender = os.environ.get("EMAIL_FROM", "onboarding@resend.dev" if test_mode else "")
    actual_recipient = "delivered@resend.dev" if test_mode else recipient
    if not api_key or not sender or not actual_recipient:
        raise EmailDeliveryError("Resend email settings are incomplete.")

    # Отправляем только текст письма, не вставляя введённые данные в HTML.
    body = json.dumps(
        {
            "from": sender,
            "to": [actual_recipient],
            "subject": f"[TEST] {subject}" if test_mode else subject,
            "text": text,
        }
    ).encode("utf-8")
    email_request = Request(
        "https://api.resend.com/emails",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(email_request, timeout=10) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        # Не записываем в лог содержимое письма или почтовый ключ.
        app.logger.error("Resend rejected an email with HTTP status %s.", error.code)
        raise EmailDeliveryError("Resend rejected the email.") from error
    except URLError as error:
        app.logger.error("Could not reach Resend: %s", error.reason)
        raise EmailDeliveryError("Resend is unreachable.") from error
    except TimeoutError as error:
        app.logger.error("Resend request timed out.")
        raise EmailDeliveryError("Resend request timed out.") from error
    except ValueError as error:
        app.logger.error("Resend returned an invalid JSON response.")
        raise EmailDeliveryError("Resend returned an invalid response.") from error
    if not result.get("id"):
        raise EmailDeliveryError("Resend returned no email ID.")
    return result["id"]


def supabase_query(path, params):
    # Кодируем фильтры API, чтобы даты и скобки не ломали адрес запроса.
    return f"{path}?{urlencode(params, quote_via=quote, safe='(),')}"


def local_date_window(day):
    # Начало и конец рабочего дня переводим в UTC для точного поиска пересечений.
    start = datetime.combine(day, wall_time(BUSINESS_START_HOUR), BUSINESS_TIME_ZONE)
    end = datetime.combine(day, wall_time(BUSINESS_END_HOUR), BUSINESS_TIME_ZONE)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def available_slots(day, now=None):
    # Строим расписание с шагом 30 минут и убираем пересечения с активными заявками.
    local_now = now or datetime.now(BUSINESS_TIME_ZONE)
    today = local_now.date()
    if day < today or day > today + timedelta(days=BOOKING_DAYS_AHEAD) or day.weekday() >= 5:
        return []

    window_start, window_end = local_date_window(day)
    query = supabase_query(
        "appointments",
        {
            "select": "starts_at,ends_at",
            "status": "in.(pending,confirmed)",
            "starts_at": f"lt.{window_end.isoformat()}",
            "ends_at": f"gt.{window_start.isoformat()}",
        },
    )
    busy_rows = supabase_request("GET", query)
    busy_ranges = [
        (
            datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")),
        )
        for row in busy_rows
    ]

    slots = []
    first_minute = BUSINESS_START_HOUR * 60
    last_minute = BUSINESS_END_HOUR * 60 - MEETING_MINUTES
    for minute_of_day in range(first_minute, last_minute + 1, SLOT_STEP_MINUTES):
        start_local = datetime.combine(
            day,
            wall_time(minute_of_day // 60, minute_of_day % 60),
            BUSINESS_TIME_ZONE,
        )
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = start_utc + timedelta(minutes=MEETING_MINUTES)
        if start_local <= local_now:
            continue
        if any(start_utc < busy_end and end_utc > busy_start for busy_start, busy_end in busy_ranges):
            continue
        slots.append(
            {
                "value": start_utc.isoformat(),
                "label": start_local.strftime("%H:%M"),
            }
        )
    return slots


def update_email_delivery_status(appointment_id, field, status):
    # Записываем исход уведомления, чтобы владелец видел сбой доставки.
    updated = supabase_request(
        "PATCH",
        supabase_query("appointments", {"id": f"eq.{appointment_id}"}),
        {field: status},
    )
    if not updated:
        raise BookingServiceError("Appointment email status was not updated.")


def send_and_record_email(appointment_id, field, recipient, subject, text):
    # Письмо и его статус обрабатываются отдельно от сохранения самой записи.
    try:
        send_email(recipient, subject, text)
    except EmailDeliveryError:
        app.logger.exception("Appointment notification could not be sent.")
        try:
            update_email_delivery_status(appointment_id, field, "failed")
        except BookingServiceError:
            app.logger.exception("Could not save failed email status.")
        return False

    try:
        update_email_delivery_status(appointment_id, field, "sent")
    except BookingServiceError:
        app.logger.exception("Email was accepted, but its delivery status was not saved.")
        return False
    return True


def appointment_email_text(appointment, event):
    # Создаём простое текстовое письмо для клиента без HTML-шаблонов.
    start = datetime.fromisoformat(appointment["starts_at"].replace("Z", "+00:00"))
    local_start = start.astimezone(BUSINESS_TIME_ZONE)
    local_end = local_start + timedelta(minutes=MEETING_MINUTES)
    event_copy = {
        "request": "Мы получили твою заявку. Барсук подтвердит встречу отдельно.",
        "confirmed": "Барсук подтвердил встречу.",
        "cancelled": "Встреча отменена. Выбери другое свободное время на сайте.",
    }[event]
    return (
        f"Привет, {appointment['name']}!\n\n"
        f"{event_copy}\n"
        f"Дата: {local_start.strftime('%d.%m.%Y')}\n"
        f"Время: {local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')} (Europe/Moscow)\n"
        f"Телефон Барсука: +71488\n"
    )


def notify_new_appointment(appointment):
    # Каждое уведомление имеет собственный статус: ошибка одного не скрывает другое.
    appointment_id = appointment["id"]
    client_sent = send_and_record_email(
        appointment_id,
        "client_email_status",
        appointment["email"],
        "Заявка на встречу отправлена",
        appointment_email_text(appointment, "request"),
    )
    owner_email = os.environ.get("OWNER_EMAIL", "")
    owner_text = (
        f"Новая заявка от {appointment['name']}.\n"
        f"Телефон: {appointment['phone']}\n"
        f"Почта: {appointment['email']}\n"
        f"{appointment_email_text(appointment, 'request')}"
        f"Комментарий: {appointment.get('note') or 'не указан'}"
    )
    owner_sent = send_and_record_email(
        appointment_id,
        "owner_email_status",
        owner_email,
        "Новая заявка на встречу",
        owner_text,
    )
    return client_sent, owner_sent


def notify_appointment_status(appointment, event):
    # Клиент получает письмо после того, как новый статус уже записан в базе.
    subject = {
        "confirmed": "Встреча с Барсуком подтверждена",
        "cancelled": "Встреча с Барсуком отменена",
    }[event]
    return send_and_record_email(
        appointment["id"],
        "client_email_status",
        appointment.get("email", ""),
        subject,
        appointment_email_text(appointment, event),
    )


def owner_only():
    # Все действия со списком и личными данными требуют входа владельца.
    if not session.get("owner_authenticated"):
        return redirect(url_for("owner_login"))
    return None


def format_appointment(appointment):
    # Владелец видит время в часовом поясе расписания, а не в UTC базы.
    start = datetime.fromisoformat(appointment["starts_at"].replace("Z", "+00:00"))
    local_start = start.astimezone(BUSINESS_TIME_ZONE)
    appointment["starts_at_display"] = local_start.strftime("%d.%m.%Y, %H:%M")
    appointment["ends_at_display"] = (
        datetime.fromisoformat(appointment["ends_at"].replace("Z", "+00:00"))
        .astimezone(BUSINESS_TIME_ZONE)
        .strftime("%H:%M")
    )
    appointment["status_label"] = {
        "pending": "Ожидает подтверждения",
        "confirmed": "Подтверждена",
        "cancelled": "Отменена",
    }.get(appointment["status"], appointment["status"])
    for field in ("client_email_status", "owner_email_status"):
        appointment[f"{field}_label"] = {
            "pending": "Отправляется",
            "sent": "Отправлено",
            "failed": "Ошибка отправки",
            "not_sent": "Не отправлялось",
        }.get(appointment.get(field), "Неизвестно")
    return appointment


def check_login_limit(client_ip, now=None):
    # Ограничитель замедляет подбор пароля; для нескольких серверов нужен общий счётчик.
    current_time = time.time() if now is None else now
    attempts = [stamp for stamp in login_failures.get(client_ip, []) if current_time - stamp < LOGIN_LOCK_SECONDS]
    login_failures[client_ip] = attempts
    return len(attempts) >= LOGIN_FAILURE_LIMIT


@app.get("/")
def home():
    # Публичная страница содержит форму, но не показывает список заявок.
    today = datetime.now(BUSINESS_TIME_ZONE).date()
    return render_template(
        "index.html",
        booking_min_date=today.isoformat(),
        booking_max_date=(today + timedelta(days=BOOKING_DAYS_AHEAD)).isoformat(),
    )


@app.get("/api/availability")
def availability():
    # Браузер получает только свободные начала встреч на выбранную дату.
    try:
        selected_day = date.fromisoformat(request.args.get("date", ""))
    except ValueError:
        return jsonify({"error": "Выбери корректную дату."}), 400

    try:
        slots = available_slots(selected_day)
    except BookingServiceError:
        app.logger.exception("Could not load appointment availability.")
        return jsonify({"error": "Не удалось загрузить свободное время. Попробуй позже."}), 503

    return jsonify({"slots": slots})


@app.post("/booking")
def create_booking():
    # Сначала отклоняем поддельный запрос, затем проверяем и сохраняем данные.
    require_csrf_token()
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip().lower()
    starts_at_text = request.form.get("starts_at", "").strip()
    note = request.form.get("note", "").strip()

    # Проверяем введённые данные на сервере: проверкам браузера доверять нельзя.
    phone_digits = re.sub(r"\D", "", phone)
    if not 2 <= len(name) <= 100:
        flash("Укажи имя длиной от 2 до 100 символов.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if not 7 <= len(phone_digits) <= 15 or not re.fullmatch(r"[+\d\s().-]{7,25}", phone):
        flash("Проверь номер телефона: нужно от 7 до 15 цифр.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Укажи корректный адрес электронной почты.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if len(note) > 1000:
        flash("Комментарий не должен быть длиннее 1000 символов.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if request.form.get("consent") != "yes":
        flash("Подтверди, что данные нужны для связи по поводу встречи.", "error")
        return redirect(url_for("home", _anchor="booking"))

    # Клиент может изменить поля формы, поэтому слот сверяется с серверным расписанием.
    try:
        starts_at = datetime.fromisoformat(starts_at_text)
        if starts_at.tzinfo is None:
            raise ValueError("Slot timestamp must include its timezone.")
        starts_at_utc = starts_at.astimezone(timezone.utc)
        local_start = starts_at_utc.astimezone(BUSINESS_TIME_ZONE)
        slots = available_slots(local_start.date())
        if starts_at_utc.isoformat() not in {slot["value"] for slot in slots}:
            flash("Это время уже занято или недоступно. Выбери другое.", "error")
            return redirect(url_for("home", _anchor="booking"))
    except ValueError:
        flash("Выбери свободное время из списка.", "error")
        return redirect(url_for("home", _anchor="booking"))
    except BookingServiceError:
        app.logger.exception("Could not verify the selected appointment slot.")
        flash("Не удалось проверить свободное время. Попробуй позже.", "error")
        return redirect(url_for("home", _anchor="booking"))

    try:
        # Ограничение в базе атомарно запрещает одновременную бронь пересекающихся слотов.
        saved = supabase_request(
            "POST",
            "appointments",
            {
                "name": name,
                "phone": phone,
                "email": email,
                "requested_at": local_start.replace(tzinfo=None).isoformat(),
                "time_zone": "Europe/Moscow",
                "time_zone_offset": int(local_start.utcoffset().total_seconds() // 60),
                "starts_at": starts_at_utc.isoformat(),
                "ends_at": (starts_at_utc + timedelta(minutes=MEETING_MINUTES)).isoformat(),
                "note": note or None,
                "status": "pending",
                "client_email_status": "pending",
                "owner_email_status": "pending",
            },
        )
    except SlotUnavailableError:
        flash("Кто-то только что занял это время. Выбери другой свободный слот.", "error")
        return redirect(url_for("home", _anchor="booking"))
    except BookingServiceError:
        # Лучше сообщить об ошибке, чем ложно подтвердить несохранённую запись.
        flash("Не получилось сохранить заявку. Попробуй позже или свяжись с Барсуком по телефону.", "error")
        return redirect(url_for("home", _anchor="booking"))

    # Перенаправление после POST не создаёт дубликат при обновлении страницы.
    if not saved:
        app.logger.error("Supabase accepted a booking without returning its row.")
        flash("Не удалось подтвердить сохранение. Свяжись с Барсуком.", "error")
    else:
        client_email_sent, owner_email_sent = notify_new_appointment(saved[0])
        flash("Заявка отправлена! Барсук свяжется с тобой для подтверждения встречи.", "success")
        if not client_email_sent or not owner_email_sent:
            flash(
                "Заявка сохранена, но одно из писем не отправилось. "
                "Барсук увидит статус доставки в кабинете.",
                "error",
            )
    return redirect(url_for("home", _anchor="booking"))


@app.route("/owner/login", methods=["GET", "POST"])
def owner_login():
    # Пароль задаётся в настройках сервера и не хранится в репозитории.
    owner_password = os.environ.get("OWNER_PASSWORD", "")
    if len(owner_password) < 16:
        abort(503, description="Задай OWNER_PASSWORD длиной не менее 16 символов.")

    if request.method == "POST":
        require_csrf_token()
        client_ip = request.remote_addr or "unknown"
        if check_login_limit(client_ip):
            abort(429, description="Слишком много попыток. Попробуй позже.")

        submitted_password = request.form.get("password", "")
        if hmac.compare_digest(submitted_password.encode("utf-8"), owner_password.encode("utf-8")):
            # Очищаем старую сессию перед входом, чтобы не использовать её повторно.
            session.clear()
            session["owner_authenticated"] = True
            login_failures.pop(client_ip, None)
            return redirect(url_for("owner_appointments"))

        login_failures.setdefault(client_ip, []).append(time.time())
        flash("Пароль не подошёл.", "error")

    return render_template("owner_login.html")


@app.get("/owner/appointments")
def owner_appointments():
    # Личные данные клиентов видны только после входа владельца.
    auth_redirect = owner_only()
    if auth_redirect:
        return auth_redirect

    query = supabase_query(
        "appointments",
        {
            "select": "id,name,phone,email,starts_at,ends_at,note,status,client_email_status,owner_email_status,created_at",
            "order": "created_at.desc",
            "limit": "100",
        },
    )
    try:
        # Показываем владельцу последние 100 заявок, а не всю таблицу без ограничений.
        appointments = supabase_request("GET", query)
    except BookingServiceError:
        abort(503, description="Не удалось загрузить записи. Проверь настройки Supabase.")

    appointments = [format_appointment(appointment) for appointment in appointments]
    return render_template("owner_appointments.html", appointments=appointments)


def change_appointment_status(appointment_id, new_status, allowed_statuses):
    # Меняем статус в базе до письма; отменённый слот сразу становится свободным.
    auth_redirect = owner_only()
    if auth_redirect:
        return auth_redirect
    require_csrf_token()
    query = supabase_query(
        "appointments",
        {
            "id": f"eq.{appointment_id}",
            "status": f"in.({','.join(allowed_statuses)})",
            "select": "id,name,phone,email,starts_at,ends_at,note,status",
        },
    )
    try:
        rows = supabase_request(
            "PATCH",
            query,
            {"status": new_status, "client_email_status": "pending"},
        )
    except BookingServiceError:
        abort(503, description="Не удалось обновить запись. Попробуй ещё раз.")
    if not rows:
        flash("Запись уже изменилась или была удалена. Обнови список.", "error")
        return redirect(url_for("owner_appointments"))

    appointment = rows[0]
    email_sent = notify_appointment_status(appointment, new_status)
    if email_sent:
        flash("Статус записи обновлён, письмо клиенту отправлено.", "success")
    else:
        flash(
            "Статус записи обновлён, но письмо клиенту не отправилось. "
            "Проверь Resend и повтори отправку из карточки записи.",
            "error",
        )
    return redirect(url_for("owner_appointments"))


@app.post("/owner/appointments/<int:appointment_id>/confirm")
def confirm_appointment(appointment_id):
    # Подтверждение оставляет слот занятым и отправляет клиенту письмо.
    return change_appointment_status(appointment_id, "confirmed", ["pending"])


@app.post("/owner/appointments/<int:appointment_id>/cancel")
def cancel_appointment(appointment_id):
    # Отмена освобождает время и сообщает клиенту об отмене.
    return change_appointment_status(appointment_id, "cancelled", ["pending", "confirmed"])


@app.post("/owner/appointments/<int:appointment_id>/delete")
def delete_appointment(appointment_id):
    # Удаление стирает персональные данные и освобождает время без письма клиенту.
    auth_redirect = owner_only()
    if auth_redirect:
        return auth_redirect
    require_csrf_token()
    try:
        deleted = supabase_request(
            "DELETE",
            supabase_query("appointments", {"id": f"eq.{appointment_id}", "select": "id"}),
        )
    except BookingServiceError:
        abort(503, description="Не удалось удалить запись. Попробуй ещё раз.")
    if not deleted:
        flash("Эта запись уже удалена.", "error")
    else:
        flash("Запись удалена, время снова свободно.", "success")
    return redirect(url_for("owner_appointments"))


@app.post("/owner/appointments/<int:appointment_id>/retry-email")
def retry_appointment_email(appointment_id):
    # Владелец может повторить уведомления после настройки или сбоя почты.
    auth_redirect = owner_only()
    if auth_redirect:
        return auth_redirect
    require_csrf_token()
    try:
        rows = supabase_request(
            "GET",
            supabase_query(
                "appointments",
                {
                    "id": f"eq.{appointment_id}",
                    "select": "id,name,phone,email,starts_at,ends_at,note,status,client_email_status,owner_email_status",
                },
            ),
        )
    except BookingServiceError:
        abort(503, description="Не удалось загрузить запись. Попробуй ещё раз.")
    if not rows:
        flash("Эта запись уже удалена.", "error")
        return redirect(url_for("owner_appointments"))

    appointment = rows[0]
    event = appointment["status"] if appointment["status"] != "pending" else "request"
    retry_results = []
    if appointment["client_email_status"] in ("failed", "pending"):
        retry_results.append(
            send_and_record_email(
                appointment_id,
                "client_email_status",
                appointment.get("email", ""),
                "Заявка на встречу",
                appointment_email_text(appointment, event),
            )
        )
    if appointment["owner_email_status"] in ("failed", "pending"):
        retry_results.append(
            send_and_record_email(
                appointment_id,
                "owner_email_status",
                os.environ.get("OWNER_EMAIL", ""),
                "Обновление заявки на встречу",
                f"Заявка №{appointment_id}: {appointment['name']}, "
                f"{appointment['email']}, статус: {appointment['status']}.",
            )
        )
    if retry_results and all(retry_results):
        flash("Неотправленные письма отправлены.", "success")
    elif retry_results:
        flash("Не все письма удалось отправить. Проверь настройки Resend.", "error")
    else:
        flash("Для этой записи нет писем с ошибкой отправки.", "success")
    return redirect(url_for("owner_appointments"))


@app.post("/owner/logout")
def owner_logout():
    # Выход тоже защищён CSRF-токеном, поскольку он меняет сессию.
    require_csrf_token()
    session.clear()
    return redirect(url_for("owner_login"))


if __name__ == "__main__":
    # Render задаёт PORT; локально удобно использовать порт 5000.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
