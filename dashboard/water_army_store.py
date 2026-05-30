"""
水军账号存储 — 收录、查询、管理所有检测到/手动标记的水军账号。

数据格式 (data/water_army/accounts.json):
[
  {
    "mid": 123456,
    "uname": "水军号名称",
    "avatar": "https://...",
    "suspicion_score": 85.5,
    "risk_level": "high",
    "added_by": "auto" | "manual",
    "added_at": "2026-05-29T23:00:00+08:00",
    "reasons": [
      {
        "type": "auto_detect" | "manual_mark" | "report_detail",
        "bvid": "BV...",
        "score": 85.5,
        "summary": "特征 F3=0.9, F12=1.0 (账号骨架异常)...",
        "feature_scores": {"F1": 0.5, "F2": 0.3, ...},
        "llm_type_name": "模板刷评",
        "recorded_at": "..."
      }
    ],
    "sources": ["BV1xx", "BV2yy"],
    "notes": ""
  }
]
"""

import json
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# 北京时区
TZ_BEIJING = timezone(timedelta(hours=8))

DATA_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "data"
STORE_FILE = DATA_DIR / "water_army" / "accounts.json"
STORE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(TZ_BEIJING).isoformat()


class WaterArmyStore:
    """线程安全的水军账号存储，支持增删改查 + 去重合并。"""

    @staticmethod
    def _load() -> list[dict]:
        if not STORE_FILE.exists():
            return []
        try:
            with open(STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def _save(accounts: list[dict]) -> None:
        os.makedirs(STORE_FILE.parent, exist_ok=True)
        tmp = str(STORE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(STORE_FILE))

    # ------------------------------------------------------------------
    #  CRUD
    # ------------------------------------------------------------------

    @classmethod
    def add(cls, mid: int, uname: str = "", avatar: str = "",
            suspicion_score: float = 0, risk_level: str = "low",
            added_by: str = "manual", reason: Optional[dict] = None,
            bvid: str = "", notes: str = "") -> dict:
        """
        添加水军账号。

        reason 格式 (WaterArmyReason):
          {"type": "auto_detect"|"manual_mark"|"report_detail",
           "bvid": "...", "score": 85.5, "summary": "...",
           "feature_scores": {...}, "llm_type_name": "..."}
        """
        with STORE_LOCK:
            accounts = cls._load()
            now = _now_iso()

            # 查找已有记录
            existing = None
            for a in accounts:
                if a.get("mid") == mid:
                    existing = a
                    break

            if existing:
                # 更新分数 (取最高)
                existing["suspicion_score"] = max(existing.get("suspicion_score", 0), suspicion_score)
                existing["risk_level"] = cls._calc_risk(existing["suspicion_score"])
                # 手动收录时优先标记为 manual（手动标记比自动检测更权威）
                if added_by == "manual" and existing.get("added_by") != "manual":
                    existing["added_by"] = "manual"
                # 追加原因
                reason_entry = reason or cls._build_reason(added_by, bvid, suspicion_score)
                reason_entry["recorded_at"] = now
                existing.setdefault("reasons", []).append(reason_entry)
                # 追加来源 BV
                if bvid and bvid not in existing.get("sources", []):
                    existing.setdefault("sources", []).append(bvid)
                # 更新昵称/头像 (优先保留非空)
                if uname and not existing.get("uname"):
                    existing["uname"] = uname
                if avatar and not existing.get("avatar"):
                    existing["avatar"] = avatar
                if notes:
                    existing["notes"] = (existing.get("notes", "") + "; " + notes).strip("; ")
            else:
                reason_entry = reason or cls._build_reason(added_by, bvid, suspicion_score)
                reason_entry["recorded_at"] = now
                account = {
                    "mid": mid,
                    "uname": uname or f"UID{mid}",
                    "avatar": avatar,
                    "suspicion_score": suspicion_score,
                    "risk_level": cls._calc_risk(suspicion_score),
                    "added_by": added_by,
                    "added_at": now,
                    "reasons": [reason_entry],
                    "sources": [bvid] if bvid else [],
                    "notes": notes,
                }
                accounts.append(account)

            # 按评分降序排列
            accounts.sort(key=lambda a: a.get("suspicion_score", 0), reverse=True)
            cls._save(accounts)

        return cls.get(mid) or {}

    @classmethod
    def batch_add_from_report(cls, scored_users: list, bvid: str = "",
                               threshold: float = 70.0) -> int:
        """从分析报告的 scored_users 中批量自动收录高风险账号。返回新增/更新数量。"""
        count = 0
        for user in scored_users:
            mid = user.get("mid", 0)
            score = user.get("score", user.get("suspicious_score", 0))
            if not mid or score < threshold:
                continue

            feature_scores = {}
            for k, v in user.items():
                if k.startswith("F") and isinstance(v, (int, float)):
                    feature_scores[k] = v

            # 生成摘要
            top_f = sorted(feature_scores.items(), key=lambda x: x[1], reverse=True)[:3]
            summary_parts = [f"{k}={v:.1f}" for k, v in top_f]
            summary = "特征: " + ", ".join(summary_parts) if summary_parts else f"可疑度={score:.0f}"

            reason = {
                "type": "auto_detect",
                "bvid": bvid,
                "score": score,
                "summary": summary,
                "feature_scores": feature_scores,
                "llm_type_name": user.get("llm_type_name", ""),
            }

            uname = user.get("uname", user.get("name", ""))
            avatar = user.get("avatar", user.get("face", ""))
            risk = user.get("risk_level", "high" if score >= 70 else "medium")

            cls.add(
                mid=mid, uname=uname, avatar=avatar,
                suspicion_score=score, risk_level=risk,
                added_by="auto", reason=reason, bvid=bvid,
            )
            count += 1

        return count

    @classmethod
    def get(cls, mid: int) -> Optional[dict]:
        with STORE_LOCK:
            accounts = cls._load()
            for a in accounts:
                if a.get("mid") == mid:
                    return a
        return None

    @classmethod
    def list_all(cls, sort_by: str = "score", order: str = "desc",
                 risk_filter: str = "", search: str = "",
                 added_by: str = "", page: int = 1, per_page: int = 20) -> dict:
        """
        获取水军账号列表，支持排序、筛选、搜索、分页。

        sort_by: "score" | "time" | "reasons"
        risk_filter: "high" | "medium" | "low"
        search: MID 或昵称关键字
        """
        with STORE_LOCK:
            accounts = cls._load()

        # 筛选
        if risk_filter:
            accounts = [a for a in accounts if a.get("risk_level") == risk_filter]
        if added_by:
            accounts = [a for a in accounts if a.get("added_by") == added_by]
        if search:
            s = search.strip().lower()
            accounts = [
                a for a in accounts
                if s in str(a.get("mid", ""))
                or s in (a.get("uname", "") or "").lower()
            ]

        # 排序
        key_map = {
            "score": lambda a: a.get("suspicious_score", 0),
            "time": lambda a: a.get("added_at", ""),
            "reasons": lambda a: len(a.get("reasons", [])),
        }
        reverse = order == "desc"
        accounts.sort(key=key_map.get(sort_by, key_map["score"]), reverse=reverse)

        total = len(accounts)
        start = (page - 1) * per_page
        page_data = accounts[start:start + per_page]

        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total > 0 else 1,
            "data": page_data,
        }

    @classmethod
    def remove(cls, mid: int) -> bool:
        with STORE_LOCK:
            accounts = cls._load()
            new_list = [a for a in accounts if a.get("mid") != mid]
            if len(new_list) == len(accounts):
                return False
            cls._save(new_list)
        return True

    @classmethod
    def batch_remove(cls, mids: list[int]) -> int:
        mid_set = set(mids)
        with STORE_LOCK:
            accounts = cls._load()
            removed = len([a for a in accounts if a.get("mid") in mid_set])
            new_list = [a for a in accounts if a.get("mid") not in mid_set]
            cls._save(new_list)
        return removed

    @classmethod
    def update_notes(cls, mid: int, notes: str) -> bool:
        with STORE_LOCK:
            accounts = cls._load()
            for a in accounts:
                if a.get("mid") == mid:
                    a["notes"] = notes
                    cls._save(accounts)
                    return True
        return False

    @classmethod
    def stats(cls) -> dict:
        with STORE_LOCK:
            accounts = cls._load()
        total = len(accounts)
        auto = sum(1 for a in accounts if a.get("added_by") == "auto")
        manual = sum(1 for a in accounts if a.get("added_by") == "manual")
        high = sum(1 for a in accounts if a.get("risk_level") == "high")
        medium = sum(1 for a in accounts if a.get("risk_level") == "medium")
        low = sum(1 for a in accounts if a.get("risk_level") == "low")
        avg_score = sum(a.get("suspicious_score", 0) for a in accounts) / total if total > 0 else 0
        return {
            "total": total,
            "auto": auto,
            "manual": manual,
            "high": high,
            "medium": medium,
            "low": low,
            "avg_score": round(avg_score, 1),
        }

    @classmethod
    def export_json(cls) -> list[dict]:
        with STORE_LOCK:
            return cls._load()

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_risk(score: float) -> str:
        if score >= 60:
            return "high"
        elif score >= 30:
            return "medium"
        return "low"

    @staticmethod
    def _build_reason(added_by: str, bvid: str, score: float) -> dict:
        if added_by == "manual":
            return {"type": "manual_mark", "bvid": bvid, "score": score,
                    "summary": "手动标记为水军账号"}
        return {"type": "auto_detect", "bvid": bvid, "score": score,
                "summary": f"自动检测: 可疑度 {score:.0f}"}
