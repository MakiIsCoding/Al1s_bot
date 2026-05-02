import asyncio
import json
import os
import random
import re
import shutil
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
from jmcomic import (
    JmDownloader,
    JmModuleConfig,
    JmcomicException,
    JsonResolveFailException,
    MissingAlbumPhotoException,
    RequestRetryAllFailException,
    create_option_by_str,
)
from PIL import Image as PILImage
from PIL import ImageFilter

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import At, Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


LEGACY_DATA_PATH = Path("/home/ubuntu/.local/share/nonebot2/bot-jmcomic/jmcomic_data.json")


@dataclass
class SearchState:
    query: str
    start_idx: int
    total_results: list[str]
    api_page: int
    created_at: datetime = field(default_factory=datetime.now)

    def is_expired(self, ttl_minutes: int = 30) -> bool:
        return datetime.now() - self.created_at > timedelta(minutes=ttl_minutes)


class SearchManager:
    def __init__(self, ttl_minutes: int = 30):
        self.states: dict[str, SearchState] = {}
        self.ttl_minutes = ttl_minutes

    def get_state(self, user_id: str) -> SearchState | None:
        state = self.states.get(user_id)
        if state and state.is_expired(self.ttl_minutes):
            self.states.pop(user_id, None)
            return None
        return state

    def set_state(self, user_id: str, state: SearchState) -> None:
        self.states[user_id] = state

    def remove_state(self, user_id: str) -> None:
        self.states.pop(user_id, None)

    def clean_expired(self) -> None:
        expired_users = [
            user_id
            for user_id, state in self.states.items()
            if state.is_expired(self.ttl_minutes)
        ]
        for user_id in expired_users:
            self.states.pop(user_id, None)


