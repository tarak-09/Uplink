import tempfile
import unittest
from pathlib import Path

from uplink.config import ConfigError, Settings, load_settings, read_env_file


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name, ".env")

    def tearDown(self):
        self.folder.cleanup()

    def test_reads_the_usual_dotenv_shapes(self):
        self.path.write_text(
            "# comment\n"
            "\n"
            "MODEL_API_KEY=abc123\n"
            "export LANGUAGE=te\n"
            'NETWORK_NAME="Freifunk München"\n'
            "WATCH_NODES='a1, Kiosk-2 '\n"
            "CONTACT=ops@example.org # who to call\n"
            "WHATSAPP_TOKEN=          # paste your token here\n"
            "FEED_URL=https://map.example.org/data/meshviewer.json#latest\n"
            "not a line\n"
            "1BAD=x\n",
            encoding="utf-8",
        )
        values = read_env_file(self.path)
        self.assertEqual(values["MODEL_API_KEY"], "abc123")
        self.assertEqual(values["LANGUAGE"], "te")
        self.assertEqual(values["NETWORK_NAME"], "Freifunk München")
        self.assertEqual(values["CONTACT"], "ops@example.org")
        self.assertEqual(values["WHATSAPP_TOKEN"], "", "a comment is not a value")
        self.assertEqual(values["FEED_URL"], "https://map.example.org/data/meshviewer.json#latest")
        self.assertNotIn("1BAD", values)
        settings = load_settings({}, self.path)
        self.assertEqual(settings.language, "telugu")
        self.assertEqual(settings.watch_nodes, ("a1", "kiosk-2"))

    def test_the_real_environment_wins(self):
        self.path.write_text("LANGUAGE=hindi\nPOLL_INTERVAL_SECONDS=120\n", encoding="utf-8")
        settings = load_settings({"LANGUAGE": "english"}, self.path)
        self.assertEqual((settings.language, settings.poll_interval), ("english", 120))

    def test_a_missing_file_is_fine(self):
        self.assertEqual(load_settings({}, Path(self.folder.name, "absent.env")), Settings())


class ValidationTest(unittest.TestCase):
    def test_converters(self):
        settings = load_settings(
            {
                "ANONYMIZE": "yes",
                "RECORD_SNAPSHOTS": "off",
                "WHATSAPP_TO": "+49 (151) 1234-5678",
                "DATA_DIR": "/tmp/uplink",
                "MODEL_REASONING_EFFORT": "MINIMAL",
            },
            None,
        )
        self.assertTrue(settings.anonymize)
        self.assertFalse(settings.record_snapshots)
        self.assertEqual(settings.whatsapp_to, "4915112345678")
        self.assertEqual(settings.data_dir, Path("/tmp/uplink"))
        self.assertEqual(settings.reasoning_effort, "minimal")

    def test_bad_values_name_the_variable(self):
        cases = {
            "ANONYMIZE": "maybe",
            "POLL_INTERVAL_SECONDS": "soon",
            "LANGUAGE": "klingon",
            "WHATSAPP_TO": "12",
            "FEED_URL": "ftp://x",
            "MODEL_REASONING_EFFORT": "none",
            "WHATSAPP_API_VERSION": "26",
            "WEBHOOK_PORT": "70000",
            "STALE_AFTER_SECONDS": "30",
            "ALERTS_PER_HOUR": "0",
            "TELEGRAM_CHAT_ID": "@me",
            "TELEGRAM_BOT_TOKEN": "nope",
        }
        for variable, value in cases.items():
            with self.subTest(variable=variable), self.assertRaises(ConfigError) as caught:
                load_settings({variable: value}, None)
            self.assertTrue(
                str(caught.exception).startswith(variable) or variable in str(caught.exception), caught.exception
            )

    def test_polling_faster_than_the_map_updates_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            load_settings({"POLL_INTERVAL_SECONDS": "10"}, None)
        self.assertIn("volunteer-run", str(caught.exception))
        local = load_settings({"POLL_INTERVAL_SECONDS": "5", "FEED_URL": "http://127.0.0.1:8000/meshviewer.json"}, None)
        self.assertEqual(local.poll_interval, 5)
        self.assertTrue(load_settings({"FEED_URL": "file:///tmp/m.json"}, None).feed_is_local)

    def test_overrides_and_readiness(self):
        settings = load_settings({"MODEL_API_KEY": "k"}, None, data_dir=Path("x"), language=None)
        self.assertEqual(settings.data_dir, Path("x"))
        self.assertTrue(settings.has_model)

    def test_the_messenger(self):
        with self.assertRaises(ConfigError) as caught:
            load_settings({"MESSENGER": "signal"}, None)
        self.assertIn("MESSENGER must be whatsapp", str(caught.exception))
        whatsapp = load_settings({"WHATSAPP_TO": "4915112345678"}, None)
        self.assertEqual((whatsapp.messenger, whatsapp.uses_telegram), ("whatsapp", False), "WhatsApp by default")
        self.assertEqual(whatsapp.missing_for_alerts(), ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"])
        self.assertEqual(whatsapp.missing_for_webhook()[-2:], ["WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"])

    def test_the_old_provider_setting_says_what_to_rename_it_to(self):
        for old, new in (
            ("telegram", "MESSENGER=telegram"),
            ("cloud", "MESSENGER=whatsapp"),
            ("", "MESSENGER=whatsapp"),
        ):
            with self.subTest(old=old), self.assertRaises(ConfigError) as caught:
                load_settings({"WHATSAPP_PROVIDER": old}, None)
            self.assertIn("WHATSAPP_PROVIDER is now called MESSENGER: in .env, replace", str(caught.exception))
            self.assertIn(new, str(caught.exception))

    def test_telegram(self):
        telegram = load_settings(
            {
                "MESSENGER": "Telegram",
                "TELEGRAM_BOT_TOKEN": "123456:AAE-secret_6",
                "TELEGRAM_CHAT_ID": "-1001234",
            },
            None,
        )
        self.assertTrue(telegram.uses_telegram)
        self.assertEqual((telegram.telegram_chat_id, telegram.missing_for_alerts()), ("-1001234", []))
        self.assertNotIn("AAE-secret_6", repr(telegram))
        bare = load_settings({"MESSENGER": "telegram", "WHATSAPP_TO": "4915112345678"}, None)
        self.assertEqual(bare.missing_for_alerts(), ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        self.assertEqual(bare.missing_for_webhook(), ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"], "no webhook secrets")
        self.assertEqual(load_settings({"TELEGRAM_CHAT_ID": "0042"}, None).telegram_chat_id, "42")

    def test_a_malformed_bot_token_is_refused_without_showing_it(self):
        for token in ("bot123456:AAE-secret", "123456:AAE secret", "AAE-secret"):
            with self.subTest(token=token), self.assertRaises(ConfigError) as caught:
                load_settings({"TELEGRAM_BOT_TOKEN": token}, None)
            self.assertTrue(str(caught.exception).startswith("TELEGRAM_BOT_TOKEN"))
            self.assertNotIn("secret", str(caught.exception))

    def test_secrets_stay_out_of_repr(self):
        settings = load_settings(
            {
                "MODEL_API_KEY": "sk-secret-1",
                "WHATSAPP_TOKEN": "EAAG-secret-2",
                "WHATSAPP_APP_SECRET": "secret-3",
                "WHATSAPP_VERIFY_TOKEN": "secret-4",
            },
            None,
        )
        text = repr(settings)
        for secret in ("sk-secret-1", "EAAG-secret-2", "secret-3", "secret-4", "secret-5"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
