"""FastAPI REST & WoW-style Sharded WebSocket router for Anime Universe City."""

from __future__ import annotations

import asyncio
import math
import os
import re
import secrets
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Cookie, File, Header, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from antonella.services.au_city_service import (
    AU_CITY_DISTRICTS,
    AU_FLOOR_PLANS,
    AU_SHOP_CATALOG,
    AU_TO_ZEN_RATE,
    DEFAULT_AVATAR_APPEARANCE,
    GROUP_PHASE_HARD_CAP,
    INDOOR_FURNITURE_CATEGORIES,
    NEWSLETTER_BONUS_ZC,
    PERMANENT_HOME_ITEMS,
    PROXIMITY_RADIUS_M,
    PUBLIC_PHASE_SOFT_CAP,
    STYLE_TIER_LABELS,
    VEHICLE_PHYSICS_CLASSES,
    AuCityService,
    compute_10x_catalog_budget,
    get_au_city_google_client_id,
)

router = APIRouter(prefix="/api/au-city", tags=["au-city"])
au_service = AuCityService()
SESSION_COOKIE_NAME = "au_city_session"


class GoogleAuthReq(BaseModel):
    id_token: str


class LocalDevAuthReq(BaseModel):
    email: str = "dev.versekeeper@animeuniverse.local"
    display_name: str = "Versekeeper"


class ProfileUpdateReq(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    display_name: str = ""
    bio: str = ""


class ShopBuyReq(BaseModel):
    item_id: str
    pay_with: str = "zc"
    target_location: str = "inventory"


class LoadoutReq(BaseModel):
    skin_id: Optional[str] = None
    vehicle_id: Optional[str] = None
    pet_id: Optional[str] = None


class ConvertCoinReq(BaseModel):
    direction: str = "au_to_zen"
    amount: int


class UpgradeHomeReq(BaseModel):
    tier_id: str
    pay_with: str = "zc"


class StorageMoveReq(BaseModel):
    inv_id: str
    target_location: str
    pos_x: float = 0.0
    pos_y: float = 0.0
    pos_z: float = 0.0
    rot_y: float = 0.0


class PermanentHomeMoveReq(BaseModel):
    item_id: str
    pos_x: float = 0.0
    pos_y: float = 0.0
    pos_z: float = 0.0
    rot_y: float = 0.0


class InviteHomeReq(BaseModel):
    username: str


class TradeCreateReq(BaseModel):
    target_username: str
    offer_zc: int = 0
    request_zc: int = 0
    offer_inv_id: str = ""


class GroupCreateReq(BaseModel):
    name: str
    is_private: bool = False


class ContactAddReq(BaseModel):
    username: str
    nickname: str = ""


class DmSendReq(BaseModel):
    target_username: str
    body: str


class NewsletterReq(BaseModel):
    email: str


class DiscoveryReq(BaseModel):
    district_id: str


class SettingsPatchReq(BaseModel):
    settings: Dict[str, Any]


class SwitchPhaseReq(BaseModel):
    phase_id: str


class AvatarAppearanceReq(BaseModel):
    appearance: Dict[str, Any]


class AdminChatDeleteReq(BaseModel):
    msg_id: str


class AdminChatClearReq(BaseModel):
    channel: str = "global"


class AdminMuteReq(BaseModel):
    user_id: str
    minutes: float = 0.0


class AdminRenameReq(BaseModel):
    user_id: str
    username: str
    display_name: str = ""


class AdminBanReq(BaseModel):
    user_id: str
    banned: bool = True
    reason: str = ""


class AdminCurrencyReq(BaseModel):
    user_id: str
    mode: str = "add"
    zencoins: int = 0
    au_coins: int = 0
    reason: str = ""


class AdminTopupReq(BaseModel):
    user_id: Optional[str] = None


class WorldLayoutUpsertReq(BaseModel):
    obj_id: Optional[str] = None
    id: Optional[str] = None
    kind: str = "prop"
    model: str
    label: str = ""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rot_y: float = 0.0
    scale: Optional[float] = None
    scale_x: Optional[float] = None
    scale_y: Optional[float] = None
    scale_z: Optional[float] = None
    deleted: bool = False
    meta: Dict[str, Any] = Field(default_factory=dict)


class WorldTimeReq(BaseModel):
    cycle_hour: Optional[float] = None
    cycle_speed: Optional[float] = None
    paused: Optional[bool] = None
    preset: Optional[str] = None


def _extract_token(cookie_tok: Optional[str], header_tok: Optional[str]) -> str:
    return (header_tok or cookie_tok or "").strip()


def _require_user(cookie_tok: Optional[str], header_tok: Optional[str]) -> Dict[str, Any]:
    tok = _extract_token(cookie_tok, header_tok)
    user = au_service.get_user_by_token(tok)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in with Google to access your AU City account.")
    if user.get("banned"):
        raise HTTPException(status_code=403, detail=f"Account banned: {user.get('ban_reason') or 'Suspended by administrator.'}")
    return user


def _require_admin(cookie_tok: Optional[str], header_tok: Optional[str]) -> Dict[str, Any]:
    user = _require_user(cookie_tok, header_tok)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator privileges required.")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=7 * 86400,
        path="/",
    )


