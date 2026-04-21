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
from src.logger import configure_logging
from src.models import Post
from src.pipeline import process_communities


class LoggingTests(unittest.TestCase):
    def test_configure_logging_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = GeneralSettings(
                log_file=str(Path(tmpdir) / "poster.log"),
                log_level="INFO",
            )

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


if __name__ == "__main__":
    unittest.main()
