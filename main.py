import asyncio
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple, TypeVar

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from gotify import AsyncGotify
from gotify.response_types import Message


ApplicationInfo = Dict[str, Any]
ApplicationMatch = Tuple[str, ApplicationInfo]
MutationResult = TypeVar("MutationResult")


@register(
    "astrbot_plugin_gotify_push",
    "kaish",
    "监听 Gotify 消息并推送",
    "1.1",
)
class MyPlugin(Star):
    STORAGE_FILENAME = "subscriptions.json"
    COMMAND_ALIASES = {"gotify_add", "gotify_del", "gotify_list", "gotify_clear"}

    DEFAULT_CLEANUP_INTERVAL_SECONDS = 600
    DEFAULT_RECONNECT_DELAY_SECONDS = 15
    DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60
    DEFAULT_RATE_LIMIT_MAX_MESSAGES = 20
    DEFAULT_DUPLICATE_WINDOW_SECONDS = 10
    DEFAULT_MAX_TITLE_LENGTH = 200
    DEFAULT_MAX_BODY_LENGTH = 2000

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.server = self.normalize_server_url(config.get("server"))
        self.token = self.normalize_text(config.get("client_token"))

        self.cleanup_interval_seconds = self.read_int_config(
            "cleanup_interval_seconds",
            self.DEFAULT_CLEANUP_INTERVAL_SECONDS,
            minimum=60,
        )
        self.reconnect_delay_seconds = self.read_int_config(
            "reconnect_delay_seconds",
            self.DEFAULT_RECONNECT_DELAY_SECONDS,
            minimum=3,
        )
        self.rate_limit_window_seconds = self.read_int_config(
            "rate_limit_window_seconds",
            self.DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
            minimum=1,
        )
        self.rate_limit_max_messages = self.read_int_config(
            "rate_limit_max_messages",
            self.DEFAULT_RATE_LIMIT_MAX_MESSAGES,
            minimum=1,
        )
        self.duplicate_window_seconds = self.read_int_config(
            "duplicate_window_seconds",
            self.DEFAULT_DUPLICATE_WINDOW_SECONDS,
            minimum=0,
        )
        self.max_title_length = self.read_int_config(
            "max_title_length",
            self.DEFAULT_MAX_TITLE_LENGTH,
            minimum=20,
        )
        self.max_body_length = self.read_int_config(
            "max_body_length",
            self.DEFAULT_MAX_BODY_LENGTH,
            minimum=100,
        )

        self.gotify: Optional[AsyncGotify] = None

        self.cache_app: Dict[str, ApplicationInfo] = {}
        self.apps_by_token: Dict[str, ApplicationMatch] = {}
        self.apps_by_name: Dict[str, List[ApplicationMatch]] = defaultdict(list)

        self.umo_app_subscriptions: Dict[str, Set[str]] = {}
        self.subscriptions_lock = asyncio.Lock()
        self.applications_refresh_lock = asyncio.Lock()

        self.delivery_history: Dict[str, Deque[float]] = defaultdict(deque)
        self.recent_message_fingerprints: Dict[str, float] = {}

        self.listen_task: Optional[asyncio.Task] = None
        self.cleanup_task: Optional[asyncio.Task] = None

    @staticmethod
    def normalize_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    @classmethod
    def normalize_server_url(cls, value: Any) -> str:
        server = cls.normalize_text(value)
        return server.rstrip("/")

    def read_int_config(self, key: str, default: int, minimum: int) -> int:
        value = self.config.get(key, default)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            logger.warning(f"配置 {key} 无效，已使用默认值 {default}")
            return default
        if parsed < minimum:
            logger.warning(f"配置 {key} 过小，已使用最小值 {minimum}")
            return minimum
        return parsed

    def get_runtime_config_issue(self) -> Optional[Tuple[str, str]]:
        if not self.server and not self.token:
            return "missing", "server 和 client_token 尚未配置"
        if not self.server:
            return "missing", "server 尚未配置"
        if not self.server.startswith(("http://", "https://")):
            return "invalid", "server 必须以 http:// 或 https:// 开头"
        if not self.token:
            return "missing", "client_token 尚未配置"
        return None

    def get_runtime_not_ready_message(self) -> Optional[str]:
        issue = self.get_runtime_config_issue()
        if not issue:
            return None

        _, reason = issue
        return f"插件暂不可用，请先完成配置: {reason}"

    def get_subscriptions_file_path(self) -> Path:
        plugin_name = getattr(self, "name", "astrbot_plugin_gotify_push")
        data_root = os.fspath(get_astrbot_data_path())
        return Path(
            os.path.join(
                data_root,
                "plugin_data",
                plugin_name,
                self.STORAGE_FILENAME,
            )
        )

    @staticmethod
    def read_json_file(file_path: Path) -> Dict[str, List[str]]:
        with file_path.open("r", encoding="utf-8") as file:
            return json.load(file)

    @staticmethod
    def write_json_file(file_path: Path, payload: Dict[str, List[str]]):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = file_path.with_suffix(file_path.suffix + ".tmp")

        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, file_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    @classmethod
    def normalize_subscription_payload(cls, raw_data: Any) -> Dict[str, Set[str]]:
        normalized: Dict[str, Set[str]] = {}
        if not isinstance(raw_data, dict):
            return normalized

        for raw_umo, raw_apps in raw_data.items():
            umo = str(raw_umo).strip()
            if not umo:
                continue

            if isinstance(raw_apps, list):
                apps_iterable = raw_apps
            elif isinstance(raw_apps, str):
                apps_iterable = [raw_apps]
            else:
                continue

            apps = {str(app).strip() for app in apps_iterable if str(app).strip()}
            if apps:
                normalized[umo] = apps

        return normalized

    async def load_subscriptions(self):
        storage_file = self.get_subscriptions_file_path()
        subscriptions: Dict[str, Set[str]] = {}

        if storage_file.exists():
            try:
                raw_data = await asyncio.to_thread(self.read_json_file, storage_file)
                subscriptions = self.normalize_subscription_payload(raw_data)
            except json.JSONDecodeError as exc:
                logger.error(f"订阅持久化文件格式错误: {storage_file}, {exc}")
            except Exception as exc:
                logger.error(f"读取订阅持久化文件失败: {storage_file}, {exc}")

        async with self.subscriptions_lock:
            self.umo_app_subscriptions = subscriptions

    async def save_subscriptions_locked(self):
        payload = {
            umo: sorted(apps)
            for umo, apps in self.umo_app_subscriptions.items()
            if apps
        }
        await asyncio.to_thread(
            self.write_json_file,
            self.get_subscriptions_file_path(),
            payload,
        )

    async def mutate_subscriptions(
        self,
        mutation: Callable[[Dict[str, Set[str]]], Tuple[bool, MutationResult]],
    ) -> MutationResult:
        async with self.subscriptions_lock:
            snapshot = {
                umo: set(apps)
                for umo, apps in self.umo_app_subscriptions.items()
            }
            changed, result = mutation(self.umo_app_subscriptions)
            if not changed:
                return result

            try:
                await self.save_subscriptions_locked()
            except Exception:
                self.umo_app_subscriptions = snapshot
                raise

            return result

    def ensure_runtime_ready(self) -> bool:
        issue = self.get_runtime_config_issue()
        if not issue:
            return True

        issue_type, reason = issue
        if issue_type == "missing":
            logger.info(f"Gotify 插件未启用: {reason}，已跳过初始化")
        else:
            logger.error(f"Gotify 插件初始化失败: {reason}")
        return False

    async def update_applications(self) -> bool:
        if not self.gotify:
            return False

        async with self.applications_refresh_lock:
            try:
                applications = await self.gotify.get_applications()
            except Exception as exc:
                logger.error(f"刷新 Gotify 应用列表失败: {exc}")
                return False

            cache_app: Dict[str, ApplicationInfo] = {}
            apps_by_token: Dict[str, ApplicationMatch] = {}
            apps_by_name: Dict[str, List[ApplicationMatch]] = defaultdict(list)

            for app in applications:
                if not isinstance(app, dict) or "id" not in app:
                    continue

                app_id = str(app["id"])
                cache_app[app_id] = app

                token = self.normalize_text(app.get("token"))
                name = self.normalize_text(app.get("name"))
                match = (app_id, app)

                if token:
                    apps_by_token[token] = match
                if name:
                    apps_by_name[name].append(match)

            self.cache_app = cache_app
            self.apps_by_token = apps_by_token
            self.apps_by_name = apps_by_name
            return True

    @classmethod
    def format_app_display(cls, app_info: ApplicationInfo, fallback: str = "") -> str:
        app_name = cls.normalize_text(app_info.get("name"))
        app_token = cls.normalize_text(app_info.get("token"))

        if app_name and app_token:
            return f"{app_name} (token: {app_token})"
        if app_name:
            return app_name
        if app_token:
            return f"token: {app_token}"
        return cls.normalize_text(fallback)

    def find_application_matches_in_cache(
        self, identifier: str
    ) -> Tuple[List[ApplicationMatch], str]:
        normalized_identifier = self.normalize_text(identifier)
        if not normalized_identifier:
            return [], ""

        token_match = self.apps_by_token.get(normalized_identifier)
        if token_match:
            return [token_match], "token"

        name_matches = self.apps_by_name.get(normalized_identifier, [])
        if name_matches:
            return list(name_matches), "name"

        return [], ""

    async def resolve_application_matches(
        self, identifier: str
    ) -> Tuple[List[ApplicationMatch], str]:
        matches, matched_by = self.find_application_matches_in_cache(identifier)
        if matches:
            return matches, matched_by

        if await self.update_applications():
            return self.find_application_matches_in_cache(identifier)
        return [], ""

    def resolve_application_in_cache(
        self, identifier: str
    ) -> Tuple[Optional[str], Optional[ApplicationInfo], str]:
        matches, matched_by = self.find_application_matches_in_cache(identifier)
        if not matches:
            return None, None, ""
        app_id, app_info = matches[0]
        return app_id, app_info, matched_by

    def format_subscription_values(self, values: List[str]) -> List[str]:
        formatted_values: List[str] = []
        seen = set()

        for value in values:
            _, app_info, _ = self.resolve_application_in_cache(value)
            display = value
            if app_info:
                display = self.format_app_display(app_info, fallback=value)
            if display in seen:
                continue
            seen.add(display)
            formatted_values.append(display)

        return formatted_values

    async def cleanup_deleted_subscriptions(self) -> int:
        known_tokens = set(self.apps_by_token.keys())
        if not known_tokens:
            return 0

        def mutation(
            subscriptions: Dict[str, Set[str]],
        ) -> Tuple[bool, int]:
            removed_count = 0

            for umo, app_tokens in list(subscriptions.items()):
                remaining = {token for token in app_tokens if token in known_tokens}
                removed_count += len(app_tokens) - len(remaining)
                if remaining:
                    subscriptions[umo] = remaining
                else:
                    del subscriptions[umo]

            return removed_count > 0, removed_count

        return await self.mutate_subscriptions(mutation)

    def prune_runtime_caches(self):
        now = time.monotonic()

        if self.duplicate_window_seconds <= 0:
            self.recent_message_fingerprints.clear()
        else:
            duplicate_expire_before = now - self.duplicate_window_seconds
            stale_fingerprints = [
                key
                for key, timestamp in self.recent_message_fingerprints.items()
                if timestamp < duplicate_expire_before
            ]
            for key in stale_fingerprints:
                del self.recent_message_fingerprints[key]

        rate_limit_expire_before = now - self.rate_limit_window_seconds
        stale_umos = []
        for umo, history in self.delivery_history.items():
            while history and history[0] < rate_limit_expire_before:
                history.popleft()
            if not history:
                stale_umos.append(umo)

        for umo in stale_umos:
            del self.delivery_history[umo]

    async def run_periodic_cleanup(self):
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval_seconds)
                try:
                    self.prune_runtime_caches()
                    if not await self.update_applications():
                        continue
                    removed_count = await self.cleanup_deleted_subscriptions()
                    if removed_count > 0:
                        logger.info(f"定时清理完成，已自动清理 {removed_count} 条失效订阅")
                except Exception as exc:
                    logger.error(f"定时清理任务异常: {exc}")
        except asyncio.CancelledError:
            logger.info("定时清理任务已停止")
            raise

    @classmethod
    def truncate_text(cls, value: Any, max_length: int, fallback: str) -> str:
        text = cls.normalize_text(value)
        if not text:
            return fallback
        if len(text) <= max_length:
            return text
        return text[: max_length - 3].rstrip() + "..."

    def build_message_content(self, app_name: str, msg: Message) -> str:
        title = self.truncate_text(msg.get("title"), self.max_title_length, "无标题")
        body = self.truncate_text(msg.get("message"), self.max_body_length, "无内容")
        return f"--- Message ---\n应用：{app_name}\n标题：{title}\n内容：{body}"

    def build_message_fingerprint(self, app_id: str, msg: Message) -> str:
        title = self.normalize_text(msg.get("title"))
        body = self.normalize_text(msg.get("message"))
        return f"{app_id}|{title}|{body}"

    def is_duplicate_message(self, app_id: str, msg: Message) -> bool:
        if self.duplicate_window_seconds <= 0:
            return False

        self.prune_runtime_caches()
        fingerprint = self.build_message_fingerprint(app_id, msg)
        now = time.monotonic()
        last_seen = self.recent_message_fingerprints.get(fingerprint)
        if last_seen and now - last_seen < self.duplicate_window_seconds:
            return True

        self.recent_message_fingerprints[fingerprint] = now
        return False

    def consume_delivery_quota(self, umo: str) -> bool:
        self.prune_runtime_caches()
        history = self.delivery_history[umo]
        if len(history) >= self.rate_limit_max_messages:
            return False

        history.append(time.monotonic())
        return True

    @classmethod
    def build_app_identifiers(cls, app_info: ApplicationInfo) -> Set[str]:
        identifiers = set()
        app_name = cls.normalize_text(app_info.get("name"))
        app_token = cls.normalize_text(app_info.get("token"))

        if app_name:
            identifiers.add(app_name)
        if app_token:
            identifiers.add(app_token)

        return identifiers

    @classmethod
    def parse_command_args(cls, event: AstrMessageEvent) -> List[str]:
        message_str = cls.normalize_text(event.message_str)
        if not message_str:
            return []

        parts = message_str.split()
        if not parts:
            return []

        first = parts[0].lstrip("/")
        if first in cls.COMMAND_ALIASES:
            return parts[1:]
        return parts

    async def initialize(self):
        await self.load_subscriptions()
        if not self.ensure_runtime_ready():
            return

        self.gotify = AsyncGotify(base_url=self.server, client_token=self.token)

        if await self.update_applications():
            removed_count = await self.cleanup_deleted_subscriptions()
            if removed_count > 0:
                logger.info(f"启动时已自动清理 {removed_count} 条失效订阅")

        self.listen_task = asyncio.create_task(self.start_listen())
        self.cleanup_task = asyncio.create_task(self.run_periodic_cleanup())
        logger.info(
            f"插件初始化完成，已加载 {len(self.umo_app_subscriptions)} 个 UMO 订阅"
        )

    async def handle_message(self, msg: Message):
        raw_app_id = msg.get("appid")
        if raw_app_id is None:
            logger.info("Gotify 消息未携带 appid")
            return

        app_id = str(raw_app_id)
        app_info = self.cache_app.get(app_id)
        if not app_info:
            if not await self.update_applications():
                return
            app_info = self.cache_app.get(app_id)
            if not app_info:
                logger.info(f"appid {app_id} 不在应用列表中")
                return

        app_name = self.normalize_text(app_info.get("name"))
        if not app_name:
            logger.info(f"appid {app_id} 对应应用缺少 name")
            return

        if self.is_duplicate_message(app_id, msg):
            logger.warning(f"检测到重复推送，已跳过 appid={app_id}")
            return

        app_identifiers = self.build_app_identifiers(app_info)
        if not app_identifiers:
            logger.info(f"appid {app_id} 对应应用缺少可匹配标识")
            return

        async with self.subscriptions_lock:
            target_umos = [
                umo
                for umo, apps in self.umo_app_subscriptions.items()
                if apps.intersection(app_identifiers)
            ]

        if not target_umos:
            return

        message_content = self.build_message_content(app_name, msg)
        dropped_umos: List[str] = []

        for umo in target_umos:
            if not self.consume_delivery_quota(umo):
                dropped_umos.append(umo)
                continue
            try:
                send_msg = MessageChain().message(message_content)
                await self.context.send_message(umo, send_msg)
            except Exception as exc:
                logger.error(f"向 UMO {umo} 推送消息失败: {exc}")

        if dropped_umos:
            logger.warning(
                f"消息推送触发限流，已跳过 {len(dropped_umos)} 个 UMO: "
                + ", ".join(dropped_umos[:5])
            )

    async def start_listen(self):
        if not self.gotify:
            return

        while True:
            received = 0
            try:
                async for msg in self.gotify.stream():
                    received += 1
                    await self.handle_message(msg)
            except asyncio.CancelledError:
                logger.info("Gotify 监听任务已停止")
                raise
            except Exception as exc:
                logger.error(f"Gotify 连接断开，已收到消息数 {received}，{exc}")

            await asyncio.sleep(self.reconnect_delay_seconds)

    async def add_subscription_tokens(
        self, umo: str, tokens: List[str]
    ) -> Tuple[List[str], List[str], int]:
        def mutation(
            subscriptions: Dict[str, Set[str]],
        ) -> Tuple[bool, Tuple[List[str], List[str], int]]:
            apps = subscriptions.setdefault(umo, set())
            existed_tokens = [token for token in tokens if token in apps]
            new_tokens = [token for token in tokens if token not in apps]
            if new_tokens:
                apps.update(new_tokens)
                return True, (new_tokens, existed_tokens, len(apps))
            return False, (new_tokens, existed_tokens, len(apps))

        return await self.mutate_subscriptions(mutation)

    async def remove_subscription_tokens(
        self, umo: str, remove_candidates: Set[str]
    ) -> Tuple[bool, int]:
        def mutation(
            subscriptions: Dict[str, Set[str]],
        ) -> Tuple[bool, Tuple[bool, int]]:
            apps = subscriptions.get(umo)
            if not apps:
                return False, (False, 0)

            removed_tokens = [item for item in remove_candidates if item in apps]
            if not removed_tokens:
                return False, (True, 0)

            for removed_token in removed_tokens:
                apps.remove(removed_token)

            removed_all = False
            if not apps:
                del subscriptions[umo]
                removed_all = True

            return True, (removed_all, len(removed_tokens))

        return await self.mutate_subscriptions(mutation)

    async def clear_subscriptions(self):
        def mutation(
            subscriptions: Dict[str, Set[str]],
        ) -> Tuple[bool, None]:
            if not subscriptions:
                return False, None
            subscriptions.clear()
            return True, None

        await self.mutate_subscriptions(mutation)

    async def clear_umo_subscriptions(self, umo: str) -> bool:
        def mutation(
            subscriptions: Dict[str, Set[str]],
        ) -> Tuple[bool, bool]:
            if umo not in subscriptions:
                return False, False
            del subscriptions[umo]
            return True, True

        return await self.mutate_subscriptions(mutation)

    @filter.command("gotify_add")
    async def gotify_add(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return

        args = self.parse_command_args(event)
        if len(args) < 2:
            yield event.plain_result("用法: /gotify_add <umo> <app|token>")
            return

        umo = self.normalize_text(args[0])
        app = self.normalize_text(" ".join(args[1:]))
        if not umo or not app:
            yield event.plain_result("用法: /gotify_add <umo> <app|token>")
            return

        runtime_message = self.get_runtime_not_ready_message()
        if runtime_message:
            yield event.plain_result(runtime_message)
            return

        matched_apps, _ = await self.resolve_application_matches(app)
        if not matched_apps:
            yield event.plain_result("未找到应用，请填写 app name 或 app token")
            return

        token_display_map: Dict[str, str] = {}
        for _, app_info in matched_apps:
            token = self.normalize_text(app_info.get("token"))
            if token:
                token_display_map[token] = self.format_app_display(
                    app_info,
                    fallback=token,
                )

        target_tokens = sorted(token_display_map.keys())
        if not target_tokens:
            yield event.plain_result("匹配到的应用都未返回 token，无法添加订阅")
            return

        try:
            new_tokens, existed_tokens, app_count = await self.add_subscription_tokens(
                umo,
                target_tokens,
            )
        except Exception as exc:
            logger.error(f"保存订阅失败: {exc}")
            yield event.plain_result("保存订阅失败，请检查插件日志或磁盘权限")
            return

        if not new_tokens:
            if len(target_tokens) == 1:
                yield event.plain_result(
                    f"该应用已被添加: {umo} -> {token_display_map[target_tokens[0]]}"
                )
            else:
                yield event.plain_result(
                    f"该应用已被添加: {umo} -> {app}\n共 {len(target_tokens)} 个 token 已存在"
                )
            return

        if len(target_tokens) == 1:
            yield event.plain_result(
                f"添加成功: {umo} -> {token_display_map[target_tokens[0]]}\n"
                f"当前该 UMO 共监听 {app_count} 个应用"
            )
            return

        yield event.plain_result(
            f"添加成功: {umo} -> {app}\n"
            f"本次新增 {len(new_tokens)} 个 token，已存在 {len(existed_tokens)} 个\n"
            f"当前该 UMO 共监听 {app_count} 个应用"
        )

    @filter.command("gotify_del")
    async def gotify_del(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return

        args = self.parse_command_args(event)
        if not args:
            yield event.plain_result("用法: /gotify_del <umo> [app|token]")
            return

        umo = self.normalize_text(args[0])
        app = self.normalize_text(" ".join(args[1:])) if len(args) > 1 else ""
        if not umo:
            yield event.plain_result("用法: /gotify_del <umo> [app|token]")
            return

        if not app:
            async with self.subscriptions_lock:
                exists = umo in self.umo_app_subscriptions
            if not exists:
                yield event.plain_result(f"未找到 UMO: {umo}")
                return

            try:
                await self.clear_umo_subscriptions(umo)
            except Exception as exc:
                logger.error(f"删除订阅失败: {exc}")
                yield event.plain_result("删除订阅失败，请检查插件日志或磁盘权限")
                return

            yield event.plain_result(f"已删除 UMO {umo} 的全部订阅")
            return

        if await self.update_applications():
            try:
                await self.cleanup_deleted_subscriptions()
            except Exception as exc:
                logger.warning(f"自动清理失效订阅失败: {exc}")

        matched_apps, _ = self.find_application_matches_in_cache(app)
        remove_candidates = {app}
        remove_display = app

        for _, app_info in matched_apps:
            token = self.normalize_text(app_info.get("token"))
            if token:
                remove_candidates.add(token)

        if len(matched_apps) == 1:
            remove_display = self.format_app_display(matched_apps[0][1], fallback=app)

        try:
            removed_all, removed_token_count = await self.remove_subscription_tokens(
                umo,
                remove_candidates,
            )
        except Exception as exc:
            logger.error(f"删除订阅失败: {exc}")
            yield event.plain_result("删除订阅失败，请检查插件日志或磁盘权限")
            return

        if removed_token_count == 0:
            async with self.subscriptions_lock:
                exists = umo in self.umo_app_subscriptions
            if not exists:
                yield event.plain_result(f"未找到 UMO: {umo}")
            else:
                yield event.plain_result(f"UMO {umo} 未订阅应用: {remove_display}")
            return

        if removed_all:
            if removed_token_count > 1:
                yield event.plain_result(
                    f"已删除订阅: {umo} -> {remove_display}\n"
                    f"本次共删除 {removed_token_count} 个 token\n"
                    "该 UMO 已无任何订阅并自动移除"
                )
            else:
                yield event.plain_result(
                    f"已删除订阅: {umo} -> {remove_display}\n"
                    "该 UMO 已无任何订阅并自动移除"
                )
            return

        if removed_token_count > 1:
            yield event.plain_result(
                f"已删除订阅: {umo} -> {remove_display}\n"
                f"本次共删除 {removed_token_count} 个 token"
            )
            return

        yield event.plain_result(f"已删除订阅: {umo} -> {remove_display}")

    @filter.command("gotify_clear")
    async def gotify_clear(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return

        try:
            await self.clear_subscriptions()
        except Exception as exc:
            logger.error(f"清除订阅失败: {exc}")
            yield event.plain_result("清除订阅失败，请检查插件日志或磁盘权限")
            return

        yield event.plain_result("已清除全部订阅配置")

    @filter.command("gotify_list")
    async def gotify_list(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return

        args = self.parse_command_args(event)
        if len(args) > 1:
            yield event.plain_result("用法: /gotify_list [umo]")
            return

        removed_count = 0
        if await self.update_applications():
            try:
                removed_count = await self.cleanup_deleted_subscriptions()
            except Exception as exc:
                logger.warning(f"自动清理失效订阅失败: {exc}")

        async with self.subscriptions_lock:
            snapshot = {
                umo: sorted(apps)
                for umo, apps in self.umo_app_subscriptions.items()
                if apps
            }

        if not args:
            if not snapshot:
                yield event.plain_result("当前没有任何 UMO 订阅")
                return

            lines = ["当前全部 UMO 订阅:"]
            if removed_count > 0:
                lines.append(f"已自动清理失效订阅: {removed_count} 条")

            for idx, umo in enumerate(sorted(snapshot.keys()), start=1):
                lines.append(f"{idx}. UMO: {umo}")
                display_values = self.format_subscription_values(snapshot[umo])
                for app_idx, display in enumerate(display_values, start=1):
                    lines.append(f"  {app_idx}. {display}")

            yield event.plain_result("\n".join(lines))
            return

        umo = self.normalize_text(args[0])
        apps = snapshot.get(umo)
        if not apps:
            yield event.plain_result(f"未找到 UMO: {umo}")
            return

        lines = [f"UMO: {umo}"]
        if removed_count > 0:
            lines.append(f"已自动清理失效订阅: {removed_count} 条")
        lines.append("监听应用:")

        display_values = self.format_subscription_values(apps)
        for idx, display in enumerate(display_values, start=1):
            lines.append(f"{idx}. {display}")

        yield event.plain_result("\n".join(lines))

    async def close_gotify_client(self):
        if not self.gotify:
            return

        try:
            aclose = getattr(self.gotify, "aclose", None)
            close = getattr(self.gotify, "close", None)
            if callable(aclose):
                await aclose()
            elif callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        except Exception as exc:
            logger.warning(f"关闭 Gotify 客户端时发生异常: {exc}")
        finally:
            self.gotify = None

    async def terminate(self):
        tasks = []

        if self.listen_task and not self.listen_task.done():
            logger.info("Gotify 连接关闭")
            self.listen_task.cancel()
            tasks.append(self.listen_task)

        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            tasks.append(self.cleanup_task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self.prune_runtime_caches()
        await self.close_gotify_client()