class JmComicDataManager:
    DEFAULT_RESTRICTED_TAGS: list[str] = []
    DEFAULT_RESTRICTED_IDS: list[str] = []

    def __init__(self, filepath: Path, default_enabled: bool = False):
        self.filepath = filepath
        self.default_enabled = default_enabled
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = {}
        self._migrate_legacy_if_needed()
        self._load_data()
        if "restricted_tags" not in self.data or not self.data["restricted_tags"]:
            self.data["restricted_tags"] = self.DEFAULT_RESTRICTED_TAGS.copy()
        if "restricted_ids" not in self.data:
            self.data["restricted_ids"] = self.DEFAULT_RESTRICTED_IDS.copy()
        self.save()

    def _migrate_legacy_if_needed(self) -> None:
        if self.filepath.exists() or not LEGACY_DATA_PATH.exists():
            return
        try:
            self.filepath.write_text(
                LEGACY_DATA_PATH.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            logger.info("[jmcomic] migrated legacy data from %s", LEGACY_DATA_PATH)
        except Exception as exc:
            logger.warning("[jmcomic] migrate legacy data failed: %s", exc)

    def _load_data(self) -> None:
        if not self.filepath.exists():
            self.data = {}
            return
        try:
            self.data = json.loads(self.filepath.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("[jmcomic] load data failed: %s", exc)
            self.data = {}

    def save(self) -> None:
        try:
            self.filepath.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("[jmcomic] save data failed: %s", exc)

    def set_group_folder_id(self, group_id: int | str, folder_id: str) -> None:
        group_data = self.data.setdefault(str(group_id), {})
        group_data["folder_id"] = folder_id
        self.save()

    def get_group_folder_id(self, group_id: int | str) -> str | None:
        return self.data.get(str(group_id), {}).get("folder_id")

    def get_user_limit(self, user_id: int | str, default_limit: int) -> int:
        user_limits = self.data.setdefault("user_limits", {})
        return int(user_limits.get(str(user_id), default_limit))

    def set_user_limit(self, user_id: int | str, limit: int) -> None:
        user_limits = self.data.setdefault("user_limits", {})
        user_limits[str(user_id)] = limit
        self.save()

    def add_blacklist(self, group_id: int | str, user_id: int | str) -> None:
        group_data = self.data.setdefault(str(group_id), {})
        blacklist = group_data.setdefault("blacklist", [])
        user_id = str(user_id)
        if user_id not in blacklist:
            blacklist.append(user_id)
            self.save()

    def remove_blacklist(self, group_id: int | str, user_id: int | str) -> None:
        group_data = self.data.get(str(group_id), {})
        blacklist = group_data.get("blacklist", [])
        user_id = str(user_id)
        if user_id in blacklist:
            blacklist.remove(user_id)
            self.save()

    def is_user_blacklisted(self, group_id: int | str, user_id: int | str) -> bool:
        blacklist = self.data.get(str(group_id), {}).get("blacklist", [])
        return str(user_id) in blacklist

    def list_blacklist(self, group_id: int | str) -> list[str]:
        return list(self.data.get(str(group_id), {}).get("blacklist", []))

    def is_group_enabled(self, group_id: int | str) -> bool:
        return bool(self.data.get(str(group_id), {}).get("enabled", self.default_enabled))

    def set_group_enabled(self, group_id: int | str, enabled: bool) -> None:
        group_data = self.data.setdefault(str(group_id), {})
        group_data["enabled"] = enabled
        self.save()

    def add_restricted_jm_id(self, jm_id: str) -> None:
        restricted_ids = self.data.setdefault("restricted_ids", [])
        if jm_id not in restricted_ids:
            restricted_ids.append(jm_id)
            self.save()

    def is_jm_id_restricted(self, jm_id: str) -> bool:
        return jm_id in self.data.setdefault("restricted_ids", [])

    def add_restricted_tag(self, tag: str) -> None:
        restricted_tags = self.data.setdefault("restricted_tags", [])
        if tag not in restricted_tags:
            restricted_tags.append(tag)
            self.save()

    def has_restricted_tag(self, tags: list[str]) -> bool:
        restricted_tags = set(self.data.setdefault("restricted_tags", []))
        return any(tag in restricted_tags for tag in tags)


def modify_pdf_md5(original_pdf_path: str, output_path: str) -> bool:
    try:
        with open(original_pdf_path, "rb") as file_obj:
            content = file_obj.read()

        random_bytes = struct.pack("d", random.random())
        if content.endswith(b"%%EOF"):
            modified_content = content[:-5] + b"\n% Random: " + random_bytes + b"\n%%EOF"
        else:
            modified_content = content + b"\n% Random: " + random_bytes + b"\n%%EOF"

        with open(output_path, "wb") as file_obj:
            file_obj.write(modified_content)
        return True
    except Exception as exc:
        logger.error("[jmcomic] modify pdf md5 failed: %s", exc)
        return False


class JmComicPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_data_dir = Path(get_astrbot_plugin_data_path()) / self.name
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.plugin_data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.data_manager = JmComicDataManager(
            filepath=self.plugin_data_dir / "jmcomic_data.json",
            default_enabled=bool(self.config.get("jmcomic_allow_groups", False)),
        )
        self.search_manager = SearchManager()
        self.upload_tracker: dict[tuple[str, str], tuple[float, str]] = {}
        self.pending_disable: dict[str, float] = {}
        self.upload_cooldown = 300
        self.maintenance_task: asyncio.Task | None = None
        self.client = None
        self.downloader = None
        self.last_cache_cleanup_date: str | None = None
        self.last_limit_reset_date: str | None = None
        self._init_jm_client()

    async def initialize(self) -> None:
        self.maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def terminate(self) -> None:
        if self.maintenance_task is None:
            return
        self.maintenance_task.cancel()
        try:
            await self.maintenance_task
        except asyncio.CancelledError:
            pass
        self.maintenance_task = None

    def _config_bool(self, key: str, default: bool = False) -> bool:
        return bool(self.config.get(key, default))

    def _config_int(self, key: str, default: int) -> int:
        return int(self.config.get(key, default) or default)

    def _config_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return "" if value is None else str(value)

    def _build_option_config(self) -> str:
        login_block = ""
        username = self._config_str("jmcomic_username")
        password = self._config_str("jmcomic_password")
        if username and password:
            login_block = (
                "  after_init:\n"
                "    - plugin: login\n"
                "      kwargs:\n"
                f"        username: {username}\n"
                f"        password: {password}\n"
            )

        return (
            f"""
log: {str(self._config_bool("jmcomic_log", False)).lower()}

client:
  impl: api
  retry_times: 1
  postman:
    meta_data:
      proxies: {self._config_str("jmcomic_proxies", "system")}

download:
  image:
    suffix: .jpg
  threading:
    image: {self._config_int("jmcomic_thread_count", 10)}

dir_rule:
  base_dir: {self.cache_dir.as_posix()}
  rule: Bd_Pid

plugins:
{login_block}  after_photo:
    - plugin: img2pdf
      kwargs:
        pdf_dir: {self.cache_dir.as_posix()}
        filename_rule: Pid
"""
        )

    def _init_jm_client(self) -> None:
        try:
            option = create_option_by_str(self._build_option_config(), mode="yml")
            self.client = option.build_jm_client()
            self.downloader = JmDownloader(option)
        except Exception as exc:
            self.client = None
            self.downloader = None
            logger.error("[jmcomic] init failed: %s", exc)

    def _ensure_client(self) -> bool:
        return self.client is not None and self.downloader is not None

    def _results_per_page(self) -> int:
        return self._config_int("jmcomic_results_per_page", 5)

    def _blocked_message(self) -> str:
        return self._config_str("jmcomic_blocked_message", "搜索到屏蔽本子")

    def _modify_real_md5(self) -> bool:
        return self._config_bool("jmcomic_modify_real_md5", True)

    def _get_bot(self, event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("当前平台不支持 JM 插件所需的 OneBot 文件操作")
        return bot

    def _is_global_admin_by_id(self, user_id: str) -> bool:
        config = getattr(self.context, "_config", {}) or {}
        admin_ids = {str(item) for item in config.get("admins_id", [])}
        return str(user_id) in admin_ids

    def _is_global_admin(self, event: AstrMessageEvent) -> bool:
        return self._is_global_admin_by_id(event.get_sender_id())

    async def _get_member_role(self, bot, group_id: str, user_id: str) -> str:
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            return str(info.get("role", "member"))
        except Exception as exc:
            logger.warning("[jmcomic] get_group_member_info failed: %s", exc)
            return "member"

    async def _can_manage_target(
        self,
        bot,
        group_id: str,
        operator_id: str,
        target_id: str,
    ) -> bool:
        if self._is_global_admin_by_id(operator_id):
            return True
        operator_role = await self._get_member_role(bot, group_id, operator_id)
        target_role = await self._get_member_role(bot, group_id, target_id)
        if operator_role == "owner":
            return True
        if operator_role == "admin" and target_role not in {"owner", "admin"}:
            return True
        return False

    async def _get_photo_info_async(self, photo_id: str):
        def _run():
            try:
                return self.client.get_photo_detail(photo_id)
            except MissingAlbumPhotoException:
                raise
            except JsonResolveFailException as exc:
                logger.error(
                    "[jmcomic] photo detail json parse failed (HTTP %s): %s",
                    exc.resp.status_code,
                    exc.resp.text,
                )
            except RequestRetryAllFailException:
                logger.error("[jmcomic] photo detail failed after retries")
            except JmcomicException as exc:
                logger.error("[jmcomic] photo detail error: %s", exc)
            return None

        return await asyncio.to_thread(_run)

    async def _search_album_async(self, query: str, page: int = 1):
        def _run():
            try:
                return self.client.search_site(search_query=query, page=page)
            except JsonResolveFailException as exc:
                logger.error(
                    "[jmcomic] search json parse failed (HTTP %s): %s",
                    exc.resp.status_code,
                    exc.resp.text,
                )
            except RequestRetryAllFailException:
                logger.error("[jmcomic] search failed after retries")
            except JmcomicException as exc:
                logger.error("[jmcomic] search error: %s", exc)
            return None

        return await asyncio.to_thread(_run)

    async def _download_photo_async(self, photo) -> bool:
        def _run() -> bool:
            try:
                with self.downloader as downloader:
                    downloader.download_by_photo_detail(photo)
                return True
            except Exception as exc:
                logger.error("[jmcomic] download failed for %s: %s", photo.id, exc)
                return False

        return await asyncio.to_thread(_run)

    async def _download_avatar(self, photo_id: int | str) -> bytes | None:
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
            for domain in JmModuleConfig.DOMAIN_IMAGE_LIST:
                url = f"https://{domain}/media/albums/{photo_id}.jpg"
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    if not response.content or len(response.content) < 1024:
                        return None
                    return response.content
                except Exception:
                    continue
        logger.warning("[jmcomic] cover download failed for %s", photo_id)
        return None

    async def _blur_image_async(self, image_bytes: bytes) -> bytes:
        def _run() -> bytes:
            image = PILImage.open(BytesIO(image_bytes))
            blurred = image.filter(ImageFilter.GaussianBlur(radius=7))
            output = BytesIO()
            blurred.save(output, format="JPEG")
            return output.getvalue()

        return await asyncio.to_thread(_run)

    async def _send_forward_nodes(self, event: AstrMessageEvent, nodes: list[Node]) -> None:
        await event.send(MessageChain(chain=[Nodes(nodes=nodes)]))

    def _extract_first_at_user(self, event: AstrMessageEvent) -> str | None:
        for component in event.get_messages():
            if isinstance(component, At):
                qq = str(component.qq)
                if qq and qq != "all":
                    return qq
        return None

    async def _ensure_core_access(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        if group_id:
            if not self.data_manager.is_group_enabled(group_id):
                return False
            if self.data_manager.is_user_blacklisted(group_id, event.get_sender_id()):
                return False
        return True

    def _disable_pending_key(self, event: AstrMessageEvent) -> str:
        return f"{event.get_group_id()}:{event.get_sender_id()}"

    def _build_query_text(self, photo) -> str:
        tags_text = " ".join(f"#{tag}" for tag in photo.tags)
        return f"查询到jm{photo.id}: {photo.title}\n🎨 作者: {photo.author}\n🔖 标签: {tags_text}\n"

    def _build_search_text(self, photo) -> str:
        return (
            "━━━━━ JM搜索 ━━━━━\n"
            f"📖 标题：{photo.title}\n"
            f"✍️ 作者：{photo.author}\n"
            f"🆔 JM号：{photo.id}\n"
            f"📄 页数：{len(photo)} 页\n"
            "🔖 标签：" + " ".join(f"#{tag}" for tag in photo.tags) + "\n"
            "━━━━━━━━━━━━━━━\n"
        )

    def _build_download_text(self, photo) -> str:
        return (
            "━━━━━ JM下载 ━━━━━\n"
            f"📖 标题：{photo.title}\n"
            f"✍️ 作者：{photo.author}\n"
            f"🆔 JM号：{photo.id}\n"
            f"📄 页数：{len(photo)} 页\n"
            "━━━━━━━━━━━━━━━"
        )

    async def _build_search_nodes(
        self,
        event: AstrMessageEvent,
        result_ids: list[str],
    ) -> list[Node]:
        photos = await asyncio.gather(
            *(self._get_photo_info_async(photo_id) for photo_id in result_ids),
            return_exceptions=True,
        )
        avatars = await asyncio.gather(
            *(self._download_avatar(photo_id) for photo_id in result_ids),
            return_exceptions=True,
        )

        nodes: list[Node] = []
        for photo, avatar in zip(photos, avatars):
            if isinstance(photo, Exception) or photo is None:
                continue

            if self.data_manager.has_restricted_tag(photo.tags):
                content = [Plain(self._blocked_message())]
            else:
                content = [Plain(self._build_search_text(photo))]
                if isinstance(avatar, bytes):
                    content.append(Image.fromBytes(avatar))

            nodes.append(
                Node(
                    name="jm搜索结果",
                    uin=event.get_self_id() or "0",
                    content=content,
                )
            )
        return nodes

    async def _send_photo_query_forward(self, event: AstrMessageEvent, photo) -> None:
        content = [Plain(self._build_query_text(photo))]
        avatar = await self._download_avatar(photo.id)
        if avatar:
            try:
                content.append(Image.fromBytes(await self._blur_image_async(avatar)))
            except Exception as exc:
                logger.warning("[jmcomic] blur cover failed: %s", exc)
        await self._send_forward_nodes(
            event,
            [Node(name="jm查询结果", uin=event.get_self_id() or "0", content=content)],
        )

    async def _handle_restricted_download(self, event: AstrMessageEvent, photo) -> bool:
        restricted = self.data_manager.is_jm_id_restricted(str(photo.id)) or self.data_manager.has_restricted_tag(photo.tags)
        if not restricted:
            return False

        group_id = event.get_group_id()
        if group_id and not self._is_global_admin(event):
            try:
                bot = self._get_bot(event)
                await bot.call_action(
                    "set_group_ban",
                    group_id=int(group_id),
                    user_id=int(event.get_sender_id()),
                    duration=86400,
                )
            except Exception:
                pass
            self.data_manager.add_blacklist(group_id, event.get_sender_id())
            await event.send(
                MessageChain(
                    chain=[
                        At(qq=event.get_sender_id()),
                        Plain("该本子（或其tag）被禁止下载!你已被加入本群jm黑名单"),
                    ]
                )
            )
            return True

        await event.send(MessageChain().message("该本子（或其tag）被禁止下载！"))
        return True

    async def _upload_pdf(
        self,
        event: AstrMessageEvent,
        pdf_path: str,
        filename: str,
        upload_key: tuple[str, str],
    ) -> None:
        bot = self._get_bot(event)
        current_time = time.time()
        if not os.path.exists(pdf_path):
            self.upload_tracker[upload_key] = (current_time, "failed")
            logger.error("[jmcomic] upload skipped, pdf missing: %s", pdf_path)
            await event.send(MessageChain().message("❌ 下载后的 PDF 文件不存在，上传已取消"))
            return
        file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)

        try:
            if event.get_group_id():
                payload = {
                    "group_id": int(event.get_group_id()),
                    "file": pdf_path,
                    "name": filename,
                }
                folder_id = self.data_manager.get_group_folder_id(event.get_group_id())
                if folder_id:
                    payload["folder_id"] = folder_id
                await bot.call_action("upload_group_file", **payload)
            else:
                await bot.call_action(
                    "upload_private_file",
                    user_id=int(event.get_sender_id()),
                    file=pdf_path,
                    name=filename,
                )

            self.upload_tracker[upload_key] = (current_time, "success")
            logger.info("[jmcomic] upload success: %s (%.2fMB)", filename, file_size_mb)
        except Exception as exc:
            if "timeout" in str(exc).lower():
                logger.warning(
                    "[jmcomic] upload timeout but may continue in background: %s (%.2fMB)",
                    filename,
                    file_size_mb,
                )
                return

            self.upload_tracker[upload_key] = (current_time, "failed")
            logger.error("[jmcomic] upload failed: %s", exc)
            await event.send(MessageChain().message("❌ 文件上传失败\n可能是权限不足或文件过大"))

    async def _maybe_delete_cache(self) -> None:
        now = datetime.now()
        if now.hour != 3:
            return
        today = now.strftime("%Y-%m-%d")
        if self.last_cache_cleanup_date == today:
            return
        self.last_cache_cleanup_date = today
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir, ignore_errors=True)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[jmcomic] cache cleaned: %s", self.cache_dir)

    async def _maybe_reset_user_limits(self) -> None:
        now = datetime.now()
        if now.weekday() != 0 or now.hour != 0:
            return
        today = now.strftime("%Y-%m-%d")
        if self.last_limit_reset_date == today:
            return
        self.last_limit_reset_date = today

        limits = self.data_manager.data.get("user_limits", {})
        if not limits:
            return

        default_limit = self._config_int("jmcomic_user_limits", 30)
        for user_id in list(limits.keys()):
            self.data_manager.set_user_limit(user_id, default_limit)
        logger.info("[jmcomic] user limits reset complete")

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                self.search_manager.clean_expired()
                await self._maybe_delete_cache()
                await self._maybe_reset_user_limits()
            except Exception as exc:
                logger.error("[jmcomic] maintenance loop failed: %s", exc)
            await asyncio.sleep(600)

    def _normalized_message_text(self, event: AstrMessageEvent) -> str:
        return str(event.get_message_str() or "").strip()

    def _extract_inline_suffix(
        self,
        event: AstrMessageEvent,
        command_names: list[str],
    ) -> str:
        message = self._normalized_message_text(event)
        lowered = message.lower()
        for command_name in command_names:
            if lowered.startswith(command_name.lower()):
                return message[len(command_name) :].strip()
        return ""

    def _clean_query_text(self, photo) -> str:
        tags_text = " ".join(f"#{tag}" for tag in photo.tags)
        return f"查询到 jm{photo.id}: {photo.title}\n🎨 作者: {photo.author}\n🔖 标签: {tags_text}\n"

    def _clean_search_text(self, photo) -> str:
        return (
            "━━━━━ JM搜索 ━━━━━\n"
            f"📖 标题：{photo.title}\n"
            f"✍️ 作者：{photo.author}\n"
            f"🆔 JM号：{photo.id}\n"
            f"📄 页数：{len(photo)} 页\n"
            "🔖 标签：" + " ".join(f"#{tag}" for tag in photo.tags) + "\n"
            "━━━━━━━━━━━━━━━\n"
        )

    def _clean_download_text(self, photo) -> str:
        return (
            "━━━━━ JM下载 ━━━━━\n"
            f"📖 标题：{photo.title}\n"
            f"✍️ 作者：{photo.author}\n"
            f"🆔 JM号：{photo.id}\n"
            f"📄 页数：{len(photo)} 页\n"
            "━━━━━━━━━━━━━━━"
        )

    async def _build_search_nodes_clean(
        self,
        event: AstrMessageEvent,
        result_ids: list[str],
    ) -> list[Node]:
        photos = await asyncio.gather(
            *(self._get_photo_info_async(photo_id) for photo_id in result_ids),
            return_exceptions=True,
        )
        avatars = await asyncio.gather(
            *(self._download_avatar(photo_id) for photo_id in result_ids),
            return_exceptions=True,
        )

        nodes: list[Node] = []
        for photo, avatar in zip(photos, avatars):
            if isinstance(photo, Exception) or photo is None:
                continue

            if self.data_manager.has_restricted_tag(photo.tags):
                content = [Plain(self._config_str("jmcomic_blocked_message", "搜索到屏蔽本子"))]
            else:
                content = [Plain(self._clean_search_text(photo))]
                if isinstance(avatar, bytes):
                    content.append(Image.fromBytes(avatar))

            nodes.append(
                Node(
                    name="jm搜索结果",
                    uin=event.get_self_id() or "0",
                    content=content,
                )
            )
        return nodes

    async def _send_photo_query_forward_clean(self, event: AstrMessageEvent, photo) -> None:
        content = [Plain(self._clean_query_text(photo))]
        avatar = await self._download_avatar(photo.id)
        if avatar:
            try:
                content.append(Image.fromBytes(await self._blur_image_async(avatar)))
            except Exception as exc:
                logger.warning("[jmcomic] blur cover failed: %s", exc)
        await self._send_forward_nodes(
            event,
            [Node(name="jm查询结果", uin=event.get_self_id() or "0", content=content)],
        )

    async def _build_pdf_from_images(self, photo_id: str) -> str | None:
        image_dir = self.cache_dir / str(photo_id)
        if not image_dir.exists():
            return None

        image_files = sorted(
            [
                path
                for path in image_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
        )
        if not image_files:
            return None

        pdf_path = self.cache_dir / f"{photo_id}.pdf"

        def _run() -> str | None:
            images = []
            try:
                for image_path in image_files:
                    with PILImage.open(image_path) as img:
                        images.append(img.convert("RGB"))
                if not images:
                    return None
                first, rest = images[0], images[1:]
                first.save(pdf_path, save_all=True, append_images=rest)
                return str(pdf_path)
            finally:
                for image in images:
                    try:
                        image.close()
                    except Exception:
                        pass

        return await asyncio.to_thread(_run)

    async def _resolve_pdf_path(self, photo) -> str | None:
        photo_id = str(photo.id)
        direct_path = self.cache_dir / f"{photo_id}.pdf"
        if direct_path.exists():
            return str(direct_path)

        for candidate in self.cache_dir.glob(f"**/{photo_id}.pdf"):
            if candidate.is_file():
                return str(candidate)

        built_path = await self._build_pdf_from_images(photo_id)
        if built_path and os.path.exists(built_path):
            logger.info("[jmcomic] built fallback pdf for %s: %s", photo_id, built_path)
            return built_path

        return None

    async def _handle_jm_download_clean(self, event: AstrMessageEvent, photo_id: str):
        if not await self._ensure_core_access(event):
            return
        if not self._ensure_client():
            yield event.plain_result("JM客户端初始化失败，请联系管理员检查配置").stop_event()
            return

        photo_id = str(photo_id).strip()
        if not photo_id.isdigit():
            yield event.plain_result("请输入要下载的jm号").stop_event()
            return

        upload_group = event.get_group_id() or f"private_{event.get_sender_id()}"
        upload_key = (upload_group, photo_id)
        current_time = time.time()

        if upload_key in self.upload_tracker:
            last_time, status = self.upload_tracker[upload_key]
            time_passed = current_time - last_time
            if status == "uploading" and time_passed < self.upload_cooldown:
                yield event.plain_result(
                    f"⚠️ 该本子正在上传中\n请等待 {int(self.upload_cooldown - time_passed)} 秒后重试"
                ).stop_event()
                return
            if status == "success" and time_passed < 60:
                yield event.plain_result("✅ 该本子刚刚已上传成功，请在群文件中查看").stop_event()
                return

        self.upload_tracker[upload_key] = (current_time, "uploading")

        try:
            photo = await self._get_photo_info_async(photo_id)
        except MissingAlbumPhotoException:
            self.upload_tracker[upload_key] = (current_time, "failed")
            yield event.plain_result("未查找到本子").stop_event()
            return

        if photo is None:
            self.upload_tracker[upload_key] = (current_time, "failed")
            yield event.plain_result("查询时发生错误").stop_event()
            return

        if await self._handle_restricted_download(event, photo):
            self.upload_tracker[upload_key] = (current_time, "failed")
            yield event.make_result().stop_event()
            return

        try:
            avatar = await self._download_avatar(photo.id)
            chain = MessageChain().message(self._clean_download_text(photo))
            if avatar:
                chain.chain.append(Image.fromBytes(avatar))
            chain.message("\n⏳ 开始下载...")
            await event.send(chain)
        except Exception as exc:
            logger.warning("[jmcomic] send download summary failed: %s", exc)

        pdf_path = await self._resolve_pdf_path(photo)
        if not pdf_path:
            ok = await self._download_photo_async(photo)
            if not ok:
                self.upload_tracker[upload_key] = (current_time, "failed")
                yield event.plain_result("下载失败").stop_event()
                return
            pdf_path = await self._resolve_pdf_path(photo)
            if not pdf_path:
                self.upload_tracker[upload_key] = (current_time, "failed")
                logger.error("[jmcomic] pdf not found after download for %s", photo.id)
                yield event.plain_result("下载完成了图片，但 PDF 生成失败").stop_event()
                return

        if self._modify_real_md5():
            try:
                random_suffix = f"{int(time.time())}{random.randint(1000, 9999)}"
                renamed_pdf_path = str(self.cache_dir / f"{photo.id}_{random_suffix}.pdf")
                modified = await asyncio.to_thread(modify_pdf_md5, pdf_path, renamed_pdf_path)
                if modified:
                    pdf_path = renamed_pdf_path
            except Exception as exc:
                self.upload_tracker[upload_key] = (current_time, "failed")
                logger.error("[jmcomic] modify md5 failed: %s", exc)
                yield event.plain_result("处理文件失败").stop_event()
                return

        safe_title = re.sub(r'[<>:"/\\\\|?*]', "_", photo.title)[:100]
        filename = f"{safe_title}.pdf"
        await self._upload_pdf(event, pdf_path, filename, upload_key)
        yield event.make_result().stop_event()

    async def _handle_jm_query_clean(self, event: AstrMessageEvent, photo_id: str):
        if not await self._ensure_core_access(event):
            return
        if not self._ensure_client():
            yield event.plain_result("JM客户端初始化失败，请联系管理员检查配置").stop_event()
            return

        photo_id = str(photo_id).strip()
        if not photo_id.isdigit():
            yield event.plain_result("请输入要查询的jm号").stop_event()
            return

        try:
            photo = await self._get_photo_info_async(photo_id)
        except MissingAlbumPhotoException:
            yield event.plain_result("未查找到本子").stop_event()
            return

        if photo is None:
            yield event.plain_result("查询时发生错误").stop_event()
            return

        try:
            await self._send_photo_query_forward_clean(event, photo)
        except Exception as exc:
            logger.error("[jmcomic] query result send failed: %s", exc)
            yield event.plain_result("查询结果发送失败").stop_event()
            return

        yield event.make_result().stop_event()

    async def _handle_jm_search_clean(self, event: AstrMessageEvent, query: str):
        if not await self._ensure_core_access(event):
            return
        if not self._ensure_client():
            yield event.plain_result("JM客户端初始化失败，请联系管理员检查配置").stop_event()
            return

        query = str(query).strip()
        if not query:
            yield event.plain_result("请输入要搜索的内容").stop_event()
            return

        await event.send(MessageChain().message("正在搜索中..."))
        page = await self._search_album_async(query)
        if page is None:
            yield event.plain_result("搜索失败").stop_event()
            return

        result_ids = list(page.iter_id())
        if not result_ids:
            yield event.plain_result("未搜索到本子").stop_event()
            return

        current_results = result_ids[: self._results_per_page()]
        nodes = await self._build_search_nodes_clean(event, current_results)
        if not nodes:
            yield event.plain_result("搜索结果发送失败").stop_event()
            return

        try:
            await self._send_forward_nodes(event, nodes)
        except Exception as exc:
            logger.error("[jmcomic] search results send failed: %s", exc)
            yield event.plain_result("搜索结果发送失败").stop_event()
            return

        if len(result_ids) > self._results_per_page():
            self.search_manager.set_state(
                str(event.get_sender_id()),
                SearchState(
                    query=query,
                    start_idx=self._results_per_page(),
                    total_results=result_ids,
                    api_page=1,
                ),
            )
            await event.send(MessageChain().message("搜索有更多结果，使用“jm下一页”查看更多"))
        else:
            await event.send(MessageChain().message("已发送所有搜索结果"))

        yield event.make_result().stop_event()

    async def _handle_jm_next_page_clean(self, event: AstrMessageEvent):
        if not await self._ensure_core_access(event):
            return
        if not self._ensure_client():
            yield event.plain_result("JM客户端初始化失败，请联系管理员检查配置").stop_event()
            return

        state = self.search_manager.get_state(str(event.get_sender_id()))
        if not state:
            yield event.plain_result("没有进行中的搜索，请先使用“jm搜索”命令").stop_event()
            return

        await event.send(MessageChain().message("正在搜索更多内容..."))
        end_idx = state.start_idx + self._results_per_page()
        is_return_all = False

        if end_idx >= len(state.total_results):
            if len(state.total_results) % 80 == 0:
                state.api_page += 1
                next_page = await self._search_album_async(state.query, page=state.api_page)
                if next_page is None:
                    is_return_all = True
                else:
                    next_results = list(next_page.iter_id())
                    if not next_results or next_results[-1] == state.total_results[-1]:
                        is_return_all = True
                    else:
                        state.total_results.extend(next_results)
            else:
                is_return_all = True

        current_results = state.total_results[state.start_idx:end_idx]
        nodes = await self._build_search_nodes_clean(event, current_results)
        if not nodes:
            self.search_manager.remove_state(str(event.get_sender_id()))
            yield event.plain_result("下一页结果发送失败").stop_event()
            return

        try:
            await self._send_forward_nodes(event, nodes)
        except Exception as exc:
            self.search_manager.remove_state(str(event.get_sender_id()))
            logger.error("[jmcomic] next page send failed: %s", exc)
            yield event.plain_result("下一页结果发送失败").stop_event()
            return

        if is_return_all:
            self.search_manager.remove_state(str(event.get_sender_id()))
            await event.send(MessageChain().message("已显示所有搜索结果"))
        else:
            state.start_idx = end_idx
            await event.send(MessageChain().message("搜索有更多结果，使用“jm下一页”查看更多"))

        yield event.make_result().stop_event()

    @filter.command("jm下载", alias={"JM下载"})
    async def jm_download_clean(self, event: AstrMessageEvent, photo_id: str = ""):
        async for result in self._handle_jm_download_clean(event, photo_id):
            yield result

    @filter.regex(r"^(?:/?(?:jm|JM)下载)\s*\d+\s*$")
    async def jm_download_inline_clean(self, event: AstrMessageEvent):
        photo_id = self._extract_inline_suffix(event, ["jm下载", "JM下载"])
        async for result in self._handle_jm_download_clean(event, photo_id):
            yield result

    @filter.command("jm查询", alias={"JM查询"})
    async def jm_query_clean(self, event: AstrMessageEvent, photo_id: str = ""):
        async for result in self._handle_jm_query_clean(event, photo_id):
            yield result

    @filter.regex(r"^(?:/?(?:jm|JM)查询)\s*\d+\s*$")
    async def jm_query_inline_clean(self, event: AstrMessageEvent):
        photo_id = self._extract_inline_suffix(event, ["jm查询", "JM查询"])
        async for result in self._handle_jm_query_clean(event, photo_id):
            yield result

    @filter.command("jm搜索", alias={"JM搜索"})
    async def jm_search_clean(self, event: AstrMessageEvent, query: GreedyStr = ""):
        async for result in self._handle_jm_search_clean(event, query):
            yield result

    @filter.regex(r"^(?:/?(?:jm|JM)搜索)\s*.+$")
    async def jm_search_inline_clean(self, event: AstrMessageEvent):
        query = self._extract_inline_suffix(event, ["jm搜索", "JM搜索"])
        async for result in self._handle_jm_search_clean(event, query):
            yield result

    @filter.command("jm 下一页", alias={"JM 下一页", "jm下一页", "JM下一页"})
    async def jm_next_page_clean(self, event: AstrMessageEvent):
        async for result in self._handle_jm_next_page_clean(event):
            yield result

    @filter.regex(r"^(?:/?(?:jm|JM)(?:\s*\u4e0b\u4e00\u9875|\u4e0b\u4e00\u9875))\s*$")
    async def jm_next_page_inline_clean(self, event: AstrMessageEvent):
        async for result in self._handle_jm_next_page_clean(event):
            yield result

    @filter.command("jm下载", alias={"JM下载"})
    async def jm_download(self, event: AstrMessageEvent, photo_id: str = ""):
        if not await self._ensure_core_access(event):
            return
        if not self._ensure_client():
            yield event.plain_result("JM客户端初始化失败，请联系管理员检查配置").stop_event()
            return

        photo_id = str(photo_id).strip()
        if not photo_id.isdigit():
            yield event.plain_result("请输入要下载的jm号").stop_event()
            return

        upload_group = event.get_group_id() or f"private_{event.get_sender_id()}"
        upload_key = (upload_group, photo_id)
        current_time = time.time()

        if upload_key in self.upload_tracker:
            last_time, status = self.upload_tracker[upload_key]
            time_passed = current_time - last_time
            if status == "uploading" and time_passed < self.upload_cooldown:
                yield event.plain_result(
                    f"⚠️ 该本子正在上传中\n请等待 {int(self.upload_cooldown - time_passed)} 秒后重试"
                ).stop_event()
                return
            if status == "success" and time_passed < 60:
                yield event.plain_result("✅ 该本子刚刚已上传成功，请在群文件中查看").stop_event()
                return

        self.upload_tracker[upload_key] = (current_time, "uploading")

        try:
            photo = await self._get_photo_info_async(photo_id)
        except MissingAlbumPhotoException:
            self.upload_tracker[upload_key] = (current_time, "failed")
            yield event.plain_result("未查找到本子").stop_event()
            return

        if photo is None:
            self.upload_tracker[upload_key] = (current_time, "failed")
            yield event.plain_result("查询时发生错误").stop_event()
            return

        if await self._handle_restricted_download(event, photo):
            self.upload_tracker[upload_key] = (current_time, "failed")
            yield event.make_result().stop_event()
            return

        try:
            await event.send(MessageChain().message(self._build_download_text(photo)))
            avatar = await self._download_avatar(photo.id)
            if avatar:
                await event.send(MessageChain(chain=[Image.fromBytes(avatar)]))
            await event.send(MessageChain().message("\n⏳ 开始下载..."))
        except Exception as exc:
            logger.warning("[jmcomic] send download summary failed: %s", exc)

        pdf_path = str(self.cache_dir / f"{photo.id}.pdf")
        if not os.path.exists(pdf_path):
            ok = await self._download_photo_async(photo)
            if not ok:
                self.upload_tracker[upload_key] = (current_time, "failed")
                yield event.plain_result("下载失败").stop_event()
                return

        if self._modify_real_md5():
            try:
                random_suffix = f"{int(time.time())}{random.randint(1000, 9999)}"
                renamed_pdf_path = str(self.cache_dir / f"{photo.id}_{random_suffix}.pdf")
                modified = await asyncio.to_thread(modify_pdf_md5, pdf_path, renamed_pdf_path)
                if modified:
                    pdf_path = renamed_pdf_path
            except Exception as exc:
                self.upload_tracker[upload_key] = (current_time, "failed")
                logger.error("[jmcomic] modify md5 failed: %s", exc)
                yield event.plain_result("处理文件失败").stop_event()
                return

        safe_title = re.sub(r'[<>:"/\\\\|?*]', "_", photo.title)[:100]
        filename = f"{safe_title}.pdf"
        await self._upload_pdf(event, pdf_path, filename, upload_key)
        yield event.make_result().stop_event()

    @filter.command("jm查询", alias={"JM查询"})
    async def jm_query(self, event: AstrMessageEvent, photo_id: str = ""):
        if not await self._ensure_core_access(event):
            return
        if not self._ensure_client():
            yield event.plain_result("JM客户端初始化失败，请联系管理员检查配置").stop_event()
            return

        photo_id = str(photo_id).strip()
        if not photo_id.isdigit():
            yield event.plain_result("请输入要查询的jm号").stop_event()
            return

        try:
            photo = await self._get_photo_info_async(photo_id)
        except MissingAlbumPhotoException:
            yield event.plain_result("未查找到本子").stop_event()
            return

        if photo is None:
            yield event.plain_result("查询时发生错误").stop_event()
            return

        try:
            await self._send_photo_query_forward(event, photo)
        except Exception as exc:
            logger.error("[jmcomic] query result send failed: %s", exc)
            yield event.plain_result("查询结果发送失败").stop_event()
            return

        yield event.make_result().stop_event()

    @filter.command("jm搜索", alias={"JM搜索"})
    async def jm_search(self, event: AstrMessageEvent, query: GreedyStr = ""):
        if not await self._ensure_core_access(event):
            return
        if not self._ensure_client():
            yield event.plain_result("JM客户端初始化失败，请联系管理员检查配置").stop_event()
            return

        query = str(query).strip()
        if not query:
            yield event.plain_result("请输入要搜索的内容").stop_event()
            return

        await event.send(MessageChain().message("正在搜索中..."))
        page = await self._search_album_async(query)
        if page is None:
            yield event.plain_result("搜索失败").stop_event()
            return

        result_ids = list(page.iter_id())
        if not result_ids:
            yield event.plain_result("未搜索到本子").stop_event()
            return

        current_results = result_ids[: self._results_per_page()]
        nodes = await self._build_search_nodes(event, current_results)
        if not nodes:
            yield event.plain_result("搜索结果发送失败").stop_event()
            return

        try:
            await self._send_forward_nodes(event, nodes)
        except Exception as exc:
            logger.error("[jmcomic] search results send failed: %s", exc)
            yield event.plain_result("搜索结果发送失败").stop_event()
            return

        if len(result_ids) > self._results_per_page():
            self.search_manager.set_state(
                str(event.get_sender_id()),
                SearchState(
                    query=query,
                    start_idx=self._results_per_page(),
                    total_results=result_ids,
                    api_page=1,
                ),
            )
            await event.send(MessageChain().message("搜索有更多结果，使用'jm下一页'指令查看更多"))
        else:
            await event.send(MessageChain().message("已发送所有搜索结果"))

        yield event.make_result().stop_event()

    @filter.command("jm 下一页", alias={"JM 下一页", "jm下一页", "JM下一页"})
    async def jm_next_page(self, event: AstrMessageEvent):
        if not await self._ensure_core_access(event):
            return
        if not self._ensure_client():
            yield event.plain_result("JM客户端初始化失败，请联系管理员检查配置").stop_event()
            return

        state = self.search_manager.get_state(str(event.get_sender_id()))
        if not state:
            yield event.plain_result("没有进行中的搜索，请先使用'jm搜索'命令").stop_event()
            return

        await event.send(MessageChain().message("正在搜索更多内容..."))
        end_idx = state.start_idx + self._results_per_page()
        is_return_all = False

        if end_idx >= len(state.total_results):
            if len(state.total_results) % 80 == 0:
                state.api_page += 1
                next_page = await self._search_album_async(state.query, page=state.api_page)
                if next_page is None:
                    is_return_all = True
                else:
                    next_results = list(next_page.iter_id())
                    if not next_results or next_results[-1] == state.total_results[-1]:
                        is_return_all = True
                    else:
                        state.total_results.extend(next_results)
            else:
                is_return_all = True

        current_results = state.total_results[state.start_idx:end_idx]
        nodes = await self._build_search_nodes(event, current_results)
        if not nodes:
            self.search_manager.remove_state(str(event.get_sender_id()))
            yield event.plain_result("下一页结果发送失败").stop_event()
            return

        try:
            await self._send_forward_nodes(event, nodes)
        except Exception as exc:
            self.search_manager.remove_state(str(event.get_sender_id()))
            logger.error("[jmcomic] next page send failed: %s", exc)
            yield event.plain_result("下一页结果发送失败").stop_event()
            return

        if is_return_all:
            self.search_manager.remove_state(str(event.get_sender_id()))
            await event.send(MessageChain().message("已显示所有搜索结果"))
        else:
            state.start_idx = end_idx
            await event.send(MessageChain().message("搜索有更多结果，使用'jm下一页'指令查看更多"))

        yield event.make_result().stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("jm设置文件夹", alias={"JM设置文件夹"})
    async def jm_set_folder(self, event: AstrMessageEvent, folder_name: GreedyStr = ""):
        folder_name = str(folder_name).strip()
        if not folder_name:
            yield event.plain_result("请输入要设置的文件夹名称").stop_event()
            return

        bot = self._get_bot(event)
        group_id = event.get_group_id()
        role = await self._get_member_role(bot, group_id, event.get_sender_id())
        if not (self._is_global_admin(event) or role in {"owner", "admin"}):
            yield event.plain_result("权限不足").stop_event()
            return

        found_folder_id = None
        try:
            root_data = await bot.call_action("get_group_root_files", group_id=int(group_id))
            for folder_item in root_data.get("folders", []):
                if folder_item.get("folder_name") == folder_name:
                    found_folder_id = folder_item.get("folder_id")
                    break
        except Exception as exc:
            logger.warning("[jmcomic] get group root files failed: %s", exc)

        if found_folder_id:
            self.data_manager.set_group_folder_id(group_id, found_folder_id)
            yield event.plain_result("已设置本子储存文件夹").stop_event()
            return

        try:
            result = await bot.call_action(
                "create_group_file_folder",
                group_id=int(group_id),
                folder_name=folder_name,
            )
            ret_code = (
                result.get("result", {}).get("retCode")
                if isinstance(result.get("result"), dict)
                else result.get("retcode", 0)
            )
            if ret_code not in {0, None}:
                yield event.plain_result("未找到该文件夹,创建文件夹失败").stop_event()
                return

            folder_id = (
                result.get("groupItem", {})
                .get("folderInfo", {})
                .get("folderId")
            ) or result.get("folder_id")
            if not folder_id:
                yield event.plain_result("未找到该文件夹,主动创建文件夹失败").stop_event()
                return

            self.data_manager.set_group_folder_id(group_id, str(folder_id))
            yield event.plain_result("已设置本子储存文件夹").stop_event()
        except Exception as exc:
            logger.warning("[jmcomic] create group file folder failed: %s", exc)
            yield event.plain_result("未找到该文件夹,主动创建文件夹失败").stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("jm拉黑", alias={"JM拉黑"})
    async def jm_ban_user(self, event: AstrMessageEvent):
        bot = self._get_bot(event)
        group_id = event.get_group_id()
        operator_id = event.get_sender_id()
        role = await self._get_member_role(bot, group_id, operator_id)
        if not (self._is_global_admin(event) or role in {"owner", "admin"}):
            yield event.plain_result("权限不足").stop_event()
            return

        user_id = self._extract_first_at_user(event)
        if not user_id:
            yield event.plain_result("请使用@指定要拉黑的用户").stop_event()
            return

        if not await self._can_manage_target(bot, group_id, operator_id, user_id):
            yield event.plain_result("权限不足").stop_event()
            return

        self.data_manager.add_blacklist(group_id, user_id)
        await event.send(MessageChain(chain=[At(qq=user_id), Plain("已加入本群jm黑名单")]))
        yield event.make_result().stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("jm解除拉黑", alias={"JM解除拉黑"})
    async def jm_unban_user(self, event: AstrMessageEvent):
        bot = self._get_bot(event)
        group_id = event.get_group_id()
        operator_id = event.get_sender_id()
        role = await self._get_member_role(bot, group_id, operator_id)
        if not (self._is_global_admin(event) or role in {"owner", "admin"}):
            yield event.plain_result("权限不足").stop_event()
            return

        user_id = self._extract_first_at_user(event)
        if not user_id:
            yield event.plain_result("请使用@指定要解除拉黑的用户").stop_event()
            return

        if not await self._can_manage_target(bot, group_id, operator_id, user_id):
            yield event.plain_result("权限不足").stop_event()
            return

        self.data_manager.remove_blacklist(group_id, user_id)
        await event.send(MessageChain(chain=[At(qq=user_id), Plain("已从本群jm黑名单中移除")]))
        yield event.make_result().stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("jm黑名单", alias={"JM黑名单"})
    async def jm_blacklist(self, event: AstrMessageEvent):
        bot = self._get_bot(event)
        group_id = event.get_group_id()
        role = await self._get_member_role(bot, group_id, event.get_sender_id())
        if not (self._is_global_admin(event) or role in {"owner", "admin"}):
            yield event.plain_result("权限不足").stop_event()
            return

        blacklist = self.data_manager.list_blacklist(group_id)
        if not blacklist:
            yield event.plain_result("当前群的黑名单列表为空").stop_event()
            return

        chain = MessageChain().message("当前群的黑名单列表：\n")
        for user_id in blacklist:
            chain.chain.append(At(qq=user_id))
            chain.chain.append(Plain("\n"))
        await event.send(chain)
        yield event.make_result().stop_event()

    @filter.command("jm启用群")
    async def jm_enable_group(self, event: AstrMessageEvent, group_ids: GreedyStr = ""):
        if not self._is_global_admin(event):
            yield event.plain_result("权限不足").stop_event()
            return

        success = []
        for group_id in str(group_ids).split():
            if group_id.isdigit():
                self.data_manager.set_group_enabled(group_id, True)
                success.append(group_id)

        if success:
            yield event.plain_result("以下群已启用插件功能：\n" + " ".join(success)).stop_event()
            return
        yield event.plain_result("没有做任何处理。").stop_event()

    @filter.command("jm禁用群")
    async def jm_disable_group(self, event: AstrMessageEvent, group_ids: GreedyStr = ""):
        if not self._is_global_admin(event):
            yield event.plain_result("权限不足").stop_event()
            return

        success = []
        for group_id in str(group_ids).split():
            if group_id.isdigit():
                self.data_manager.set_group_enabled(group_id, False)
                success.append(group_id)

        if success:
            yield event.plain_result("以下群已禁用插件功能：\n" + " ".join(success)).stop_event()
            return
        yield event.plain_result("没有做任何处理。").stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("开启jm", alias={"开启JM"})
    async def jm_enable_here(self, event: AstrMessageEvent):
        if not self._is_global_admin(event):
            yield event.plain_result("权限不足").stop_event()
            return
        self.data_manager.set_group_enabled(event.get_group_id(), True)
        yield event.plain_result("已启用本群jm功能！").stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("关闭jm", alias={"关闭JM"})
    async def jm_disable_here(self, event: AstrMessageEvent):
        bot = self._get_bot(event)
        role = await self._get_member_role(bot, event.get_group_id(), event.get_sender_id())
        if not (self._is_global_admin(event) or role in {"owner", "admin"}):
            yield event.plain_result("权限不足").stop_event()
            return

        self.pending_disable[self._disable_pending_key(event)] = time.time() + 120
        yield event.plain_result(
            "禁用后只能请求神秘存在再次开启该功能！确认要关闭吗？发送'确认'关闭"
        ).stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.regex(r"^确认$")
    async def jm_disable_confirm(self, event: AstrMessageEvent):
        key = self._disable_pending_key(event)
        expire_at = self.pending_disable.get(key)
        if not expire_at:
            return
        if time.time() > expire_at:
            self.pending_disable.pop(key, None)
            return

        self.pending_disable.pop(key, None)
        self.data_manager.set_group_enabled(event.get_group_id(), False)
        yield event.plain_result("已禁用本群jm功能！").stop_event()

    @filter.command("jm禁用id", alias={"JM禁用id"})
    async def jm_forbid_id(self, event: AstrMessageEvent, jm_ids: GreedyStr = ""):
        if not self._is_global_admin(event):
            yield event.plain_result("权限不足").stop_event()
            return

        success = []
        for jm_id in str(jm_ids).split():
            if jm_id.isdigit():
                self.data_manager.add_restricted_jm_id(jm_id)
                success.append(jm_id)

        if success:
            yield event.plain_result("以下jm号已加入禁止下载列表：\n" + " ".join(success)).stop_event()
            return
        yield event.plain_result("没有做任何处理").stop_event()

    @filter.command("jm禁用tag", alias={"JM禁用tag"})
    async def jm_forbid_tag(self, event: AstrMessageEvent, tags: GreedyStr = ""):
        if not self._is_global_admin(event):
            yield event.plain_result("权限不足").stop_event()
            return

        success = []
        for tag in str(tags).split():
            if tag:
                self.data_manager.add_restricted_tag(tag)
                success.append(tag)

        if success:
            yield event.plain_result("以下tag已加入禁止下载列表：\n" + " ".join(success)).stop_event()
            return
        yield event.plain_result("没有做任何处理").stop_event()
