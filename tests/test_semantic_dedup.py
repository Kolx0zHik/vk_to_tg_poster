import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.cache import Cache
from src.config import (
    Community,
    Config,
    GeneralSettings,
    LLMSettings,
    SemanticDedupSettings,
    TelegramSettings,
    VKSettings,
    config_to_dict,
    parse_config_dict,
)
from src.dedup import DedupError, DedupResult, SemanticDedup, _extract_json
from src.envfile import env_file_path, load_env_file
from src.models import Post
from src.pipeline import process_communities


class EnvFileTests(unittest.TestCase):
    def test_load_env_file_sets_new_keys_and_keeps_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = Path(tmpdir) / ".env"
            env.write_text(
                "VK_API_TOKEN=vtoken\n"
                "TELEGRAM_BOT_TOKEN=tgbot\n"
                "# комментарий\n"
                "JUST=1\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "keepme"}, clear=True):
                load_env_file(str(Path(tmpdir) / "config.yaml"))

                self.assertEqual(os.environ["VK_API_TOKEN"], "vtoken")
                self.assertEqual(os.environ["JUST"], "1")
                self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "keepme")

    def test_missing_env_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {}, clear=True):
                load_env_file(str(Path(tmpdir) / "config.yaml"))
            self.assertNotIn("VK_API_TOKEN", os.environ)

    def test_env_file_path_is_next_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = str(Path(tmpdir) / "nested" / "config.yaml")
            with patch.dict(os.environ, {"CONFIG_PATH": "/tmp/other/config.yaml"}, clear=False):
                self.assertEqual(env_file_path(config_path), Path(tmpdir) / "nested" / ".env")

    def test_env_file_path_falls_back_to_config_path_env(self) -> None:
        with patch.dict(os.environ, {"CONFIG_PATH": "/some/where/config.yaml"}, clear=False):
            self.assertEqual(env_file_path(None), Path("/some/where/.env"))


class ConfigSecretsAndLlmTests(unittest.TestCase):
    def test_parse_ignores_legacy_tokens_and_reads_llm(self) -> None:
        cfg = parse_config_dict(
            {
                "general": {"semantic_dedup": {"enabled": True, "window_days": 7}},
                "vk": {"token": "old-vk"},
                "telegram": {"channel_id": "@ch", "bot_token": "old-tg"},
                "llm": {"base_url": "https://api.example.com/v1", "model": "model-x"},
                "communities": [{"id": "club1", "name": "One"}],
            },
            require_tokens=False,
            require_channel=False,
        )

        self.assertEqual(cfg.telegram.channel_id, "@ch")
        self.assertEqual(cfg.llm.base_url, "https://api.example.com/v1")
        self.assertEqual(cfg.llm.model, "model-x")
        self.assertTrue(cfg.general.semantic_dedup.enabled)
        self.assertEqual(cfg.general.semantic_dedup.window_days, 7)
        self.assertFalse(hasattr(cfg.vk, "token"))

    def test_semantic_dedup_defaults_disabled_with_4_day_window(self) -> None:
        cfg = parse_config_dict({}, require_tokens=False, require_channel=False)

        self.assertFalse(cfg.general.semantic_dedup.enabled)
        self.assertEqual(cfg.general.semantic_dedup.window_days, 4)

    def test_config_to_dict_has_no_secrets(self) -> None:
        cfg = Config(
            general=GeneralSettings(),
            vk=VKSettings(),
            telegram=TelegramSettings(channel_id="@ch"),
            communities=[],
        )

        data = config_to_dict(cfg)

        self.assertNotIn("token", data["vk"])
        self.assertNotIn("bot_token", data["telegram"])
        self.assertIn("semantic_dedup", data["general"])
        self.assertIn("llm", data)


class CachePublishedTextTests(unittest.TestCase):
    def _cache(self, tmpdir: str) -> Cache:
        return Cache(str(Path(tmpdir) / "cache.json"))

    def test_mark_published_keeps_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = self._cache(tmpdir)
            cache.record_post(-123, Post(id=1, owner_id=-123, date=100, text="первый пост"))
            cache.mark_published("-123_1")

            store = json.loads((Path(tmpdir) / "cache.json").read_text(encoding="utf-8"))
            self.assertEqual(store["posts"]["-123_1"]["status"], "published")
            self.assertEqual(store["posts"]["-123_1"]["text"], "первый пост")

    def test_published_candidates_lists_recent_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = self._cache(tmpdir)
            cache.record_post(-123, Post(id=2, owner_id=-123, date=200, text="новый"))
            cache.mark_published("-123_2")
            cache.record_post(-123, Post(id=1, owner_id=-123, date=100, text="старый"))
            cache.mark_failed("-123_1")

            candidates = cache.published_candidates(int(time.time()) - 86400)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["key"], "-123_2")
            self.assertEqual(candidates[0]["text"], "новый")

    def test_prune_clears_text_older_than_window(self) -> None:
        old_ts = int(time.time()) - 10 * 86400
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = self._cache(tmpdir)
            cache.record_post(-123, Post(id=1, owner_id=-123, date=100, text="старый"))
            cache.mark_published("-123_1")
            cache._store["posts"]["-123_1"]["ts"] = old_ts
            cache._persist()

            cleared = cache.prune_published_text(4)

            self.assertEqual(cleared, 1)
            self.assertNotIn("text", cache._store["posts"]["-123_1"])


class FakeLLMResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> dict:
        return self._payload


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class DedupClientTests(unittest.TestCase):
    @staticmethod
    def _client() -> SemanticDedup:
        return SemanticDedup("https://api.example.com/v1", "model-x", api_key="key")

    def test_check_parses_duplicate_answer(self) -> None:
        client = self._client()
        client.session.post = lambda url, **kw: FakeLLMResponse(
            200,
            _completion('{"is_duplicate":true,"reason":"Тот же инфоповод","matched_message_id":"17"}'),
        )

        result = client.check({"chat_id": "-1", "message_id": "9", "raw_text": "пост"}, [{"raw_text": "пост"}])

        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.matched_message_id, "17")

    def test_check_parses_fenced_json(self) -> None:
        client = self._client()
        client.session.post = lambda url, **kw: FakeLLMResponse(
            200,
            _completion('```json\n{"is_duplicate":false,"reason":"Разная тема","matched_message_id":""}\n```'),
        )

        result = client.check({}, [])

        self.assertFalse(result.is_duplicate)

    def test_retries_429_then_succeeds(self) -> None:
        client = self._client()
        calls: list[int] = []

        def fake_post(url, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return FakeLLMResponse(429, {})
            return FakeLLMResponse(200, _completion('{"is_duplicate":false,"reason":"Нет","matched_message_id":""}'))

        client.session.post = fake_post
        with patch("src.dedup.time.sleep"):
            result = client.check({}, [])

        self.assertFalse(result.is_duplicate)
        self.assertEqual(len(calls), 2)

    def test_unconfigured_client_raises(self) -> None:
        with self.assertRaises(DedupError):
            SemanticDedup("", "").check({}, [])

    def test_non_json_content_raises(self) -> None:
        client = self._client()
        client.session.post = lambda url, **kw: FakeLLMResponse(200, _completion("просто текст"))

        with self.assertRaises(DedupError):
            client.check({}, [])

    def test_extract_json_rejects_garbage(self) -> None:
        with self.assertRaises(DedupError):
            _extract_json("без единого json")


class PagedFakeVK:
    def __init__(self, posts: list[Post]) -> None:
        self.posts = posts

    def resolve_screen_name(self, screen_name: str) -> tuple[str, int]:
        return ("group", 123)

    def fetch_posts(self, owner_id: int, count: int = 10, offset: int = 0) -> list[Post]:
        return self.posts[offset : offset + count]


class RecordingTG:
    def __init__(self) -> None:
        self.sent_posts: list[int] = []

    def send_post(self, post: Post, allowed) -> None:
        self.sent_posts.append(post.id)


class PipelineDedupTests(unittest.TestCase):
    @staticmethod
    def _config() -> Config:
        return Config(
            general=GeneralSettings(
                posts_limit=10,
                semantic_dedup=SemanticDedupSettings(enabled=True, window_days=4),
            ),
            vk=VKSettings(),
            telegram=TelegramSettings(channel_id="@ch"),
            llm=LLMSettings(base_url="https://api.example.com", model="model-x"),
            communities=[Community(id="club123", name="Club")],
        )

    def _seed_candidate(self, cache: Cache) -> None:
        cache.record_post(-123, Post(id=1, owner_id=-123, date=10, text="тот же инфоповод"))
        cache.mark_published("-123_1")

    def test_duplicate_post_is_skipped(self) -> None:
        posts = [Post(id=2, owner_id=-123, date=20, text="новый инфоповод")]
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Cache(str(Path(tmpdir) / "cache.json"))
            self._seed_candidate(cache)

            tg = RecordingTG()
            with patch("src.pipeline.SemanticDedup") as mock_dedup_cls:
                mock_dedup_cls.return_value.check.return_value = DedupResult(
                    is_duplicate=True, reason="Дубль", matched_message_id="1"
                )
                process_communities(self._config(), PagedFakeVK(posts), tg, cache)

            self.assertEqual(tg.sent_posts, [])
            store = json.loads((Path(tmpdir) / "cache.json").read_text(encoding="utf-8"))
            self.assertEqual(store["posts"]["-123_2"]["status"], "skipped")

    def test_dedup_failure_still_publishes(self) -> None:
        posts = [Post(id=2, owner_id=-123, date=20, text="новый")]
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Cache(str(Path(tmpdir) / "cache.json"))
            self._seed_candidate(cache)

            tg = RecordingTG()
            with patch("src.pipeline.SemanticDedup") as mock_dedup_cls:
                mock_dedup_cls.return_value.check.side_effect = DedupError("LLM недоступен")
                process_communities(self._config(), PagedFakeVK(posts), tg, cache)

            self.assertEqual(tg.sent_posts, [2])

    def test_dedup_disabled_publishes_normally(self) -> None:
        config = Config(
            general=GeneralSettings(posts_limit=10, semantic_dedup=SemanticDedupSettings(enabled=False)),
            vk=VKSettings(),
            telegram=TelegramSettings(channel_id="@ch"),
            communities=[Community(id="club123", name="Club")],
        )
        posts = [Post(id=2, owner_id=-123, date=20, text="новый")]
        with tempfile.TemporaryDirectory() as tmpdir:
            tg = RecordingTG()
            process_communities(config, PagedFakeVK(posts), tg, Cache(str(Path(tmpdir) / "cache.json")))

            self.assertEqual(tg.sent_posts, [2])


if __name__ == "__main__":
    unittest.main()