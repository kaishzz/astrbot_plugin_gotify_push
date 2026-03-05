from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from gotify import AsyncGotify
from gotify.response_types import Message
import asyncio
from typing import Dict, List, Optional, Set, Tuple
from astrbot.core.message.message_event_result import MessageChain


@register(
    "astrbot_plugin_gotify_push",
    "ksbjt",
    "监听 Gotify 消息并推送",
    "1.2.1",
)
class MyPlugin(Star):
    STORAGE_KEY = "umo_app_subscriptions"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.context = context
        self.server = config.get("server")
        self.token = config.get("client_token")
        self.gotify: AsyncGotify = AsyncGotify(
            base_url=self.server, client_token=self.token
        )
        self.cache_app: Dict[str, Dict] = {}
        self.umo_app_subscriptions: Dict[str, Set[str]] = {}
        self.subscriptions_lock = asyncio.Lock()

    async def update_applications(self):
        try:
            applications = await self.gotify.get_applications()
        except Exception as e:
            logger.error(f"刷新 Gotify 应用列表失败: {e}")
            return
        self.cache_app = {
            str(app.get("id")): app
            for app in applications
            if isinstance(app, dict) and "id" in app
        }

    async def load_subscriptions(self):
        raw_data = await self.get_kv_data(self.STORAGE_KEY, {})
        normalized: Dict[str, Set[str]] = {}

        if isinstance(raw_data, dict):
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

        async with self.subscriptions_lock:
            self.umo_app_subscriptions = normalized

    async def save_subscriptions_locked(self):
        payload = {
            umo: sorted(apps)
            for umo, apps in self.umo_app_subscriptions.items()
            if apps
        }
        await self.put_kv_data(self.STORAGE_KEY, payload)

    @staticmethod
    def normalize_text(value) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    @classmethod
    def build_app_identifiers(cls, app_info: Dict) -> Set[str]:
        identifiers = set()
        app_name = cls.normalize_text(app_info.get("name"))
        app_token = cls.normalize_text(app_info.get("token"))

        if app_name:
            identifiers.add(app_name)
        if app_token:
            identifiers.add(app_token)

        return identifiers

    @classmethod
    def format_app_display(cls, app_info: Dict, fallback: str = "") -> str:
        app_name = cls.normalize_text(app_info.get("name"))
        app_token = cls.normalize_text(app_info.get("token"))

        if app_name and app_token:
            return f"{app_name} (token: {app_token})"
        if app_name:
            return app_name
        if app_token:
            return f"token: {app_token}"
        return cls.normalize_text(fallback)

    def resolve_application_in_cache(
        self, identifier: str
    ) -> Tuple[Optional[str], Optional[Dict], str]:
        normalized_identifier = self.normalize_text(identifier)
        if not normalized_identifier:
            return None, None, ""

        for app_id, app_info in self.cache_app.items():
            app_token = self.normalize_text(app_info.get("token"))
            if app_token and normalized_identifier == app_token:
                return app_id, app_info, "token"

        for app_id, app_info in self.cache_app.items():
            app_name = self.normalize_text(app_info.get("name"))
            if app_name and normalized_identifier == app_name:
                return app_id, app_info, "name"

        return None, None, ""

    async def resolve_application_by_identifier(
        self, identifier: str
    ) -> Tuple[Optional[str], Optional[Dict], str]:
        app_id, app_info, matched_by = self.resolve_application_in_cache(identifier)
        if app_id:
            return app_id, app_info, matched_by

        await self.update_applications()
        return self.resolve_application_in_cache(identifier)

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

    @staticmethod
    def parse_command_args(event: AstrMessageEvent) -> List[str]:
        message_str = (event.message_str or "").strip()
        if not message_str:
            return []
        parts = message_str.split()
        if not parts:
            return []
        command_aliases = {"gotify_add", "gotify_del", "gotify_list", "gotify_clear"}
        first = parts[0].lstrip("/")
        if first in command_aliases:
            return parts[1:]
        return parts

    async def initialize(self):
        await self.load_subscriptions()
        await self.update_applications()
        self.listen_task = asyncio.create_task(self.start_listen())
        logger.info(
            f"插件初始化完成, 已加载 {len(self.umo_app_subscriptions)} 个 UMO 订阅"
        )

    async def handle_message(self, msg: Message):
        raw_app_id = msg.get("appid")
        if raw_app_id is None:
            logger.info("Gotify 消息未携带 appid")
            return
        app_id = str(raw_app_id)

        app_info = self.cache_app.get(app_id)
        if not app_info:
            await self.update_applications()
            app_info = self.cache_app.get(app_id)
            if not app_info:
                logger.info(f"appid {app_id} 不在应用列表中")
                return

        app_name = app_info.get("name")
        if not app_name:
            logger.info(f"appid {app_id} 对应应用缺少 name")
            return

        app_identifiers = self.build_app_identifiers(app_info)
        if not app_identifiers:
            logger.info(f"appid {app_id} 对应应用缺少可匹配标识(name/token)")
            return

        async with self.subscriptions_lock:
            target_umos = [
                umo
                for umo, apps in self.umo_app_subscriptions.items()
                if apps.intersection(app_identifiers)
            ]

        if not target_umos:
            return

        for umo in target_umos:
            send_msg = MessageChain().message(
                f"--- Message ---\n应用：{app_name}\n标题：{msg.get('title')}\n内容：{msg.get('message')}"
            )
            try:
                await self.context.send_message(umo, send_msg)
            except Exception as e:
                logger.error(f"向 UMO {umo} 推送消息失败: {e}")

    async def start_listen(self):
        while True:
            received: int = 0
            try:
                async for msg in self.gotify.stream():
                    logger.info(msg)
                    received = received + 1
                    await self.handle_message(msg)

            except Exception as e:
                logger.error(
                    f"Gotify 连接断开, 已收到的消息: {received}, 尝试重连: {e}"
                )
            if received == 0:
                await asyncio.sleep(60)  # 等待 1 分钟后重连
        pass

    @filter.command("gotify_add")
    async def gotify_add(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return

        args = self.parse_command_args(event)
        if len(args) < 2:
            yield event.plain_result("用法: /gotify_add <umo> <app|token>")
            return

        umo = args[0].strip()
        app = " ".join(args[1:]).strip()
        if not umo or not app:
            yield event.plain_result("用法: /gotify_add <umo> <app|token>")
            return

        _, resolved_app_info, _ = await self.resolve_application_by_identifier(app)
        if not resolved_app_info:
            yield event.plain_result("未找到应用，请填写 app name 或 app token")
            return

        store_value = self.normalize_text(resolved_app_info.get("token"))
        if not store_value:
            yield event.plain_result("该应用未返回 token，无法添加订阅")
            return
        display_target = self.format_app_display(resolved_app_info, fallback=store_value)

        async with self.subscriptions_lock:
            apps = self.umo_app_subscriptions.setdefault(umo, set())
            existed = store_value in apps
            apps.add(store_value)
            await self.save_subscriptions_locked()
            app_count = len(apps)

        if existed:
            yield event.plain_result(f"该应用已添加: {umo} -> {display_target}")
            return

        yield event.plain_result(
            f"添加成功: {umo} -> {display_target}\n当前该 UMO 共监听 {app_count} 个应用"
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

        umo = args[0].strip()
        app = " ".join(args[1:]).strip() if len(args) > 1 else ""
        result_message = ""
        removed_all = False
        remove_display = app

        if app:
            await self.update_applications()

        async with self.subscriptions_lock:
            apps = self.umo_app_subscriptions.get(umo)
            if not apps:
                result_message = f"未找到 UMO: {umo}"
            elif not app:
                del self.umo_app_subscriptions[umo]
                await self.save_subscriptions_locked()
                result_message = f"已删除 UMO {umo} 的全部订阅"
            else:
                _, resolved_app_info, _ = self.resolve_application_in_cache(app)
                remove_candidates = {app}
                if resolved_app_info:
                    remove_display = self.format_app_display(
                        resolved_app_info, fallback=app
                    )
                    remove_candidates.update(self.build_app_identifiers(resolved_app_info))
                    store_value = self.normalize_text(resolved_app_info.get("token"))
                    if store_value:
                        remove_candidates.add(store_value)

                remove_key = next((item for item in remove_candidates if item in apps), "")
                if not remove_key:
                    result_message = f"UMO {umo} 未订阅应用: {remove_display}"
                else:
                    apps.remove(remove_key)
                    if not apps:
                        del self.umo_app_subscriptions[umo]
                        removed_all = True
                    await self.save_subscriptions_locked()

        if result_message:
            yield event.plain_result(result_message)
            return

        if removed_all:
            yield event.plain_result(
                f"已删除订阅: {umo} -> {remove_display}\n该 UMO 已无任何订阅并自动移除"
            )
            return

        yield event.plain_result(f"已删除订阅: {umo} -> {remove_display}")

    @filter.command("gotify_clear")
    async def gotify_clear(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return

        async with self.subscriptions_lock:
            self.umo_app_subscriptions.clear()
            await self.put_kv_data(self.STORAGE_KEY, {})

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

        await self.update_applications()
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
            for idx, umo in enumerate(sorted(snapshot.keys()), start=1):
                lines.append(f"{idx}. UMO: {umo}")
                display_values = self.format_subscription_values(snapshot[umo])
                for app_idx, display in enumerate(display_values, start=1):
                    lines.append(f"  {app_idx}. {display}")
            yield event.plain_result("\n".join(lines))
            return

        umo = args[0].strip()
        apps = snapshot.get(umo)
        if not apps:
            yield event.plain_result(f"未找到 UMO: {umo}")
            return

        lines = [f"UMO: {umo}", "监听应用:"]
        display_values = self.format_subscription_values(apps)
        for idx, display in enumerate(display_values, start=1):
            lines.append(f"{idx}. {display}")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        if hasattr(self, "listen_task") and not self.listen_task.done():
            logger.info("Gotify 连接关闭")
            self.listen_task.cancel()
