import asyncio
import base64
import hashlib
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import requests
from openai import AsyncOpenAI
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as MsgImage
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


def now_ts() -> float:
    return time.time()


class LegacyToolsPlugin(Star):
    BANGUMI_API_URL = "https://api.bgm.tv/calendar"

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self.name
        self.cache_dir = self.data_dir / "cache"
        self.origin_dir = self.data_dir / "translate_origin"
        self.saved_dir = self.data_dir / "translate_saved"
        self.history_dir = self.data_dir / "history"
        for path in (self.cache_dir, self.origin_dir, self.saved_dir, self.history_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.pending_super_resolution: dict[str, float] = {}
        self.pending_translate: dict[str, float] = {}
        self.tasks: list[asyncio.Task] = []
        self.last_bot = None

    async def initialize(self) -> None:
        self.tasks = [
            asyncio.create_task(self._cleanup_loop()),
            asyncio.create_task(self._dayanime_push_loop()),
        ]

    async def terminate(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.tasks = []

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        return bool(self.config.get(key, default))

    def _cfg_int(self, key: str, default: int) -> int:
        return int(self.config.get(key, default) or default)

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return "" if value is None else str(value)

    def _session_key(self, event: AstrMessageEvent) -> str:
        return f"{event.get_platform_id()}:{event.get_session_id()}:{event.get_sender_id()}"

    def _is_global_admin(self, event: AstrMessageEvent) -> bool:
        cfg = getattr(self.context, "_config", {}) or {}
        return str(event.get_sender_id()) in {str(i) for i in cfg.get("admins_id", [])}

    def _font(self, size: int):
        for path in (
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _wrap_text(self, text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
        lines: list[str] = []
        current = ""
        for char in text:
            test = current + char
            width = draw.textbbox((0, 0), test, font=font)[2]
            if width <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = char
        if current:
            lines.append(current)
        return lines

    async def _get_image_bytes(self, event: AstrMessageEvent) -> bytes | None:
        for comp in event.get_messages():
            if isinstance(comp, MsgImage):
                if comp.url or (comp.file and str(comp.file).startswith("http")):
                    url = comp.url or comp.file
                    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        return resp.content
                path = await comp.convert_to_file_path()
                return Path(path).read_bytes()
        return None

    async def _download_url_bytes(self, url: str, timeout: float = 30.0) -> bytes:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    async def _fetch_today_anime(self) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "AstrBot/legacy-tools"}) as client:
                resp = await client.get(self.BANGUMI_API_URL)
                resp.raise_for_status()
                data = resp.json()
            weekday = datetime.now().isoweekday()
            for item in data:
                if item.get("weekday", {}).get("id") == weekday:
                    return {"weekday_cn": item.get("weekday", {}).get("cn", ""), "items": item.get("items", [])}
        except Exception as exc:
            logger.error("[legacy_tools] fetch dayanime failed: %s", exc)
        return None

    async def _download_cover(self, url: str, anime_id: int) -> PILImage.Image:
        cache_path = self.cache_dir / f"cover_{anime_id}.jpg"
        if cache_path.exists():
            try:
                return PILImage.open(cache_path).convert("RGB")
            except Exception:
                pass
        try:
            if url.startswith("http://"):
                url = url.replace("http://", "https://", 1)
            url = url.replace("/c/", "/l/")
            content = await self._download_url_bytes(url, timeout=15)
            img = PILImage.open(BytesIO(content)).convert("RGB")
            img.save(cache_path, "JPEG", quality=95)
            return img
        except Exception as exc:
            logger.warning("[legacy_tools] cover download failed: %s", exc)
            img = PILImage.new("RGB", (200, 282), (220, 220, 220))
            draw = ImageDraw.Draw(img)
            font = self._font(20)
            text = "\u6682\u65e0\u5c01\u9762"
            box = draw.textbbox((0, 0), text, font=font)
            draw.text(((200 - (box[2] - box[0])) // 2, 130), text, fill=(130, 130, 130), font=font)
            return img

    async def _render_anime_image(self, data: dict) -> Path:
        cover_w, cover_h = 200, 282
        card_w, card_h = 220, 370
        per_row, spacing, margin = 5, 20, 40
        items = sorted(
            data.get("items", []),
            key=lambda x: x.get("collection", {}).get("doing", 0),
            reverse=True,
        )
        rows = max(1, (len(items) + per_row - 1) // per_row)
        width = per_row * card_w + (per_row - 1) * spacing + 2 * margin
        height = 120 + rows * card_h + (rows - 1) * spacing + 60
        img = PILImage.new("RGB", (width, height), (250, 250, 252))
        draw = ImageDraw.Draw(img)
        title_font, sub_font, name_font, info_font = self._font(38), self._font(20), self._font(17), self._font(15)
        title = "\u4eca\u65e5\u65b0\u756a\u901f\u62a5"
        box = draw.textbbox((0, 0), title, font=title_font)
        draw.text(((width - (box[2] - box[0])) // 2, margin), title, fill=(255, 107, 129), font=title_font)
        subtitle = f"{data.get('weekday_cn', '')} · \u5171 {len(items)} \u90e8\u52a8\u753b\u66f4\u65b0"
        box = draw.textbbox((0, 0), subtitle, font=sub_font)
        draw.text(((width - (box[2] - box[0])) // 2, margin + 55), subtitle, fill=(102, 102, 102), font=sub_font)
        current_y = margin + 120
        covers = await asyncio.gather(
            *[self._download_cover(a.get("images", {}).get("common", ""), int(a.get("id", 0))) for a in items],
            return_exceptions=True,
        )
        for idx, anime in enumerate(items):
            row, col = divmod(idx, per_row)
            x = margin + col * (card_w + spacing)
            y = current_y + row * (card_h + spacing)
            draw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=10, fill=(255, 255, 255))
            cover = covers[idx]
            if isinstance(cover, PILImage.Image):
                cover = cover.resize((cover_w, cover_h), PILImage.Resampling.LANCZOS)
                img.paste(cover, (x + 10, y + 10))
            name = anime.get("name_cn") or anime.get("name") or "\u672a\u77e5"
            ty = y + cover_h + 18
            for line in self._wrap_text(name, name_font, card_w - 20, draw)[:2]:
                box = draw.textbbox((0, 0), line, font=name_font)
                draw.text((x + max(10, (card_w - (box[2] - box[0])) // 2), ty), line, fill=(51, 51, 51), font=name_font)
                ty += 22
            score = anime.get("rating", {}).get("score")
            score_text = f"\u8bc4\u5206 {score:.1f}" if isinstance(score, (int, float)) and score else "\u6682\u65e0\u8bc4\u5206"
            box = draw.textbbox((0, 0), score_text, font=info_font)
            draw.text((x + (card_w - (box[2] - box[0])) // 2, ty + 5), score_text, fill=(102, 102, 102), font=info_font)
        footer = "\u6570\u636e\u6765\u6e90: Bangumi\u756a\u7ec4\u8ba1\u5212"
        box = draw.textbbox((0, 0), footer, font=info_font)
        draw.text(((width - (box[2] - box[0])) // 2, height - 40), footer, fill=(102, 102, 102), font=info_font)
        out = self.cache_dir / f"dayanime_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        img.save(out, "JPEG", quality=96)
        return out

    def _render_help_image(self) -> Path:
        plugins = [
            ("\u4eca\u65e5\u65b0\u756a", "dayanime / \u4eca\u65e5\u52a8\u753b / \u4eca\u65e5\u65b0\u756a", "\u83b7\u53d6\u4eca\u5929\u66f4\u65b0\u7684\u52a8\u753b\u5217\u8868"),
            ("\u8272\u56fe", "setu / \u8272\u56fe", "\u968f\u673a\u83b7\u53d6\u4e8c\u6b21\u5143\u56fe\u7247\uff0c\u652f\u6301\u6807\u7b7e"),
            ("JM", "jm\u641c\u7d22 / jm\u4e0b\u8f7d / jm\u67e5\u8be2", "JM \u641c\u7d22\u3001\u67e5\u8be2\u548c\u4e0b\u8f7d"),
            ("AI", "@\u673a\u5668\u4eba", "\u667a\u80fd\u804a\u5929\u548c Agent \u5de5\u5177\u80fd\u529b"),
            ("\u56fe\u7247\u7ffb\u8bd1", "\u7ffb\u8bd1\u56fe\u7247", "\u8bc6\u522b\u56fe\u7247\u6587\u5b57\u5e76\u7ffb\u8bd1"),
            ("\u56fe\u7247\u8d85\u5206", "\u56fe\u7247\u8d85\u5206", "\u4f7f\u7528\u963f\u91cc\u4e91\u56fe\u7247\u589e\u5f3a"),
            ("\u7fa4\u804a\u603b\u7ed3", "\u603b\u7ed3", "\u603b\u7ed3\u6700\u8fd1\u7fa4\u804a\u5185\u5bb9"),
        ]
        card_w, card_h, per_row, spacing, margin = 280, 210, 3, 20, 40
        rows = (len(plugins) + per_row - 1) // per_row
        width = per_row * card_w + (per_row - 1) * spacing + 2 * margin
        height = 130 + rows * card_h + (rows - 1) * spacing + 60
        img = PILImage.new("RGB", (width, height), (250, 250, 252))
        draw = ImageDraw.Draw(img)
        title_font, name_font, text_font = self._font(42), self._font(22), self._font(16)
        title = "Bot \u4f7f\u7528\u5e2e\u52a9"
        box = draw.textbbox((0, 0), title, font=title_font)
        draw.text(((width - (box[2] - box[0])) // 2, 36), title, fill=(255, 107, 129), font=title_font)
        colors = [(255, 107, 129), (255, 149, 0), (88, 86, 214), (52, 199, 89), (90, 200, 250), (175, 82, 222), (255, 159, 10)]
        y0 = 130
        for idx, (name, cmd, desc) in enumerate(plugins):
            row, col = divmod(idx, per_row)
            x = margin + col * (card_w + spacing)
            y = y0 + row * (card_h + spacing)
            color = colors[idx % len(colors)]
            draw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=10, fill=(255, 255, 255))
            draw.rectangle([x, y, x + card_w, y + 8], fill=color)
            draw.text((x + 18, y + 28), name, fill=color, font=name_font)
            ty = y + 68
            for line in self._wrap_text(desc, text_font, card_w - 36, draw)[:3]:
                draw.text((x + 18, ty), line, fill=(51, 51, 51), font=text_font)
                ty += 22
            draw.text((x + 18, y + card_h - 50), "/" + cmd, fill=(88, 166, 255), font=text_font)
        out = self.cache_dir / "help.jpg"
        img.save(out, "JPEG", quality=96)
        return out

    async def _super_resolution(self, image_bytes: bytes) -> tuple[bytes, str]:
        def _run():
            from alibabacloud_imageenhan20190930.client import Client
            from alibabacloud_imageenhan20190930.models import MakeSuperResolutionImageAdvanceRequest
            from alibabacloud_tea_openapi.models import Config
            from alibabacloud_tea_util.models import RuntimeOptions
            access_key_id = self._cfg_str("alibaba_cloud_access_key_id") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
            access_key_secret = self._cfg_str("alibaba_cloud_access_key_secret") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
            client = Client(Config(access_key_id=access_key_id, access_key_secret=access_key_secret, endpoint="imageenhan.cn-shanghai.aliyuncs.com", region_id="cn-shanghai"))
            req = MakeSuperResolutionImageAdvanceRequest(url_object=BytesIO(image_bytes), upscale_factor=4, output_quality=100)
            resp = client.make_super_resolution_image_advance(req, RuntimeOptions())
            result_url = resp.body.data.url
            return requests.get(result_url, timeout=60).content, result_url
        return await asyncio.to_thread(_run)

    async def _translate_image(self, image_bytes: bytes) -> Path:
        app_id = self._cfg_str("baidu_translate_app_id")
        app_key = self._cfg_str("baidu_translate_app_key")
        if not app_id or not app_key:
            raise RuntimeError("Baidu image translate app_id/app_key is not configured")
        ts = int(time.time() * 1000)
        origin = self.origin_dir / f"{ts}.jpg"
        origin.write_bytes(image_bytes)

        def _run() -> Path:
            salt = str(random.randint(10000, 99999))
            sign_str = app_id + hashlib.md5(image_bytes).hexdigest() + salt + "APICUID" + "mac" + app_key
            sign = hashlib.md5(sign_str.encode()).hexdigest()
            payload = {"from": "auto", "to": "zh", "appid": app_id, "salt": salt, "sign": sign, "cuid": "APICUID", "mac": "mac", "version": 3, "paste": 1}
            files = {"image": (origin.name, image_bytes, "image/jpeg")}
            resp = requests.post("http://api.fanyi.baidu.com/api/trans/sdk/picture", data=payload, files=files, timeout=60)
            data = resp.json()
            paste_img = data.get("data", {}).get("pasteImg")
            if not paste_img:
                raise RuntimeError(f"Baidu API failed: {data}")
            out = self.saved_dir / f"{origin.stem}_translated.jpg"
            out.write_bytes(base64.b64decode(paste_img))
            return out
        return await asyncio.to_thread(_run)

    def _append_group_history(self, event: AstrMessageEvent) -> None:
        group_id = event.get_group_id()
        text = event.get_message_str().strip()
        if not group_id or not text:
            return
        path = self.history_dir / f"{group_id}.json"
        try:
            records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except Exception:
            records = []
        records.append({"time": datetime.now().isoformat(timespec="seconds"), "user": event.get_sender_name() or event.get_sender_id(), "text": text})
        limit = max(500, self._cfg_int("summary_history_limit", 300) * 3)
        records = records[-limit:]
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    async def _generate_summary(self, group_id: str) -> str:
        path = self.history_dir / f"{group_id}.json"
        if not path.exists():
            return "\u6682\u65e0\u804a\u5929\u8bb0\u5f55\u53ef\u4f9b\u603b\u7ed3"
        records = json.loads(path.read_text(encoding="utf-8"))
        records = records[-self._cfg_int("summary_history_limit", 300):]
        if not records:
            return "\u6682\u65e0\u804a\u5929\u8bb0\u5f55\u53ef\u4f9b\u603b\u7ed3"
        chat = "\n".join(f"[{r['time'][-8:-3]}] {r['user']}: {r['text']}" for r in records)
        api_key = self._cfg_str("summary_api_key")
        if not api_key:
            return "\u7fa4\u804a\u603b\u7ed3 API Key \u672a\u914d\u7f6e"
        client = AsyncOpenAI(base_url=self._cfg_str("summary_api_base", "https://api.deepseek.com/v1"), api_key=api_key, timeout=self._cfg_int("summary_timeout", 60))
        system_prompt = (
            "\u4f60\u662f\u4e00\u4e2a\u7fa4\u804a\u5206\u6790\u52a9\u624b\uff0c\u8bf7\u7528\u7b80\u6d01\u4e2d\u6587\u603b\u7ed3\u7fa4\u804a\u3002\n"
            "\u683c\u5f0f\uff1a\u3010\u65f6\u95f4\u7ebf\u3011\u3010\u8bdd\u9898\u3011\u3010\u6d3b\u8dc3\u7528\u6237\u3011\u3010\u6c1b\u56f4\u3011\u3002\n"
            "\u603b\u5b57\u6570\u63a7\u5236\u5728300\u5b57\u5185\uff0c\u4e0d\u8981\u4f7f\u7528 markdown\u3002"
        )
        resp = await client.chat.completions.create(
            model=self._cfg_str("summary_model", "deepseek-chat"),
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": chat}],
            max_tokens=1000,
            temperature=0.7,
        )
        content = resp.choices[0].message.content
        return content.strip() if content else "\u751f\u6210\u603b\u7ed3\u5931\u8d25\uff1a\u8fd4\u56de\u4e3a\u7a7a"

    async def _cleanup_once(self) -> None:
        image_days = self._cfg_int("image_cache_retention_days", 3)
        threshold = now_ts() - image_days * 86400
        count = 0
        for root in (self.cache_dir, self.origin_dir, self.saved_dir):
            for file in root.glob("*"):
                if file.is_file() and file.stat().st_mtime < threshold:
                    file.unlink(missing_ok=True)
                    count += 1
        retain_days = self._cfg_int("message_retention_days", 30)
        cutoff = datetime.now() - timedelta(days=retain_days)
        for file in self.history_dir.glob("*.json"):
            try:
                records = [r for r in json.loads(file.read_text(encoding="utf-8")) if datetime.fromisoformat(r["time"]) >= cutoff]
                file.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                logger.warning("[legacy_tools] cleanup history failed %s: %s", file, exc)
        logger.info("[legacy_tools] cleanup done, removed %s cache files", count)

    async def _sleep_until(self, hour: int, minute: int) -> None:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

    async def _cleanup_loop(self) -> None:
        while True:
            await self._sleep_until(3, 0)
            try:
                await self._cleanup_once()
            except Exception as exc:
                logger.error("[legacy_tools] cleanup loop failed: %s", exc)

    async def _dayanime_push_loop(self) -> None:
        while True:
            await self._sleep_until(self._cfg_int("dayanime_push_hour", 8), self._cfg_int("dayanime_push_minute", 0))
            if not self._cfg_bool("dayanime_daily_push_enabled", True):
                continue
            if self.last_bot is None:
                logger.warning("[legacy_tools] skip dayanime push: no aiocqhttp bot captured yet")
                continue
            data = await self._fetch_today_anime()
            if not data or not data.get("items"):
                continue
            path = await self._render_anime_image(data)
            message = [{"type": "image", "data": {"file": f"file:///{path}"}}]
            for group_id in self.config.get("dayanime_push_groups", []) or []:
                try:
                    await self.last_bot.call_action("send_group_msg", group_id=int(group_id), message=message)
                    await asyncio.sleep(1)
                except Exception as exc:
                    logger.error("[legacy_tools] dayanime push failed group=%s: %s", group_id, exc)

    @filter.regex(r"[\s\S]*")
    async def observe_messages(self, event: AstrMessageEvent):
        if getattr(event, "bot", None) is not None:
            self.last_bot = event.bot
        self._append_group_history(event)
        key = self._session_key(event)
        if key in self.pending_super_resolution:
            if now_ts() > self.pending_super_resolution.pop(key):
                yield event.plain_result("\u64cd\u4f5c\u5df2\u8d85\u65f6\uff0c\u8bf7\u91cd\u65b0\u53d1\u9001\u201c\u56fe\u7247\u8d85\u5206\u201d\u547d\u4ee4").stop_event()
                return
            image_bytes = await self._get_image_bytes(event)
            if not image_bytes:
                self.pending_super_resolution[key] = now_ts() + 60
                yield event.plain_result("\u672a\u68c0\u6d4b\u5230\u56fe\u7247\uff0c\u8bf7\u91cd\u65b0\u53d1\u9001").stop_event()
                return
            try:
                result_bytes, url = await self._super_resolution(image_bytes)
                out = self.cache_dir / f"super_res_{int(now_ts())}.jpg"
                out.write_bytes(result_bytes)
                await event.send(MessageChain().file_image(str(out)).message("\n\u8d85\u5206\u56fe\u7247\u94fe\u63a5\uff1a" + url))
            except Exception as exc:
                yield event.plain_result("\u56fe\u7247\u5904\u7406\u5931\u8d25\uff1a" + str(exc)).stop_event()
                return
            yield event.make_result().stop_event()
            return
        if key in self.pending_translate:
            if now_ts() > self.pending_translate.pop(key):
                yield event.plain_result("\u64cd\u4f5c\u5df2\u8d85\u65f6\uff0c\u8bf7\u91cd\u65b0\u53d1\u9001\u201c\u7ffb\u8bd1\u56fe\u7247\u201d\u547d\u4ee4").stop_event()
                return
            image_bytes = await self._get_image_bytes(event)
            if not image_bytes:
                self.pending_translate[key] = now_ts() + 60
                yield event.plain_result("\u672a\u68c0\u6d4b\u5230\u56fe\u7247\uff0c\u8bf7\u91cd\u65b0\u53d1\u9001").stop_event()
                return
            try:
                out = await self._translate_image(image_bytes)
                await event.send(MessageChain().file_image(str(out)))
            except Exception as exc:
                yield event.plain_result("\u7ffb\u8bd1\u5931\u8d25\uff1a" + str(exc)).stop_event()
                return
            yield event.make_result().stop_event()

    @filter.command("dayanime", alias={"\u4eca\u65e5\u52a8\u753b", "\u4eca\u65e5\u65b0\u756a"})
    async def dayanime(self, event: AstrMessageEvent):
        await event.send(MessageChain().message("\u6b63\u5728\u83b7\u53d6\u4eca\u65e5\u65b0\u756a..."))
        data = await self._fetch_today_anime()
        if not data or not data.get("items"):
            yield event.plain_result("\u83b7\u53d6\u52a8\u753b\u6570\u636e\u5931\u8d25\u6216\u4eca\u5929\u6ca1\u6709\u52a8\u753b\u66f4\u65b0").stop_event()
            return
        path = await self._render_anime_image(data)
        yield event.image_result(str(path)).stop_event()

    @filter.command("\u56fe\u7247\u8d85\u5206")
    async def super_resolution_start(self, event: AstrMessageEvent):
        self.pending_super_resolution[self._session_key(event)] = now_ts() + 60
        yield event.plain_result("\u8bf7\u572860\u79d2\u5185\u53d1\u9001\u8981\u8d85\u5206\u7684\u56fe\u7247").stop_event()

    @filter.command("\u7ffb\u8bd1\u56fe\u7247")
    async def translate_start(self, event: AstrMessageEvent):
        self.pending_translate[self._session_key(event)] = now_ts() + 60
        yield event.plain_result("\u8bf7\u572860\u79d2\u5185\u53d1\u9001\u8981\u7ffb\u8bd1\u7684\u56fe\u7247").stop_event()

    @filter.command("\u603b\u7ed3")
    async def summary(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("\u603b\u7ed3\u529f\u80fd\u4ec5\u652f\u6301\u7fa4\u804a").stop_event()
            return
        await event.send(MessageChain().message("\u6b63\u5728\u8bfb\u53d6\u804a\u5929\u8bb0\u5f55\u5e76\u751f\u6210\u603b\u7ed3\uff0c\u8bf7\u7a0d\u5019..."))
        try:
            summary = await self._generate_summary(group_id)
        except Exception as exc:
            logger.error("[legacy_tools] summary failed: %s", exc)
            yield event.plain_result("\u751f\u6210\u603b\u7ed3\u5931\u8d25\uff1a" + str(exc)).stop_event()
            return
        yield event.plain_result("\u7fa4\u804a\u603b\u7ed3\n\n" + summary).stop_event()

    @filter.regex(r"^(?:help|\u5e2e\u52a9|\u83dc\u5355|\u529f\u80fd)$")
    async def help_image(self, event: AstrMessageEvent):
        if not event.is_at_or_wake_command and not event.is_private_chat():
            return
        try:
            path = self._render_help_image()
            yield event.image_result(str(path)).stop_event()
        except Exception as exc:
            logger.error("[legacy_tools] render help failed: %s", exc)
            yield event.plain_result("Bot help: dayanime / setu / jm / AI / translate / super resolution / summary").stop_event()
