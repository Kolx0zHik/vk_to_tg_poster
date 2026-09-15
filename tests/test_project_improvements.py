import asyncio
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _install_test_stubs() -> None:
    if "fastapi" not in sys.modules:
        fastapi = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        class FastAPI:
            def __init__(self, *args, **kwargs):
                self.routes = []

            def mount(self, *args, **kwargs):
                return None

            def get(self, *args, **kwargs):
                def decorator(func):
                    return func

                return decorator

            def post(self, *args, **kwargs):
                def decorator(func):
                    return func

                return decorator

        fastapi.FastAPI = FastAPI
        fastapi.HTTPException = HTTPException
        sys.modules["fastapi"] = fastapi

        staticfiles = types.ModuleType("fastapi.staticfiles")

        class StaticFiles:
            def __init__(self, *args, **kwargs):
                pass

        staticfiles.StaticFiles = StaticFiles
        sys.modules["fastapi.staticfiles"] = staticfiles

        responses = types.ModuleType("fastapi.responses")

        class HTMLResponse:
            def __init__(self, content: str):
                self.content = content

        responses.HTMLResponse = HTMLResponse
        sys.modules["fastapi.responses"] = responses

    if "pydantic" not in sys.modules:
        pydantic = types.ModuleType("pydantic")

        def Field(default=None, **kwargs):
            if "default_factory" in kwargs:
                return kwargs["default_factory"]()
            return default

        def field_validator(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        class BaseModel:
            def __init__(self, **kwargs):
                annotations = {}
                for cls in reversed(self.__class__.__mro__):
                    annotations.update(getattr(cls, "__annotations__", {}))
                for key in annotations:
                    if key in kwargs:
                        value = kwargs[key]
                    else:
                        value = getattr(self.__class__, key, None)
                    setattr(self, key, value)

            @classmethod
            def model_validate(cls, data):
                return cls(**data)

            def model_dump(self):
                result = {}
                annotations = {}
                for cls in reversed(self.__class__.__mro__):
                    annotations.update(getattr(cls, "__annotations__", {}))
                for key in annotations:
                    value = getattr(self, key)
                    if hasattr(value, "model_dump"):
                        value = value.model_dump()
                    elif isinstance(value, list):
                        value = [item.model_dump() if hasattr(item, "model_dump") else item for item in value]
                    result[key] = value
                return result

        pydantic.BaseModel = BaseModel
        pydantic.Field = Field
        pydantic.field_validator = field_validator
        sys.modules["pydantic"] = pydantic


_install_test_stubs()

from src import web
from src.cache import Cache
from src.config import Community, Config, ContentTypes, GeneralSettings, TelegramSettings, VKSettings
from src.logger import configure_logging, redact_secrets
from src.models import Attachment, Post
from src.pipeline import _resolve_owner_id, process_communities
from src.tg_client import TelegramClient
from src.version import get_version


class LoggingTests(unittest.TestCase):
    def test_configure_logging_is_idempotent_and_keeps_file_log_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "poster.log"
            settings = GeneralSettings(log_file=str(log_path), log_level="DEBUG")

            logger = configure_logging(settings)
            logger = configure_logging(settings)

            self.assertEqual(len(logger.handlers), 2)
            self.assertFalse(logger.propagate)

            file_levels = {handler.level for handler in logger.handlers if getattr(handler, "baseFilename", None)}
            self.assertEqual(file_levels, {logging.INFO})

            stream_levels = {
                handler.level
                for handler in logger.handlers
                if not getattr(handler, "baseFilename", None)
            }
            self.assertEqual(stream_levels, {logging.WARNING})

            for handler in logger.handlers:
                if not getattr(handler, "baseFilename", None):
                    handler.setLevel(logging.CRITICAL)

            logger.debug("скрытый debug")
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logger.exception("Короткая ошибка")

            for handler in logger.handlers:
                flush = getattr(handler, "flush", None)
                if flush:
                    flush()

            content = log_path.read_text(encoding="utf-8")
            self.assertIn("Короткая ошибка", content)
            self.assertNotIn("скрытый debug", content)
            self.assertNotIn("Traceback", content)
            self.assertNotIn("RuntimeError: boom", content)


class WebConfigTests(unittest.TestCase):
    def test_save_config_preserves_log_retention_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            payload = web.SaveRequest(
                general=web.GeneralModel(
                    cron="*/15 * * * *",
                    log_retention_days=9,
                ),
                telegram=web.TelegramModel(channel_id="@channel"),
                communities=[],
            )

            with patch.object(web, "CONFIG_PATH", config_path):
                asyncio.run(web.save_config(payload))
                saved = config_path.read_text(encoding="utf-8")

            self.assertIn("log_retention_days: 9", saved)

    def test_load_ui_config_uses_runtime_defaults_when_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"

            with patch.object(web, "CONFIG_PATH", config_path):
                data = web._load_ui_config()

            self.assertEqual(data["general"]["cache_file"], "data/cache.json")
            self.assertEqual(data["general"]["log_file"], "data/logs/poster.log")
            self.assertEqual(data["communities"], [])


class VersionTests(unittest.TestCase):
    def test_get_version_reads_version_file(self) -> None:
        self.assertRegex(get_version(), r"^\d+\.\d+\.\d+$")

    def test_dockerfile_copies_version_file(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY VERSION ./", dockerfile)

    def test_dockerfile_does_not_copy_dev_config_into_runtime_image(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertNotIn("COPY config ./config", dockerfile)


class EntrypointTests(unittest.TestCase):
    def test_entrypoint_seeds_runtime_config_without_example_copy(self) -> None:
        entrypoint = Path("entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn('CONFIG_PATH="${CONFIG_PATH:-data/config.yaml}"', entrypoint)
        self.assertNotIn("DATA_EXAMPLE_CONFIG", entrypoint)
        self.assertNotIn("config.example.yaml", entrypoint)

    def test_entrypoint_traps_shutdown_and_waits_for_children(self) -> None:
        entrypoint = Path("entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn("trap", entrypoint)
        self.assertIn("wait", entrypoint)


class DataDirectoryDefaultsTests(unittest.TestCase):
    def test_general_settings_use_data_directory_defaults(self) -> None:
        settings = GeneralSettings()

        self.assertEqual(settings.cache_file, "data/cache.json")
        self.assertEqual(settings.log_file, "data/logs/poster.log")

    def test_web_models_use_data_directory_defaults(self) -> None:
        general = web.GeneralModel(cron="*/15 * * * *")

        self.assertEqual(general.cache_file, "data/cache.json")
        self.assertEqual(general.log_file, "data/logs/poster.log")


class AvatarCacheTests(unittest.TestCase):
    def test_community_info_uses_cached_avatar_when_refresh_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            avatar_cache_path = Path(tmpdir) / "avatars.json"
            cache_payload = {
                "-123": {
                    "photo": "https://example.com/avatar.jpg",
                    "name": "Test Club",
                    "fetched_at": 1_700_000_000,
                }
            }
            avatar_cache_path.write_text(json.dumps(cache_payload), encoding="utf-8")

            cfg = Config(
                general=GeneralSettings(refresh_avatars=False),
                vk=VKSettings(token="token"),
                telegram=TelegramSettings(),
                communities=[],
            )

            with patch.object(web, "AVATAR_CACHE", avatar_cache_path):
                with patch.object(web, "load_config", return_value=cfg):
                    with patch.object(web, "_fetch_vk_info", side_effect=AssertionError("VK should not be called")):
                        result = asyncio.run(web.community_info("club123"))

            self.assertEqual(result["name"], "Test Club")
            self.assertEqual(result["photo"], "https://example.com/avatar.jpg")


class CountingCache(Cache):
    def __init__(self, path: str):
        self.persist_count = 0
        super().__init__(path)

    def _persist(self) -> None:
        self.persist_count += 1
        super()._persist()


class FakeVKClient:
    def resolve_screen_name(self, screen_name: str) -> tuple[str, int]:
        return ("group", 123)

    def fetch_posts(self, owner_id: int, count: int = 10, offset: int = 0) -> list[Post]:
        return [
            Post(id=10, owner_id=owner_id, date=200, text="hello"),
        ]


class FakeTGClient:
    def __init__(self) -> None:
        self.sent_posts: list[int] = []

    def send_post(self, post: Post, allowed: ContentTypes) -> None:
        self.sent_posts.append(post.id)


class CachePersistenceTests(unittest.TestCase):
    def test_pipeline_flushes_cache_once_per_community(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CountingCache(str(Path(tmpdir) / "cache.json"))
            config = Config(
                general=GeneralSettings(posts_limit=10),
                vk=VKSettings(token="token"),
                telegram=TelegramSettings(bot_token="token", channel_id="@channel"),
                communities=[Community(id="club123", name="Club 123")],
            )
            tg_client = FakeTGClient()

            process_communities(config, FakeVKClient(), tg_client, cache)

            self.assertEqual(tg_client.sent_posts, [10])
            self.assertEqual(cache.persist_count, 1)


class CapturingTelegramClient(TelegramClient):
    def __init__(self) -> None:
        super().__init__("token", "@channel")
        self.calls: list[tuple[str, dict, bool]] = []

    def _post_with_retry(self, method: str, data: dict, json_mode: bool = False) -> None:
        self.calls.append((method, data, json_mode))


class TelegramCaptionTests(unittest.TestCase):
    def test_single_photo_long_caption_is_truncated_into_one_photo_message(self) -> None:
        client = CapturingTelegramClient()
        post = Post(
            id=7,
            owner_id=-123,
            text=" ".join(f"word{i:03d}" for i in range(200)),
            attachments=[Attachment(type="photo", url="https://example.com/photo.jpg")],
        )

        client.send_post(post, ContentTypes())

        self.assertEqual([call[0] for call in client.calls], ["sendPhoto"])
        _, data, _ = client.calls[0]
        caption = data["caption"]
        continuation = "...\n\n<b>Продолжение текста читайте в источнике.</b>"

        self.assertLessEqual(len(caption), 1024)
        self.assertTrue(caption.endswith(continuation))
        self.assertEqual(data["parse_mode"], "HTML")
        self.assertIn("reply_markup", data)
        self.assertRegex(caption.removesuffix(continuation).rsplit(" ", 1)[-1], r"^word\d{3}$")


def _fake_vk_token(secret: str) -> str:
    """Assemble a fake VK token at runtime so no token-shaped literal is committed."""
    return ".".join(["vk1", "a", secret])


def _fake_bot_token(secret: str) -> str:
    """Assemble a fake Telegram bot token at runtime (no token-shaped literal in source)."""
    return ":".join(["123456789", secret])


class SecretRedactionTests(unittest.TestCase):
    def test_redact_url_access_token(self) -> None:
        token = _fake_vk_token("SECRETTOKENVALUE123456")
        text = f"url /method/utils.resolveScreenName?screen_name=urenadm&access_token={token}&v=5.199"

        result = redact_secrets(text)

        self.assertNotIn(token, result)
        self.assertIn("access_token=<redacted>", result)
        self.assertIn("v=5.199", result)

    def test_redact_vk_error_payload_param(self) -> None:
        result = redact_secrets("{'key': 'oauth', 'value': 'secret-oauth-value'}")

        self.assertNotIn("secret-oauth-value", result)
        self.assertIn("'value': '<redacted>'", result)

    def test_redact_telegram_bot_token_in_url(self) -> None:
        token = _fake_bot_token("A" * 35)
        text = f"https://api.telegram.org/bot{token}/sendMessage"

        result = redact_secrets(text)

        self.assertNotIn(token, result)
        self.assertNotIn("api.telegram.org/bot" + token, result)

    def test_file_log_does_not_contain_secret(self) -> None:
        secret = "SUPERSECRETTOKEN123"
        token = _fake_vk_token(secret)
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "poster.log"
            settings = GeneralSettings(log_file=str(log_path), log_level="INFO")
            logger = configure_logging(settings)
            for handler in logger.handlers:
                if not getattr(handler, "baseFilename", None):
                    handler.setLevel(logging.CRITICAL)

            logger.error(
                "Failed to resolve VK community id: %s",
                f"HTTPSConnectionPool url /method/utils.resolveScreenName?screen_name=x&access_token={token}&v=5.199",
            )
            for handler in logger.handlers:
                flush = getattr(handler, "flush", None)
                if flush:
                    flush()

            content = log_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, content)
            self.assertIn("<redacted>", content)

    def test_api_logs_redacts_existing_secrets(self) -> None:
        secret = "OLDSECRETTOKEN99"
        token = _fake_vk_token(secret)
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "poster.log"
            log_path.write_text(
                f"2026-01-01 00:00:00 [ERROR] url .../x?access_token={token}&v=5.199\n",
                encoding="utf-8",
            )
            cfg = Config(
                general=GeneralSettings(log_file=str(log_path)),
                vk=VKSettings(),
                telegram=TelegramSettings(),
                communities=[],
            )

            with patch.object(web, "load_config", return_value=cfg):
                result = asyncio.run(web.get_logs(lines=10))

            joined = "".join(result["lines"])
            self.assertNotIn(secret, joined)
            self.assertIn("<redacted>", joined)


class VKClientErrorSanitizationTests(unittest.TestCase):
    def test_request_error_does_not_leak_token(self) -> None:
        import requests

        from src.vk_client import VKClient

        secret = "TOPSEKRETTOKEN"
        token = _fake_vk_token(secret)

        class BoomSession:
            def get(self, *args, **kwargs):
                raise requests.ConnectionError(
                    f"HTTPSConnectionPool url /method/wall.get?access_token={token}&v=5.199"
                )

        client = VKClient(token)
        client.session = BoomSession()

        with self.assertRaises(RuntimeError) as ctx:
            client._request("wall.get", {"access_token": token}, 5)

        message = str(ctx.exception)
        self.assertNotIn(secret, message)
        self.assertIn("VK request failed (wall.get)", message)


class CountingVKClient:
    def __init__(self) -> None:
        self.resolve_calls = 0
        self.fetched_owners: list[int] = []

    def resolve_screen_name(self, screen_name: str) -> tuple[str, int]:
        self.resolve_calls += 1
        return ("group", 123)

    def fetch_posts(self, owner_id: int, count: int = 10, offset: int = 0) -> list[Post]:
        self.fetched_owners.append(owner_id)
        return [Post(id=1, owner_id=owner_id, date=100, text="hi")]


class OwnerIdCacheTests(unittest.TestCase):
    def _config(self) -> Config:
        return Config(
            general=GeneralSettings(posts_limit=10),
            vk=VKSettings(token="token"),
            telegram=TelegramSettings(bot_token="token", channel_id="@channel"),
            communities=[Community(id="screenname", name="Screen")],
        )

    def test_screen_name_resolved_once_then_served_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = str(Path(tmpdir) / "cache.json")

            vk1 = CountingVKClient()
            process_communities(self._config(), vk1, FakeTGClient(), Cache(cache_path))
            self.assertEqual(vk1.resolve_calls, 1)
            self.assertEqual(vk1.fetched_owners, [-123])

            vk2 = CountingVKClient()
            process_communities(self._config(), vk2, FakeTGClient(), Cache(cache_path))
            self.assertEqual(vk2.resolve_calls, 0)
            self.assertEqual(vk2.fetched_owners, [-123])

    def test_local_id_does_not_use_api_or_cache(self) -> None:
        vk = CountingVKClient()
        cache = Cache(str(Path(tempfile.mkdtemp()) / "cache.json"))

        owner_id = _resolve_owner_id("club123", vk, cache)

        self.assertEqual(owner_id, -123)
        self.assertEqual(vk.resolve_calls, 0)


class FakeHTTPResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


class VKClientRequestTests(unittest.TestCase):
    def _make_client(self, responses):
        from src.vk_client import VKClient

        client = VKClient("token")
        state = {"n": 0}

        def fake_get(url, params=None, timeout=None):
            index = min(state["n"], len(responses) - 1)
            state["n"] += 1
            item = responses[index]
            if isinstance(item, Exception):
                raise item
            return item

        client.session.get = fake_get
        return client, state

    def test_retries_rate_limit_then_succeeds(self) -> None:
        client, state = self._make_client(
            [
                FakeHTTPResponse({"error": {"error_code": 6, "error_msg": "Too many requests per second"}}),
                FakeHTTPResponse({"response": {"items": []}}),
            ]
        )

        with patch("src.vk_client.time.sleep") as mock_sleep:
            payload = client._request("wall.get", {}, 5)

        self.assertEqual(payload["response"]["items"], [])
        self.assertEqual(state["n"], 2)
        self.assertTrue(mock_sleep.called)

    def test_persistent_rate_limit_raises_after_retries(self) -> None:
        from src import vk_client as vk_module

        client, state = self._make_client(
            [FakeHTTPResponse({"error": {"error_code": 6, "error_msg": "Too many"}})]
        )

        with patch("src.vk_client.time.sleep"):
            with self.assertRaises(RuntimeError):
                client._request("wall.get", {}, 5)

        self.assertEqual(state["n"], vk_module.VK_MAX_RETRIES + 1)

    def test_non_retryable_error_is_not_retried(self) -> None:
        client, state = self._make_client(
            [FakeHTTPResponse({"error": {"error_code": 113, "error_msg": "Invalid user id"}})]
        )

        with patch("src.vk_client.time.sleep"):
            with self.assertRaises(RuntimeError):
                client._request("wall.get", {}, 5)

        self.assertEqual(state["n"], 1)

    def test_network_error_is_retried_and_sanitized(self) -> None:
        import requests

        client, state = self._make_client(
            [
                requests.ConnectionError("url https://api.vk.com/method/wall.get?access_token=vk1.a.SECRET"),
                FakeHTTPResponse({"response": {"items": []}}),
            ]
        )

        with patch("src.vk_client.time.sleep"):
            payload = client._request("wall.get", {}, 5)

        self.assertEqual(state["n"], 2)
        self.assertIn("response", payload)

    def test_throttle_sleeps_between_calls(self) -> None:
        client, state = self._make_client([FakeHTTPResponse({"response": {"items": []}})])

        with patch("src.vk_client.time.sleep") as mock_sleep:
            client._request("wall.get", {}, 5)
            client._request("wall.get", {}, 5)

        self.assertEqual(state["n"], 2)
        self.assertTrue(any(call.args and call.args[0] > 0 for call in mock_sleep.call_args_list))


if __name__ == "__main__":
    unittest.main()
