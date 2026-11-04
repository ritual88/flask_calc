import io
import json
import os
import re
import unittest
from unittest.mock import patch
from urllib.error import URLError

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

    def test_booking_is_saved_and_client_sees_confirmation(self):
        # Подменяем удалённый API тестовым ответом: настоящие данные никуда не отправляются.
        with patch.object(booking_app, "urlopen", return_value=io.BytesIO(b'[{"id":12}]')) as mocked:
            response = self.client.post(
                "/booking",
                data={
                    "_csrf_token": self.csrf_token(),
                    "name": "Алекс",
                    "phone": "+7 900 123-45-67",
                    "requested_at": "2030-05-10T12:30",
                    "time_zone": "Europe/Moscow",
                    "time_zone_offset": "-180",
                    "note": "Привет",
                    "consent": "yes",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Заявка отправлена".encode(), response.data)
        self.assertNotIn("Алекс".encode(), response.data)
        sent_request = mocked.call_args.args[0]
        self.assertEqual(sent_request.get_method(), "POST")
        self.assertIn("/rest/v1/appointments", sent_request.full_url)
        self.assertEqual(json.loads(sent_request.data)["time_zone"], "Europe/Moscow")
        self.assertEqual(json.loads(sent_request.data)["time_zone_offset"], 180)

    def test_invalid_phone_is_rejected_without_database_request(self):
        # Некорректный телефон отклоняется до отправки личных данных в Supabase.
        with patch.object(booking_app, "urlopen") as mocked:
            response = self.client.post(
                "/booking",
                data={
                    "_csrf_token": self.csrf_token(),
                    "name": "Алекс",
                    "phone": "123",
                    "requested_at": "2030-05-10T12:30",
                    "time_zone": "Europe/Moscow",
                    "time_zone_offset": "-180",
                    "consent": "yes",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Проверь номер телефона".encode(), response.data)
        mocked.assert_not_called()

    def test_past_meeting_time_is_rejected(self):
        # A requested time must still be in the future after applying the client's UTC offset.
        with patch.object(booking_app, "urlopen") as mocked:
            response = self.client.post(
                "/booking",
                data={
                    "_csrf_token": self.csrf_token(),
                    "name": "Алекс",
                    "phone": "+7 900 123-45-67",
                    "requested_at": "2020-05-10T12:30",
                    "time_zone": "Europe/Moscow",
                    "time_zone_offset": "-180",
                    "consent": "yes",
                },
                follow_redirects=True,
            )

        self.assertIn("Выбери дату и время в будущем".encode(), response.data)
        mocked.assert_not_called()

    def test_database_failure_does_not_claim_success(self):
        # A connection failure must never produce a success-shaped confirmation.
        with patch.object(booking_app, "urlopen", side_effect=URLError("test outage")):
            response = self.client.post(
                "/booking",
                data={
                    "_csrf_token": self.csrf_token(),
                    "name": "Алекс",
                    "phone": "+7 900 123-45-67",
                    "requested_at": "2030-05-10T12:30",
                    "time_zone": "Europe/Moscow",
                    "time_zone_offset": "-180",
                    "consent": "yes",
                },
                follow_redirects=True,
            )

        self.assertIn("Не получилось сохранить заявку".encode(), response.data)
        self.assertNotIn("Заявка отправлена".encode(), response.data)

    def test_public_visitor_cannot_open_private_appointment_list(self):
        # Без авторизации закрытый список перенаправляет посетителя ко входу.
        response = self.client.get("/owner/appointments")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/owner/login", response.headers["Location"])

    def test_owner_login_unlocks_private_appointment_list(self):
        # Подменяем ответ API, чтобы тест никогда не обращался к настоящей базе.
        with patch.object(
            booking_app,
            "urlopen",
            return_value=io.BytesIO(
                b'[{"id":12,"name":"\xd0\x90\xd0\xbb\xd0\xb5\xd0\xba\xd1\x81",'
                b'"phone":"+7 900 123-45-67","requested_at":"2030-05-10T12:30:00",'
                b'"time_zone":"Europe/Moscow","time_zone_offset":180,'
                b'"note":null,"status":"pending","created_at":"2030-01-01T10:00:00+00:00"}]'
            ),
        ):
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
        self.assertIn("+7 900 123-45-67", private_page.get_data(as_text=True))
        self.assertIn("Ожидает подтверждения", private_page.get_data(as_text=True))

    def test_bad_csrf_token_is_rejected(self):
        # Поддельная отправка с другого сайта не должна создать запись.
        with patch.object(booking_app, "urlopen") as mocked:
            response = self.client.post("/booking", data={"_csrf_token": "ошибка"})

        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
