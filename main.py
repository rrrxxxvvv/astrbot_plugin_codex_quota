from __future__ import annotations

import asyncio
import json
import re
import os
import secrets

from aiohttp import web
from pathlib import Path
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.event import AstrMessageEvent, MessageChain, filter

from .quota_bridge import QuotaStore, format_snapshot, verify_signature


class CodexQuotaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_codex_quota"

        self._push_preferences = {}
        self._preferences_file = self.data_dir / "push_preferences.json"
        if self._preferences_file.exists():
            self._push_preferences = json.loads(self._preferences_file.read_text(encoding="utf-8"))

    def _cfg(self, key, default=None):
        return self.config.get(key, default)

    async def initialize(self):
        self._ensure_connection_secret()
        await self._start_codex_quota()

    def _ensure_connection_secret(self):
        existing = self.config.get("codex_quota_secret", "")
        if existing and str(existing).strip():
            return
        self.config["codex_quota_secret"] = secrets.token_urlsafe(32)
        try:
            self.config.save_config()
        except Exception:
            self.config["codex_quota_secret"] = existing
            raise RuntimeError("连接密钥保存失败，请检查插件配置文件写入权限") from None
        logger.info("[codex_quota] 已生成并保存连接密钥，请重新打开插件配置复制到桌面端")

    async def terminate(self):
        await self._stop_codex_quota()

    async def _start_codex_quota(self):
        self._quota_runner = None
        self._quota_push_task = None
        self._quota_stores = {}
        if not self._cfg("codex_quota_enabled", False):
            return
        secret = str(self._cfg("codex_quota_secret", ""))
        if len(secret) < 32:
            logger.error("[codex_quota] 额度接收未启动：连接密钥至少需要 32 个字符")
            return
        runner = None
        try:
            for file in (self.data_dir / "accounts").glob("*.json"):
                if re.fullmatch(r"[1-9][0-9]{4,11}", file.stem):
                    store = QuotaStore(file)
                    if store.state.get("snapshot", {}).get("qq") == file.stem:
                        self._quota_stores[file.stem] = store
            self._quota_lock = asyncio.Lock()
            app = web.Application(client_max_size=32768)
            app.router.add_post("/v1/quota", self._receive_codex_quota)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            host = str(self._cfg("codex_quota_host", "0.0.0.0"))
            port = int(self._cfg("codex_quota_port", 18765))
            await web.TCPSite(runner, host, port).start()
            self._quota_runner = runner
            self._quota_push_task = asyncio.create_task(self._codex_quota_push_loop())
            logger.info(f"[codex_quota] 额度接收已启动，端口 {port}")
        except Exception as e:
            if runner:
                await runner.cleanup()
            logger.error(f"[codex_quota] 额度接收启动失败：{type(e).__name__}")

    async def _stop_codex_quota(self):
        task = getattr(self, "_quota_push_task", None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runner = getattr(self, "_quota_runner", None)
        if runner:
            await runner.cleanup()

    async def _receive_codex_quota(self, request):
        body = await request.read()
        secret = str(self._cfg("codex_quota_secret", ""))
        if not verify_signature(body, request.headers.get("X-Quota-Time", ""), request.headers.get("X-Quota-Signature", ""), secret):
            return web.json_response({"ok": False}, status=401)
        try:
            raw = json.loads(body)
            async with self._quota_lock:
                qq = raw.get("qq") if isinstance(raw, dict) else None
                if not isinstance(qq, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", qq):
                    raise ValueError("missing bound QQ")
                store = self._quota_stores.get(qq)
                if store is None:
                    store = QuotaStore(self.data_dir / "accounts" / (qq + ".json"))
                store.accept(raw)
                self._quota_stores[qq] = store
        except (ValueError, TypeError, KeyError, OverflowError):
            return web.json_response({"ok": False}, status=400)
        return web.json_response({"ok": True})

    def _quota_platforms(self):
        return [p for p in self.context.platform_manager.get_insts() if p.meta().name == "aiocqhttp"]

    async def _quota_friends(self, platform):
        rows = await asyncio.wait_for(platform.get_client().call_action("get_friend_list"), timeout=15)
        if not isinstance(rows, list):
            raise ValueError("Invalid friend list response")
        return {str(row["user_id"]) for row in rows if isinstance(row, dict) and re.fullmatch(r"[1-9][0-9]{4,11}", str(row.get("user_id", "")))}

    async def _quota_friend_session(self, event):
        sender = str(event.get_sender_id())
        for platform in self._quota_platforms():
            session = f"{platform.meta().id}:FriendMessage:{sender}"
            if event.unified_msg_origin == session:
                try:
                    if sender in await self._quota_friends(platform):
                        return session
                except Exception:
                    logger.warning("[codex_quota] 无法获取好友列表，暂不处理查询或订阅")
        return None

    def _save_push_preference(self, session, enabled):
        updated = dict(self._push_preferences)
        updated[session] = enabled
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self._preferences_file.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(updated, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(self._preferences_file)
        self._push_preferences = updated

    async def _quota_push_once(self):
        for qq, store in list(self._quota_stores.items()):
            await self._quota_push_store(qq, store)

    async def _quota_push_store(self, qq, store):
        async with self._quota_lock:
            store.queue_expiry_reminders()
            if not store.state.get("pending"):
                return
        # A failed friend-list lookup never falls back to an old or guessed audience.
        platforms = self._quota_platforms()
        if not platforms:
            return
        current = set()
        for platform in platforms:
            if qq in await self._quota_friends(platform):
                current.add(f"{platform.meta().id}:FriendMessage:{qq}")
        if not current:
            return
        async with self._quota_lock:
            entries = store.state["pending"]
            for entry in entries:
                if "recipients" not in entry:
                    entry["recipients"] = sorted(s for s in current if self._push_preferences.get(s, True))
                entry["recipients"] = [s for s in entry["recipients"] if s in current and self._push_preferences.get(s, True)]
            store.save()
            targets = sorted({s for entry in entries for s in entry["recipients"]})
        for session in targets:
            async with self._quota_lock:
                store.queue_expiry_reminders()
                snapshot = store.state.get("snapshot")
                from datetime import datetime, timezone
                fresh = snapshot and (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["checkedAt"])).total_seconds() <= 180
                entries = [entry for entry in store.state["pending"] if session in entry.get("recipients", []) and (entry.get("kind") != "cardExpiry" or fresh)]
                if not entries:
                    continue
                changes = list(dict.fromkeys(c for entry in entries for c in entry["changes"]))
                categories = list(dict.fromkeys(c for e in entries for c in (["重置卡到期提醒"] if e.get("kind") == "cardExpiry" else e.get("categories", ["额度更新"]))))
                heading = " / ".join(categories)
                text = heading + "\n" + "\n".join(changes) + "\n\n" + format_snapshot(store.state.get("snapshot"))
            try:
                if self._push_preferences.get(session, True):
                    result = await asyncio.wait_for(self.context.send_message(session, MessageChain().message(text)), timeout=20)
                    if result is False:
                        raise RuntimeError("No matching message platform")
                async with self._quota_lock:
                    for entry in entries:
                        if session in entry["recipients"]:
                            entry["recipients"].remove(session)
                    store.save()
            except Exception as e:
                logger.warning(f"[codex_quota] 一位好友推送失败，稍后重试：{type(e).__name__}")
            await asyncio.sleep(0.2)
        async with self._quota_lock:
            store.state["pending"] = [entry for entry in store.state["pending"] if entry.get("recipients") or "recipients" not in entry]
            store.save()

    async def _codex_quota_push_loop(self):
        while True:
            try:
                await self._quota_push_once()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[codex_quota] 额度推送失败，稍后重试：{type(e).__name__}")
                await asyncio.sleep(60)

    @filter.command("额度查询", alias={"Codex额度", "codex额度", "周额度"})
    async def cmd_codex_quota(self, event: AstrMessageEvent):
        if not await self._quota_friend_session(event):
            yield event.plain_result("请添加机器人为好友后私聊使用；若已是好友，请稍后重试。")
            return
        store = getattr(self, "_quota_stores", {}).get(str(event.get_sender_id()))
        if not store:
            yield event.plain_result("尚未收到绑定到你 QQ 的额度数据，请在桌面端填写你的 QQ 号并上传。")
            return
        yield event.plain_result(format_snapshot(store.state.get("snapshot")))

    @filter.command("开启额度推送")
    async def cmd_enable_quota_push(self, event: AstrMessageEvent):
        session = await self._quota_friend_session(event)
        if not session:
            yield event.plain_result("请添加机器人为好友后私聊使用；若已是好友，请稍后重试。")
            return
        self._save_push_preference(session, True)
        yield event.plain_result("已开启额度推送，下次额度更新时通知你。")

    @filter.command("关闭额度推送")
    async def cmd_disable_quota_push(self, event: AstrMessageEvent):
        session = await self._quota_friend_session(event)
        if not session:
            yield event.plain_result("请添加机器人为好友后私聊使用；若已是好友，请稍后重试。")
            return
        self._save_push_preference(session, False)
        store = getattr(self, "_quota_stores", {}).get(str(event.get_sender_id()))
        if store:
            async with self._quota_lock:
                for entry in store.state.get("pending", []):
                    if session in entry.get("recipients", []):
                        entry["recipients"].remove(session)
                store.save()
        yield event.plain_result("已关闭额度推送，仍可使用 /额度查询。")

    @filter.command("额度推送状态")
    async def cmd_quota_push_status(self, event: AstrMessageEvent):
        session = await self._quota_friend_session(event)
        if not session:
            yield event.plain_result("请添加机器人为好友后私聊使用；若已是好友，请稍后重试。")
            return
        enabled = self._push_preferences.get(session, True)
        yield event.plain_result("你的额度推送：" + ("已开启" if enabled else "已关闭"))
