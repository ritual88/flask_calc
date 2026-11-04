import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

app = Flask(__name__)

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
        # Записываем только код ошибки: ответ базы может содержать личные данные.
        app.logger.error("Supabase request failed with HTTP status %s.", error.code)
        raise BookingServiceError("Supabase rejected the request.") from error
    except URLError as error:
        # Сообщаем о проблеме связи, не записывая пароль или данные клиента в журнал.
        app.logger.error("Could not reach Supabase: %s", error.reason)
        raise BookingServiceError("Supabase is unreachable.") from error

    if not response_body:
        return []
    return json.loads(response_body)


def check_login_limit(client_ip, now=None):
    # Ограничитель замедляет подбор пароля; для нескольких серверов нужен общий счётчик.
    current_time = time.time() if now is None else now
    attempts = [stamp for stamp in login_failures.get(client_ip, []) if current_time - stamp < LOGIN_LOCK_SECONDS]
    login_failures[client_ip] = attempts
    return len(attempts) >= LOGIN_FAILURE_LIMIT


@app.get("/")
def home():
    # Публичная страница содержит форму, но не показывает список заявок.
    return render_template("index.html")


@app.post("/booking")
def create_booking():
    # Сначала отклоняем поддельный запрос, затем проверяем и сохраняем данные.
    require_csrf_token()
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    requested_at_text = request.form.get("requested_at", "").strip()
    time_zone = request.form.get("time_zone", "").strip()
    browser_offset_text = request.form.get("time_zone_offset", "").strip()
    note = request.form.get("note", "").strip()

    # Проверяем введённые данные на сервере: проверкам браузера доверять нельзя.
    phone_digits = re.sub(r"\D", "", phone)
    if not 2 <= len(name) <= 100:
        flash("Укажи имя длиной от 2 до 100 символов.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if not 7 <= len(phone_digits) <= 15 or not re.fullmatch(r"[+\d\s().-]{7,25}", phone):
        flash("Проверь номер телефона: нужно от 7 до 15 цифр.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if len(note) > 1000:
        flash("Комментарий не должен быть длиннее 1000 символов.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if not re.fullmatch(r"[A-Za-z0-9_+./-]{1,100}", time_zone):
        flash("Не удалось определить твой часовой пояс. Обнови страницу и попробуй ещё раз.", "error")
        return redirect(url_for("home", _anchor="booking"))
    try:
        # JavaScript reports UTC-minus-local; invert it to get the local UTC offset.
        time_zone_offset = -int(browser_offset_text)
    except ValueError:
        flash("Не удалось определить смещение часового пояса. Обнови страницу и попробуй ещё раз.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if not -840 <= time_zone_offset <= 840:
        flash("Указан некорректный часовой пояс.", "error")
        return redirect(url_for("home", _anchor="booking"))
    if request.form.get("consent") != "yes":
        flash("Подтверди, что данные нужны для связи по поводу встречи.", "error")
        return redirect(url_for("home", _anchor="booking"))

    # datetime-local не содержит часовой пояс, поэтому сохраняем время и зону браузера отдельно.
    try:
        requested_at = datetime.strptime(requested_at_text, "%Y-%m-%dT%H:%M")
    except ValueError:
        flash("Выбери корректные дату и время встречи.", "error")
        return redirect(url_for("home", _anchor="booking"))
    requested_at_utc = requested_at.replace(
        tzinfo=timezone(timedelta(minutes=time_zone_offset))
    ).astimezone(timezone.utc)
    if requested_at_utc <= datetime.now(timezone.utc):
        flash("Выбери дату и время в будущем.", "error")
        return redirect(url_for("home", _anchor="booking"))

    try:
        # Сохраняем только данные, нужные для связи и согласования встречи.
        saved = supabase_request(
            "POST",
            "appointments",
            {
                "name": name,
                "phone": phone,
                "requested_at": requested_at.isoformat(),
                "time_zone": time_zone,
                "time_zone_offset": time_zone_offset,
                "note": note or None,
                "status": "pending",
            },
        )
    except BookingServiceError:
        # Лучше сообщить об ошибке, чем ложно подтвердить несохранённую запись.
        flash("Не получилось сохранить заявку. Попробуй позже или свяжись с Барсуком по телефону.", "error")
        return redirect(url_for("home", _anchor="booking"))

    # Перенаправление после POST не создаёт дубликат при обновлении страницы.
    if not saved:
        app.logger.error("Supabase accepted a booking without returning its row.")
        flash("Не удалось подтвердить сохранение. Свяжись с Барсуком.", "error")
    else:
        flash("Заявка отправлена! Барсук свяжется с тобой для подтверждения времени.", "success")
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
    if not session.get("owner_authenticated"):
        return redirect(url_for("owner_login"))

    query = urlencode(
        {
            "select": "id,name,phone,requested_at,time_zone,time_zone_offset,note,status,created_at",
            "order": "created_at.desc",
            "limit": "100",
        },
        quote_via=quote,
    )
    try:
        # Показываем владельцу последние 100 заявок, а не всю таблицу без ограничений.
        appointments = supabase_request("GET", f"appointments?{query}")
    except BookingServiceError:
        abort(503, description="Не удалось загрузить записи. Проверь настройки Supabase.")

    return render_template("owner_appointments.html", appointments=appointments)


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
