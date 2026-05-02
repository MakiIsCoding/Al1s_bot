import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


class SetuPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.api_url = self.config.get("api_url", "https://api.lolicon.app/setu/v2")
        self.max_api_retry = int(self.config.get("max_api_retry", 5) or 5)
        self.timeout = float(self.config.get("timeout_seconds", 60) or 60)
        self.strict_tag_match = bool(self.config.get("strict_tag_match", True))
        self.cache_dir = Path(get_astrbot_plugin_data_path()) / self.name / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def terminate(self) -> None:
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
            self.cleanup_task = None

    def _is_global_admin(self, event: AstrMessageEvent) -> bool:
        config = getattr(self.context, "_config", {}) or {}
        admin_ids = {str(i) for i in config.get("admins_id", [])}
        return str(event.get_sender_id()) in admin_ids

    def _get_r18_tag_whitelist_groups(self) -> set[str]:
        groups = self.config.get("r18_tag_whitelist_groups", []) or []
        return {str(group).strip() for group in groups if str(group).strip()}

    def _is_r18_tag_whitelisted(self, group_id: str) -> bool:
        return group_id in self._get_r18_tag_whitelist_groups()

    def _persist_whitelist_groups(self, groups: set[str]) -> None:
        config_path = Path("/home/ubuntu/astrbot/AstrBot/data/config/al1s_setu_config.json")
        normalized_groups = sorted({str(group).strip() for group in groups if str(group).strip()})
        self.config["r18_tag_whitelist_groups"] = normalized_groups

        if not config_path.exists():
            return

        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
            data["r18_tag_whitelist_groups"] = normalized_groups
            config_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("[setu] failed to persist whitelist groups: %s", e)

    def _ensure_group_in_r18_whitelist(self, group_id: str) -> None:
        groups = self._get_r18_tag_whitelist_groups()
        if group_id in groups:
            return
        groups.add(group_id)
        self._persist_whitelist_groups(groups)
        logger.info("[setu] group %s added to r18 whitelist", group_id)

    def _get_sensitive_r18_tags(self) -> set[str]:
        tags = self.config.get(
            "sensitive_r18_tags",
            ["sm", "bdsm", "bondage", "拘束", "束缚", "绑缚", "調教", "调教"],
        )
        return {self._normalize_tag(tag) for tag in tags if str(tag).strip()}

    def _normalize_tag(self, tag: str) -> str:
        return "".join(ch for ch in str(tag).strip().lower() if not ch.isspace() and ch not in "-_/" )

    def _expand_tag_aliases(self, tag: str) -> set[str]:
        normalized = self._normalize_tag(tag)
        alias_groups = {
            "sm": {"sm", "bdsm", "bondage", "拘束", "束缚", "绑缚", "ボンデージ"},
            "bdsm": {"sm", "bdsm", "bondage", "拘束", "束缚", "绑缚", "ボンデージ"},
            "bondage": {"sm", "bdsm", "bondage", "拘束", "束缚", "绑缚", "ボンデージ"},
            "拘束": {"sm", "bdsm", "bondage", "拘束", "束缚", "绑缚", "ボンデージ"},
            "束缚": {"sm", "bdsm", "bondage", "拘束", "束缚", "绑缚", "ボンデージ"},
            "绑缚": {"sm", "bdsm", "bondage", "拘束", "束缚", "绑缚", "ボンデージ"},
            "調教": {"調教", "调教", "sm", "bdsm"},
            "调教": {"調教", "调教", "sm", "bdsm"},
        }
        values = alias_groups.get(normalized, {normalized})
        return {self._normalize_tag(value) for value in values}

    def _has_sensitive_r18_tag(self, tags: list[str]) -> bool:
        sensitive = self._get_sensitive_r18_tags()
        for tag in tags:
            if self._normalize_tag(tag) in sensitive:
                return True
        return False

    def _matches_requested_tags(self, requested_tags: list[str], item_tags: list[str]) -> bool:
        if not requested_tags or not self.strict_tag_match:
            return True

        normalized_item_tags = {self._normalize_tag(tag) for tag in item_tags}
        for requested in requested_tags:
            alias_set = self._expand_tag_aliases(requested)
            if normalized_item_tags.isdisjoint(alias_set):
                return False
        return True

    async def _is_r18_enabled(self, group_id: str) -> bool:
        return bool(await self.get_kv_data(f"r18_enabled:{group_id}", False))

    async def _set_r18_enabled(self, group_id: str, enabled: bool) -> None:
        await self.put_kv_data(f"r18_enabled:{group_id}", enabled)
        logger.info("[setu] group %s r18 set to %s", group_id, enabled)

    async def _fetch_setu(self, tags: list[str], r18: int) -> dict | None:
        payload = {
            "tag": tags if tags else [],
            "size": "regular",
            "num": 1,
            "r18": r18,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.api_url, json=payload)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error("[setu] fetch failed: %s", e)
            return None

    async def _download_image(self, url: str, filepath: Path) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                filepath.write_bytes(resp.content)
            return True
        except Exception as e:
            logger.error("[setu] download failed: %s", e)
            return False

    async def _send_setu_result(
        self,
        event: AstrMessageEvent,
        summary_text: str,
        filepath: Path,
        image_url: str,
    ) -> None:
        await event.send(MessageChain().message(summary_text))

        try:
            await event.send(MessageChain().file_image(str(filepath)))
            return
        except Exception as e:
            logger.warning("[setu] local image send failed, fallback to url/plain: %s", e)

        try:
            await event.send(MessageChain().url_image(image_url))
            return
        except Exception as e:
            logger.warning("[setu] url image send failed, fallback to plain url: %s", e)

        await event.send(MessageChain().message(f"原图链接：{image_url}"))

    async def _cleanup_cache(self) -> int:
        count = 0
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
            for file in self.cache_dir.glob(pattern):
                try:
                    file.unlink()
                    count += 1
                except FileNotFoundError:
                    continue
                except Exception as e:
                    logger.warning("[setu] failed to delete cache file %s: %s", file, e)
        return count

    async def _cleanup_loop(self) -> None:
        while True:
            now = datetime.now()
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            await asyncio.sleep((next_run - now).total_seconds())
            try:
                count = await self._cleanup_cache()
                logger.info("[setu] cache cleanup completed: %s files removed", count)
            except Exception as e:
                logger.error("[setu] cache cleanup failed: %s", e)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("setu", alias={"色图"})
    async def handle_setu(self, event: AstrMessageEvent, tags: GreedyStr = ""):
        """获取一张色图，可附带标签，多个标签使用 / 分隔。"""
        group_id = event.get_group_id()
        tag_list = [tag.strip() for tag in tags.split("/") if tag.strip()] if tags else []

        r18_mode = 0
        r18_enabled = await self._is_r18_enabled(group_id)
        r18_whitelisted = self._is_r18_tag_whitelisted(group_id)
        if r18_enabled and r18_whitelisted:
            r18_mode = 1

        if tag_list and self._has_sensitive_r18_tag(tag_list) and r18_mode == 0:
            yield event.plain_result(
                "当前群未开启 R18，`sm` / `bdsm` 这类标签会被 API 过滤或严重跑偏。"
                "先把群号加入插件的 R18 白名单，再执行 `setu开启r18`。"
            ).stop_event()
            return

        for attempt in range(1, self.max_api_retry + 1):
            data = await self._fetch_setu(tag_list, r18_mode)
            if data and isinstance(data.get("data"), list) and not data["data"]:
                yield event.plain_result("没有找到符合该关键词的色图，请尝试更换标签~").stop_event()
                return

            if not data or not data.get("data"):
                logger.warning("[setu] invalid response on attempt %s", attempt)
                await asyncio.sleep(1)
                continue

            item = data["data"][0]
            item_tags = item.get("tags") or []
            if tag_list and not self._matches_requested_tags(tag_list, item_tags):
                logger.warning(
                    "[setu] response tags do not match request on attempt %s: requested=%s item_tags=%s",
                    attempt,
                    tag_list,
                    item_tags,
                )
                await asyncio.sleep(1)
                continue

            pid = str(item.get("pid", "unknown"))
            image_url = item.get("urls", {}).get("regular")
            if not image_url:
                await asyncio.sleep(1)
                continue

            suffix = Path(image_url.split("?")[0]).suffix or ".jpg"
            filepath = self.cache_dir / f"{pid}{suffix}"
            if not await self._download_image(image_url, filepath):
                await asyncio.sleep(1)
                continue

            display_tags = "、".join(item_tags[:3]) if item_tags else "无标签"
            r18_indicator = " [R18]" if item.get("r18", False) else ""
            summary_text = (
                f"标题：{item.get('title', '无标题')}{r18_indicator}\n"
                f"作者：{item.get('author', '未知作者')}\n"
                f"标签：{display_tags}\n"
                f"PID：{pid}"
            )
            await self._send_setu_result(event, summary_text, filepath, image_url)
            yield event.make_result().stop_event()
            return

        if tag_list:
            yield event.plain_result(
                f"没有拿到和标签 `{ '/'.join(tag_list) }` 足够匹配的图片。"
                "如果是 `sm` / `bdsm` 这类标签，请先开启本群 R18。"
            ).stop_event()
            return

        yield event.plain_result("图片发送失败，可能被腾讯屏蔽或 API 抽风了，请稍后再试~").stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("setu开启r18", alias={"色图开启r18", "setu开启R18", "色图开启R18"})
    async def enable_r18(self, event: AstrMessageEvent):
        """开启本群 R18 色图，仅全局管理员可用。"""
        if not self._is_global_admin(event):
            yield event.plain_result("只有全局管理员可以开启本群的 R18 色图功能。").stop_event()
            return

        group_id = event.get_group_id()
        self._ensure_group_in_r18_whitelist(group_id)
        await self._set_r18_enabled(group_id, True)
        yield event.plain_result(
            "✅ 已开启本群的R18色图功能！\n"
            "当前群已自动加入 R18 白名单，现在只会返回R18内容。"
        ).stop_event()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.command("setu关闭r18", alias={"色图关闭r18", "setu关闭R18", "色图关闭R18"})
    async def disable_r18(self, event: AstrMessageEvent):
        """关闭本群 R18 色图，群管理员和全局管理员可用。"""
        if not (self._is_global_admin(event) or event.is_admin()):
            yield event.plain_result("只有群管理员或全局管理员可以关闭本群的 R18 色图功能。").stop_event()
            return
        await self._set_r18_enabled(event.get_group_id(), False)
        yield event.plain_result("✅ 已关闭本群的R18色图功能！\n现在只会返回非R18内容。").stop_event()