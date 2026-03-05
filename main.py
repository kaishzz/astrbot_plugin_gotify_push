from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.event.filter import PermissionType
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from gotify import AsyncGotify
from gotify.response_types import Message
import asyncio
from typing import Dict, List, Set
from astrbot.core.message.message_event_result import MessageChain


@register(
    "astrbot_plugin_gotify_push",
    "ksbjt",
    "监听 Gotify 消息并推送",
    "1.1.0",
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
    def parse_command_args(event: AstrMessageEvent) -> List[str]:
        message_str = (event.message_str or "").strip()
        if not message_str:
            return []
        parts = message_str.split()
        if not parts:
            return []
        command_aliases = {"gotify_add", "gotify_del", "gotify_view", "gotify_查看"}
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

        async with self.subscriptions_lock:
            target_umos = [
                umo
                for umo, apps in self.umo_app_subscriptions.items()
                if app_name in apps
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

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("gotify_add")
    async def gotify_add(self, event: AstrMessageEvent):
        args = self.parse_command_args(event)
        if len(args) < 2:
            yield event.plain_result("用法: /gotify_add <umo> <app>")
            return

        umo = args[0].strip()
        app = " ".join(args[1:]).strip()
        if not umo or not app:
            yield event.plain_result("用法: /gotify_add <umo> <app>")
            return

        async with self.subscriptions_lock:
            apps = self.umo_app_subscriptions.setdefault(umo, set())
            existed = app in apps
            apps.add(app)
            await self.save_subscriptions_locked()
            app_count = len(apps)

        if existed:
            yield event.plain_result(f"订阅已存在: {umo} -> {app}")
            return

        yield event.plain_result(
            f"添加成功: {umo} -> {app}\n当前该 UMO 共监听 {app_count} 个应用"
        )

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("gotify_del")
    async def gotify_del(self, event: AstrMessageEvent):
        args = self.parse_command_args(event)
        if not args:
            yield event.plain_result("用法: /gotify_del <umo> [app]")
            return

        umo = args[0].strip()
        app = " ".join(args[1:]).strip() if len(args) > 1 else ""
        result_message = ""
        removed_all = False

        async with self.subscriptions_lock:
            apps = self.umo_app_subscriptions.get(umo)
            if not apps:
                result_message = f"未找到 UMO: {umo}"
            elif not app:
                del self.umo_app_subscriptions[umo]
                await self.save_subscriptions_locked()
                result_message = f"已删除 UMO {umo} 的全部订阅"
            elif app not in apps:
                result_message = f"UMO {umo} 未订阅应用: {app}"
            else:
                apps.remove(app)
                if not apps:
                    del self.umo_app_subscriptions[umo]
                    removed_all = True
                await self.save_subscriptions_locked()

        if result_message:
            yield event.plain_result(result_message)
            return

        if removed_all:
            yield event.plain_result(
                f"已删除订阅: {umo} -> {app}\n该 UMO 已无任何订阅并自动移除"
            )
            return

        yield event.plain_result(f"已删除订阅: {umo} -> {app}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("gotify_view", alias={"gotify_查看"})
    async def gotify_view(self, event: AstrMessageEvent):
        args = self.parse_command_args(event)
        if len(args) > 1:
            yield event.plain_result("用法: /gotify_查看 [umo]")
            return

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
                lines.append(f"{idx}. {umo} -> {', '.join(snapshot[umo])}")
            yield event.plain_result("\n".join(lines))
            return

        umo = args[0].strip()
        apps = snapshot.get(umo)
        if not apps:
            yield event.plain_result(f"未找到 UMO: {umo}")
            return

        lines = [f"UMO: {umo}", "监听应用:"]
        for idx, app in enumerate(apps, start=1):
            lines.append(f"{idx}. {app}")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        if hasattr(self, "listen_task") and not self.listen_task.done():
            logger.info("Gotify 连接关闭")
            self.listen_task.cancel()
