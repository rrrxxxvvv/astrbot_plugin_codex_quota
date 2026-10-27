"""Authenticated snapshot receiver; no OpenAI credentials or chat history are transferred."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import copy
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path


def verify_signature(body: bytes, timestamp: str, signature: str, secret: str, now=None):
    try:
        if len(secret) < 32 or abs((now or time.time()) - int(timestamp)) > 300:
            return False
        expected = hmac.new(secret.encode(), timestamp.encode() + b"\n" + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (ValueError, TypeError):
        return False


def validate_snapshot(raw, now=None):
    now = time.time() if now is None else now
    if not isinstance(raw, dict) or raw.get("version") != 2:
        raise ValueError("unsupported snapshot")
    qq = raw.get("qq")
    if not isinstance(qq, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", qq):
        raise ValueError("missing or invalid bound QQ")
    key = raw.get("accountKey", "")
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("invalid account key")
    checked = datetime.fromisoformat(raw["checkedAt"].replace("Z", "+00:00"))
    if checked.tzinfo is None or checked.timestamp() > now + 300:
        raise ValueError("invalid sample time")
    windows = raw.get("windows")
    if not isinstance(windows, list) or not 1 <= len(windows) <= 32:
        raise ValueError("invalid windows")
    clean, keys = [], set()
    for w in windows:
        if not isinstance(w, dict):
            raise ValueError("invalid window")
        for name in ("usedPercent", "windowDurationMins", "resetsAt"):
            value = w.get(name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError("invalid quota value")
        if not 0 <= w["usedPercent"] <= 100 or not 0 < w["windowDurationMins"] <= 525600 or not 0 < w["resetsAt"] < 4102444800:
            raise ValueError("quota out of range")
        if not isinstance(w.get("key"), str) or not 1 <= len(w["key"]) <= 200 or w["key"] in keys:
            raise ValueError("invalid window key")
        keys.add(w["key"])
        label = w.get("label", "额度")
        if not isinstance(label, str) or len(label) > 200 or any(ord(c) < 32 for c in label):
            raise ValueError("invalid label")
        clean.append({k: w[k] for k in ("key", "usedPercent", "windowDurationMins", "resetsAt")} | {"label": label})
    cards = raw.get("resetCredits")
    if cards is not None:
        if not isinstance(cards, dict) or type(cards.get("availableCount")) is not int or not 0 <= cards["availableCount"] <= 10000:
            raise ValueError("invalid reset credit count")
        expiry = cards.get("expiresAt")
        if not isinstance(expiry, list) or len(expiry) > 10000:
            raise ValueError("invalid reset credit expiry list")
        if any(x is not None and (type(x) not in (int, float) or not math.isfinite(x) or not 0 < x < 4102444800) for x in expiry):
            raise ValueError("invalid reset credit expiry")
        identified = cards.get("cards", [])
        if not isinstance(identified, list) or len(identified) > 10000:
            raise ValueError("invalid reset cards")
        unique = set()
        for card in identified:
            if not isinstance(card, dict) or not isinstance(card.get("id"), str) or not re.fullmatch(r"[a-f0-9]{64}", card["id"]) or card["id"] in unique:
                raise ValueError("invalid card identity")
            unique.add(card["id"])
            expiry_at = card.get("expiresAt")
            if expiry_at is not None and (type(expiry_at) not in (int, float) or not math.isfinite(expiry_at) or not 0 < expiry_at < 4102444800):
                raise ValueError("invalid card expiry")
        cards = {"availableCount": cards["availableCount"], "expiresAt": expiry, "cards": [{"id": c["id"], "expiresAt": c.get("expiresAt")} for c in identified]}
    return {"version": 2, "qq": qq, "accountKey": key, "checkedAt": checked.isoformat(), "windows": clean, "resetCredits": cards}


def bj_time(value):
    dt = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def format_snapshot(snapshot, now=None):
    if not snapshot:
        return "尚未收到桌面额度数据，请检查监控程序和上传配置。"
    now = time.time() if now is None else now
    age = now - datetime.fromisoformat(snapshot["checkedAt"]).timestamp()
    lines = ["Codex 额度（北京时间）"]
    if age > 180:
        lines.append("数据已过期：桌面监控可能离线，以下为最后一次采集结果。")
    for w in snapshot["windows"]:
        lines.extend([f'{w["label"]}：已用 {w["usedPercent"]:g}%，剩余 {100-w["usedPercent"]:g}%', f'重置时间：{bj_time(w["resetsAt"])}'])
    lines.append(f'最后采集：{bj_time(snapshot["checkedAt"])}')
    cards = snapshot.get("resetCredits")
    if cards is None:
        lines.append("重置卡剩余次数／到期时间：接口暂未提供")
    else:
        lines.append(f'重置卡剩余次数：{cards["availableCount"]}')
        for index, expiry in enumerate(cards["expiresAt"], 1):
            label = bj_time(expiry) if expiry is not None else "接口未提供"
            lines.append(f'重置卡 {index} 到期时间：{label}')
        if cards["availableCount"] and not cards["expiresAt"]:
            lines.append("到期时间：接口未提供")
    return "\n".join(lines)


class QuotaStore:
    def __init__(self, path):
        self.path = Path(path)
        self.state = {"snapshot": None, "pending": []}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)

    def accept(self, raw, now=None):
        current = validate_snapshot(raw, now)
        old = self.state.get("snapshot")
        if old and datetime.fromisoformat(current["checkedAt"]) <= datetime.fromisoformat(old["checkedAt"]):
            return False
        same = old and old["accountKey"] == current["accountKey"]
        before = {w["key"]: w for w in old["windows"]} if same else {}
        changes = []
        categories = []
        sampled = datetime.fromisoformat(current["checkedAt"]).timestamp()
        for w in current["windows"]:
            prev = before.get(w["key"])
            if not prev:
                continue
            rolled = w["resetsAt"] > prev["resetsAt"] and sampled >= prev["resetsAt"]
            if rolled or w["usedPercent"] < prev["usedPercent"]:
                if rolled:
                    category = "每周额度更新" if w["windowDurationMins"] == 10080 else "五小时额度更新" if w["windowDurationMins"] == 300 else "额度周期更新"
                    detail = "原定周期已结束，进入新周期"
                elif sampled < prev["resetsAt"] and w["usedPercent"] == 0 and prev["usedPercent"] > 0:
                    category = "OpenAI 重置（推测）"
                    detail = "原定周期尚未结束，用量提前归零；接口未提供重置原因，无法确认是官方重置还是使用重置卡"
                else:
                    category = "可用额度增加"
                    detail = "检测到已用比例下降，接口未提供原因"
                categories.append(category)
                changes.append(f'{category} · {w["label"]}：{detail}（已用 {prev["usedPercent"]:g}% → {w["usedPercent"]:g}%）')
        pending = list(self.state.get("pending", [])) if same else []
        if changes:
            pending.append({"at": current["checkedAt"], "changes": changes, "categories": list(dict.fromkeys(categories))})
        previous = self.state
        self.state = {"snapshot": current, "pending": pending[-100:], "expiryNotified": self.state.get("expiryNotified", {})}
        try:
            self.save()
        except Exception:
            self.state = previous
            raise
        return True

    def queue_expiry_reminders(self, now=None):
        now = time.time() if now is None else now
        snap = self.state.get("snapshot")
        if not snap:
            return
        before = copy.deepcopy(self.state)
        credits = snap.get("resetCredits") or {}
        active = {c["id"]: c for c in credits.get("cards", []) if c.get("expiresAt") and c["expiresAt"] > now} if credits.get("availableCount", 0) > 0 else {}
        # Do not deliver queued alerts after the card is used, removed or expires.
        self.state["pending"] = [e for e in self.state.get("pending", []) if e.get("kind") != "cardExpiry" or (e.get("cardId") in active and active[e["cardId"]]["expiresAt"] == e.get("expiresAt"))]
        fresh = 0 <= now - datetime.fromisoformat(snap["checkedAt"]).timestamp() <= 180
        seen = self.state.setdefault("expiryNotified", {})
        if fresh:
            for card in active.values():
                remaining = card["expiresAt"] - now
                for seconds, label in ((7 * 86400, "不足一周"), (86400, "不足一天")):
                    key = f'{snap["accountKey"]}:{card["id"]}:{card["expiresAt"]}:{seconds}'
                    if remaining < seconds and key not in seen:
                        seen[key] = now
                        self.state["pending"].append({"kind": "cardExpiry", "cardId": card["id"], "expiresAt": card["expiresAt"], "at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "changes": [f'重置卡（{card["id"][:8]}）即将到期：{label}；到期时间：{bj_time(card["expiresAt"])}（北京时间）']})
        if self.state != before:
            try:
                self.save()
            except Exception:
                self.state = before
                raise