class _LoopSafeLock:
    """Thread-safe lock usable via `async with` or `with` across multiple event loops (e.g. pytest TestClient)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def __enter__(self) -> "_LoopSafeLock":
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._lock.release()

    async def __aenter__(self) -> "_LoopSafeLock":
        self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()


class AuPhaseManager:
    """World of Warcraft-style dynamic sharding, party gravity, and private home instancing."""

    def __init__(self, service: AuCityService):
        self.service = service
        self.clients: Dict[str, Dict[str, Any]] = {}
        self.lock = _LoopSafeLock()

    def _assign_outdoor_phase_locked(self, user_bundle: Dict[str, Any]) -> str:
        grp = user_bundle.get("group")
        if grp and grp.get("group_id"):
            gid = grp["group_id"]
            phase = f"group:{gid}"
            cnt = sum(1 for c in self.clients.values() if c["phase_id"] == phase)
            if cnt < GROUP_PHASE_HARD_CAP:
                return phase

        # Auto-bucket ungrouped players into public shards capped at 24 for smooth 60fps WebGL
        idx = 1
        while True:
            cand = f"public-{idx}"
            cnt = sum(1 for c in self.clients.values() if c["phase_id"] == cand)
            if cnt < PUBLIC_PHASE_SOFT_CAP:
                return cand
            idx += 1

    async def connect(self, ws: WebSocket, user_bundle: Dict[str, Any]) -> str:
        await ws.accept()
        uid = user_bundle["user_id"]
        async with self.lock:
            phase_id = self._assign_outdoor_phase_locked(user_bundle)
            self.clients[uid] = {
                "ws": ws,
                "user": user_bundle,
                "phase_id": phase_id,
                "zone": "outdoor",
                "x": 0.0,
                "y": 0.0,
                "z": 16.0,
                "rot_y": 0.0,
                "mode": "map",
                "vehicle_id": "",
                "emote": "",
            }
        await self.broadcast_phase_snapshot(phase_id)
        return phase_id

    async def disconnect(self, uid: str) -> None:
        old_phase = None
        async with self.lock:
            rec = self.clients.pop(uid, None)
            if rec:
                old_phase = rec["phase_id"]
        if old_phase:
            await self.broadcast_phase_snapshot(old_phase)

    async def disconnect_with_close(self, uid: str, code: int = 4403, reason: str = "") -> None:
        old_phase = None
        ws_obj = None
        async with self.lock:
            rec = self.clients.pop(uid, None)
            if rec:
                old_phase = rec["phase_id"]
                ws_obj = rec.get("ws")
        if ws_obj:
            try:
                await ws_obj.send_json({"type": "banned", "detail": reason or "Banned by administrator."})
            except Exception:
                pass
            try:
                await ws_obj.close(code=code, reason=(reason or "Banned")[:120])
            except Exception:
                pass
        if old_phase:
            await self.broadcast_phase_snapshot(old_phase)

    async def broadcast_all(self, packet: Dict[str, Any]) -> None:
        async with self.lock:
            sockets = [c["ws"] for c in self.clients.values()]
        for sock in sockets:
            try:
                await sock.send_json(packet)
            except Exception:
                pass

    async def send_to_user(self, uid: str, packet: Dict[str, Any]) -> bool:
        async with self.lock:
            rec = self.clients.get(uid)
            sock = rec.get("ws") if rec else None
        if not sock:
            return False
        try:
            await sock.send_json(packet)
            return True
        except Exception:
            return False

    async def refresh_user_payload(self, uid: str) -> None:
        bundle = await asyncio.to_thread(self.service.get_user_bundle, uid)
        if not bundle:
            return
        async with self.lock:
            rec = self.clients.get(uid)
            if not rec:
                return
            rec["user"] = bundle
            phase_id = rec["phase_id"]
            player_pub = self._public_player_payload(rec)
            sockets = [c["ws"] for c in self.clients.values() if c["phase_id"] == phase_id]
        pkt = {
            "type": "player_profile_updated",
            "user_id": uid,
            "username": player_pub["username"],
            "display_name": player_pub["display_name"],
            "avatar_glb_url": player_pub.get("avatar_glb_url"),
            "avatar_appearance": player_pub.get("avatar_appearance"),
            "player": player_pub,
        }
        for sock in sockets:
            try:
                await sock.send_json(pkt)
            except Exception:
                pass

    def list_online_players(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for uid, rec in self.clients.items():
            u = rec.get("user") or {}
            out.append({
                "user_id": uid,
                "username": u.get("username") or u.get("display_name") or "Versekeeper",
                "display_name": u.get("display_name") or "Versekeeper",
                "zone": rec.get("zone") or "outdoor",
            })
        return out

    def _public_player_payload(self, c: Dict[str, Any]) -> Dict[str, Any]:
        u = c["user"]
        return {
            "user_id": u["user_id"],
            "username": u.get("username") or u.get("display_name") or "Versekeeper",
            "display_name": u.get("display_name") or "Versekeeper",
            "avatar_glb_url": u.get("avatar_glb_url"),
            "avatar_appearance": u.get("avatar_appearance") or DEFAULT_AVATAR_APPEARANCE,
            "equipped_skin_id": u.get("equipped_skin_id"),
            "equipped_pet_id": u.get("equipped_pet_id"),
            "x": c["x"],
            "y": c["y"],
            "z": c["z"],
            "rot_y": c["rot_y"],
            "mode": c["mode"],
            "vehicle_id": c["vehicle_id"],
            "emote": c["emote"],
        }

    async def broadcast_phase_snapshot(self, phase_id: str) -> None:
        async with self.lock:
            peers = [c for c in self.clients.values() if c["phase_id"] == phase_id]
            players_payload = [self._public_player_payload(c) for c in peers]
            sockets = [c["ws"] for c in peers]
        packet = {"type": "phase_state", "phase_id": phase_id, "player_count": len(players_payload), "players": players_payload}
        for sock in sockets:
            try:
                await sock.send_json(packet)
            except Exception:
                pass

    async def switch_zone(self, uid: str, target_zone: str) -> Tuple[str, str]:
        bundle = self.service.get_user_bundle(uid)
        if not bundle:
            raise ValueError("User not found.")
        old_phase = None
        if target_zone.startswith("home:"):
            owner_id = target_zone.split(":", 1)[1]
            # Verifies ownership or invite before transitioning into private home phase
            home_data = self.service.get_home_view(owner_id, uid)
            max_guests = int(home_data["floor_plan"]["max_guests"])
            async with self.lock:
                occ = sum(1 for k, c in self.clients.items() if k != uid and c["phase_id"] == target_zone)
                if uid != owner_id and occ >= max_guests:
                    raise ValueError(f"Host apartment is at capacity ({max_guests} guests). Owner can upgrade floor plan for more capacity.")
                rec = self.clients.get(uid)
                if rec:
                    old_phase = rec["phase_id"]
                    rec["user"] = bundle
                    rec["zone"] = target_zone
                    rec["phase_id"] = target_zone
                    rec["x"], rec["y"], rec["z"] = 0.0, 0.0, 3.5
                    new_phase = target_zone
                else:
                    return "", ""
        else:
            async with self.lock:
                rec = self.clients.get(uid)
                if not rec:
                    return "", ""
                old_phase = rec["phase_id"]
                rec["user"] = bundle
                rec["zone"] = "outdoor"
                new_phase = self._assign_outdoor_phase_locked(bundle)
                rec["phase_id"] = new_phase
                rec["x"], rec["y"], rec["z"] = 0.0, 0.0, 16.0

        if old_phase and old_phase != new_phase:
            await self.broadcast_phase_snapshot(old_phase)
        await self.broadcast_phase_snapshot(new_phase)
        return old_phase or "", new_phase

    async def switch_public_phase(self, uid: str, target_phase: str) -> str:
        """Move an outdoor player into an explicit public city channel (public-1..public-12 or auto)."""
        req = (target_phase or "auto").strip().lower()
        old_phase = None
        async with self.lock:
            rec = self.clients.get(uid)
            if not rec:
                raise ValueError("Not connected to the city.")
            if str(rec.get("zone") or "").startswith("home:"):
                raise ValueError("Exit your apartment before switching city channels.")
            old_phase = rec["phase_id"]
            if req == "auto":
                new_phase = self._assign_outdoor_phase_locked(rec["user"])
            else:
                if not req.startswith("public-") or not req.split("-", 1)[1].isdigit():
                    raise ValueError("Choose a valid city channel (public-1 to public-12).")
                idx = int(req.split("-", 1)[1])
                if not (1 <= idx <= 12):
                    raise ValueError("Choose a valid city channel (1 to 12).")
                cnt = sum(1 for k, c in self.clients.items() if k != uid and c["phase_id"] == req)
                if cnt >= PUBLIC_PHASE_SOFT_CAP:
                    raise ValueError(f"City Channel {idx} is full ({PUBLIC_PHASE_SOFT_CAP}/{PUBLIC_PHASE_SOFT_CAP}).")
                new_phase = req
            rec["phase_id"] = new_phase

        if old_phase and old_phase != new_phase:
            await self.broadcast_phase_snapshot(old_phase)
        await self.broadcast_phase_snapshot(new_phase)
        return new_phase

    async def handle_message(self, uid: str, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "move":
            async with self.lock:
                rec = self.clients.get(uid)
                if not rec:
                    return
                rec["x"] = float(msg.get("x", rec["x"]))
                rec["y"] = float(msg.get("y", rec["y"]))
                rec["z"] = float(msg.get("z", rec["z"]))
                rec["rot_y"] = float(msg.get("rot_y", rec["rot_y"]))
                rec["mode"] = str(msg.get("mode", rec["mode"]))[:12]
                rec["vehicle_id"] = str(msg.get("vehicle_id", ""))[:40]
                rec["emote"] = str(msg.get("emote", ""))[:24]
                phase_id = rec["phase_id"]
                payload = {"type": "player_moved", "player": self._public_player_payload(rec)}
                targets = [c["ws"] for k, c in self.clients.items() if c["phase_id"] == phase_id and k != uid]
            for sock in targets:
                try:
                    await sock.send_json(payload)
                except Exception:
                    pass
            return

        if mtype == "switch_zone":
            target_zone = str(msg.get("zone") or "outdoor")
            try:
                _, new_phase = await self.switch_zone(uid, target_zone)
                async with self.lock:
                    sock = self.clients.get(uid, {}).get("ws")
                if sock:
                    await sock.send_json({"type": "zone_switched", "zone": target_zone, "phase_id": new_phase})
            except (ValueError, PermissionError) as exc:
                async with self.lock:
                    sock = self.clients.get(uid, {}).get("ws")
                if sock:
                    await sock.send_json({"type": "error", "detail": str(exc)})
            return

        if mtype == "refresh_phase":
            await self.switch_zone(uid, self.clients.get(uid, {}).get("zone", "outdoor"))
            return

        if mtype == "switch_phase":
            target_phase = str(msg.get("phase_id") or "auto")
            try:
                new_phase = await self.switch_public_phase(uid, target_phase)
                async with self.lock:
                    sock = self.clients.get(uid, {}).get("ws")
                if sock:
                    await sock.send_json({"type": "phase_switched", "phase_id": new_phase})
            except (ValueError, PermissionError) as exc:
                async with self.lock:
                    sock = self.clients.get(uid, {}).get("ws")
                if sock:
                    await sock.send_json({"type": "error", "detail": str(exc)})
            return

        if mtype == "respawn":
            async with self.lock:
                rec = self.clients.get(uid)
                if not rec:
                    return
                if str(rec.get("zone") or "").startswith("home:"):
                    rec["x"], rec["y"], rec["z"], rec["rot_y"] = 0.0, 0.0, 2.5, 0.0
                else:
                    rec["x"], rec["y"], rec["z"], rec["rot_y"] = 0.0, 0.0, 18.0, 0.0
                phase_id = rec["phase_id"]
                payload = {"type": "player_moved", "player": self._public_player_payload(rec)}
                targets = [c["ws"] for k, c in self.clients.items() if c["phase_id"] == phase_id and k != uid]
            for sock in targets:
                try:
                    await sock.send_json(payload)
                except Exception:
                    pass
            return

        if mtype == "instrument":
            async with self.lock:
                rec = self.clients.get(uid)
                if not rec:
                    return
                phase_id = rec["phase_id"]
                pkt = {
                    "type": "instrument",
                    "user_id": uid,
                    "username": rec["user"].get("username") or "Versekeeper",
                    "kind": str(msg.get("kind") or "guitar")[:16],
                    "note": str(msg.get("note") or "Em")[:16],
                }
                targets = [c["ws"] for k, c in self.clients.items() if c["phase_id"] == phase_id and k != uid]
            for sock in targets:
                try:
                    await sock.send_json(pkt)
                except Exception:
                    pass
            return

        if mtype == "chat":
            async with self.lock:
                rec = self.clients.get(uid)
                if not rec:
                    return
                phase_id = rec["phase_id"]
                zone = rec["zone"]
                ux, uz = rec["x"], rec["z"]
                uname = rec["user"].get("username") or rec["user"].get("display_name") or "Versekeeper"

            req_ch = str(msg.get("channel") or "proximity").lower()
            if zone.startswith("home:") or phase_id.startswith("home:"):
                channel = "home_private"
            elif req_ch == "global":
                channel = "global"
            else:
                channel = "proximity"

            try:
                saved = await asyncio.to_thread(
                    self.service.save_chat_message,
                    channel,
                    phase_id,
                    uid,
                    uname,
                    str(msg.get("body") or ""),
                    ux,
                    uz,
                )
            except (ValueError, PermissionError) as exc:
                async with self.lock:
                    sock = self.clients.get(uid, {}).get("ws")
                if sock:
                    await sock.send_json({"type": "chat_blocked", "detail": str(exc)})
                return

            pkt = {"type": "chat", "message": saved}
            async with self.lock:
                if channel == "global":
                    targets = [c["ws"] for c in self.clients.values() if not c["phase_id"].startswith("home:")]
                elif channel == "home_private":
                    targets = [c["ws"] for c in self.clients.values() if c["phase_id"] == phase_id]
                else:
                    targets = [
                        c["ws"]
                        for c in self.clients.values()
                        if c["phase_id"] == phase_id and math.hypot(c["x"] - ux, c["z"] - uz) <= PROXIMITY_RADIUS_M
                    ]
            for sock in targets:
                try:
                    await sock.send_json(pkt)
                except Exception:
                    pass


phase_manager = AuPhaseManager(au_service)


@router.get("/health")
def au_city_health():
    return {
        "ok": True,
        "public_phase_soft_cap": PUBLIC_PHASE_SOFT_CAP,
        "group_phase_hard_cap": GROUP_PHASE_HARD_CAP,
        "active_online": len(phase_manager.clients),
    }


@router.get("/config")
def au_city_config():
    google_cid = get_au_city_google_client_id()
    return {
        "ok": True,
        "google_client_id_configured": bool(google_cid),
        "google_client_id": google_cid,
        "phase_caps": {"public": PUBLIC_PHASE_SOFT_CAP, "group": GROUP_PHASE_HARD_CAP},
        "proximity_radius_m": PROXIMITY_RADIUS_M,
        "au_to_zen_rate": AU_TO_ZEN_RATE,
        "newsletter_bonus_zc": NEWSLETTER_BONUS_ZC,
        "districts": AU_CITY_DISTRICTS,
        "floor_plans": AU_FLOOR_PLANS,
        "permanent_home_items": PERMANENT_HOME_ITEMS,
        "vehicle_physics_classes": VEHICLE_PHYSICS_CLASSES,
        "style_tiers": STYLE_TIER_LABELS,
        "indoor_furniture_categories": list(INDOOR_FURNITURE_CATEGORIES),
        "default_avatar_appearance": DEFAULT_AVATAR_APPEARANCE,
        "shop_catalog": AU_SHOP_CATALOG,
        "tripo_manifest": au_service.get_tripo_manifest(),
    }


@router.get("/world/state")
def get_world_state():
    """Public read of synchronized world layout overrides and 24-hour world clock."""
    return {
        "ok": True,
        "layout": au_service.get_world_layout(),
        "time": au_service.get_world_clock(),
    }


@router.get("/online-players")
def get_online_players():
    """Return currently connected online players with user_id, username, display_name, and zone."""
    return {"ok": True, "players": phase_manager.list_online_players()}


@router.get("/phases")
def list_public_phases():
    counts: Dict[str, int] = {}
    for c in phase_manager.clients.values():
        pid = str(c.get("phase_id") or "")
        if pid.startswith("public-"):
            counts[pid] = counts.get(pid, 0) + 1
    max_idx = 6
    for pid in counts:
        part = pid.split("-", 1)[1] if "-" in pid else ""
        if part.isdigit():
            max_idx = max(max_idx, min(12, int(part)))
    channels = []
    for i in range(1, max_idx + 1):
        pid = f"public-{i}"
        channels.append({
            "phase_id": pid,
            "label": f"City Channel {i}",
            "player_count": counts.get(pid, 0),
            "capacity": PUBLIC_PHASE_SOFT_CAP,
        })
    return {"channels": channels}


@router.post("/phase/switch")
async def switch_public_phase(
    body: SwitchPhaseReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        new_phase = await phase_manager.switch_public_phase(user["user_id"], body.phase_id)
        return {"ok": True, "phase_id": new_phase}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/settings")
def update_user_settings(
    body: SettingsPatchReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        updated = au_service.update_user_settings(user["user_id"], body.settings)
        return {"ok": True, "user": updated}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/google")
async def auth_google(body: GoogleAuthReq, response: Response):
    try:
        claims = await asyncio.to_thread(au_service.verify_google_id_token, body.id_token)
        res = au_service.upsert_google_user_session(claims["sub"], claims["email"], claims["name"])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_session_cookie(response, res["session_token"])
    return res


@router.post("/auth/local-google-dev")
def auth_local_google_dev(body: LocalDevAuthReq, response: Response):
    if get_au_city_google_client_id():
        raise HTTPException(status_code=403, detail="Live Google Sign-In is active; use Google Sign-In.")
    email = (body.email or "dev.versekeeper@animeuniverse.local").strip().lower()
    sub = "local_dev_" + email
    try:
        res = au_service.upsert_google_user_session(sub, email, body.display_name or email.split("@")[0])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    _set_session_cookie(response, res["session_token"])
    return res


@router.get("/auth/me")
def auth_me(
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    tok = _extract_token(au_city_session, x_au_session)
    user = au_service.get_user_by_token(tok)
    if user and user.get("banned"):
        raise HTTPException(status_code=403, detail=f"Account banned: {user.get('ban_reason') or 'Suspended by administrator.'}")
    return {"authenticated": bool(user), "user": user}


@router.post("/auth/profile")
async def auth_profile(
    body: ProfileUpdateReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        updated = await asyncio.to_thread(au_service.update_profile, user["user_id"], body.username, body.display_name, body.bio)
        await phase_manager.refresh_user_payload(user["user_id"])
        return {"ok": True, "user": updated}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/logout")
def auth_logout(
    response: Response,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    tok = _extract_token(au_city_session, x_au_session)
    au_service.logout(tok)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.put("/avatar/appearance")
async def update_avatar_appearance(
    body: AvatarAppearanceReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        updated = await asyncio.to_thread(au_service.update_avatar_appearance, user["user_id"], body.appearance)
        await phase_manager.refresh_user_payload(user["user_id"])
        return {"ok": True, "avatar_appearance": updated["avatar_appearance"], "user": updated}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/avatar/file/{user_id}.glb")
def get_avatar_glb_file(user_id: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{3,64}", user_id or ""):
        raise HTTPException(status_code=400, detail="Invalid avatar user_id.")
    blob = au_service.get_avatar_blob(user_id)
    if not blob:
        raise HTTPException(status_code=404, detail="Custom avatar model not found.")
    return Response(
        content=blob["data"],
        media_type="model/gltf-binary",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/avatar/upload")
async def upload_avatar_to_tripo(
    file: UploadFile = File(...),
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    img_bytes = await file.read()
    try:
        res = await asyncio.to_thread(au_service.generate_user_avatar_from_image, user["user_id"], img_bytes, file.filename or "avatar.png")
        await phase_manager.refresh_user_payload(user["user_id"])
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/shop/buy")
def shop_buy(
    body: ShopBuyReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return au_service.buy_item(user["user_id"], body.item_id, body.pay_with, body.target_location)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/loadout/equip")
def equip_loadout(
    body: LoadoutReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return {"ok": True, "user": au_service.equip_loadout(user["user_id"], body.skin_id, body.vehicle_id, body.pet_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/wallet/convert-au-to-zen")
def wallet_convert(
    body: ConvertCoinReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    if body.direction != "au_to_zen":
        raise HTTPException(status_code=400, detail="Zencoins cannot be converted back into AU Coins.")
    try:
        return {"ok": True, "user": au_service.convert_au_to_zencoins(user["user_id"], body.amount)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/checkin")
def daily_checkin(
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    return au_service.claim_daily_checkin(user["user_id"])


@router.post("/districts/discover")
def discover_district(
    body: DiscoveryReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return au_service.record_district_discovery(user["user_id"], body.district_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/newsletter/subscribe")
def subscribe_newsletter(
    body: NewsletterReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = au_service.get_user_by_token(_extract_token(au_city_session, x_au_session))
    try:
        return au_service.subscribe_newsletter(body.email, user["user_id"] if user else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/storage/move")
def storage_move(
    body: StorageMoveReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return {"ok": True, "user": au_service.move_item_location(user["user_id"], body.inv_id, body.target_location, body.pos_x, body.pos_y, body.pos_z, body.rot_y)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/home/permanent-move")
def permanent_home_move(
    body: PermanentHomeMoveReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return {
            "ok": True,
            "home": au_service.move_permanent_home_item(
                user["user_id"],
                body.item_id,
                body.pos_x,
                body.pos_y,
                body.pos_z,
                body.rot_y,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/home/upgrade")
def home_upgrade(
    body: UpgradeHomeReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return {"ok": True, "user": au_service.upgrade_apartment_tier(user["user_id"], body.tier_id, body.pay_with)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/home/invite")
async def home_invite(
    body: InviteHomeReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        res = await asyncio.to_thread(au_service.invite_to_home, user["user_id"], body.username)
        guest_id = res["invited"]["user_id"]
        await phase_manager.send_to_user(
            guest_id,
            {
                "type": "home_invite",
                "from_user": {
                    "user_id": user["user_id"],
                    "username": user.get("username") or user.get("display_name") or "Versekeeper",
                    "display_name": user.get("display_name") or "Versekeeper",
                },
                "owner_id": user["user_id"],
            },
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/home/{owner_id}")
def get_home(
    owner_id: str,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return au_service.get_home_view(owner_id, user["user_id"])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/trades")
def list_trades(
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    return {"trades": au_service.list_trades(user["user_id"])}


@router.post("/trades")
def create_trade(
    body: TradeCreateReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return au_service.create_trade(user["user_id"], body.target_username, body.offer_zc, body.request_zc, body.offer_inv_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/trades/{trade_id}/accept")
def accept_trade(
    trade_id: str,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return au_service.accept_trade(user["user_id"], trade_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/groups")
def list_groups():
    groups = au_service.list_public_groups()
    phase_counts: Dict[str, int] = {}
    for c in phase_manager.clients.values():
        phase_counts[c["phase_id"]] = phase_counts.get(c["phase_id"], 0) + 1
    return {"groups": groups, "live_phases": phase_counts}


@router.post("/groups")
def create_group(
    body: GroupCreateReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return au_service.create_group(user["user_id"], body.name, body.is_private)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/groups/{group_id}/join")
def join_group(
    group_id: str,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return au_service.join_group(user["user_id"], group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/groups/leave")
def leave_group(
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    return au_service.leave_group(user["user_id"])


@router.get("/contacts")
def list_contacts(
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    return {"contacts": au_service.list_contacts(user["user_id"])}


@router.post("/contacts")
def add_contact(
    body: ContactAddReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return {"contacts": au_service.add_contact(user["user_id"], body.username, body.nickname)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/dm")
def list_dms(
    with_username: Optional[str] = None,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    return {"messages": au_service.list_dms(user["user_id"], with_username)}


@router.post("/dm")
def send_dm(
    body: DmSendReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    user = _require_user(au_city_session, x_au_session)
    try:
        return au_service.send_dm(user["user_id"], body.target_username, body.body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/chat/history")
def chat_history(channel: str = "global", phase_id: Optional[str] = None):
    return {"messages": au_service.list_chat_history(channel, phase_id)}


# ---------- Admin Console, Moderation & Synchronized World Editor ----------
@router.get("/admin/overview")
def admin_overview(
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    _require_admin(au_city_session, x_au_session)
    overview = au_service.get_admin_overview()
    overview["online_user_ids"] = list(phase_manager.clients.keys())
    return overview


@router.post("/admin/chat/delete")
async def admin_delete_chat(
    body: AdminChatDeleteReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    try:
        res = await asyncio.to_thread(au_service.admin_delete_chat, admin["email"], body.msg_id)
        await phase_manager.broadcast_all({
            "type": "chat_deleted",
            "msg_id": res["msg_id"],
            "channel": res["channel"],
            "deleted_by": admin["email"],
        })
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/chat/clear")
async def admin_clear_chat(
    body: AdminChatClearReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    try:
        res = await asyncio.to_thread(au_service.admin_clear_chat, admin["email"], body.channel)
        await phase_manager.broadcast_all({
            "type": "chat_cleared",
            "channel": res["channel"],
            "cleared_count": res["cleared_count"],
        })
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/user/mute")
async def admin_mute_user(
    body: AdminMuteReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    try:
        res = await asyncio.to_thread(au_service.admin_mute_user, admin["email"], body.user_id, body.minutes)
        async with phase_manager.lock:
            rec = phase_manager.clients.get(body.user_id)
            if rec and res.get("user"):
                rec["user"] = res["user"]
        await phase_manager.send_to_user(
            body.user_id,
            {
                "type": "moderation_notice",
                "action": "unmute" if body.minutes <= 0 else "mute",
                "minutes": body.minutes,
                "muted_until": res["user"]["muted_until"],
            },
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/user/rename")
async def admin_rename_user(
    body: AdminRenameReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    try:
        res = await asyncio.to_thread(au_service.admin_rename_user, admin["email"], body.user_id, body.username, body.display_name)
        await phase_manager.refresh_user_payload(body.user_id)
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/user/ban")
async def admin_ban_user(
    body: AdminBanReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    try:
        res = await asyncio.to_thread(au_service.admin_ban_user, admin["email"], body.user_id, body.banned, body.reason)
        if body.banned:
            await phase_manager.disconnect_with_close(body.user_id, code=4403, reason=body.reason or "Banned by administrator.")
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/user/currency")
async def admin_user_currency(
    body: AdminCurrencyReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    try:
        res = await asyncio.to_thread(
            au_service.admin_adjust_currency,
            admin["email"],
            body.user_id,
            body.mode,
            body.zencoins,
            body.au_coins,
            body.reason,
        )
        u = res["user"]
        async with phase_manager.lock:
            rec = phase_manager.clients.get(body.user_id)
            if rec:
                rec["user"] = u
        await phase_manager.send_to_user(
            body.user_id,
            {
                "type": "wallet_updated",
                "zencoins": u["zencoins"],
                "au_coins": u["au_coins"],
                "user": u,
            },
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/topup-test-wallet")
async def admin_topup_test_wallet(
    body: Optional[AdminTopupReq] = Body(default=None),
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    target_uid = (body.user_id if body and body.user_id else admin["user_id"])
    try:
        res = await asyncio.to_thread(au_service.admin_topup_test_wallet, admin["email"], target_uid)
        u = res["user"]
        async with phase_manager.lock:
            rec = phase_manager.clients.get(target_uid)
            if rec:
                rec["user"] = u
        await phase_manager.send_to_user(
            target_uid,
            {
                "type": "wallet_updated",
                "zencoins": u["zencoins"],
                "au_coins": u["au_coins"],
                "user": u,
            },
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/world/layout")
async def admin_upsert_world_layout(
    body: WorldLayoutUpsertReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    try:
        saved = await asyncio.to_thread(au_service.upsert_world_layout_object, admin["email"], body.model_dump())
        layout = await asyncio.to_thread(au_service.get_world_layout)
        await phase_manager.broadcast_all({
            "type": "world_layout_update",
            "layout": layout,
            "object": saved,
        })
        return {"ok": True, "object": saved, "layout": layout}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/world/layout/reset")
async def admin_reset_world_layout(
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    layout = await asyncio.to_thread(au_service.reset_world_layout, admin["email"])
    await phase_manager.broadcast_all({"type": "world_layout_update", "layout": layout})
    return {"ok": True, "layout": layout}


@router.post("/admin/world/time")
async def admin_set_world_time(
    body: WorldTimeReq,
    au_city_session: Optional[str] = Cookie(default=None),
    x_au_session: Optional[str] = Header(default=None),
):
    admin = _require_admin(au_city_session, x_au_session)
    try:
        time_state = await asyncio.to_thread(
            au_service.set_world_clock,
            admin["email"],
            body.cycle_hour,
            body.cycle_speed,
            body.paused,
            body.preset,
        )
        await phase_manager.broadcast_all({"type": "world_time_update", "time": time_state})
        return {"ok": True, "time": time_state}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.websocket("/ws")
async def au_city_ws(websocket: WebSocket):
    tok = (websocket.query_params.get("token") or websocket.cookies.get(SESSION_COOKIE_NAME) or "").strip()
    user = au_service.get_user_by_token(tok)
    if not user:
        await websocket.close(code=4401)
        return
    if user.get("banned"):
        await websocket.close(code=4403)
        return
    uid = user["user_id"]
    phase_id = await phase_manager.connect(websocket, user)
    await websocket.send_json({"type": "connected", "user_id": uid, "phase_id": phase_id})
    try:
        while True:
            data = await websocket.receive_json()
            await phase_manager.handle_message(uid, data)
    except WebSocketDisconnect:
        await phase_manager.disconnect(uid)
    except Exception:
        await phase_manager.disconnect(uid)
