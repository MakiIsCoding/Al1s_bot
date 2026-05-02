import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
from openai import AsyncOpenAI
from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as MsgImage
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


class EmojiToolsPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self.name
        self.emoji_dir = self.data_dir / "emojis"
        self.imgllm_dir = self.data_dir / "imgllm"
        self.data_file = self.data_dir / "emoji_data.json"
        for path in (self.data_dir, self.emoji_dir, self.imgllm_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.collecting_users: set[str] = set()
        self.classifying_users: set[str] = set()
        self.lock = asyncio.Lock()
        self.emojis = self._load_data()

    def _cfg_int(self, key: str, default: int) -> int:
        return int(self.config.get(key, default) or default)

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return "" if value is None else str(value)

    def _superusers(self) -> set[str]:
        configured = self.config.get("superusers") or ["2694958402", "2294900459"]
        return {str(item) for item in configured}

    def _is_superuser(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in self._superusers()

    def _load_data(self) -> list[dict]:
        if not self.data_file.exists():
            return []
        try:
            return json.loads(self.data_file.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_data(self) -> None:
        self.data_file.write_text(json.dumps(self.emojis, ensure_ascii=False, indent=2), encoding="utf-8")

    def _sanitize_filename(self, value: str) -> str:
        value = re.sub(r'[\\/:*?"<>|]', "", value).strip()
        value = re.sub(r"\s+", "_", value)
        return value[:50] or "unknown"

    def _stats(self) -> dict:
        total_size = 0
        tagged = 0
        for item in self.emojis:
            if item.get("tags"):
                tagged += 1
            path = Path(item.get("file_path", ""))
            if path.exists():
                total_size += path.stat().st_size
        max_count = self._cfg_int("max_emojis", 200)
        return {
            "total_count": len(self.emojis),
            "max_emojis": max_count,
            "tagged_count": tagged,
            "untagged_count": max(0, len(self.emojis) - tagged),
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "storage_full": len(self.emojis) >= max_count,
        }

    async def _get_all_image_bytes(self, event: AstrMessageEvent) -> list[bytes]:
        images = []
        for comp in event.get_messages():
            if isinstance(comp, MsgImage):
                try:
                    if comp.url or (comp.file and str(comp.file).startswith("http")):
                        url = comp.url or comp.file
                        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                            resp = await client.get(url)
                            resp.raise_for_status()
                            images.append(resp.content)
                    else:
                        path = await comp.convert_to_file_path()
                        images.append(Path(path).read_bytes())
                except Exception as exc:
                    logger.error("[emoji_tools] failed to read image: %s", exc)
        return images

    async def _get_image_bytes(self, event: AstrMessageEvent) -> bytes | None:
        images = await self._get_all_image_bytes(event)
        return images[0] if images else None

    def _image_info(self, image_bytes: bytes) -> tuple[int, int, int]:
        with PILImage.open(BytesIO(image_bytes)) as img:
            width, height = img.size
        return width, height, len(image_bytes)

    async def _vision_json(self, image_bytes: bytes, prompt: str) -> dict:
        api_key = self._cfg_str("vision_api_key") or os.getenv("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise RuntimeError("vision_api_key is not configured")
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=self._cfg_str("vision_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            timeout=90,
        )
        data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
        resp = await client.chat.completions.create(
            model=self._cfg_str("vision_model", "qwen-vl-plus"),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content or "{}"
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {"category": text.strip()[:30] or "unknown", "analysis": text.strip()}
        try:
            return json.loads(match.group(0))
        except Exception:
            return {"category": text.strip()[:30] or "unknown", "analysis": text.strip()}

    async def _classify_emotion(self, image_bytes: bytes) -> tuple[str, str]:
        categories = [p.name for p in self.imgllm_dir.iterdir() if p.is_dir()]
        prompt = (
            "Analyze this meme image. Return strict JSON with category and analysis. "
            "Category should be a concrete Chinese emotion/action phrase. "
            "Prefer existing categories only when exactly suitable: "
            + ", ".join(categories[:80])
            + '. Example: {"category":"尴尬而不失礼貌的微笑","analysis":"..."}'
        )
        data = await self._vision_json(image_bytes, prompt)
        category = self._sanitize_filename(str(data.get("category") or "unknown"))
        analysis = str(data.get("analysis") or "")
        return category, analysis

    async def _tag_emoji(self, image_bytes: bytes) -> dict:
        prompt = (
            "Analyze this emoji/meme. Return strict JSON with keys: emotions(list), "
            "scenes(list), features(list), description(string). Keep Chinese concise."
        )
        data = await self._vision_json(image_bytes, prompt)
        return {
            "emotions": data.get("emotions") if isinstance(data.get("emotions"), list) else [],
            "scenes": data.get("scenes") if isinstance(data.get("scenes"), list) else [],
            "features": data.get("features") if isinstance(data.get("features"), list) else [],
            "description": str(data.get("description") or ""),
        }

    async def _store_classified_image(self, image_bytes: bytes) -> tuple[str, str, Path]:
        category, analysis = await self._classify_emotion(image_bytes)
        category_dir = self.imgllm_dir / category
        category_dir.mkdir(parents=True, exist_ok=True)
        indexes = [int(p.stem) for p in category_dir.glob("*.jpg") if p.stem.isdigit()]
        next_index = max(indexes, default=-1) + 1
        path = category_dir / f"{next_index}.jpg"
        path.write_bytes(image_bytes)
        return category, analysis, path

    async def _collect_emoji(self, event: AstrMessageEvent, image_bytes: bytes) -> bool:
        async with self.lock:
            if len(self.emojis) >= self._cfg_int("max_emojis", 200):
                return False
            digest = hashlib.sha256(image_bytes).hexdigest()
            if any(item.get("sha256") == digest for item in self.emojis):
                return False
            width, height, size = self._image_info(image_bytes)
            if size < 1024 or size > 5 * 1024 * 1024:
                return False
            emoji_id = uuid.uuid4().hex
            path = self.emoji_dir / f"{emoji_id}.jpg"
            path.write_bytes(image_bytes)
            self.emojis.append(
                {
                    "emoji_id": emoji_id,
                    "sha256": digest,
                    "file_path": str(path),
                    "collected_time": datetime.now().isoformat(timespec="seconds"),
                    "source_group": event.get_group_id() or "private",
                    "source_user": event.get_sender_id(),
                    "usage_count": 0,
                    "file_info": {"size": size, "width": width, "height": height},
                    "tags": {},
                }
            )
            self._save_data()
            return True

    async def _handle_collect_images(self, event: AstrMessageEvent) -> bool:
        images = await self._get_all_image_bytes(event)
        if not images:
            return False
        collected = 0
        for image_bytes in images:
            if await self._collect_emoji(event, image_bytes):
                collected += 1
        stats = self._stats()
        if collected:
            await event.send(MessageChain().message(f"成功收集 {collected} 个表情包\n当前: {stats['total_count']}/{stats['max_emojis']}\n继续发送图片或发送 /结束 退出"))
        else:
            await event.send(MessageChain().message("图片不符合标准、已存在，或表情包库已满\n标准：1KB~5MB"))
        return True

    @filter.regex(r"[\s\S]*")
    async def observe_private(self, event: AstrMessageEvent):
        if not event.is_private_chat() or not self._is_superuser(event):
            return
        user_id = event.get_sender_id()
        text = event.get_message_str().strip()
        if user_id in self.classifying_users and text not in {"addimg", "/addimg", "endimg", "/endimg"}:
            images = await self._get_all_image_bytes(event)
            if not images:
                return
            lines = []
            try:
                for idx, image_bytes in enumerate(images, 1):
                    category, analysis, path = await self._store_classified_image(image_bytes)
                    lines.append(f"{idx}. 分类: {category}\n分析: {analysis}\n文件: {path}")
                await event.send(MessageChain().message("已保存\n" + "\n\n".join(lines)))
            except Exception as exc:
                await event.send(MessageChain().message("处理失败: " + str(exc)))
            yield event.make_result().stop_event()
            return
        if user_id in self.collecting_users and not text.startswith("/"):
            handled = await self._handle_collect_images(event)
            if handled:
                yield event.make_result().stop_event()

    @filter.command("addimg")
    async def addimg(self, event: AstrMessageEvent):
        if not event.is_private_chat():
            yield event.plain_result("此命令仅限私聊使用").stop_event()
            return
        if not self._is_superuser(event):
            yield event.plain_result("你无权限使用此命令").stop_event()
            return
        self.classifying_users.add(event.get_sender_id())
        yield event.plain_result("开始处理，发送图片给我吧！").stop_event()

    @filter.command("endimg")
    async def endimg(self, event: AstrMessageEvent):
        self.classifying_users.discard(event.get_sender_id())
        yield event.plain_result("结束处理，已停止接收图片").stop_event()

    @filter.command("\u6536\u96c6\u8868\u60c5")
    async def collect_start(self, event: AstrMessageEvent):
        if not event.is_private_chat() or not self._is_superuser(event):
            yield event.plain_result("你无权限使用此命令").stop_event()
            return
        self.collecting_users.add(event.get_sender_id())
        yield event.plain_result("已进入表情包收集模式\n请发送表情包图片\n发送 /结束 退出收集模式").stop_event()

    @filter.command("\u7ed3\u675f")
    async def collect_end(self, event: AstrMessageEvent):
        self.collecting_users.discard(event.get_sender_id())
        stats = self._stats()
        yield event.plain_result(f"已退出表情包收集模式\n总数量: {stats['total_count']}/{stats['max_emojis']}\n已打标签: {stats['tagged_count']}\n未打标签: {stats['untagged_count']}").stop_event()

    @filter.command("\u72b6\u6001")
    async def status(self, event: AstrMessageEvent):
        if not event.is_private_chat() or not self._is_superuser(event):
            return
        stats = self._stats()
        yield event.plain_result(f"表情包库状态\n总数量: {stats['total_count']}/{stats['max_emojis']}\n已打标签: {stats['tagged_count']}\n未打标签: {stats['untagged_count']}\n总大小: {stats['total_size_mb']} MB\n库状态: {'已满' if stats['storage_full'] else '正常'}").stop_event()

    @filter.command("\u6e05\u7406")
    async def clean(self, event: AstrMessageEvent):
        if not event.is_private_chat() or not self._is_superuser(event):
            return
        cutoff = datetime.now() - timedelta(days=self._cfg_int("expire_days", 30))
        before = len(self.emojis)
        kept = []
        for item in self.emojis:
            try:
                t = datetime.fromisoformat(item.get("collected_time", "1970-01-01T00:00:00"))
            except Exception:
                t = datetime.fromtimestamp(0)
            if t >= cutoff:
                kept.append(item)
            else:
                Path(item.get("file_path", "")).unlink(missing_ok=True)
        self.emojis = kept
        self._save_data()
        yield event.plain_result(f"清理完成\n清理前: {before} 个\n清理后: {len(self.emojis)} 个\n已删除: {before - len(self.emojis)} 个").stop_event()

    @filter.command("\u5217\u8868")
    async def list_recent(self, event: AstrMessageEvent):
        if not event.is_private_chat() or not self._is_superuser(event):
            return
        if not self.emojis:
            yield event.plain_result("表情包库为空").stop_event()
            return
        recent = sorted(self.emojis, key=lambda x: x.get("collected_time", ""), reverse=True)[:10]
        lines = [f"最近收集的表情包（共 {len(self.emojis)} 个）"]
        for idx, item in enumerate(recent, 1):
            info = item.get("file_info", {})
            lines.append(f"{idx}. ID: {item['emoji_id'][:8]} | {info.get('size', 0) / 1024:.1f}KB | {info.get('width')}x{info.get('height')} | 使用 {item.get('usage_count', 0)} 次")
        yield event.plain_result("\n".join(lines)).stop_event()

    @filter.command("\u6253\u6807")
    async def tag(self, event: AstrMessageEvent, count: int = 5):
        if not event.is_private_chat() or not self._is_superuser(event):
            return
        match = re.search(r"\d+", event.get_message_str())
        if match:
            count = int(match.group(0))
        count = max(1, min(int(count or 5), 20))
        untagged = [item for item in self.emojis if not item.get("tags")]
        if not untagged:
            yield event.plain_result("所有表情包都已打标签").stop_event()
            return
        await event.send(MessageChain().message(f"开始分析 {min(count, len(untagged))} 个表情包...\n未打标签: {len(untagged)} 个"))
        success = 0
        failed = 0
        samples = []
        for item in untagged[:count]:
            path = Path(item.get("file_path", ""))
            try:
                tags = await self._tag_emoji(path.read_bytes())
                item["tags"] = tags
                success += 1
                if len(samples) < 3:
                    samples.append((item, tags))
            except Exception as exc:
                failed += 1
                logger.error("[emoji_tools] tag failed %s: %s", path, exc)
        self._save_data()
        lines = [f"打标完成\n成功: {success} 个\n失败: {failed} 个\n剩余未打标: {max(0, len(untagged) - success)} 个"]
        for idx, (item, tags) in enumerate(samples, 1):
            lines.append(f"{idx}. ID: {item['emoji_id'][:8]}\n情绪: {', '.join(tags.get('emotions', []))}\n场景: {', '.join(tags.get('scenes', []))}\n描述: {tags.get('description', '')}")
        await event.send(MessageChain().message("\n\n".join(lines)))
        for item, _ in samples:
            path = Path(item.get("file_path", ""))
            if path.exists():
                await event.send(MessageChain().file_image(str(path)))
                await asyncio.sleep(0.5)
        yield event.make_result().stop_event()
