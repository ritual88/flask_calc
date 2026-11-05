import io
import json
import os
import re
import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

# Тестовые пароли существуют только во время теста и не записываются в проект.
os.environ.setdefault("FLASK_SECRET_KEY", "test-session-signing-key-not-for-production")
os.environ.setdefault("OWNER_PASSWORD", "test-owner-password")
os.environ.setdefault("SUPABASE_URL", "https://unit-test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-only-service-key")

import app as booking_app


class BookingFlowTests(unittest.TestCase):
    def setUp(self):
        # Тестовый клиент Flask проверяет формы и сессии без запуска сервера.
        booking_app.app.config.update(TESTING=True)
        self.client = booking_app.app.test_client()

    def csrf_token(self, path="/"):
        # Читаем скрытый токен, который настоящая страница выдаёт браузеру.
        response = self.client.get(path)
        match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode("ascii")

    def future_slot(self):
        # Выбираем рабочий слот с запасом, чтобы тест не зависел от дня запуска.
        selected_day = datetime.now(booking_app.BUSINESS_TIME_ZONE).date() + timedelta(days=1)
        while selected_day.weekday() >= 5:
            selected_day += timedelta(days=1)
        local_start = datetime.combine(
            selected_day,
            time(12, 0),
            booking_app.BUSINESS_TIME_ZONE,
        )
        return local_start.astimezone(timezone.utc).isoformat()

    def appointment(self, **overrides):
        # Фикстура имитирует строку, которую возвращает Supabase.
        starts_at = self.future_slot()
        row = {
            "id": 12,
            "name": "Алекс",
            "phone": "+7 900 123-45-67",
            "email": "alex@example.com",
            "starts_at": starts_at,
            "ends_at": (
                datetime.fromisoformat(starts_at) + timedelta(minutes=60)
            ).isoformat(),
            "note": "Привет",
            "status": "pending",
            "client_email_status": "pending",
            "owner_email_status": "pending",
            "created_at": "2030-01-01T10:00:00+00:00",
        }
        row.update(overrides)
        return row

    def booking_form(self, **overrides):
        # Все обязательные поля формы собраны вместе, чтобы сценарии были компактными.
        data = {
            "_csrf_token": self.csrf_token(),
            "name": "Алекс",
            "phone": "+7 900 123-45-67",
            "email": "alex@example.com",
            "starts_at": self.future_slot(),
            "note": "Привет",
            "consent": "yes",
        }
        data.update(overrides)
        return data

    def test_home_page_has_email_date_and_dynamic_slot_fields(self):
        # Страница отдаёт обязательные поля и адрес API для загрузки слотов.
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="email"', page)
        self.assertIn('name="booking_date"', page)
        self.assertIn('name="starts_at"', page)
        self.assertIn('data-availability-url="/api/availability"', page)

    def test_booking_is_saved_and_client_sees_confirmation(self):
        # Удалённая база и отправка писем заменены безопасными тестовыми ответами.
        slot = self.future_slot()
        saved = self.appointment()
        with patch.object(booking_app, "available_slots", return_value=[
            {"value": slot, "label": "12:00"}
        ]), patch.object(booking_app, "supabase_request", return_value=[saved]) as api, \
                patch.object(booking_app, "notify_new_appointment", return_value=(True, True)):
            response = self.client.post(
                "/booking",
                data=self.booking_form(starts_at=slot),
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Заявка отправлена".encode(), response.data)
        self.assertNotIn("Алекс".encode(), response.data)
        payload = api.call_args.args[2]
        self.assertEqual(payload["email"], "alex@example.com")
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["time_zone"], "Europe/Moscow")
        self.assertEqual(payload["time_zone_offset"], 180)

    def test_invalid_email_is_rejected_without_database_request(self):
        # Адрес проверяется сервером, даже если браузерную проверку обошли.
        with patch.object(booking_app, "supabase_request") as api:
            response = self.client.post(
                "/booking",
                data=self.booking_form(email="not-an-email"),
                follow_redirects=True,
            )

        self.assertIn("корректный адрес электронной почты".encode(), response.data)
        api.assert_not_called()

    def test_invalid_phone_is_rejected_without_database_request(self):
        # Некорректный телефон отклоняется до отправки личных данных в Supabase.
        with patch.object(booking_app, "supabase_request") as api:
            response = self.client.post(
                "/booking",
                data=self.booking_form(phone="123"),
                follow_redirects=True,
            )

        self.assertIn("Проверь номер телефона".encode(), response.data)
        api.assert_not_called()

    def test_slot_that_is_not_available_is_rejected(self):
        # Подменённое значение формы не обходит повторную серверную проверку.
        with patch.object(booking_app, "available_slots", return_value=[]), patch.object(
            booking_app, "supabase_request"
        ) as api:
            response = self.client.post(
                "/booking",
                data=self.booking_form(),
                follow_redirects=True,
            )

        self.assertIn("уже занято или недоступно".encode(), response.data)
        api.assert_not_called()

    def test_database_overlap_conflict_has_clear_message(self):
        # Конфликт, возникший после проверки слота, сообщает о занятом времени.
        slot = self.future_slot()
        with patch.object(booking_app, "available_slots", return_value=[
            {"value": slot, "label": "12:00"}
        ]), patch.object(
            booking_app,
            "supabase_request",
            side_effect=booking_app.SlotUnavailableError("already booked"),
        ):
            response = self.client.post(
                "/booking",
                data=self.booking_form(starts_at=slot),
                follow_redirects=True,
            )

        self.assertIn("Кто-то только что занял это время".encode(), response.data)
        self.assertNotIn("Заявка отправлена".encode(), response.data)

    def test_database_failure_does_not_claim_success(self):
        # Ошибка Supabase не должна выглядеть как сохранённая заявка.
        slot = self.future_slot()
        with patch.object(booking_app, "available_slots", return_value=[
            {"value": slot, "label": "12:00"}
        ]), patch.object(
            booking_app,
            "supabase_request",
            side_effect=booking_app.BookingServiceError("test outage"),
        ):
            response = self.client.post(
                "/booking",
                data=self.booking_form(starts_at=slot),
                follow_redirects=True,
            )

        self.assertIn("Не получилось сохранить заявку".encode(), response.data)
        self.assertNotIn("Заявка отправлена".encode(), response.data)

    def test_availability_endpoint_rejects_invalid_date(self):
        # Некорректная дата не должна отправляться дальше в запрос к базе.
        with patch.object(booking_app, "available_slots") as slots:
            response = self.client.get("/api/availability?date=not-a-date")

        self.assertEqual(response.status_code, 400)
        slots.assert_not_called()

    def test_availability_endpoint_returns_slots(self):
        # Публичный endpoint возвращает только свободное время без данных клиентов.
        expected = [{"value": self.future_slot(), "label": "12:00"}]
        with patch.object(booking_app, "available_slots", return_value=expected):
            response = self.client.get("/api/availability?date=2030-05-10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"slots": expected})

    def test_overlapping_appointments_hide_all_conflicting_slots(self):
        # Часовая встреча исключает любой получасовой слот, пересекающий её.
        selected_day = date(2030, 5, 10)
        busy_start = datetime.combine(selected_day, time(10, 30), booking_app.BUSINESS_TIME_ZONE)
        busy_row = {
            "starts_at": busy_start.astimezone(timezone.utc).isoformat(),
            "ends_at": (busy_start + timedelta(hours=1)).astimezone(timezone.utc).isoformat(),
        }
        local_now = datetime.combine(selected_day, time(9, 0), booking_app.BUSINESS_TIME_ZONE)
        with patch.object(booking_app, "supabase_request", return_value=[busy_row]):
            slots = booking_app.available_slots(selected_day, now=local_now)

        labels = [slot["label"] for slot in slots]
        self.assertNotIn("10:00", labels)
        self.assertNotIn("10:30", labels)
        self.assertNotIn("11:00", labels)
        self.assertIn("11:30", labels)
        self.assertIn("17:00", labels)

    def test_public_visitor_cannot_open_private_appointment_list(self):
        # Без авторизации закрытый список перенаправляет посетителя ко входу.
        response = self.client.get("/owner/appointments")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/owner/login", response.headers["Location"])

    def test_owner_login_unlocks_private_appointment_list(self):
        # Подменяем ответ API, чтобы тест никогда не обращался к настоящей базе.
        appointment = self.appointment()
        with patch.object(booking_app, "supabase_request", return_value=[appointment]):
            login = self.client.post(
                "/owner/login",
                data={
                    "_csrf_token": self.csrf_token("/owner/login"),
                    "password": os.environ["OWNER_PASSWORD"],
                },
            )
            private_page = self.client.get("/owner/appointments")

        self.assertEqual(login.status_code, 302)
        self.assertEqual(private_page.status_code, 200)
        page = private_page.get_data(as_text=True)
        self.assertIn("+7 900 123-45-67", page)
        self.assertIn("Ожидает подтверждения", page)
        self.assertIn("Подтвердить", page)
        self.assertIn("Отменить", page)
        self.assertIn("Удалить навсегда", page)
        self.assertIn("Повторить отправку писем", page)

    def test_owner_can_confirm_and_notify_client(self):
        # Подтверждение закрыто входом и CSRF-токеном, затем вызывает email-уведомление.
        appointment = self.appointment(status="confirmed")
        with self.client.session_transaction() as session:
            session["owner_authenticated"] = True
        token = self.csrf_token()
        with patch.object(
            booking_app, "supabase_request", return_value=[appointment]
        ) as api, patch.object(
            booking_app, "notify_appointment_status", return_value=True
        ) as notify:
            response = self.client.post(
                "/owner/appointments/12/confirm",
                data={"_csrf_token": token},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(api.call_args.args[0], "PATCH")
        self.assertEqual(api.call_args.args[2]["status"], "confirmed")
        notify.assert_called_once_with(appointment, "confirmed")

    def test_owner_can_cancel_and_delete_appointment(self):
        # Отмена освобождает время и уведомляет клиента; удаление не отправляет письмо.
        appointment = self.appointment(status="cancelled")
        with self.client.session_transaction() as session:
            session["owner_authenticated"] = True
        token = self.csrf_token()
        with patch.object(
            booking_app, "supabase_request", return_value=[appointment]
        ) as api, patch.object(
            booking_app, "notify_appointment_status", return_value=True
        ) as notify:
            cancelled = self.client.post(
                "/owner/appointments/12/cancel",
                data={"_csrf_token": token},
            )
        self.assertEqual(cancelled.status_code, 302)
        self.assertEqual(api.call_args.args[2]["status"], "cancelled")
        notify.assert_called_once_with(appointment, "cancelled")

        with patch.object(booking_app, "supabase_request", return_value=[{"id": 12}]) as api:
            deleted = self.client.post(
                "/owner/appointments/12/delete",
                data={"_csrf_token": token},
            )
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(api.call_args.args[0], "DELETE")

    def test_unauthorized_owner_action_redirects_to_login(self):
        # Нельзя менять запись или отправлять её письма без входа владельца.
        with patch.object(booking_app, "supabase_request") as api:
            response = self.client.post("/owner/appointments/12/delete")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/owner/login", response.headers["Location"])
        api.assert_not_called()

    def test_resend_test_mode_uses_only_resend_test_recipient(self):
        # Проверяем безопасный адрес и Bearer-авторизацию без сетевого вызова.
        with patch.dict(
            os.environ,
            {
                "EMAIL_TEST_MODE": "true",
                "RESEND_API_KEY": "test-resend-key",
                "EMAIL_FROM": "onboarding@resend.dev",
            },
        ), patch.object(
            booking_app,
            "urlopen",
            return_value=io.BytesIO(b'{"id":"test-email-id"}'),
        ) as mocked:
            booking_app.send_email("client@example.com", "Тест", "Текст")

        request = mocked.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["to"], ["delivered@resend.dev"])
        self.assertEqual(payload["from"], "onboarding@resend.dev")
        self.assertTrue(request.get_header("Authorization").startswith("Bearer "))

    def test_email_failure_is_recorded_for_the_owner(self):
        # Ошибка почты не теряет заявку и получает видимый статус failed.
        with patch.object(
            booking_app, "send_email", side_effect=booking_app.EmailDeliveryError("test outage")
        ), patch.object(booking_app, "update_email_delivery_status") as update_status:
            sent = booking_app.send_and_record_email(
                12,
                "client_email_status",
                "alex@example.com",
                "Тест",
                "Текст",
            )

        self.assertFalse(sent)
        update_status.assert_called_once_with(12, "client_email_status", "failed")

    def test_owner_can_retry_failed_emails(self):
        # Владелец может повторить отправку писем после исправления настроек.
        appointment = self.appointment(
            client_email_status="failed",
            owner_email_status="failed",
        )
        with self.client.session_transaction() as session:
            session["owner_authenticated"] = True
        token = self.csrf_token()
        with patch.object(
            booking_app, "supabase_request", return_value=[appointment]
        ), patch.object(
            booking_app, "send_and_record_email", side_effect=[True, True]
        ) as send:
            response = self.client.post(
                "/owner/appointments/12/retry-email",
                data={"_csrf_token": token},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(send.call_count, 2)

    def test_bad_csrf_token_is_rejected(self):
        # Поддельная отправка с другого сайта не должна создать запись.
        with patch.object(booking_app, "supabase_request") as api:
            response = self.client.post("/booking", data={"_csrf_token": "ошибка"})

        self.assertEqual(response.status_code, 400)
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
