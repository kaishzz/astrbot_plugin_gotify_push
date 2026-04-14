import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="gotify-plugin-tests-"))


def install_stub_modules():
    for module_name in list(sys.modules):
        if module_name == "main" or module_name.startswith(("astrbot", "gotify")):
            sys.modules.pop(module_name, None)

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")
    message_module = types.ModuleType("astrbot.core.message.message_event_result")
    path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
    gotify_module = types.ModuleType("gotify")
    gotify_response_module = types.ModuleType("gotify.response_types")

    class DummyLogger:
        def __init__(self):
            self.records = {"info": [], "warning": [], "error": []}

        def info(self, message, *args, **kwargs):
            self.records["info"].append(message)

        def warning(self, message, *args, **kwargs):
            self.records["warning"].append(message)

        def error(self, message, *args, **kwargs):
            self.records["error"].append(message)

        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class DummyConfig(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    class DummyEvent:
        def __init__(self, message_str="", admin=True):
            self.message_str = message_str
            self._admin = admin

        def is_admin(self):
            return self._admin

        def plain_result(self, value):
            return value

    class DummyFilter:
        @staticmethod
        def command(_name):
            def decorator(func):
                return func

            return decorator

    class DummyContext:
        def __init__(self):
            self.sent_messages = []

        async def send_message(self, umo, message):
            self.sent_messages.append((umo, message))

    class DummyStar:
        def __init__(self, context):
            self.context = context
            self.name = "astrbot_plugin_gotify_push"

        async def get_kv_data(self, _key, default=None):
            return default

    class DummyMessageChain:
        def __init__(self):
            self.content = None

        def message(self, content):
            self.content = content
            return content

    class DummyAsyncGotify:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def get_applications(self):
            return []

        async def stream(self):
            if False:
                yield {}

    def register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    api_module.AstrBotConfig = DummyConfig
    api_module.logger = DummyLogger()
    event_module.AstrMessageEvent = DummyEvent
    event_module.filter = DummyFilter()
    star_module.Context = DummyContext
    star_module.Star = DummyStar
    star_module.register = register
    message_module.MessageChain = DummyMessageChain
    path_module.get_astrbot_data_path = lambda: TEST_DATA_ROOT
    gotify_module.AsyncGotify = DummyAsyncGotify
    gotify_response_module.Message = dict

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module
    sys.modules["astrbot.core.message.message_event_result"] = message_module
    sys.modules["astrbot.core.utils.astrbot_path"] = path_module
    sys.modules["gotify"] = gotify_module
    sys.modules["gotify.response_types"] = gotify_response_module


install_stub_modules()
plugin_module = importlib.import_module("main")


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_stub_modules()
        global plugin_module
        plugin_module = importlib.import_module("main")
        plugin_module = importlib.reload(plugin_module)
        self.context = plugin_module.Context()
        self.config = plugin_module.AstrBotConfig(
            {
                "server": "https://gotify.example.com/",
                "client_token": "client-token",
                "rate_limit_window_seconds": 60,
                "rate_limit_max_messages": 1,
            }
        )

    def create_plugin(self, config=None):
        if config is None:
            plugin_config = self.config
        else:
            plugin_config = plugin_module.AstrBotConfig(config)
        return plugin_module.MyPlugin(self.context, plugin_config)

    async def test_mutate_subscriptions_rolls_back_when_save_fails(self):
        plugin = self.create_plugin()

        async def fail_save():
            raise OSError("disk full")

        plugin.save_subscriptions_locked = fail_save

        with self.assertRaises(OSError):
            await plugin.add_subscription_tokens("umo-1", ["token-a"])

        self.assertEqual(plugin.umo_app_subscriptions, {})

    async def test_cleanup_deleted_subscriptions_removes_stale_tokens(self):
        plugin = self.create_plugin()
        plugin.apps_by_token = {
            "token-a": ("1", {"id": 1, "token": "token-a", "name": "app-a"})
        }
        plugin.umo_app_subscriptions = {
            "umo-1": {"token-a", "token-b"},
            "umo-2": {"token-b"},
        }

        removed_count = await plugin.cleanup_deleted_subscriptions()

        self.assertEqual(removed_count, 2)
        self.assertEqual(plugin.umo_app_subscriptions, {"umo-1": {"token-a"}})

    async def test_clear_umo_subscriptions_removes_whole_entry(self):
        plugin = self.create_plugin()
        plugin.umo_app_subscriptions = {"umo-1": {"token-a", "token-b"}}

        removed = await plugin.clear_umo_subscriptions("umo-1")

        self.assertTrue(removed)
        self.assertEqual(plugin.umo_app_subscriptions, {})

    async def test_handle_message_applies_rate_limit(self):
        plugin = self.create_plugin()
        plugin.cache_app = {"1": {"id": 1, "name": "app-a", "token": "token-a"}}
        plugin.umo_app_subscriptions = {"umo-1": {"token-a"}}

        await plugin.handle_message({"appid": 1, "title": "hello", "message": "world"})
        await plugin.handle_message({"appid": 1, "title": "hello-2", "message": "world-2"})

        self.assertEqual(len(self.context.sent_messages), 1)
        self.assertIn("app-a", self.context.sent_messages[0][1])

    async def test_initialize_skips_with_info_log_when_config_missing(self):
        plugin = self.create_plugin({"client_token": "client-token"})

        await plugin.initialize()

        self.assertIsNone(plugin.gotify)
        self.assertIsNone(plugin.listen_task)
        self.assertIsNone(plugin.cleanup_task)
        self.assertEqual(plugin_module.logger.records["error"], [])
        self.assertIn(
            "Gotify 插件未启用: server 尚未配置，已跳过初始化",
            plugin_module.logger.records["info"],
        )

    def test_ensure_runtime_ready_logs_error_for_invalid_server(self):
        plugin = self.create_plugin(
            {
                "server": "gotify.example.com",
                "client_token": "client-token",
            }
        )

        ready = plugin.ensure_runtime_ready()

        self.assertFalse(ready)
        self.assertIn(
            "Gotify 插件初始化失败: server 必须以 http:// 或 https:// 开头",
            plugin_module.logger.records["error"],
        )

    async def test_gotify_add_returns_config_hint_when_runtime_not_ready(self):
        plugin = self.create_plugin({"client_token": "client-token"})
        event = plugin_module.AstrMessageEvent("/gotify_add umo-1 app-a")

        results = [item async for item in plugin.gotify_add(event)]

        self.assertEqual(
            results,
            ["插件暂不可用，请先完成配置: server 尚未配置"],
        )

    def test_parse_command_args_strips_alias(self):
        event = plugin_module.AstrMessageEvent("/gotify_add umo-1 app-a")

        args = plugin_module.MyPlugin.parse_command_args(event)

        self.assertEqual(args, ["umo-1", "app-a"])

    def test_get_subscriptions_file_path_accepts_string_data_root(self):
        plugin = self.create_plugin()

        original_get_path = plugin_module.get_astrbot_data_path
        plugin_module.get_astrbot_data_path = lambda: str(TEST_DATA_ROOT)
        try:
            result = plugin.get_subscriptions_file_path()
        finally:
            plugin_module.get_astrbot_data_path = original_get_path

        self.assertIsInstance(result, Path)
        self.assertEqual(result.name, plugin.STORAGE_FILENAME)

    def test_get_subscriptions_file_path_accepts_pathlike_data_root(self):
        plugin = self.create_plugin()

        class DummyPathLike:
            def __fspath__(self):
                return str(TEST_DATA_ROOT)

        original_get_path = plugin_module.get_astrbot_data_path
        plugin_module.get_astrbot_data_path = lambda: DummyPathLike()
        try:
            result = plugin.get_subscriptions_file_path()
        finally:
            plugin_module.get_astrbot_data_path = original_get_path

        self.assertIsInstance(result, Path)
        self.assertEqual(result.parent.name, "astrbot_plugin_gotify_push")

    def test_prune_runtime_caches_clears_stale_entries(self):
        plugin = self.create_plugin()
        now = plugin_module.time.monotonic()
        plugin.recent_message_fingerprints = {
            "old": now - 120,
            "new": now,
        }
        plugin.delivery_history["umo-old"].extend([now - 120])
        plugin.delivery_history["umo-new"].extend([now])

        plugin.prune_runtime_caches()

        self.assertEqual(plugin.recent_message_fingerprints, {"new": now})
        self.assertNotIn("umo-old", plugin.delivery_history)
        self.assertIn("umo-new", plugin.delivery_history)


if __name__ == "__main__":
    unittest.main()
