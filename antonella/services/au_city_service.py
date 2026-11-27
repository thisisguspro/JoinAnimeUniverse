"""Server-side persistence, economy, auth, WoW-style housing, and Tripo avatar service for Anime Universe City."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from antonella.services.au_profanity_filter import filter_pg13_text

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = Path(os.getenv("AU_CITY_DB_PATH") or (WORKSPACE_ROOT / "data" / "au_city.db"))
PUBLIC_MODELS_DIR = WORKSPACE_ROOT / "frontend" / "public" / "models" / "au_city"
DIST_MODELS_DIR = WORKSPACE_ROOT / "frontend" / "dist" / "models" / "au_city"
TRIPO_MANIFEST_PATH = WORKSPACE_ROOT / "data" / "au_city_tripo_manifest.json"
TRIPO_BASE_URL = "https://api.tripo3d.ai/v2/openapi"
TRIPO_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AntonellaTripoClient/2.0"
AU_CITY_ENV_PATH = WORKSPACE_ROOT / "projects" / "anime-universe-city" / ".env"

ADMIN_EMAILS: Set[str] = {"gmromeu13@gmail.com"}


def is_admin_email(email: Optional[str]) -> bool:
    """Return True ONLY for explicitly authorized AU City administrator accounts."""
    return bool(email and email.strip().lower() in ADMIN_EMAILS)


def get_au_city_google_client_id() -> str:
    """Resolve Anime Universe City's isolated Google OAuth Client ID (never falls back to GOOGLE_CLIENT_ID)."""
    val = os.getenv("AU_CITY_GOOGLE_CLIENT_ID", "").strip()
    if val:
        return val
    if AU_CITY_ENV_PATH.exists():
        for raw_line in AU_CITY_ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "AU_CITY_GOOGLE_CLIENT_ID":
                return v.strip().strip('"').strip("'")
    return ""

PUBLIC_PHASE_SOFT_CAP = 24
GROUP_PHASE_HARD_CAP = 32
PROXIMITY_RADIUS_M = 28.0
AU_TO_ZEN_RATE = 100
NEWSLETTER_BONUS_ZC = 250
DAILY_CHECKIN_ZC = 75
DISCOVERY_BONUS_ZC = 35
MIN_AVATAR_BYTES = 32
MAX_AVATAR_BYTES = 8 * 1024 * 1024
MAX_AVATAR_GLB_BYTES = 16 * 1024 * 1024
AVATAR_HEAD_CHANGE_FEE_AU = 15
CUSTOM_AVATAR_UPLOAD_FEE_AU = 35
ALLOWED_BASE_CHAR_IDS: Tuple[str, ...] = (
    "base_versekeeper",
    "base_ronin",
    "base_idol",
    "base_mage",
    "base_beach_m",
    "base_beach_f",
)

# Exact 10 districts from https://anime-universe-hero.vercel.app/ + Central Landmarks
AU_CITY_DISTRICTS: List[Dict[str, Any]] = [
    {
        "id": "plaza",
        "studio": "Central AU Hologram Plaza",
        "role": "CENTRAL HUB",
        "title": "Welcome to Anime Universe City",
        "desc": "The glowing heart of Anime Universe. Meet fellow Versekeepers, explore every district, spin up RP groups, or inspect live storefronts.",
        "a": "#38bdf8",
        "b": "#ff6fd8",
        "points": ["Central Holo-Ring & Waitlist Beacon", "WoW-Style Sharded Plaza Hub", "Direct teleports & street spokes to every district"],
        "tools": [["Map Mode", "M", "#38bdf8"], ["RP Mode", "RP", "#ff6fd8"]],
        "x": 0.0,
        "z": 0.0,
        "model": "au_bld_hologram_plaza.glb",
    },
    {
        "id": "anime",
        "studio": "Animeverse",
        "role": "DISCOVER AU",
        "title": "The Anime Universe Story",
        "desc": "Start here to meet Anime Universe: who we are, what we value and the worlds we are building for fans everywhere.",
        "a": "#7ec8ff",
        "b": "#ffb3e6",
        "points": ["Our story, values and team", "Featured worlds and characters", "Latest news and creator stories"],
        "tools": [["About / Our Story", "A", "#5b8cff"], ["News", "N", "#ff6fd8"]],
        "x": -34.0,
        "z": -38.0,
        "model": "au_bld_animeverse.glb",
    },
    {
        "id": "manga",
        "studio": "Mangaverse",
        "role": "FOR READERS, ARTISTS & AUTHORS",
        "title": "Read and Create on InfiniteVerse",
        "desc": "InfiniteVerse is where AU stories come to life. Find series to read and see how artists and authors can collaborate with us.",
        "a": "#ffd3a5",
        "b": "#fd6585",
        "points": ["Explore InfiniteVerse and its stories", "Credits and featured work for artists and authors", "How to collaborate on a series"],
        "tools": [["InfiniteVerse", "IV", "#fd6585"], ["Collaborate", "C", "#5b8cff"]],
        "x": 34.0,
        "z": -38.0,
        "model": "au_bld_mangaverse.glb",
    },
    {
        "id": "games",
        "studio": "Gameverse",
        "role": "FOR PLAYERS",
        "title": "AU Games on the Roadmap",
        "desc": "Follow the games we are developing, catch development updates as they are approved and meet the team behind them.",
        "a": "#7f5bff",
        "b": "#3de8ff",
        "points": ["Upcoming games and their current status", "Approved development updates", "Developer spotlights from the team"],
        "tools": [["Roadmap", "R", "#7f5bff"], ["Gaming", "G", "#36b6c9"]],
        "x": -68.0,
        "z": -26.0,
        "model": "au_bld_gameverse.glb",
    },
    {
        "id": "fans",
        "studio": "Fanverse",
        "role": "FOR FANS",
        "title": "Everything for AU Fans",
        "desc": "Discover AU products, shop official merch and stay close to the community as new worlds arrive.",
        "a": "#ff5fa2",
        "b": "#ffb25b",
        "points": ["Product highlights from the Roadmap", "The official AU store", "Newsletter and community updates"],
        "tools": [["Store", "S", "#ff5fa2"], ["Roadmap", "R", "#7f5bff"], ["News", "N", "#ff8a4c"]],
        "x": -34.0,
        "z": 36.0,
        "model": "au_bld_fanverse.glb",
    },
    {
        "id": "cosplay",
        "studio": "Cosplayverse",
        "role": "FOR COSPLAYERS & INFLUENCERS",
        "title": "Cosplay and Community Spotlights",
        "desc": "Bring AU characters to life. Find ways to take part, get seen by the community and join our promotions.",
        "a": "#f8b5c4",
        "b": "#b79cff",
        "points": ["Ways for cosplayers and influencers to take part", "Community visibility for featured creators", "Promotions and sponsor highlights"],
        "tools": [["Collaborate", "C", "#b79cff"], ["News", "N", "#ff6fd8"]],
        "x": 68.0,
        "z": -26.0,
        "model": "au_bld_cosplayverse.glb",
    },
    {
        "id": "creators",
        "studio": "Creatorverse",
        "role": "FOR CREATORS",
        "title": "Collaborate with Anime Universe",
        "desc": "Artists, authors, animators and influencers can explore open opportunities and reach the right person on our team.",
        "a": "#8ef0c6",
        "b": "#5ab8ff",
        "points": ["Open participation opportunities", "Approved credits and featured work", "A direct enquiry route to our team"],
        "tools": [["Collaborate", "C", "#36b6c9"], ["Contact", "C", "#5b8cff"]],
        "x": 34.0,
        "z": 36.0,
        "model": "au_bld_creatorverse.glb",
    },
    {
        "id": "careers",
        "studio": "Careers Hangar",
        "role": "FOR CANDIDATES",
        "title": "Join the AU Team",
        "desc": "Help build the Anime Universe. Browse real openings and apply through our official careers route.",
        "a": "#6c8dff",
        "b": "#d6deff",
        "points": ["Current openings across the company", "Life and culture at AU", "Apply through the official application route"],
        "tools": [["Careers / Join Us", "J", "#6c8dff"]],
        "x": 68.0,
        "z": 46.0,
        "model": "au_bld_careers_hangar.glb",
    },
    {
        "id": "partners",
        "studio": "Partners Campus",
        "role": "FOR PARTNERS",
        "title": "Partner with Anime Universe",
        "desc": "Studios, brands and platforms can learn about AU and reach a real person to start a conversation.",
        "a": "#bfe3ff",
        "b": "#5b6ee8",
        "points": ["Company and business information", "Partnership and promotion opportunities", "A direct business enquiry route"],
        "tools": [["Partners & Investors", "P", "#5b6ee8"], ["Contact", "C", "#5b8cff"]],
        "x": -78.0,
        "z": 20.0,
        "model": "au_bld_partners_campus.glb",
    },
    {
        "id": "investors",
        "studio": "Investors Tower",
        "role": "FOR INVESTORS",
        "title": "Invest in Anime Universe",
        "desc": "Understand the AU story, the products on our Roadmap and how to reach our team directly.",
        "a": "#ffe3a1",
        "b": "#f7a8c8",
        "points": ["Company story and business information", "An overview of the product Roadmap", "A direct investor enquiry route"],
        "tools": [["Partners & Investors", "P", "#e0a24c"], ["Roadmap", "R", "#7f5bff"]],
        "x": 78.0,
        "z": 10.0,
        "model": "au_bld_investors_tower.glb",
    },
    {
        "id": "news",
        "studio": "Lantern Street",
        "role": "NEWS & UPDATES",
        "title": "What's New in the AU",
        "desc": "Catch published updates, product announcements and stories from our creators, all in one place.",
        "a": "#ff7a6b",
        "b": "#ffd36e",
        "points": ["Latest company updates", "Creator stories and features", "Announcements as they go live"],
        "tools": [["News / Updates", "N", "#ff7a6b"], ["Support", "S", "#5b8cff"]],
        "x": -68.0,
        "z": 58.0,
        "model": "au_bld_lantern_street.glb",
    },
    {
        "id": "residences",
        "studio": "AU Residences Tower",
        "role": "SHARED APARTMENT TOWER",
        "title": "Your Instanced AU Apartment",
        "desc": "Everyone shares the same skyline tower, yet each Versekeeper owns a private instanced home inside. Upgrade floor plans for larger layouts, panoramic penthouse views, more guests, and extra furniture.",
        "a": "#fb7185",
        "b": "#c084fc",
        "points": ["Invite-only private home instances & chat", "Starter Studio -> Sakura Loft -> Neo Duplex -> Celestial Penthouse", "Permanent MangaVerse Bookshelf, Piggy Bank Store & Infinity Verse TV"],
        "tools": [["Enter My Home", "H", "#fb7185"], ["Floor Plans", "U", "#c084fc"]],
        "x": 0.0,
        "z": -66.0,
        "model": "au_bld_residences_tower.glb",
    },
    {
        "id": "emporium",
        "studio": "AU City Emporium",
        "role": "SKINS, VEHICLES, PETS & FURNITURE",
        "title": "AU City Emporium Store",
        "desc": "Spend AU Coins and Zencoins on character skins, equal-handling vehicles, companion pets, playable instruments, and apartment furniture.",
        "a": "#34d399",
        "b": "#38bdf8",
        "points": ["Equal speed/handling across every vehicle class", "Zencoin-earnable items + AU Coin exclusive collectibles", "Direct delivery into Inventory or Storage Room"],
        "tools": [["Open Store", "B", "#34d399"]],
        "x": 0.0,
        "z": 66.0,
        "model": "au_bld_emporium.glb",
    },
]

VEHICLE_PHYSICS_CLASSES: Dict[str, Dict[str, float]] = {
    "car": {"max_speed": 22.0, "accel": 16.0, "reverse_speed": 9.0, "turn_rate": 2.35, "grip": 0.92, "lean_max": 0.05},
    "motorcycle": {"max_speed": 20.5, "accel": 19.0, "reverse_speed": 5.0, "turn_rate": 2.95, "grip": 0.90, "lean_max": 0.42},
    "bike": {"max_speed": 11.5, "accel": 10.5, "reverse_speed": 3.5, "turn_rate": 3.30, "grip": 0.96, "lean_max": 0.28},
}

AU_FLOOR_PLANS: Dict[str, Dict[str, Any]] = {
    "studio": {
        "id": "studio",
        "name": "Starter Studio",
        "cost_zc": 0,
        "cost_au": 0,
        "max_guests": 6,
        "max_furniture": 15,
        "room_w": 14.0,
        "room_d": 12.0,
        "view_label": "Lantern Street Courtyard View",
        "sky_tint": "#1e293b",
        "win_eligible": True,
    },
    "loft": {
        "id": "loft",
        "name": "Sakura Harbor Loft",
        "cost_zc": 1200,
        "cost_au": 12,
        "max_guests": 12,
        "max_furniture": 35,
        "room_w": 18.0,
        "room_d": 16.0,
        "view_label": "Harbor Pier & Sunset Skyline",
        "sky_tint": "#312e81",
        "win_eligible": True,
    },
    "duplex": {
        "id": "duplex",
        "name": "Neo-Shibuya Duplex",
        "cost_zc": 3500,
        "cost_au": 35,
        "max_guests": 20,
        "max_furniture": 65,
        "room_w": 24.0,
        "room_d": 20.0,
        "view_label": "Neon Mid-Rise City Horizon",
        "sky_tint": "#4c1d95",
        "win_eligible": True,
    },
    "penthouse": {
        "id": "penthouse",
        "name": "Celestial Rooftop Penthouse",
        "cost_zc": 0,
        "cost_au": 90,
        "max_guests": 32,
        "max_furniture": 120,
        "room_w": 30.0,
        "room_d": 26.0,
        "view_label": "360 Glass Dome Overlooking Central AU Hologram Plaza",
        "sky_tint": "#0f172a",
        "win_eligible": False,
    },
}

# Permanently built into every player's apartment; consume 0 slots and cannot be deleted or stored.
PERMANENT_HOME_ITEMS: List[Dict[str, Any]] = [
    {
        "item_id": "perm_mangaverse_bookshelf",
        "name": "MangaVerse Bookshelf",
        "interaction": "open_mangaverse",
        "model": "au_perm_mangaverse_bookshelf.glb",
        "slots_used": 0,
        "removable": False,
        "pos": [-5.2, 0.0, -4.6],
        "rot_y": 0.0,
        "desc": "Permanent bookshelf portal to MangaVerse / MangaReader (Netflix-style preview).",
    },
    {
        "item_id": "perm_au_piggy_bank",
        "name": "AU Piggy Bank",
        "interaction": "open_store",
        "model": "au_perm_au_piggy_bank.glb",
        "slots_used": 0,
        "removable": False,
        "pos": [0.0, 0.85, -4.8],
        "rot_y": 0.0,
        "desc": "Permanent ceramic AU piggy bank opening the AU City Store & Coin Converter.",
    },
    {
        "item_id": "perm_infinity_verse_tv",
        "name": "Infinity Verse Arcade TV",
        "interaction": "open_infinity_verse",
        "model": "au_perm_infinity_verse_tv.glb",
        "slots_used": 0,
        "removable": False,
        "pos": [5.2, 0.0, -4.6],
        "rot_y": -0.35,
        "desc": "Permanent smart console TV launching Infinity Verse / YouTube Playables placeholder hub.",
    },
]

STYLE_TIER_LABELS: Dict[str, str] = {
    "zen_wood": "Cozy Zen Wood",
    "tokyo_loft": "Tokyo Loft Modern",
    "shibuya_neon": "Shibuya Cyber Neon",
    "imperial_gold": "Imperial Sakura Gold",
}

INDOOR_FURNITURE_CATEGORIES: Tuple[str, ...] = (
    "sofa",
    "table",
    "desk",
    "bed",
    "lamp",
    "divider",
    "shelf",
    "plant",
    "rug",
    "arcade",
)

_RAW_SHOP_CATALOG: List[Dict[str, Any]] = [
    # Skins & Cosmetics
    {"id": "au_skin_versekeeper", "name": "Versekeeper Jacket", "category": "skin", "sub_category": "outfit", "style_tier": "tokyo_loft", "price_zc": 0, "price_au": 0, "currency": "zc", "tradeable": False, "model": "au_avatar_versekeeper.glb", "desc": "Starter Anime Universe blazer & hologram emblem."},
    {"id": "au_skin_shonen_blade", "name": "Crimson Ronin Haori", "category": "skin", "sub_category": "outfit", "style_tier": "zen_wood", "price_zc": 350, "price_au": 4, "currency": "both", "tradeable": True, "model": "au_avatar_ronin.glb", "desc": "Anime streetwear haori coat in detailed PBR fabric."},
    {"id": "au_skin_cyber_idol", "name": "Neon Shibuya Idol", "category": "skin", "sub_category": "outfit", "style_tier": "shibuya_neon", "price_zc": 650, "price_au": 7, "currency": "both", "tradeable": True, "model": "au_avatar_idol.glb", "desc": "Glowing holographic trim stage outfit."},
    {"id": "au_skin_celestial_mage", "name": "Astral Starweaver Robe", "category": "skin", "sub_category": "outfit", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 25, "currency": "au", "tradeable": False, "model": "au_avatar_mage.glb", "desc": "AU Coin exclusive celestial coat."},

    # Vehicles (10 total — equal speed/handling across every vehicle class)
    {"id": "au_veh_sedan_crimson", "name": "Kitsune Coupe S", "category": "vehicle", "sub_category": "car", "vehicle_class": "car", "style_tier": "tokyo_loft", "price_zc": 900, "price_au": 9, "currency": "both", "tradeable": True, "model": "au_veh_sedan_crimson.glb", "desc": "Equal-handling street coupe (Class: Car)."},
    {"id": "au_veh_coupe_cyber", "name": "Akiba GT Neon", "category": "vehicle", "sub_category": "car", "vehicle_class": "car", "style_tier": "shibuya_neon", "price_zc": 0, "price_au": 22, "currency": "au", "tradeable": False, "model": "au_veh_coupe_cyber.glb", "desc": "Cyber finish coupe — identical physics to Kitsune Coupe S (Class: Car)."},
    {"id": "au_veh_van_shibuya", "name": "Shibuya Drift Kei Van", "category": "vehicle", "sub_category": "car", "vehicle_class": "car", "style_tier": "tokyo_loft", "price_zc": 850, "price_au": 9, "currency": "both", "tradeable": True, "model": "au_veh_van_shibuya.glb", "desc": "Boxy Tokyo micro-van customized for street cruising — equal Car physics."},
    {"id": "au_veh_hyper_gt", "name": "Akihabara Hyper GT", "category": "vehicle", "sub_category": "car", "vehicle_class": "car", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 28, "currency": "au", "tradeable": False, "model": "au_veh_hyper_gt.glb", "desc": "Imperial gold-accented anime grand tourer — equal Car physics."},
    {"id": "au_veh_moto_sakura", "name": "Sakura Superbike", "category": "vehicle", "sub_category": "motorcycle", "vehicle_class": "motorcycle", "style_tier": "zen_wood", "price_zc": 650, "price_au": 7, "currency": "both", "tradeable": True, "model": "au_veh_moto_sakura.glb", "desc": "Agile lean-physics street motorcycle (Class: Motorcycle)."},
    {"id": "au_veh_moto_obsidian", "name": "Shogun Interceptor Moto", "category": "vehicle", "sub_category": "motorcycle", "vehicle_class": "motorcycle", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 18, "currency": "au", "tradeable": False, "model": "au_veh_moto_obsidian.glb", "desc": "Stealth matte motorcycle — identical physics to Sakura Superbike."},
    {"id": "au_veh_moto_akira", "name": "Neo-Tokyo Akira Runner", "category": "vehicle", "sub_category": "motorcycle", "vehicle_class": "motorcycle", "style_tier": "shibuya_neon", "price_zc": 780, "price_au": 8, "currency": "both", "tradeable": True, "model": "au_veh_moto_akira.glb", "desc": "Glowing cyber sport-cruiser bike — identical physics to Sakura Superbike."},
    {"id": "au_veh_bike_city", "name": "Lantern City Cruiser Bike", "category": "vehicle", "sub_category": "bike", "vehicle_class": "bike", "style_tier": "zen_wood", "price_zc": 180, "price_au": 2, "currency": "both", "tradeable": True, "model": "au_veh_bike_city.glb", "desc": "Crisp commuter bicycle with front basket (Class: Bike)."},
    {"id": "au_veh_scooter_neon", "name": "AU Fold Electric Scooter", "category": "vehicle", "sub_category": "bike", "vehicle_class": "bike", "style_tier": "shibuya_neon", "price_zc": 220, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_veh_scooter_neon.glb", "desc": "Zippy boardwalk cruiser — identical physics to City Bike."},
    {"id": "au_veh_moped_sakura", "name": "Sakura Delivery Moped", "category": "vehicle", "sub_category": "bike", "vehicle_class": "bike", "style_tier": "zen_wood", "price_zc": 240, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_veh_moped_sakura.glb", "desc": "Pastel petal-trimmed Tokyo city moped — identical physics to City Bike."},

    # Pets
    {"id": "au_pet_kitsune", "name": "Mini Nine-Tail Kitsune", "category": "pet", "sub_category": "companion", "style_tier": "zen_wood", "price_zc": 500, "price_au": 5, "currency": "both", "tradeable": True, "model": "au_pet_kitsune.glb", "desc": "Floating fox spirit companion with spring-tether follow."},
    {"id": "au_pet_shiba", "name": "Cyber Shiba Inu", "category": "pet", "sub_category": "companion", "style_tier": "tokyo_loft", "price_zc": 450, "price_au": 5, "currency": "both", "tradeable": True, "model": "au_pet_shiba.glb", "desc": "Hologram-collar Shiba follower."},
    {"id": "au_pet_mecha_cat", "name": "Neko-Bot Drone", "category": "pet", "sub_category": "companion", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 18, "currency": "au", "tradeable": False, "model": "au_pet_mecha_cat.glb", "desc": "AU Coin exclusive hovering calico robot."},
    {"id": "au_pet_star_slime", "name": "Starlight Slime", "category": "pet", "sub_category": "companion", "style_tier": "shibuya_neon", "price_zc": 300, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_pet_star_slime.glb", "desc": "Bouncy translucent cosmic mascot."},

    # 2 Interactive RP Props
    {"id": "au_prop_guitar", "name": "Playable Acoustic Guitar", "category": "furniture", "sub_category": "prop", "style_tier": "zen_wood", "interactive": "guitar", "price_zc": 320, "price_au": 4, "currency": "both", "tradeable": True, "model": "au_prop_guitar.glb", "desc": "Play anime acoustic chords in RP Mode or inside your apartment."},
    {"id": "au_prop_radio", "name": "Retro AU Boombox Radio", "category": "furniture", "sub_category": "prop", "style_tier": "tokyo_loft", "interactive": "radio", "price_zc": 280, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_prop_radio.glb", "desc": "Spin lo-fi Tokyo beats and city radio stations."},

    # 40 Indoor Furniture Items: 10 categories x 4 distinct styles
    # 1. Sofa
    {"id": "au_furn_tatami_lounge", "name": "Sakura Lounge Sofa", "category": "furniture", "sub_category": "sofa", "style_tier": "zen_wood", "price_zc": 240, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_furn_tatami_lounge.glb", "desc": "Plush modern Japanese low sofa in warm cedar and sakura linen."},
    {"id": "au_furn_sofa_loft", "name": "Tokyo Bouclé Sectional", "category": "furniture", "sub_category": "sofa", "style_tier": "tokyo_loft", "price_zc": 440, "price_au": 5, "currency": "both", "tradeable": True, "model": "au_furn_sofa_loft.glb", "desc": "Deep architectural loft sofa with slate throw cushions."},
    {"id": "au_furn_sofa_cyber", "name": "Shibuya Holo-Trim Couch", "category": "furniture", "sub_category": "sofa", "style_tier": "shibuya_neon", "price_zc": 720, "price_au": 8, "currency": "both", "tradeable": True, "model": "au_furn_sofa_cyber.glb", "desc": "Midnight lounge sofa with cyan-violet underglow piping."},
    {"id": "au_furn_sofa_imperial", "name": "Imperial Gold Velvet Divan", "category": "furniture", "sub_category": "sofa", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 20, "currency": "au", "tradeable": False, "model": "au_furn_sofa_imperial.glb", "desc": "Hand-carved gilded lacquer frame upholstered in imperial rose silk."},

    # 2. Table
    {"id": "au_furn_kotatsu_table", "name": "Warm Kotatsu Table", "category": "furniture", "sub_category": "table", "style_tier": "zen_wood", "price_zc": 310, "price_au": 4, "currency": "both", "tradeable": True, "model": "au_furn_kotatsu_table.glb", "desc": "Heated low tea table with quilted blanket."},
    {"id": "au_furn_table_loft", "name": "Smoked Glass Coffee Table", "category": "furniture", "sub_category": "table", "style_tier": "tokyo_loft", "price_zc": 360, "price_au": 4, "currency": "both", "tradeable": True, "model": "au_furn_table_loft.glb", "desc": "Tempered smoked glass tabletop on matte obsidian steel legs."},
    {"id": "au_furn_table_cyber", "name": "Neon Edge Holo Table", "category": "furniture", "sub_category": "table", "style_tier": "shibuya_neon", "price_zc": 640, "price_au": 7, "currency": "both", "tradeable": True, "model": "au_furn_table_cyber.glb", "desc": "Interactive holographic map table with glowing acrylic bezel."},
    {"id": "au_furn_table_imperial", "name": "Imperial Lacquer Tea Table", "category": "furniture", "sub_category": "table", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 18, "currency": "au", "tradeable": False, "model": "au_furn_table_imperial.glb", "desc": "Urushi vermilion lacquer tea table inlaid with sakura gold leaf."},

    # 3. Desk
    {"id": "au_furn_desk_zen", "name": "Hinoki Writing Desk", "category": "furniture", "sub_category": "desk", "style_tier": "zen_wood", "price_zc": 280, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_furn_desk_zen.glb", "desc": "Fragrant Japanese cypress study desk with brass inkwell tray."},
    {"id": "au_furn_cyber_desk", "name": "Creator Dual-Monitor Desk", "category": "furniture", "sub_category": "desk", "style_tier": "tokyo_loft", "price_zc": 480, "price_au": 5, "currency": "both", "tradeable": True, "model": "au_furn_cyber_desk.glb", "desc": "Stylized creator studio workstation."},
    {"id": "au_furn_desk_cyber", "name": "Shibuya RGB Command Rig", "category": "furniture", "sub_category": "desk", "style_tier": "shibuya_neon", "price_zc": 820, "price_au": 9, "currency": "both", "tradeable": True, "model": "au_furn_desk_cyber.glb", "desc": "Triple-display streaming cockpit with neon cyan edge strip."},
    {"id": "au_furn_desk_imperial", "name": "Imperial Gold Atelier Desk", "category": "furniture", "sub_category": "desk", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 24, "currency": "au", "tradeable": False, "model": "au_furn_desk_imperial.glb", "desc": "Executive ebony and brushed-gold mangaka drafting desk."},

    # 4. Bed
    {"id": "au_furn_bed_futon", "name": "Cedar Tatami Futon Bed", "category": "furniture", "sub_category": "bed", "style_tier": "zen_wood", "price_zc": 300, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_furn_bed_futon.glb", "desc": "Low cedar platform with thick organic cotton futon & bolster."},
    {"id": "au_furn_bed_loft", "name": "Minimalist Platform Bed", "category": "furniture", "sub_category": "bed", "style_tier": "tokyo_loft", "price_zc": 520, "price_au": 6, "currency": "both", "tradeable": True, "model": "au_furn_bed_loft.glb", "desc": "Crisp linen sleeping loft platform."},
    {"id": "au_furn_bed_cyber", "name": "Cyber Pod Canopy Bed", "category": "furniture", "sub_category": "bed", "style_tier": "shibuya_neon", "price_zc": 890, "price_au": 9, "currency": "both", "tradeable": True, "model": "au_furn_bed_cyber.glb", "desc": "Futuristic capsule sleeper frame with ambient magenta light bar."},
    {"id": "au_furn_bed_imperial", "name": "Imperial Sakura Canopy Bed", "category": "furniture", "sub_category": "bed", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 26, "currency": "au", "tradeable": False, "model": "au_furn_bed_imperial.glb", "desc": "Four-poster royal lacquer canopy bed with draped sakura silk."},

    # 5. Lamp
    {"id": "au_furn_lantern_lamp", "name": "Paper Torii Floor Lamp", "category": "furniture", "sub_category": "lamp", "style_tier": "zen_wood", "price_zc": 140, "price_au": 2, "currency": "both", "tradeable": True, "model": "au_furn_lantern_lamp.glb", "desc": "Warm amber floor lantern."},
    {"id": "au_furn_lamp_loft", "name": "Brushed Steel Arc Lamp", "category": "furniture", "sub_category": "lamp", "style_tier": "tokyo_loft", "price_zc": 290, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_furn_lamp_loft.glb", "desc": "Sweeping cantilevered studio floor lamp with linen diffuser."},
    {"id": "au_furn_lamp_cyber", "name": "Neon Akira Tube Totem", "category": "furniture", "sub_category": "lamp", "style_tier": "shibuya_neon", "price_zc": 540, "price_au": 6, "currency": "both", "tradeable": True, "model": "au_furn_lamp_cyber.glb", "desc": "Vertical twin-helix neon light column pulsing in cyan and pink."},
    {"id": "au_furn_lamp_imperial", "name": "Imperial Gilded Crane Lamp", "category": "furniture", "sub_category": "lamp", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 13, "currency": "au", "tradeable": False, "model": "au_furn_lamp_imperial.glb", "desc": "Sculpted gold crane standing lamp casting warm golden halo."},

    # 6. Divider
    {"id": "au_furn_shoji_screen", "name": "Shoji Divider Screen", "category": "furniture", "sub_category": "divider", "style_tier": "zen_wood", "price_zc": 190, "price_au": 2, "currency": "both", "tradeable": True, "model": "au_furn_shoji_screen.glb", "desc": "Cedar lattice divider screen with glowing rice paper."},
    {"id": "au_furn_divider_loft", "name": "Fluted Glass Loft Partition", "category": "furniture", "sub_category": "divider", "style_tier": "tokyo_loft", "price_zc": 340, "price_au": 4, "currency": "both", "tradeable": True, "model": "au_furn_divider_loft.glb", "desc": "Black steel architectural room divider with frosted reeded glass."},
    {"id": "au_furn_divider_cyber", "name": "Holographic Prism Screen", "category": "furniture", "sub_category": "divider", "style_tier": "shibuya_neon", "price_zc": 610, "price_au": 7, "currency": "both", "tradeable": True, "model": "au_furn_divider_cyber.glb", "desc": "Translucent laser-etched cyber partition with animated scanlines."},
    {"id": "au_furn_divider_imperial", "name": "Imperial Gold Byōbu Screen", "category": "furniture", "sub_category": "divider", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 19, "currency": "au", "tradeable": False, "model": "au_furn_divider_imperial.glb", "desc": "Six-panel gilded byōbu folding screen painted with cherry blossoms."},

    # 7. Shelf
    {"id": "au_furn_shelf_zen", "name": "Bamboo Tea Display Shelf", "category": "furniture", "sub_category": "shelf", "style_tier": "zen_wood", "price_zc": 180, "price_au": 2, "currency": "both", "tradeable": True, "model": "au_furn_shelf_zen.glb", "desc": "Asymmetric chigaidana bamboo shelf for teaware and scrolls."},
    {"id": "au_furn_trophy_shelf", "name": "Versekeeper Collectible Shelf", "category": "furniture", "sub_category": "shelf", "style_tier": "tokyo_loft", "price_zc": 280, "price_au": 4, "currency": "both", "tradeable": True, "model": "au_furn_trophy_shelf.glb", "desc": "Display cabinet for earned collectibles."},
    {"id": "au_furn_shelf_cyber", "name": "Backlit Acrylic Fig Showcase", "category": "furniture", "sub_category": "shelf", "style_tier": "shibuya_neon", "price_zc": 660, "price_au": 7, "currency": "both", "tradeable": True, "model": "au_furn_shelf_cyber.glb", "desc": "LED-lit Akihabara glass figure vitrine with neon pedestal strips."},
    {"id": "au_furn_shelf_imperial", "name": "Imperial Gilded Curio Vitrine", "category": "furniture", "sub_category": "shelf", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 17, "currency": "au", "tradeable": False, "model": "au_furn_shelf_imperial.glb", "desc": "Museum-grade brass and black-lacquer heirloom display case."},

    # 8. Plant
    {"id": "au_furn_bonsai_sakura", "name": "Blooming Sakura Bonsai", "category": "furniture", "sub_category": "plant", "style_tier": "zen_wood", "price_zc": 160, "price_au": 2, "currency": "both", "tradeable": True, "model": "au_furn_bonsai_sakura.glb", "desc": "Ceramic planter bonsai tree."},
    {"id": "au_furn_plant_monstera", "name": "Ceramic Monstera Planter", "category": "furniture", "sub_category": "plant", "style_tier": "tokyo_loft", "price_zc": 280, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_furn_plant_monstera.glb", "desc": "Lush indoor tropical Monstera in minimalist white ceramic pot."},
    {"id": "au_furn_plant_cyber", "name": "Bioluminescent Cyber Terrarium", "category": "furniture", "sub_category": "plant", "style_tier": "shibuya_neon", "price_zc": 530, "price_au": 6, "currency": "both", "tradeable": True, "model": "au_furn_plant_cyber.glb", "desc": "Glowing cyan crystal flora sealed inside a levitating glass dome."},
    {"id": "au_furn_plant_imperial", "name": "Imperial Gold-Leaf Bonsai", "category": "furniture", "sub_category": "plant", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 14, "currency": "au", "tradeable": False, "model": "au_furn_plant_imperial.glb", "desc": "Ancient twisted pine bonsai potted in gilded porcelain."},

    # 9. Rug
    {"id": "au_furn_rug_zen", "name": "Woven Tatami Accent Rug", "category": "furniture", "sub_category": "rug", "style_tier": "zen_wood", "price_zc": 130, "price_au": 2, "currency": "both", "tradeable": True, "model": "au_furn_rug_zen.glb", "desc": "Hand-bound igusa rush tatami floor mat with indigo brocade border."},
    {"id": "au_furn_rug_celestial", "name": "Celestial Starlight Rug", "category": "furniture", "sub_category": "rug", "style_tier": "tokyo_loft", "price_zc": 280, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_furn_rug_celestial.glb", "desc": "Plush woven navy-and-gold astronomical lounge rug."},
    {"id": "au_furn_rug_cyber", "name": "Shibuya Grid LED Runner", "category": "furniture", "sub_category": "rug", "style_tier": "shibuya_neon", "price_zc": 560, "price_au": 6, "currency": "both", "tradeable": True, "model": "au_furn_rug_cyber.glb", "desc": "Woven dark synthwave rug bordered with fiber-optic neon threads."},
    {"id": "au_furn_rug_imperial", "name": "Imperial Sakura Silk Carpet", "category": "furniture", "sub_category": "rug", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 15, "currency": "au", "tradeable": False, "model": "au_furn_rug_imperial.glb", "desc": "Heirloom crimson-and-gold woven silk medallion carpet."},

    # 10. Arcade
    {"id": "au_furn_arcade_zen", "name": "Retro Wood Shogi & Go Table", "category": "furniture", "sub_category": "arcade", "style_tier": "zen_wood", "price_zc": 250, "price_au": 3, "currency": "both", "tradeable": True, "model": "au_furn_arcade_zen.glb", "desc": "Handcrafted kaya-wood floor Go & Shogi table with lacquered bowls."},
    {"id": "au_furn_arcade_loft", "name": "Minimalist Bartop Console", "category": "furniture", "sub_category": "arcade", "style_tier": "tokyo_loft", "price_zc": 580, "price_au": 6, "currency": "both", "tradeable": True, "model": "au_furn_arcade_loft.glb", "desc": "Brushed-aluminum tabletop retro game station for studio lofts."},
    {"id": "au_furn_arcade_cab", "name": "Infinite Verse Arcade Cabinet", "category": "furniture", "sub_category": "arcade", "style_tier": "shibuya_neon", "price_zc": 850, "price_au": 9, "currency": "both", "tradeable": True, "model": "au_furn_arcade_cab.glb", "desc": "Playable cabinet silhouette for loft game rooms."},
    {"id": "au_furn_arcade_imperial", "name": "Imperial Gold VIP Arcade Cab", "category": "furniture", "sub_category": "arcade", "style_tier": "imperial_gold", "price_zc": 0, "price_au": 28, "currency": "au", "tradeable": False, "model": "au_furn_arcade_imperial.glb", "desc": "Limited edition champagne-gold candy cabinet with crystal joystick."},
]

AU_SHOP_CATALOG: List[Dict[str, Any]] = [
    {
        **item,
        "style_label": STYLE_TIER_LABELS.get(item.get("style_tier", "tokyo_loft"), "Tokyo Loft Modern"),
        "thumbnail": f"/images/au_city/items/{item['id']}.jpg",
    }
    for item in _RAW_SHOP_CATALOG
]

CATALOG_BY_ID: Dict[str, Dict[str, Any]] = {item["id"]: item for item in AU_SHOP_CATALOG}


def compute_10x_catalog_budget() -> Dict[str, int]:
    """Compute 10x testing currency budget across all catalog items and apartment floor plans."""
    item_zc = sum(int(item.get("price_zc", 0)) for item in AU_SHOP_CATALOG)
    plan_zc = sum(int(plan.get("cost_zc", 0)) for plan in AU_FLOOR_PLANS.values())
    item_au = sum(int(item.get("price_au", 0)) for item in AU_SHOP_CATALOG)
    plan_au = sum(int(plan.get("cost_au", 0)) for plan in AU_FLOOR_PLANS.values())
    return {
        "zencoins": 10 * item_zc + 10 * plan_zc,
        "au_coins": 10 * item_au + 10 * plan_au,
    }


DEFAULT_AVATAR_APPEARANCE: Dict[str, Any] = {
    "gender": "male",
    "base_char_id": "base_versekeeper",
    "identity_initialized": False,
    "outfit_gender_fit": "auto",
    "skinTone": "#f5c9a6",
    "eyeColor": "#38bdf8",
    "hairId": "m_spiky_shonen",
    "hairColor": "#1e293b",
    "shirtId": "m_tech_hoodie",
    "shirtColor": "#2563eb",
    "pantsId": "m_slim_jeans",
    "pantsColor": "#1e293b",
    "shoesId": "m_hightop_sneakers",
    "shoesColor": "#f8fafc",
    "accessoryId": "none",
}


def sanitize_avatar_appearance(
    raw: Optional[Dict[str, Any]],
    base: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Sanitize and normalize modular 3D avatar appearance & head identity dictionary."""
    merged: Dict[str, Any] = {**DEFAULT_AVATAR_APPEARANCE}
    if isinstance(base, dict):
        for k in DEFAULT_AVATAR_APPEARANCE:
            if k == "identity_initialized":
                merged[k] = bool(base.get(k, False))
            elif k == "base_char_id":
                bv = str(base.get(k) or "").strip()
                if bv in ALLOWED_BASE_CHAR_IDS:
                    merged[k] = bv
            elif k == "outfit_gender_fit":
                fv = str(base.get(k) or "").strip().lower()
                if fv in ("auto", "m", "f"):
                    merged[k] = fv
            elif k in base and isinstance(base[k], str) and base[k].strip():
                merged[k] = base[k].strip()
    if not isinstance(raw, dict):
        return merged

    if "gender" in raw:
        g = str(raw["gender"]).strip().lower()
        if g in ("male", "female"):
            merged["gender"] = g

    if "base_char_id" in raw:
        bc = str(raw["base_char_id"]).strip()
        if bc in ALLOWED_BASE_CHAR_IDS:
            merged["base_char_id"] = bc

    if "outfit_gender_fit" in raw:
        gf = str(raw["outfit_gender_fit"]).strip().lower()
        if gf in ("auto", "m", "f"):
            merged["outfit_gender_fit"] = gf

    if "identity_initialized" in raw and bool(raw["identity_initialized"]):
        merged["identity_initialized"] = True

    hex_re = re.compile(r"^#[0-9a-fA-F]{6}$")
    for color_key in ("skinTone", "eyeColor", "hairColor", "shirtColor", "pantsColor", "shoesColor"):
        if color_key in raw:
            val = str(raw[color_key]).strip()
            if hex_re.match(val):
                merged[color_key] = val.lower()

    id_re = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
    for id_key in ("hairId", "shirtId", "pantsId", "shoesId", "accessoryId"):
        if id_key in raw:
            val = str(raw[id_key]).strip()
            if id_re.match(val):
                merged[id_key] = val

    return merged


ALLOWED_CHAT_TEXT_COLORS = {
    "#e2e8f0",
    "#f8fafc",
    "#bae6fd",
    "#fde68a",
    "#fbcfe8",
    "#a7f3d0",
}

DEFAULT_USER_SETTINGS: Dict[str, Any] = {
    "chat_privacy": "everyone",
    "dm_privacy": "everyone",
    "home_invites_privacy": "everyone",
    "chat_filter_mode": "standard",
    "chat_channel": "proximity",
    "chat_font_size": "md",
    "chat_text_color": "#e2e8f0",
    "chat_bg_opacity": 88,
    "chat_timestamps": False,
    "volume_master": 80,
    "volume_music": 70,
    "volume_sfx": 85,
    "mute_all": False,
    "camera_sensitivity": 1.0,
    "camera_zoom_speed": 1.0,
    "camera_fov": 52,
    "camera_distance": 8.5,
    "camera_invert_y": False,
    "camera_auto_follow": False,
    "graphics_shadows": True,
    "graphics_petals": True,
    "show_nametags": True,
    "show_controls_hint": True,
    "lighting_mode": "twilight",
    "avatar_mode": "glb",
}


def sanitize_user_settings(patch: Optional[Dict[str, Any]], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Clamp and validate player settings against safe allowed ranges and enums."""
    merged = {**DEFAULT_USER_SETTINGS, **(base or {})}
    if str(merged.get("avatar_mode") or "") not in ("glb", "uploaded"):
        merged["avatar_mode"] = "glb"
    if not isinstance(patch, dict):
        return merged

    def clamp_float(val: Any, lo: float, hi: float, fallback: float) -> float:
        try:
            v = float(val)
            if math.isnan(v) or math.isinf(v):
                return fallback
            return round(max(lo, min(hi, v)), 2)
        except (TypeError, ValueError):
            return fallback

    def clamp_int(val: Any, lo: int, hi: int, fallback: int) -> int:
        try:
            return max(lo, min(hi, int(round(float(val)))))
        except (TypeError, ValueError):
            return fallback

    enum_rules = {
        "chat_privacy": {"everyone", "contacts", "off"},
        "dm_privacy": {"everyone", "contacts", "nobody"},
        "home_invites_privacy": {"everyone", "contacts", "nobody"},
        "chat_filter_mode": {"standard", "strict"},
        "chat_channel": {"proximity", "global"},
        "chat_font_size": {"sm", "md", "lg"},
        "lighting_mode": {"twilight", "day", "night", "auto"},
        "avatar_mode": {"glb", "uploaded"},
    }
    for key, allowed in enum_rules.items():
        if key in patch and str(patch[key]) in allowed:
            merged[key] = str(patch[key])
    if "avatar_mode" in patch and str(patch["avatar_mode"]) not in ("glb", "uploaded"):
        merged["avatar_mode"] = "glb"

    if "chat_text_color" in patch and str(patch["chat_text_color"]).lower() in ALLOWED_CHAT_TEXT_COLORS:
        merged["chat_text_color"] = str(patch["chat_text_color"]).lower()

    int_ranges = {
        "chat_bg_opacity": (40, 100),
        "volume_master": (0, 100),
        "volume_music": (0, 100),
        "volume_sfx": (0, 100),
        "camera_fov": (42, 75),
    }
    for key, (lo, hi) in int_ranges.items():
        if key in patch:
            merged[key] = clamp_int(patch[key], lo, hi, DEFAULT_USER_SETTINGS[key])

    float_ranges = {
        "camera_sensitivity": (0.3, 2.5),
        "camera_zoom_speed": (0.4, 2.2),
        "camera_distance": (5.5, 13.5),
    }
    for key, (lo, hi) in float_ranges.items():
        if key in patch:
            merged[key] = clamp_float(patch[key], lo, hi, DEFAULT_USER_SETTINGS[key])

    for key in ("chat_timestamps", "mute_all", "camera_invert_y", "camera_auto_follow", "graphics_shadows", "graphics_petals", "show_nametags", "show_controls_hint"):
        if key in patch:
            merged[key] = bool(patch[key])

    return merged


_TABLE_PRIMARY_KEYS: Dict[str, Tuple[str, ...]] = {
    "au_users": ("user_id",),
    "au_sessions": ("token",),
    "au_inventory": ("inv_id",),
    "au_ledger": ("tx_id",),
    "au_trades": ("trade_id",),
    "au_groups": ("group_id",),
    "au_group_members": ("group_id", "user_id"),
    "au_home_invites": ("owner_id", "guest_id"),
    "au_contacts": ("user_id", "contact_id"),
    "au_direct_messages": ("dm_id",),
    "au_chat_messages": ("msg_id",),
    "au_newsletter_subs": ("email",),
    "au_avatar_blobs": ("user_id",),
    "au_city_world_layout": ("obj_id",),
    "au_world_state": ("state_key",),
    "au_admin_audit_log": ("log_id",),
}


def _split_sql_statements(script: str) -> List[str]:
    """Split a SQL script into individual statements while respecting quoted strings."""
    stmts: List[str] = []
    buf: List[str] = []
    in_quote = False
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if ch == "'":
            buf.append(ch)
            if in_quote and i + 1 < n and script[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            in_quote = not in_quote
        elif not in_quote and ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _translate_qmarks_to_percent(sql: str) -> str:
    """Translate unquoted SQLite `?` placeholders into `%s` for PostgreSQL."""
    out: List[str] = []
    in_quote = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_quote and i + 1 < n and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_quote = not in_quote
        elif not in_quote and ch == "?":
            out.append("%s")
        elif not in_quote and ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class _AuDbCursor:
    """Unified cursor wrapper normalizing rows to dicts across SQLite and PostgreSQL."""

    def __init__(self, cur: Any):
        self._cur = cur

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cur, "rowcount", 0) or 0)

    def fetchone(self) -> Optional[Dict[str, Any]]:
        row = self._cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._cur.fetchall()]


class _AuDbConn:
    """Dialect-agnostic connection wrapper exposing unified execute, upsert, and schema inspection."""

    def __init__(self, raw_con: Any, adapter: "_AuDbAdapter"):
        self._con = raw_con
        self._adapter = adapter

    def execute(self, sql: str, params: Tuple[Any, ...] | List[Any] = ()) -> _AuDbCursor:
        translated = self._adapter.translate_sql(sql)
        if self._adapter.dialect == "postgres":
            cur = self._adapter._pg_cursor(self._con)
            cur.execute(translated, tuple(params) if params else None)
            return _AuDbCursor(cur)
        cur = self._con.execute(translated, tuple(params))
        return _AuDbCursor(cur)

    def executemany(self, sql: str, seq_of_params: List[Tuple[Any, ...]]) -> None:
        translated = self._adapter.translate_sql(sql)
        if self._adapter.dialect == "postgres":
            cur = self._adapter._pg_cursor(self._con)
            cur.executemany(translated, seq_of_params)
            return
        self._con.executemany(translated, seq_of_params)

    def executescript(self, script: str) -> None:
        for stmt in _split_sql_statements(script):
            self.execute(stmt)

    def upsert(self, table: str, values: Dict[str, Any], conflict_cols: Tuple[str, ...] | List[str]) -> _AuDbCursor:
        cols = list(values.keys())
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        conflict = ", ".join(conflict_cols)
        update_cols = [c for c in cols if c not in conflict_cols]
        if update_cols:
            sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO UPDATE SET {sets}"
        else:
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO NOTHING"
        return self.execute(sql, tuple(values[c] for c in cols))

    def get_columns(self, table_name: str) -> Set[str]:
        if not re.fullmatch(r"[a-zA-Z0-9_]+", table_name):
            raise ValueError("Invalid table name.")
        if self._adapter.dialect == "postgres":
            rows = self.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?",
                (table_name,),
            ).fetchall()
            return {str(r["column_name"]) for r in rows}
        rows = self.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(r["name"]) for r in rows}


class _AuDbAdapter:
    """Dual database adapter supporting local SQLite and Render PostgreSQL (`DATABASE_URL`)."""

    def __init__(self, db_path: Path | str | None = None, database_url: str | None = None):
        if database_url is not None:
            raw_url = database_url.strip()
        elif isinstance(db_path, str) and db_path.strip().startswith(("postgres://", "postgresql://")):
            raw_url = db_path.strip()
        elif db_path is not None:
            raw_url = ""
        else:
            raw_url = os.getenv("DATABASE_URL", "").strip()

        if raw_url.startswith("postgres://"):
            raw_url = "postgresql://" + raw_url[len("postgres://"):]

        if raw_url.startswith("postgresql://"):
            self.dialect = "postgres"
            self.dsn = raw_url
            self.db_path = DEFAULT_DB_PATH
            self._pg_driver, self._pg_mod, self._pg_extras = self._load_pg_driver()
        else:
            self.dialect = "sqlite"
            self.dsn = ""
            self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._pg_driver = ""
            self._pg_mod = None
            self._pg_extras = None

    @staticmethod
    def _load_pg_driver() -> Tuple[str, Any, Any]:
        try:
            import psycopg  # type: ignore
            from psycopg.rows import dict_row  # type: ignore
            return "psycopg3", psycopg, dict_row
        except ImportError:
            pass
        try:
            import psycopg2  # type: ignore
            import psycopg2.extras  # type: ignore
            return "psycopg2", psycopg2, psycopg2.extras
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL is set to PostgreSQL, but neither 'psycopg' (v3) nor 'psycopg2' is installed."
            ) from exc

    def _pg_cursor(self, raw_con: Any) -> Any:
        if self._pg_driver == "psycopg3":
            return raw_con.cursor(row_factory=self._pg_extras)
        return raw_con.cursor(cursor_factory=self._pg_extras.RealDictCursor)

    def adapt_blob(self, data: bytes) -> Any:
        raw = bytes(data)
        if self.dialect == "postgres" and self._pg_driver == "psycopg2":
            return self._pg_mod.Binary(raw)
        return raw

    def translate_sql(self, sql: str) -> str:
        if self.dialect == "sqlite":
            return re.sub(r"\bBYTEA\b", "BLOB", sql)

        # PostgreSQL translation
        out_sql = re.sub(r"\bBLOB\b", "BYTEA", sql)

        m_ignore = re.match(r"(?is)^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+([a-zA-Z0-9_]+)\s*(.*)$", out_sql)
        if m_ignore:
            tbl, rest = m_ignore.group(1), m_ignore.group(2)
            out_sql = f"INSERT INTO {tbl} {rest} ON CONFLICT DO NOTHING"
        else:
            m_repl = re.match(
                r"(?is)^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([a-zA-Z0-9_]+)\s*\(([^)]+)\)\s*VALUES\s*(.*)$",
                out_sql,
            )
            if m_repl:
                tbl, cols_raw, vals_rest = m_repl.group(1), m_repl.group(2), m_repl.group(3)
                pks = _TABLE_PRIMARY_KEYS.get(tbl, ())
                cols = [c.strip() for c in cols_raw.split(",") if c.strip()]
                non_pks = [c for c in cols if c not in pks]
                if pks and non_pks:
                    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pks)
                    out_sql = f"INSERT INTO {tbl} ({cols_raw}) VALUES {vals_rest} ON CONFLICT ({', '.join(pks)}) DO UPDATE SET {sets}"
                elif pks:
                    out_sql = f"INSERT INTO {tbl} ({cols_raw}) VALUES {vals_rest} ON CONFLICT ({', '.join(pks)}) DO NOTHING"

        return _translate_qmarks_to_percent(out_sql)

    @contextmanager
    def connect(self, exclusive: bool = False):
        if self.dialect == "postgres":
            raw_con = self._pg_mod.connect(self.dsn)
            try:
                yield _AuDbConn(raw_con, self)
                raw_con.commit()
            except Exception:
                try:
                    raw_con.rollback()
                except Exception:
                    pass
                raise
            finally:
                raw_con.close()
            return

        raw_con = sqlite3.connect(str(self.db_path), timeout=20.0, isolation_level=None)
        raw_con.row_factory = sqlite3.Row
        raw_con.execute("PRAGMA journal_mode=WAL")
        raw_con.execute("PRAGMA foreign_keys=ON")
        try:
            raw_con.execute("BEGIN IMMEDIATE" if exclusive else "BEGIN")
            yield _AuDbConn(raw_con, self)
            raw_con.commit()
        except Exception:
            try:
                raw_con.rollback()
            except Exception:
                pass
            raise
        finally:
            raw_con.close()


class AuCityService:
    """Thread-safe dual SQLite/PostgreSQL backend service powering Anime Universe City."""

    def __init__(self, db_path: Path | str | None = None, database_url: str | None = None):
        self._db = _AuDbAdapter(db_path=db_path, database_url=database_url)
        self.db_path = self._db.db_path
        PUBLIC_MODELS_DIR.mkdir(parents=True, exist_ok=True)
        (PUBLIC_MODELS_DIR / "avatars").mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self, exclusive: bool = False):
        with self._db.connect(exclusive=exclusive) as con:
            yield con

    def _init_schema(self) -> None:
        blob_type = "BYTEA" if self._db.dialect == "postgres" else "BLOB"
        with self._conn(exclusive=True) as con:
            con.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS au_users (
                    user_id TEXT PRIMARY KEY,
                    google_sub TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL,
                    username TEXT UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    bio TEXT NOT NULL DEFAULT '',
                    profile_completed INTEGER NOT NULL DEFAULT 0,
                    avatar_glb_url TEXT NOT NULL DEFAULT '/models/au_city/au_avatar_versekeeper.glb',
                    avatar_appearance_json TEXT NOT NULL DEFAULT '{{}}',
                    equipped_skin_id TEXT NOT NULL DEFAULT 'au_skin_versekeeper',
                    equipped_vehicle_id TEXT NOT NULL DEFAULT '',
                    equipped_pet_id TEXT NOT NULL DEFAULT '',
                    au_coins INTEGER NOT NULL DEFAULT 0,
                    zencoins INTEGER NOT NULL DEFAULT 150,
                    apartment_tier TEXT NOT NULL DEFAULT 'studio',
                    newsletter_claimed INTEGER NOT NULL DEFAULT 0,
                    last_checkin_day TEXT NOT NULL DEFAULT '',
                    discovered_districts TEXT NOT NULL DEFAULT '["plaza"]',
                    settings_json TEXT NOT NULL DEFAULT '{{}}',
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    muted_until REAL NOT NULL DEFAULT 0,
                    banned INTEGER NOT NULL DEFAULT 0,
                    ban_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES au_users(user_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS au_inventory (
                    inv_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    location TEXT NOT NULL DEFAULT 'storage',
                    pos_x REAL NOT NULL DEFAULT 0.0,
                    pos_y REAL NOT NULL DEFAULT 0.0,
                    pos_z REAL NOT NULL DEFAULT 0.0,
                    rot_y REAL NOT NULL DEFAULT 0.0,
                    acquired_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES au_users(user_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS au_ledger (
                    tx_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES au_users(user_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS au_trades (
                    trade_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    offer_zc INTEGER NOT NULL DEFAULT 0,
                    request_zc INTEGER NOT NULL DEFAULT 0,
                    offer_inv_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_groups (
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    is_private INTEGER NOT NULL DEFAULT 0,
                    max_members INTEGER NOT NULL DEFAULT 32,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_group_members (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    joined_at REAL NOT NULL,
                    PRIMARY KEY (group_id, user_id),
                    FOREIGN KEY (group_id) REFERENCES au_groups(group_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS au_home_invites (
                    owner_id TEXT NOT NULL,
                    guest_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (owner_id, guest_id)
                );
                CREATE TABLE IF NOT EXISTS au_contacts (
                    user_id TEXT NOT NULL,
                    contact_id TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (user_id, contact_id)
                );
                CREATE TABLE IF NOT EXISTS au_direct_messages (
                    dm_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    flagged INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_chat_messages (
                    msg_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    phase_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    body TEXT NOT NULL,
                    flagged INTEGER NOT NULL DEFAULT 0,
                    pos_x REAL NOT NULL DEFAULT 0.0,
                    pos_z REAL NOT NULL DEFAULT 0.0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    deleted_by TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_newsletter_subs (
                    email TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_avatar_blobs (
                    user_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    data {blob_type} NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_city_world_layout (
                    obj_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    model TEXT NOT NULL,
                    label TEXT NOT NULL,
                    x REAL NOT NULL,
                    y REAL NOT NULL DEFAULT 0.0,
                    z REAL NOT NULL,
                    rot_y REAL NOT NULL DEFAULT 0.0,
                    scale_x REAL NOT NULL DEFAULT 1.0,
                    scale_y REAL NOT NULL DEFAULT 1.0,
                    scale_z REAL NOT NULL DEFAULT 1.0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{{}}',
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_world_state (
                    state_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS au_admin_audit_log (
                    log_id TEXT PRIMARY KEY,
                    admin_email TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            user_cols = con.get_columns("au_users")
            user_migrations = [
                ("settings_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("avatar_appearance_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
                ("muted_until", "REAL NOT NULL DEFAULT 0"),
                ("banned", "INTEGER NOT NULL DEFAULT 0"),
                ("ban_reason", "TEXT NOT NULL DEFAULT ''"),
            ]
            for col_name, col_def in user_migrations:
                if col_name not in user_cols:
                    con.execute(f"ALTER TABLE au_users ADD COLUMN {col_name} {col_def}")

            chat_cols = con.get_columns("au_chat_messages")
            chat_migrations = [
                ("deleted", "INTEGER NOT NULL DEFAULT 0"),
                ("deleted_by", "TEXT NOT NULL DEFAULT ''"),
            ]
            for col_name, col_def in chat_migrations:
                if col_name not in chat_cols:
                    con.execute(f"ALTER TABLE au_chat_messages ADD COLUMN {col_name} {col_def}")

            now = time.time()
            default_clock = {
                "cycle_hour": 18.2,
                "cycle_speed": 0.15,
                "paused": False,
                "anchor_epoch": now,
            }
            con.execute(
                "INSERT INTO au_world_state (state_key, value_json, updated_by, updated_at) VALUES (?, ?, '', ?) ON CONFLICT (state_key) DO NOTHING",
                ("world_clock", json.dumps(default_clock), now),
            )

            # One-time migration: eliminate legacy 'modular' primitive humanoid mode -> real GLB ('glb')
            legacy_rows = con.execute("SELECT user_id, settings_json FROM au_users WHERE settings_json LIKE '%modular%'").fetchall()
            for lr in legacy_rows:
                try:
                    cur_s = json.loads(lr.get("settings_json") or "{}")
                except Exception:
                    cur_s = {}
                if isinstance(cur_s, dict) and cur_s.get("avatar_mode") == "modular":
                    cur_s["avatar_mode"] = "glb"
                    con.execute(
                        "UPDATE au_users SET settings_json = ?, updated_at = ? WHERE user_id = ?",
                        (json.dumps(cur_s), now, lr["user_id"]),
                    )

    @staticmethod
    def _format_mute_remaining(muted_until: float, now: float) -> str:
        rem_sec = max(1, int(math.ceil(muted_until - now)))
        if rem_sec >= 3600:
            hrs, rem = divmod(rem_sec, 3600)
            return f"{hrs}h {rem // 60}m"
        if rem_sec >= 60:
            mins, secs = divmod(rem_sec, 60)
            return f"{mins}m {secs}s"
        return f"{rem_sec}s"

    def _activate_admin_if_needed(self, con: _AuDbConn, user_row: Dict[str, Any], now: float) -> None:
        """Promote ADMIN_EMAILS accounts to is_admin=1 and credit 10x budget on first activation."""
        email = str(user_row.get("email") or "").strip().lower()
        if not is_admin_email(email):
            return
        if int(user_row.get("is_admin") or 0) == 1:
            return
        budget = compute_10x_catalog_budget()
        cur_zc = int(user_row.get("zencoins") or 0)
        cur_au = int(user_row.get("au_coins") or 0)
        delta_zc = max(0, budget["zencoins"] - cur_zc)
        delta_au = max(0, budget["au_coins"] - cur_au)
        user_id = str(user_row["user_id"])
        con.execute(
            "UPDATE au_users SET is_admin = 1, zencoins = ?, au_coins = ?, updated_at = ? WHERE user_id = ?",
            (cur_zc + delta_zc, cur_au + delta_au, now, user_id),
        )
        if delta_zc > 0:
            con.execute(
                "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'zencoins', ?, 'admin_10x_test_budget', ?)",
                ("tx_" + secrets.token_hex(6), user_id, delta_zc, now),
            )
        if delta_au > 0:
            con.execute(
                "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'au_coins', ?, 'admin_10x_test_budget', ?)",
                ("tx_" + secrets.token_hex(6), user_id, delta_au, now),
            )

    def _log_admin_action(
        self,
        con: _AuDbConn,
        admin_email: str,
        action: str,
        target: str,
        payload: Dict[str, Any],
        now: float,
    ) -> None:
        con.execute(
            "INSERT INTO au_admin_audit_log (log_id, admin_email, action, target, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("adm_" + secrets.token_hex(6), admin_email.strip().lower(), action, target, json.dumps(payload), now),
        )

    # ---------- Auth & Profile ----------
    def verify_google_id_token(self, id_token: str) -> Dict[str, Any]:
        """Verify a Google ID token via Google's tokeninfo endpoint."""
        tok = (id_token or "").strip()
        if not tok:
            raise ValueError("Missing Google id_token.")
        url = f"https://oauth2.googleapis.com/tokeninfo?id_token={urllib.parse.quote(tok)}"
        req = urllib.request.Request(url, headers={"User-Agent": "AnimeUniverseCity/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ValueError(f"Google token verification failed ({e.code}).") from e
        sub = str(data.get("sub") or "").strip()
        email = str(data.get("email") or "").strip().lower()
        if not sub or not email:
            raise ValueError("Google token did not include verified account claims.")
        expected_aud = get_au_city_google_client_id()
        if not expected_aud:
            raise ValueError("Google Sign-In is not configured for AU City.")
        if str(data.get("aud") or "").strip() != expected_aud:
            raise ValueError("Google token audience mismatch.")
        return {
            "sub": sub,
            "email": email,
            "name": str(data.get("name") or email.split("@")[0]),
        }

    def upsert_google_user_session(self, google_sub: str, email: str, display_name: str = "") -> Dict[str, Any]:
        now = time.time()
        token = secrets.token_urlsafe(32)
        expires = now + 7 * 86400
        clean_email = (email or "").strip().lower()
        clean_name = filter_pg13_text(display_name or clean_email.split("@")[0])
        safe_display = str(clean_name["clean_text"] or "Versekeeper")[:40]
        admin_flag = is_admin_email(clean_email)
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT * FROM au_users WHERE google_sub = ?", (google_sub,)).fetchone()
            if row:
                user_id = row["user_id"]
                if int(row.get("banned") or 0) == 1:
                    raise PermissionError(f"Account banned: {row.get('ban_reason') or 'Suspended by administrator.'}")
                con.execute(
                    "UPDATE au_users SET email = ?, updated_at = ? WHERE user_id = ?",
                    (clean_email, now, user_id),
                )
                row["email"] = clean_email
                self._activate_admin_if_needed(con, row, now)
            else:
                user_id = "au_" + secrets.token_hex(8)
                con.execute(
                    """
                    INSERT INTO au_users (
                        user_id, google_sub, email, username, display_name, profile_completed,
                        avatar_appearance_json, au_coins, zencoins, apartment_tier, is_admin, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, ?, 0, ?, 0, 150, 'studio', 0, ?, ?)
                    """,
                    (user_id, google_sub, clean_email, safe_display, json.dumps(DEFAULT_AVATAR_APPEARANCE), now, now),
                )
                con.execute(
                    "INSERT INTO au_inventory (inv_id, user_id, item_id, location, acquired_at) VALUES (?, ?, 'au_skin_versekeeper', 'inventory', ?)",
                    ("inv_" + secrets.token_hex(6), user_id, now),
                )
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'zencoins', 150, 'welcome_bonus', ?)",
                    ("tx_" + secrets.token_hex(6), user_id, now),
                )
                if admin_flag:
                    self._activate_admin_if_needed(
                        con,
                        {"user_id": user_id, "email": clean_email, "is_admin": 0, "zencoins": 150, "au_coins": 0},
                        now,
                    )
            con.execute(
                "INSERT INTO au_sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token, user_id, expires, now),
            )
        return {"session_token": token, "user": self.get_user_bundle(user_id)}

    def get_user_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        now = time.time()
        with self._conn() as con:
            row = con.execute(
                "SELECT u.user_id FROM au_sessions s JOIN au_users u ON s.user_id = u.user_id WHERE s.token = ? AND s.expires_at > ?",
                (token, now),
            ).fetchone()
        if not row:
            return None
        return self.get_user_bundle(row["user_id"])

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._conn(exclusive=True) as con:
            con.execute("DELETE FROM au_sessions WHERE token = ?", (token,))

    def _serialize_user_dict(self, u: Dict[str, Any]) -> Dict[str, Any]:
        user_dict = dict(u)
        user_dict["discovered_districts"] = json.loads(user_dict.get("discovered_districts") or '["plaza"]')
        try:
            raw_settings = json.loads(user_dict.get("settings_json") or "{}")
        except Exception:
            raw_settings = {}
        try:
            raw_appearance = json.loads(user_dict.get("avatar_appearance_json") or "{}")
        except Exception:
            raw_appearance = {}
        user_dict["settings"] = sanitize_user_settings(raw_settings)
        user_dict["avatar_appearance"] = sanitize_avatar_appearance(raw_appearance)
        user_dict.pop("settings_json", None)
        user_dict.pop("avatar_appearance_json", None)
        user_dict["is_admin"] = bool(int(user_dict.get("is_admin") or 0))
        user_dict["banned"] = bool(int(user_dict.get("banned") or 0))
        user_dict["ban_reason"] = str(user_dict.get("ban_reason") or "")
        user_dict["muted_until"] = float(user_dict.get("muted_until") or 0.0)
        user_dict["is_muted"] = user_dict["muted_until"] > time.time()
        return user_dict

    def get_user_bundle(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as con:
            u = con.execute("SELECT * FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
        if not u:
            return None
        if is_admin_email(u.get("email")) and int(u.get("is_admin") or 0) == 0:
            now = time.time()
            with self._conn(exclusive=True) as con:
                fresh_u = con.execute("SELECT * FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
                if fresh_u:
                    self._activate_admin_if_needed(con, fresh_u, now)
        with self._conn() as con:
            u = con.execute("SELECT * FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not u:
                return None
            inv_rows = con.execute(
                "SELECT * FROM au_inventory WHERE user_id = ? ORDER BY acquired_at DESC",
                (user_id,),
            ).fetchall()
            grp = con.execute(
                "SELECT g.* FROM au_group_members gm JOIN au_groups g ON gm.group_id = g.group_id WHERE gm.user_id = ?",
                (user_id,),
            ).fetchone()
        user_dict = self._serialize_user_dict(u)
        inv = [dict(r) for r in inv_rows]
        plan = AU_FLOOR_PLANS.get(user_dict["apartment_tier"], AU_FLOOR_PLANS["studio"])
        return {
            **user_dict,
            "floor_plan": plan,
            "inventory": inv,
            "group": dict(grp) if grp else None,
        }

    def update_avatar_appearance(self, user_id: str, appearance: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT avatar_appearance_json FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                raise ValueError("User not found.")
            try:
                cur = json.loads(row.get("avatar_appearance_json") or "{}")
            except Exception:
                cur = {}
            clean_app = sanitize_avatar_appearance(appearance, base=cur)
            if isinstance(cur, dict) and cur.get("identity_initialized"):
                # Locked head identity after initial creation; must pay via select_avatar_identity
                cur_base = str(cur.get("base_char_id") or "")
                if cur_base in ALLOWED_BASE_CHAR_IDS:
                    clean_app["base_char_id"] = cur_base
                clean_app["identity_initialized"] = True
            con.execute(
                "UPDATE au_users SET avatar_appearance_json = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(clean_app), now, user_id),
            )
        return self.get_user_bundle(user_id)  # type: ignore[return-value]

    def select_avatar_identity(self, user_id: str, base_char_id: str) -> Dict[str, Any]:
        """Select character head/identity: FREE during initial creation, 15 AU Coins for later head changes."""
        bc = str(base_char_id or "").strip()
        if bc not in ALLOWED_BASE_CHAR_IDS:
            raise ValueError(f"Unknown character head identity '{bc}'.")
        now = time.time()
        female_ids = {"base_idol", "base_mage", "base_beach_f"}
        with self._conn(exclusive=True) as con:
            row = con.execute(
                "SELECT au_coins, avatar_appearance_json, avatar_glb_url FROM au_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                raise ValueError("User not found.")
            try:
                raw_cur = json.loads(row.get("avatar_appearance_json") or "{}")
            except Exception:
                raw_cur = {}
            cur_app = sanitize_avatar_appearance(None, base=raw_cur if isinstance(raw_cur, dict) else None)
            already_initialized = bool(cur_app.get("identity_initialized"))
            cur_base = str(cur_app.get("base_char_id") or "base_versekeeper")
            has_uploaded_glb = bool(row.get("avatar_glb_url") and "/avatar/file/" in str(row.get("avatar_glb_url") or ""))

            if not already_initialized or (cur_base == bc and not has_uploaded_glb):
                cost_au = 0
            else:
                cost_au = AVATAR_HEAD_CHANGE_FEE_AU
                if int(row["au_coins"]) < cost_au:
                    raise ValueError(f"Changing your character head costs {cost_au} AU Coins.")
                res = con.execute(
                    "UPDATE au_users SET au_coins = au_coins - ?, updated_at = ? WHERE user_id = ? AND au_coins >= ?",
                    (cost_au, now, user_id, cost_au),
                )
                if res.rowcount == 0:
                    raise ValueError(f"Changing your character head costs {cost_au} AU Coins.")
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, meta_json, created_at) VALUES (?, ?, 'au_coins', ?, 'avatar_head_change', ?, ?)",
                    ("tx_" + secrets.token_hex(6), user_id, -cost_au, json.dumps({"base_char_id": bc}), now),
                )

            cur_app["base_char_id"] = bc
            cur_app["identity_initialized"] = True
            cur_app["gender"] = "female" if bc in female_ids else "male"
            con.execute(
                "UPDATE au_users SET avatar_appearance_json = ?, avatar_glb_url = '', updated_at = ? WHERE user_id = ?",
                (json.dumps(cur_app), now, user_id),
            )
        return {
            "ok": True,
            "cost_au": cost_au,
            "base_char_id": bc,
            "user": self.get_user_bundle(user_id),
        }

    def get_avatar_blob(self, user_id: str) -> Optional[Dict[str, Any]]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{3,64}", user_id or ""):
            return None
        with self._conn() as con:
            row = con.execute(
                "SELECT user_id, filename, content_type, data, updated_at FROM au_avatar_blobs WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row and row.get("data") is not None:
            return {
                "user_id": row["user_id"],
                "filename": row["filename"],
                "content_type": row["content_type"] or "model/gltf-binary",
                "data": bytes(row["data"]),
                "updated_at": float(row["updated_at"]),
            }
        disk_path = PUBLIC_MODELS_DIR / "avatars" / f"{user_id}.glb"
        if disk_path.is_file():
            return {
                "user_id": user_id,
                "filename": f"{user_id}.glb",
                "content_type": "model/gltf-binary",
                "data": disk_path.read_bytes(),
                "updated_at": disk_path.stat().st_mtime,
            }
        return None

    def update_user_settings(self, user_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT settings_json FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                raise ValueError("User not found.")
            try:
                cur = json.loads(row["settings_json"] or "{}")
            except Exception:
                cur = {}
            clean_settings = sanitize_user_settings(patch, base=cur)
            con.execute(
                "UPDATE au_users SET settings_json = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(clean_settings), now, user_id),
            )
        return self.get_user_bundle(user_id)

    def update_profile(self, user_id: str, username: str, display_name: str = "", bio: str = "") -> Dict[str, Any]:
        uname = re.sub(r"[^a-zA-Z0-9_]", "", (username or "").strip())
        if not (3 <= len(uname) <= 20):
            raise ValueError("Account username must be 3-20 alphanumeric/underscore characters.")
        u_check = filter_pg13_text(uname)
        if u_check["flagged"] or u_check["blocked"]:
            raise ValueError("Username violates Anime Universe PG-13 policy.")
        dn_check = filter_pg13_text(display_name or uname)
        bio_check = filter_pg13_text(bio or "", allow_empty=True)
        if dn_check["blocked"] or bio_check["blocked"]:
            raise ValueError("Profile text blocked by PG-13 safety policy.")
        now = time.time()
        with self._conn(exclusive=True) as con:
            taken = con.execute(
                "SELECT user_id FROM au_users WHERE lower(username) = lower(?) AND user_id != ?",
                (uname, user_id),
            ).fetchone()
            if taken:
                raise ValueError("That username is already taken.")
            con.execute(
                """
                UPDATE au_users
                SET username = ?, display_name = ?, bio = ?, profile_completed = 1, updated_at = ?
                WHERE user_id = ?
                """,
                (uname, str(dn_check["clean_text"])[:32], str(bio_check["clean_text"])[:180], now, user_id),
            )
        return self.get_user_bundle(user_id)

    # ---------- Economy, Store, 1-Way Swap, Upgrades, Storage ----------
    def grant_au_coins(self, user_id: str, amount: int, reason: str = "event_grant") -> Dict[str, Any]:
        """Credit AU Coins ONLY from paid/event server hooks (never passive city earnings)."""
        amt = int(amount)
        if amt <= 0:
            raise ValueError("Grant amount must be positive.")
        now = time.time()
        with self._conn(exclusive=True) as con:
            con.execute("UPDATE au_users SET au_coins = au_coins + ?, updated_at = ? WHERE user_id = ?", (amt, now, user_id))
            con.execute(
                "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'au_coins', ?, ?, ?)",
                ("tx_" + secrets.token_hex(6), user_id, amt, reason, now),
            )
        return self.get_user_bundle(user_id)

    def convert_au_to_zencoins(self, user_id: str, au_amount: int) -> Dict[str, Any]:
        """Strict ONE-WAY conversion: AU Coins -> Zencoins (`1 AU = 100 ZC`)."""
        amt = int(au_amount)
        if amt <= 0:
            raise ValueError("Conversion requires a positive AU Coin amount.")
        zen_gain = amt * AU_TO_ZEN_RATE
        now = time.time()
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT au_coins FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not row or int(row["au_coins"]) < amt:
                raise ValueError("Insufficient AU Coins for conversion.")
            con.execute(
                "UPDATE au_users SET au_coins = au_coins - ?, zencoins = zencoins + ?, updated_at = ? WHERE user_id = ?",
                (amt, zen_gain, now, user_id),
            )
            con.execute(
                "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'au_coins', ?, 'convert_au_to_zen', ?)",
                ("tx_" + secrets.token_hex(6), user_id, -amt, now),
            )
            con.execute(
                "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'zencoins', ?, 'convert_au_to_zen', ?)",
                ("tx_" + secrets.token_hex(6), user_id, zen_gain, now),
            )
        return self.get_user_bundle(user_id)

    def subscribe_newsletter(self, email: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        clean_email = (email or "").strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean_email):
            raise ValueError("Enter a valid email address.")
        now = time.time()
        rewarded = 0
        with self._conn(exclusive=True) as con:
            existing_sub = con.execute("SELECT email FROM au_newsletter_subs WHERE email = ?", (clean_email,)).fetchone()
            if existing_sub:
                raise ValueError("That email is already subscribed to the AU newsletter.")
            con.execute(
                "INSERT INTO au_newsletter_subs (email, user_id, created_at) VALUES (?, ?, ?)",
                (clean_email, user_id or "", now),
            )
            if user_id:
                u = con.execute("SELECT newsletter_claimed FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
                if u and int(u["newsletter_claimed"]) == 0:
                    rewarded = NEWSLETTER_BONUS_ZC
                    con.execute(
                        "UPDATE au_users SET zencoins = zencoins + ?, newsletter_claimed = 1, updated_at = ? WHERE user_id = ?",
                        (rewarded, now, user_id),
                    )
                    con.execute(
                        "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'zencoins', ?, 'newsletter_signup', ?)",
                        ("tx_" + secrets.token_hex(6), user_id, rewarded, now),
                    )
        return {
            "ok": True,
            "email": clean_email,
            "rewarded_zencoins": rewarded,
            "user": self.get_user_bundle(user_id) if user_id else None,
        }

    def claim_daily_checkin(self, user_id: str) -> Dict[str, Any]:
        day_key = time.strftime("%Y-%m-%d", time.gmtime())
        now = time.time()
        with self._conn(exclusive=True) as con:
            u = con.execute("SELECT last_checkin_day FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not u:
                raise ValueError("User not found.")
            if u["last_checkin_day"] == day_key:
                return {"awarded": 0, "user": self.get_user_bundle(user_id)}
            con.execute(
                "UPDATE au_users SET zencoins = zencoins + ?, last_checkin_day = ?, updated_at = ? WHERE user_id = ?",
                (DAILY_CHECKIN_ZC, day_key, now, user_id),
            )
            con.execute(
                "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'zencoins', ?, 'daily_checkin', ?)",
                ("tx_" + secrets.token_hex(6), user_id, DAILY_CHECKIN_ZC, now),
            )
        return {"awarded": DAILY_CHECKIN_ZC, "user": self.get_user_bundle(user_id)}

    def record_district_discovery(self, user_id: str, district_id: str) -> Dict[str, Any]:
        valid_ids = {d["id"] for d in AU_CITY_DISTRICTS}
        if district_id not in valid_ids:
            raise ValueError("Unknown district.")
        now = time.time()
        awarded = 0
        with self._conn(exclusive=True) as con:
            u = con.execute("SELECT discovered_districts FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not u:
                raise ValueError("User not found.")
            visited = json.loads(u["discovered_districts"] or '["plaza"]')
            if district_id not in visited:
                visited.append(district_id)
                awarded = DISCOVERY_BONUS_ZC
                con.execute(
                    "UPDATE au_users SET discovered_districts = ?, zencoins = zencoins + ?, updated_at = ? WHERE user_id = ?",
                    (json.dumps(visited), awarded, now, user_id),
                )
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, meta_json, created_at) VALUES (?, ?, 'zencoins', ?, 'district_discovery', ?, ?)",
                    ("tx_" + secrets.token_hex(6), user_id, awarded, json.dumps({"district_id": district_id}), now),
                )
        return {"awarded_zencoins": awarded, "user": self.get_user_bundle(user_id)}

    def buy_item(self, user_id: str, item_id: str, pay_with: str = "zc", target_location: str = "inventory") -> Dict[str, Any]:
        item = CATALOG_BY_ID.get(item_id)
        if not item:
            raise ValueError("Unknown store item.")
        if target_location not in ("inventory", "storage"):
            target_location = "inventory"
        pay_mode = (pay_with or "zc").lower()
        if pay_mode not in ("zc", "au"):
            raise ValueError("Currency must be 'zc' or 'au'.")
        if item["currency"] == "au" and pay_mode != "au":
            raise ValueError("This item is an AU Coin exclusive.")
        cost = int(item["price_au"] if pay_mode == "au" else item["price_zc"])
        if pay_mode == "zc" and item["price_zc"] == 0 and item["price_au"] > 0:
            raise ValueError("This item can only be purchased with AU Coins.")
        col = "au_coins" if pay_mode == "au" else "zencoins"
        now = time.time()
        inv_id = "inv_" + secrets.token_hex(6)
        with self._conn(exclusive=True) as con:
            u = con.execute(f"SELECT {col} AS bal FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not u or int(u["bal"]) < cost:
                raise ValueError(f"Insufficient {'AU Coins' if pay_mode == 'au' else 'Zencoins'}.")
            if cost > 0:
                con.execute(f"UPDATE au_users SET {col} = {col} - ?, updated_at = ? WHERE user_id = ?", (cost, now, user_id))
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, meta_json, created_at) VALUES (?, ?, ?, ?, 'shop_purchase', ?, ?)",
                    ("tx_" + secrets.token_hex(6), user_id, col, -cost, json.dumps({"item_id": item_id}), now),
                )
            con.execute(
                "INSERT INTO au_inventory (inv_id, user_id, item_id, location, acquired_at) VALUES (?, ?, ?, ?, ?)",
                (inv_id, user_id, item_id, target_location, now),
            )
        return {"purchased_inv_id": inv_id, "user": self.get_user_bundle(user_id)}

    def equip_loadout(
        self,
        user_id: str,
        skin_id: Optional[str] = None,
        vehicle_id: Optional[str] = None,
        pet_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        with self._conn(exclusive=True) as con:
            owned = {r["item_id"] for r in con.execute("SELECT item_id FROM au_inventory WHERE user_id = ?", (user_id,)).fetchall()}
            owned.add("au_skin_versekeeper")
            updates: Dict[str, Any] = {"updated_at": now}
            if skin_id is not None:
                if skin_id and skin_id not in owned:
                    raise ValueError("You do not own that skin.")
                updates["equipped_skin_id"] = skin_id or "au_skin_versekeeper"
            if vehicle_id is not None:
                if vehicle_id and vehicle_id not in owned:
                    raise ValueError("You do not own that vehicle.")
                updates["equipped_vehicle_id"] = vehicle_id
            if pet_id is not None:
                if pet_id and pet_id not in owned:
                    raise ValueError("You do not own that pet.")
                updates["equipped_pet_id"] = pet_id
            sets = ", ".join(f"{k} = ?" for k in updates)
            con.execute(f"UPDATE au_users SET {sets} WHERE user_id = ?", (*updates.values(), user_id))
        return self.get_user_bundle(user_id)

    def upgrade_apartment_tier(self, user_id: str, tier_id: str, pay_with: str = "zc") -> Dict[str, Any]:
        plan = AU_FLOOR_PLANS.get(tier_id)
        if not plan:
            raise ValueError("Unknown apartment floor plan.")
        pay_mode = (pay_with or "zc").lower()
        cost = int(plan["cost_au"] if pay_mode == "au" else plan["cost_zc"])
        if pay_mode == "zc" and plan["cost_zc"] == 0 and plan["cost_au"] > 0:
            raise ValueError("Penthouse upgrades require AU Coins.")
        col = "au_coins" if pay_mode == "au" else "zencoins"
        now = time.time()
        with self._conn(exclusive=True) as con:
            u = con.execute(f"SELECT {col} AS bal, apartment_tier FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not u:
                raise ValueError("User not found.")
            if u["apartment_tier"] == tier_id:
                raise ValueError("You already own this floor plan.")
            if int(u["bal"]) < cost:
                raise ValueError(f"Insufficient {'AU Coins' if pay_mode == 'au' else 'Zencoins'}.")
            if cost > 0:
                con.execute(f"UPDATE au_users SET {col} = {col} - ?, updated_at = ? WHERE user_id = ?", (cost, now, user_id))
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, meta_json, created_at) VALUES (?, ?, ?, ?, 'apartment_upgrade', ?, ?)",
                    ("tx_" + secrets.token_hex(6), user_id, col, -cost, json.dumps({"tier_id": tier_id}), now),
                )
            con.execute("UPDATE au_users SET apartment_tier = ?, updated_at = ? WHERE user_id = ?", (tier_id, now, user_id))
        return self.get_user_bundle(user_id)

    # ---------- Storage Room & Instanced Home ----------
    def move_item_location(
        self,
        user_id: str,
        inv_id: str,
        target_location: str,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
        pos_z: float = 0.0,
        rot_y: float = 0.0,
    ) -> Dict[str, Any]:
        if inv_id.startswith("perm_"):
            raise ValueError("Permanent apartment fixtures (Bookshelf, Piggy Bank, TV) cannot be removed or stored.")
        if target_location not in ("storage", "inventory", "placed"):
            raise ValueError("Invalid item location.")
        with self._conn(exclusive=True) as con:
            item_row = con.execute(
                "SELECT * FROM au_inventory WHERE inv_id = ? AND user_id = ?",
                (inv_id, user_id),
            ).fetchone()
            if not item_row:
                raise ValueError("Inventory item not found.")
            if target_location == "placed" and item_row["location"] != "placed":
                u = con.execute("SELECT apartment_tier FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
                plan = AU_FLOOR_PLANS.get(u["apartment_tier"], AU_FLOOR_PLANS["studio"])
                cnt = con.execute(
                    "SELECT COUNT(*) AS c FROM au_inventory WHERE user_id = ? AND location = 'placed'",
                    (user_id,),
                ).fetchone()["c"]
                if int(cnt) >= int(plan["max_furniture"]):
                    raise ValueError(f"Floor plan limit reached ({plan['max_furniture']} items). Upgrade apartment or move an item to Storage.")
            con.execute(
                "UPDATE au_inventory SET location = ?, pos_x = ?, pos_y = ?, pos_z = ?, rot_y = ? WHERE inv_id = ? AND user_id = ?",
                (target_location, float(pos_x), float(pos_y), float(pos_z), float(rot_y), inv_id, user_id),
            )
        return self.get_user_bundle(user_id)

    def get_home_view(self, owner_id: str, requester_id: str) -> Dict[str, Any]:
        with self._conn() as con:
            owner = con.execute(
                "SELECT user_id, username, display_name, apartment_tier, avatar_glb_url, avatar_appearance_json, settings_json FROM au_users WHERE user_id = ?",
                (owner_id,),
            ).fetchone()
            if not owner:
                raise ValueError("Home owner not found.")
            if owner_id != requester_id:
                inv = con.execute(
                    "SELECT 1 FROM au_home_invites WHERE owner_id = ? AND guest_id = ?",
                    (owner_id, requester_id),
                ).fetchone()
                if not inv:
                    raise PermissionError("Private home access requires an invitation from the owner.")
            placed = con.execute(
                "SELECT * FROM au_inventory WHERE user_id = ? AND location = 'placed'",
                (owner_id,),
            ).fetchall()
            invite_rows = con.execute(
                "SELECT u.user_id, u.username, u.display_name FROM au_home_invites hi JOIN au_users u ON hi.guest_id = u.user_id WHERE hi.owner_id = ?",
                (owner_id,),
            ).fetchall()
        owner_dict = dict(owner)
        try:
            raw_app = json.loads(owner_dict.pop("avatar_appearance_json", "{}") or "{}")
        except Exception:
            raw_app = {}
        try:
            raw_settings = json.loads(owner_dict.pop("settings_json", "{}") or "{}")
        except Exception:
            raw_settings = {}
        perm_layout = raw_settings.get("perm_layout") if isinstance(raw_settings, dict) else {}
        if not isinstance(perm_layout, dict):
            perm_layout = {}
        perm_items: List[Dict[str, Any]] = []
        for base_pm in PERMANENT_HOME_ITEMS:
            pm_copy = {**base_pm, "pos": list(base_pm["pos"])}
            ov = perm_layout.get(base_pm["item_id"])
            if isinstance(ov, dict):
                try:
                    pm_copy["pos"] = [
                        max(-18.0, min(18.0, float(ov.get("pos_x", pm_copy["pos"][0])))),
                        max(0.0, min(4.0, float(ov.get("pos_y", pm_copy["pos"][1])))),
                        max(-18.0, min(18.0, float(ov.get("pos_z", pm_copy["pos"][2])))),
                    ]
                    pm_copy["rot_y"] = float(ov.get("rot_y", pm_copy.get("rot_y", 0.0))) % (math.tau)
                except (TypeError, ValueError):
                    pass
            perm_items.append(pm_copy)

        owner_dict["avatar_appearance"] = sanitize_avatar_appearance(raw_app)
        plan = AU_FLOOR_PLANS.get(owner_dict["apartment_tier"], AU_FLOOR_PLANS["studio"])
        return {
            "owner": owner_dict,
            "floor_plan": plan,
            "permanent_items": perm_items,
            "placed_items": [dict(r) for r in placed],
            "invited_guests": [dict(r) for r in invite_rows] if owner_id == requester_id else [],
        }

    def move_permanent_home_item(
        self,
        user_id: str,
        item_id: str,
        pos_x: float,
        pos_y: float,
        pos_z: float,
        rot_y: float,
    ) -> Dict[str, Any]:
        """Persist custom position/rotation/stacking height for a permanent apartment fixture."""
        valid_ids = {p["item_id"] for p in PERMANENT_HOME_ITEMS}
        if item_id not in valid_ids:
            raise ValueError(f"Unknown permanent home item '{item_id}'.")
        cx = round(max(-18.0, min(18.0, float(pos_x))), 2)
        cy = round(max(0.0, min(4.0, float(pos_y))), 2)
        cz = round(max(-18.0, min(18.0, float(pos_z))), 2)
        ry = round(float(rot_y) % math.tau, 3)
        now = time.time()
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT settings_json FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                raise ValueError("User not found.")
            try:
                cur_settings = json.loads(row.get("settings_json") or "{}")
            except Exception:
                cur_settings = {}
            if not isinstance(cur_settings, dict):
                cur_settings = {}
            perm_layout = cur_settings.get("perm_layout")
            if not isinstance(perm_layout, dict):
                perm_layout = {}
            perm_layout[item_id] = {"pos_x": cx, "pos_y": cy, "pos_z": cz, "rot_y": ry}
            cur_settings["perm_layout"] = perm_layout
            con.execute(
                "UPDATE au_users SET settings_json = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(cur_settings), now, user_id),
            )
        return self.get_home_view(user_id, user_id)

    def _enforce_social_privacy(
        self,
        con: _AuDbConn,
        recipient_id: str,
        raw_settings_json: Optional[str],
        actor_id: str,
        setting_key: str,
        nobody_msg: str,
        contacts_msg: str,
    ) -> None:
        try:
            parsed = json.loads(raw_settings_json or "{}")
        except Exception:
            parsed = {}
        mode = str(sanitize_user_settings(parsed).get(setting_key, "everyone"))
        if mode == "nobody":
            raise ValueError(nobody_msg)
        if mode == "contacts":
            row = con.execute(
                "SELECT 1 FROM au_contacts WHERE user_id = ? AND contact_id = ?",
                (recipient_id, actor_id),
            ).fetchone()
            if not row:
                raise ValueError(contacts_msg)

    def invite_to_home(self, owner_id: str, target_username: str) -> Dict[str, Any]:
        now = time.time()
        with self._conn(exclusive=True) as con:
            guest = con.execute(
                "SELECT user_id, username, display_name, settings_json FROM au_users WHERE lower(username) = lower(?)",
                (target_username.strip(),),
            ).fetchone()
            if not guest:
                raise ValueError("No player found with that username.")
            if guest["user_id"] == owner_id:
                raise ValueError("You are already the owner of this home.")
            self._enforce_social_privacy(
                con,
                guest["user_id"],
                guest["settings_json"],
                owner_id,
                "home_invites_privacy",
                "That player is currently declining apartment invitations.",
                "That player only accepts apartment invitations from their contacts.",
            )
            con.upsert(
                "au_home_invites",
                {"owner_id": owner_id, "guest_id": guest["user_id"], "created_at": now},
                ("owner_id", "guest_id"),
            )
        return {"ok": True, "invited": {"user_id": guest["user_id"], "username": guest["username"], "display_name": guest["display_name"]}}

    # ---------- User-to-User Zencoin & Item Trading ----------
    def create_trade(self, sender_id: str, target_username: str, offer_zc: int = 0, request_zc: int = 0, offer_inv_id: str = "") -> Dict[str, Any]:
        ozc = max(0, int(offer_zc))
        rzc = max(0, int(request_zc))
        if ozc == 0 and rzc == 0 and not offer_inv_id:
            raise ValueError("Trade must include Zencoins or a tradeable item.")
        now = time.time()
        tid = "trd_" + secrets.token_hex(6)
        with self._conn(exclusive=True) as con:
            rec = con.execute("SELECT user_id, username FROM au_users WHERE lower(username) = lower(?)", (target_username.strip(),)).fetchone()
            if not rec:
                raise ValueError("Recipient username not found.")
            if rec["user_id"] == sender_id:
                raise ValueError("Cannot trade with yourself.")
            sender = con.execute("SELECT zencoins FROM au_users WHERE user_id = ?", (sender_id,)).fetchone()
            if not sender or int(sender["zencoins"]) < ozc:
                raise ValueError("Insufficient Zencoins to offer.")
            if offer_inv_id:
                item_row = con.execute("SELECT item_id FROM au_inventory WHERE inv_id = ? AND user_id = ?", (offer_inv_id, sender_id)).fetchone()
                if not item_row:
                    raise ValueError("Offered inventory item not owned.")
                cat = CATALOG_BY_ID.get(item_row["item_id"], {})
                if not cat.get("tradeable", False):
                    raise ValueError("Only Zencoin-eligible items can be traded (AU exclusives are soulbound).")
            con.execute(
                """
                INSERT INTO au_trades (trade_id, sender_id, recipient_id, offer_zc, request_zc, offer_inv_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (tid, sender_id, rec["user_id"], ozc, rzc, offer_inv_id, now, now),
            )
        return {"trade_id": tid, "recipient_username": rec["username"], "status": "pending"}

    def accept_trade(self, recipient_id: str, trade_id: str) -> Dict[str, Any]:
        now = time.time()
        with self._conn(exclusive=True) as con:
            tr = con.execute("SELECT * FROM au_trades WHERE trade_id = ?", (trade_id,)).fetchone()
            if not tr or tr["status"] != "pending":
                raise ValueError("Trade is no longer pending.")
            if tr["recipient_id"] != recipient_id:
                raise PermissionError("Only the recipient may accept this trade.")
            s_row = con.execute("SELECT zencoins FROM au_users WHERE user_id = ?", (tr["sender_id"],)).fetchone()
            r_row = con.execute("SELECT zencoins FROM au_users WHERE user_id = ?", (recipient_id,)).fetchone()
            if int(s_row["zencoins"]) < int(tr["offer_zc"]):
                raise ValueError("Sender no longer has enough Zencoins.")
            if int(r_row["zencoins"]) < int(tr["request_zc"]):
                raise ValueError("You do not have enough Zencoins for the requested amount.")
            net_r = int(tr["offer_zc"]) - int(tr["request_zc"])
            if net_r != 0:
                con.execute("UPDATE au_users SET zencoins = zencoins - ?, updated_at = ? WHERE user_id = ?", (net_r, now, tr["sender_id"]))
                con.execute("UPDATE au_users SET zencoins = zencoins + ?, updated_at = ? WHERE user_id = ?", (net_r, now, recipient_id))
            if tr["offer_inv_id"]:
                res = con.execute(
                    "UPDATE au_inventory SET user_id = ?, location = 'storage', pos_x = 0, pos_y = 0, pos_z = 0 WHERE inv_id = ? AND user_id = ?",
                    (recipient_id, tr["offer_inv_id"], tr["sender_id"]),
                )
                if res.rowcount != 1:
                    raise ValueError("Offered item is no longer available.")
            con.execute("UPDATE au_trades SET status = 'accepted', updated_at = ? WHERE trade_id = ?", (now, trade_id))
        return {"ok": True, "user": self.get_user_bundle(recipient_id)}

    def list_trades(self, user_id: str) -> List[Dict[str, Any]]:
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT t.*, su.username AS sender_username, ru.username AS recipient_username
                FROM au_trades t
                JOIN au_users su ON t.sender_id = su.user_id
                JOIN au_users ru ON t.recipient_id = ru.user_id
                WHERE t.sender_id = ? OR t.recipient_id = ?
                ORDER BY t.created_at DESC LIMIT 30
                """,
                (user_id, user_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Groups (WoW Party Sync Phasing) ----------
    def create_group(self, owner_id: str, name: str, is_private: bool = False) -> Dict[str, Any]:
        clean = filter_pg13_text((name or "").strip()[:36])
        if clean["blocked"]:
            raise ValueError("Group name blocked by PG-13 policy.")
        gname = str(clean["clean_text"] or "AU Party")
        gid = "grp_" + secrets.token_hex(5)
        now = time.time()
        with self._conn(exclusive=True) as con:
            con.execute("DELETE FROM au_group_members WHERE user_id = ?", (owner_id,))
            con.execute(
                "INSERT INTO au_groups (group_id, name, owner_id, is_private, max_members, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (gid, gname, owner_id, 1 if is_private else 0, GROUP_PHASE_HARD_CAP, now),
            )
            con.execute("INSERT INTO au_group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)", (gid, owner_id, now))
        return {"group": self.get_group(gid), "user": self.get_user_bundle(owner_id)}

    def join_group(self, user_id: str, group_id: str) -> Dict[str, Any]:
        now = time.time()
        with self._conn(exclusive=True) as con:
            grp = con.execute("SELECT * FROM au_groups WHERE group_id = ?", (group_id,)).fetchone()
            if not grp:
                raise ValueError("Group not found.")
            cnt = con.execute("SELECT COUNT(*) AS c FROM au_group_members WHERE group_id = ?", (group_id,)).fetchone()["c"]
            if int(cnt) >= int(grp["max_members"]):
                raise ValueError("Group phase is full (32-player performance cap).")
            con.execute("DELETE FROM au_group_members WHERE user_id = ?", (user_id,))
            con.execute("INSERT INTO au_group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)", (group_id, user_id, now))
        return {"group": self.get_group(group_id), "user": self.get_user_bundle(user_id)}

    def leave_group(self, user_id: str) -> Dict[str, Any]:
        with self._conn(exclusive=True) as con:
            con.execute("DELETE FROM au_group_members WHERE user_id = ?", (user_id,))
        return {"ok": True, "user": self.get_user_bundle(user_id)}

    def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as con:
            g = con.execute("SELECT * FROM au_groups WHERE group_id = ?", (group_id,)).fetchone()
            if not g:
                return None
            cnt = con.execute("SELECT COUNT(*) AS c FROM au_group_members WHERE group_id = ?", (group_id,)).fetchone()["c"]
        d = dict(g)
        d["member_count"] = int(cnt)
        return d

    def list_public_groups(self) -> List[Dict[str, Any]]:
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT g.*, COUNT(gm.user_id) AS member_count, u.username AS owner_username
                FROM au_groups g
                LEFT JOIN au_group_members gm ON g.group_id = gm.group_id
                LEFT JOIN au_users u ON g.owner_id = u.user_id
                WHERE g.is_private = 0
                GROUP BY g.group_id, g.name, g.owner_id, g.is_private, g.max_members, g.created_at, u.username
                ORDER BY member_count DESC, g.created_at DESC
                LIMIT 30
                """
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- Smartphone Social: Contacts, DMs, Chat Logs ----------
    def add_contact(self, user_id: str, target_username: str, nickname: str = "") -> List[Dict[str, Any]]:
        now = time.time()
        with self._conn(exclusive=True) as con:
            c = con.execute("SELECT user_id FROM au_users WHERE lower(username) = lower(?)", (target_username.strip(),)).fetchone()
            if not c:
                raise ValueError("User not found.")
            if c["user_id"] == user_id:
                raise ValueError("Cannot add yourself as a contact.")
            nick_clean = str(filter_pg13_text(nickname, allow_empty=True)["clean_text"])[:24]
            con.upsert(
                "au_contacts",
                {"user_id": user_id, "contact_id": c["user_id"], "nickname": nick_clean, "created_at": now},
                ("user_id", "contact_id"),
            )
        return self.list_contacts(user_id)

    def list_contacts(self, user_id: str) -> List[Dict[str, Any]]:
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT u.user_id, u.username, u.display_name, u.avatar_glb_url, c.nickname
                FROM au_contacts c
                JOIN au_users u ON c.contact_id = u.user_id
                WHERE c.user_id = ?
                ORDER BY u.username ASC
                """,
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def send_dm(self, sender_id: str, target_username: str, body: str) -> Dict[str, Any]:
        chk = filter_pg13_text(body)
        if chk["blocked"]:
            raise ValueError(str(chk["reason"]))
        if not str(chk["clean_text"]).strip():
            raise ValueError("Message cannot be empty.")
        now = time.time()
        dm_id = "dm_" + secrets.token_hex(6)
        with self._conn(exclusive=True) as con:
            sender_row = con.execute(
                "SELECT muted_until, banned, ban_reason FROM au_users WHERE user_id = ?",
                (sender_id,),
            ).fetchone()
            if not sender_row:
                raise ValueError("Sender not found.")
            if int(sender_row.get("banned") or 0) == 1:
                raise PermissionError(f"Account banned: {sender_row.get('ban_reason') or 'Suspended by administrator.'}")
            muted_until = float(sender_row.get("muted_until") or 0.0)
            if muted_until > now:
                raise ValueError(f"You are muted for {self._format_mute_remaining(muted_until, now)} remaining.")

            rec = con.execute("SELECT user_id, username, settings_json FROM au_users WHERE lower(username) = lower(?)", (target_username.strip(),)).fetchone()
            if not rec:
                raise ValueError("Recipient username not found.")
            if rec["user_id"] != sender_id:
                self._enforce_social_privacy(
                    con,
                    rec["user_id"],
                    rec["settings_json"],
                    sender_id,
                    "dm_privacy",
                    "That player has disabled direct messages.",
                    "That player only accepts direct messages from their contacts.",
                )
            con.execute(
                "INSERT INTO au_direct_messages (dm_id, sender_id, recipient_id, body, flagged, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (dm_id, sender_id, rec["user_id"], str(chk["clean_text"])[:500], 1 if chk["flagged"] else 0, now),
            )
        return {
            "dm_id": dm_id,
            "recipient_id": rec["user_id"],
            "recipient_username": rec["username"],
            "body": str(chk["clean_text"])[:500],
            "flagged": bool(chk["flagged"]),
            "created_at": now,
        }

    def list_dms(self, user_id: str, with_username: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as con:
            if with_username:
                rows = con.execute(
                    """
                    SELECT d.*, su.username AS sender_username, ru.username AS recipient_username
                    FROM au_direct_messages d
                    JOIN au_users su ON d.sender_id = su.user_id
                    JOIN au_users ru ON d.recipient_id = ru.user_id
                    WHERE (d.sender_id = ? AND lower(ru.username) = lower(?))
                       OR (d.recipient_id = ? AND lower(su.username) = lower(?))
                    ORDER BY d.created_at DESC LIMIT 60
                    """,
                    (user_id, with_username.strip(), user_id, with_username.strip()),
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT d.*, su.username AS sender_username, ru.username AS recipient_username
                    FROM au_direct_messages d
                    JOIN au_users su ON d.sender_id = su.user_id
                    JOIN au_users ru ON d.recipient_id = ru.user_id
                    WHERE d.sender_id = ? OR d.recipient_id = ?
                    ORDER BY d.created_at DESC LIMIT 60
                    """,
                    (user_id, user_id),
                ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def save_chat_message(
        self,
        channel: str,
        phase_id: str,
        user_id: str,
        username: str,
        raw_body: str,
        pos_x: float = 0.0,
        pos_z: float = 0.0,
    ) -> Dict[str, Any]:
        chk = filter_pg13_text(raw_body)
        if chk["blocked"]:
            raise ValueError(str(chk["reason"]))
        clean = str(chk["clean_text"]).strip()[:400]
        if not clean:
            raise ValueError("Message cannot be empty.")
        now = time.time()
        mid = "msg_" + secrets.token_hex(6)
        with self._conn(exclusive=True) as con:
            u_mod = con.execute(
                "SELECT muted_until, banned, ban_reason FROM au_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if u_mod:
                if int(u_mod.get("banned") or 0) == 1:
                    raise ValueError(f"Account banned: {u_mod.get('ban_reason') or 'Suspended by administrator.'}")
                muted_until = float(u_mod.get("muted_until") or 0.0)
                if muted_until > now:
                    raise ValueError(f"You are muted for {self._format_mute_remaining(muted_until, now)} remaining.")
            con.execute(
                """
                INSERT INTO au_chat_messages (msg_id, channel, phase_id, user_id, username, body, flagged, pos_x, pos_z, deleted, deleted_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?)
                """,
                (mid, channel, phase_id, user_id, username, clean, 1 if chk["flagged"] else 0, float(pos_x), float(pos_z), now),
            )
        return {
            "msg_id": mid,
            "channel": channel,
            "phase_id": phase_id,
            "user_id": user_id,
            "username": username,
            "body": clean,
            "flagged": bool(chk["flagged"]),
            "matched_languages": chk["matched_languages"],
            "pos_x": float(pos_x),
            "pos_z": float(pos_z),
            "created_at": now,
        }

    def list_chat_history(self, channel: str, phase_id: Optional[str] = None, limit: int = 40) -> List[Dict[str, Any]]:
        lim = max(1, min(100, int(limit)))
        with self._conn() as con:
            if channel == "global":
                rows = con.execute(
                    "SELECT * FROM au_chat_messages WHERE channel = 'global' AND COALESCE(deleted, 0) = 0 ORDER BY created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM au_chat_messages WHERE channel = ? AND phase_id = ? AND COALESCE(deleted, 0) = 0 ORDER BY created_at DESC LIMIT ?",
                    (channel, phase_id or "", lim),
                ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---------- World Clock & Dynamic World Layout Persistence ----------
    def get_world_clock(self) -> Dict[str, Any]:
        now = time.time()
        with self._conn() as con:
            row = con.execute(
                "SELECT value_json, updated_by, updated_at FROM au_world_state WHERE state_key = 'world_clock'"
            ).fetchone()
        if not row:
            return {
                "cycle_hour": 18.2,
                "effective_hour": 18.2,
                "cycle_speed": 0.15,
                "paused": False,
                "anchor_epoch": now,
                "updated_by": "",
                "updated_at": now,
                "server_epoch": now,
            }
        try:
            st = json.loads(row["value_json"] or "{}")
        except Exception:
            st = {}
        cycle_hour = float(st.get("cycle_hour", 18.2)) % 24.0
        cycle_speed = max(0.0, float(st.get("cycle_speed", 0.15)))
        paused = bool(st.get("paused", False))
        anchor_epoch = float(st.get("anchor_epoch", now))
        effective_hour = cycle_hour if paused else (cycle_hour + (now - anchor_epoch) * cycle_speed / 60.0) % 24.0
        return {
            "cycle_hour": round(cycle_hour, 4),
            "effective_hour": round(effective_hour, 4),
            "cycle_speed": cycle_speed,
            "paused": paused,
            "anchor_epoch": anchor_epoch,
            "updated_by": str(row.get("updated_by") or ""),
            "updated_at": float(row.get("updated_at") or now),
            "server_epoch": now,
        }

    def set_world_clock(
        self,
        admin_email: str,
        cycle_hour: Optional[float] = None,
        cycle_speed: Optional[float] = None,
        paused: Optional[bool] = None,
        preset: Optional[str] = None,
    ) -> Dict[str, Any]:
        preset_hours = {
            "dawn": 6.5,
            "morning": 9.5,
            "noon": 13.0,
            "day": 13.0,
            "sunset": 18.25,
            "twilight": 18.25,
            "dusk": 18.25,
            "night": 22.5,
            "tokyo_night": 22.5,
            "midnight": 0.0,
        }
        cur = self.get_world_clock()
        now = time.time()

        target_hour = float(cur["effective_hour"])
        if preset is not None and str(preset).strip():
            pk = str(preset).strip().lower().replace(" ", "_")
            if pk not in preset_hours:
                raise ValueError(f"Unknown world time preset: {preset}")
            target_hour = preset_hours[pk]
        elif cycle_hour is not None:
            hv = float(cycle_hour)
            if math.isnan(hv) or math.isinf(hv):
                raise ValueError("Invalid cycle_hour.")
            target_hour = hv % 24.0

        new_speed = float(cur["cycle_speed"])
        if cycle_speed is not None:
            sv = float(cycle_speed)
            if math.isnan(sv) or math.isinf(sv):
                raise ValueError("Invalid cycle_speed.")
            new_speed = round(max(0.0, min(60.0, sv)), 3)

        new_paused = bool(cur["paused"]) if paused is None else bool(paused)
        state_payload = {
            "cycle_hour": round(target_hour % 24.0, 4),
            "cycle_speed": new_speed,
            "paused": new_paused,
            "anchor_epoch": now,
        }
        with self._conn(exclusive=True) as con:
            con.upsert(
                "au_world_state",
                {
                    "state_key": "world_clock",
                    "value_json": json.dumps(state_payload),
                    "updated_by": admin_email.strip().lower(),
                    "updated_at": now,
                },
                ("state_key",),
            )
            self._log_admin_action(con, admin_email, "world_time_update", "world_clock", state_payload, now)
        return self.get_world_clock()

    def get_world_layout(self) -> List[Dict[str, Any]]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM au_city_world_layout ORDER BY updated_at ASC, obj_id ASC"
            ).fetchall()
        items: List[Dict[str, Any]] = []
        for r in rows:
            try:
                meta = json.loads(r.get("meta_json") or "{}")
            except Exception:
                meta = {}
            items.append({
                "obj_id": r["obj_id"],
                "kind": r["kind"],
                "model": r["model"],
                "label": r["label"],
                "x": float(r["x"]),
                "y": float(r["y"]),
                "z": float(r["z"]),
                "rot_y": float(r["rot_y"]),
                "scale_x": float(r["scale_x"]),
                "scale_y": float(r["scale_y"]),
                "scale_z": float(r["scale_z"]),
                "deleted": bool(int(r.get("deleted") or 0)),
                "meta": meta,
                "updated_by": str(r.get("updated_by") or ""),
                "updated_at": float(r.get("updated_at") or 0.0),
            })
        return items

    def upsert_world_layout_object(self, admin_email: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_id = str(payload.get("obj_id") or payload.get("id") or "").strip()
        if not raw_id:
            raw_id = "obj_" + secrets.token_hex(5)
        obj_id = re.sub(r"[^a-zA-Z0-9_.:-]", "", raw_id)[:64]
        if not obj_id:
            raise ValueError("Invalid object ID.")

        kind = re.sub(r"[^a-zA-Z0-9_-]", "", str(payload.get("kind") or "prop").strip())[:32] or "prop"
        model = str(payload.get("model") or "").strip()[:120]
        if not model:
            raise ValueError("Object model filename is required.")
        label = str(filter_pg13_text(str(payload.get("label") or obj_id), allow_empty=True)["clean_text"]).strip()[:80] or obj_id

        def _clamp_num(val: Any, lo: float, hi: float, default: float) -> float:
            try:
                v = float(val)
                if math.isnan(v) or math.isinf(v):
                    return default
                return round(max(lo, min(hi, v)), 4)
            except (TypeError, ValueError):
                return default

        x = _clamp_num(payload.get("x", 0.0), -500.0, 500.0, 0.0)
        y = _clamp_num(payload.get("y", 0.0), -100.0, 200.0, 0.0)
        z = _clamp_num(payload.get("z", 0.0), -500.0, 500.0, 0.0)
        rot_y = _clamp_num(payload.get("rot_y", 0.0), -64.0, 64.0, 0.0)
        uniform_scale = payload.get("scale")
        default_s = _clamp_num(uniform_scale, 0.1, 10.0, 1.0) if uniform_scale is not None else 1.0
        scale_x = _clamp_num(payload.get("scale_x", default_s), 0.1, 10.0, default_s)
        scale_y = _clamp_num(payload.get("scale_y", default_s), 0.1, 10.0, default_s)
        scale_z = _clamp_num(payload.get("scale_z", default_s), 0.1, 10.0, default_s)
        deleted = 1 if bool(payload.get("deleted", False)) else 0
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        now = time.time()

        record = {
            "obj_id": obj_id,
            "kind": kind,
            "model": model,
            "label": label,
            "x": x,
            "y": y,
            "z": z,
            "rot_y": rot_y,
            "scale_x": scale_x,
            "scale_y": scale_y,
            "scale_z": scale_z,
            "deleted": deleted,
            "meta_json": json.dumps(meta),
            "updated_by": admin_email.strip().lower(),
            "updated_at": now,
        }
        with self._conn(exclusive=True) as con:
            con.upsert("au_city_world_layout", record, ("obj_id",))
            self._log_admin_action(
                con,
                admin_email,
                "world_layout_upsert",
                obj_id,
                {"kind": kind, "model": model, "x": x, "y": y, "z": z, "rot_y": rot_y, "deleted": bool(deleted)},
                now,
            )
        return {
            **{k: v for k, v in record.items() if k != "meta_json"},
            "deleted": bool(deleted),
            "meta": meta,
        }

    def reset_world_layout(self, admin_email: str) -> List[Dict[str, Any]]:
        now = time.time()
        with self._conn(exclusive=True) as con:
            con.execute("DELETE FROM au_city_world_layout")
            self._log_admin_action(con, admin_email, "world_layout_reset", "all", {}, now)
        return []

    # ---------- Admin Console & Moderation ----------
    def get_admin_overview(self) -> Dict[str, Any]:
        with self._conn() as con:
            user_rows = con.execute(
                "SELECT * FROM au_users ORDER BY is_admin DESC, updated_at DESC LIMIT 200"
            ).fetchall()
            chat_rows = con.execute(
                "SELECT * FROM au_chat_messages ORDER BY created_at DESC LIMIT 120"
            ).fetchall()
            audit_rows = con.execute(
                "SELECT * FROM au_admin_audit_log ORDER BY created_at DESC LIMIT 80"
            ).fetchall()

        users = [self._serialize_user_dict(u) for u in user_rows]
        recent_chat = []
        for r in chat_rows:
            d = dict(r)
            d["flagged"] = bool(int(d.get("flagged") or 0))
            d["deleted"] = bool(int(d.get("deleted") or 0))
            recent_chat.append(d)

        audit_log = []
        for r in audit_rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
            except Exception:
                d["payload"] = {}
            audit_log.append(d)

        return {
            "ok": True,
            "users": users,
            "recent_chat": recent_chat,
            "world_layout": self.get_world_layout(),
            "world_time": self.get_world_clock(),
            "audit_log": audit_log,
            "test_budget": compute_10x_catalog_budget(),
        }

    def admin_delete_chat(self, admin_email: str, msg_id: str) -> Dict[str, Any]:
        mid = (msg_id or "").strip()
        if not mid:
            raise ValueError("msg_id is required.")
        now = time.time()
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT msg_id, channel, username FROM au_chat_messages WHERE msg_id = ?", (mid,)).fetchone()
            if not row:
                raise ValueError("Chat message not found.")
            con.execute(
                "UPDATE au_chat_messages SET deleted = 1, deleted_by = ? WHERE msg_id = ?",
                (admin_email.strip().lower(), mid),
            )
            self._log_admin_action(con, admin_email, "chat_delete", mid, {"channel": row["channel"], "username": row["username"]}, now)
        return {"ok": True, "msg_id": mid, "channel": row["channel"]}

    def admin_clear_chat(self, admin_email: str, channel: str) -> Dict[str, Any]:
        ch = (channel or "global").strip().lower()
        if ch not in ("global", "proximity", "home_private", "all"):
            raise ValueError("Channel must be 'global', 'proximity', 'home_private', or 'all'.")
        now = time.time()
        with self._conn(exclusive=True) as con:
            if ch == "all":
                res = con.execute(
                    "UPDATE au_chat_messages SET deleted = 1, deleted_by = ? WHERE COALESCE(deleted, 0) = 0",
                    (admin_email.strip().lower(),),
                )
            else:
                res = con.execute(
                    "UPDATE au_chat_messages SET deleted = 1, deleted_by = ? WHERE channel = ? AND COALESCE(deleted, 0) = 0",
                    (admin_email.strip().lower(), ch),
                )
            cleared = int(res.rowcount)
            self._log_admin_action(con, admin_email, "chat_clear", ch, {"cleared_count": cleared}, now)
        return {"ok": True, "channel": ch, "cleared_count": cleared}

    def admin_mute_user(self, admin_email: str, user_id: str, minutes: float) -> Dict[str, Any]:
        uid = (user_id or "").strip()
        mins = max(0.0, min(525600.0, float(minutes)))
        now = time.time()
        muted_until = (now + mins * 60.0) if mins > 0 else 0.0
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT user_id, email FROM au_users WHERE user_id = ?", (uid,)).fetchone()
            if not row:
                raise ValueError("Target user not found.")
            con.execute(
                "UPDATE au_users SET muted_until = ?, updated_at = ? WHERE user_id = ?",
                (muted_until, now, uid),
            )
            self._log_admin_action(
                con,
                admin_email,
                "user_unmute" if mins <= 0 else "user_mute",
                uid,
                {"minutes": mins, "muted_until": muted_until},
                now,
            )
        return {"ok": True, "user": self.get_user_bundle(uid)}

    def admin_rename_user(
        self,
        admin_email: str,
        user_id: str,
        username: str,
        display_name: str = "",
    ) -> Dict[str, Any]:
        uid = (user_id or "").strip()
        uname = re.sub(r"[^a-zA-Z0-9_]", "", (username or "").strip())
        if not (3 <= len(uname) <= 20):
            raise ValueError("Account username must be 3-20 alphanumeric/underscore characters.")
        u_check = filter_pg13_text(uname)
        if u_check["flagged"] or u_check["blocked"]:
            raise ValueError("Username violates Anime Universe PG-13 policy.")
        dn_check = filter_pg13_text(display_name or uname)
        if dn_check["blocked"]:
            raise ValueError("Display name blocked by PG-13 safety policy.")
        safe_display = str(dn_check["clean_text"] or uname)[:32]
        now = time.time()
        with self._conn(exclusive=True) as con:
            target = con.execute("SELECT user_id, username FROM au_users WHERE user_id = ?", (uid,)).fetchone()
            if not target:
                raise ValueError("Target user not found.")
            taken = con.execute(
                "SELECT user_id FROM au_users WHERE lower(username) = lower(?) AND user_id != ?",
                (uname, uid),
            ).fetchone()
            if taken:
                raise ValueError("That username is already taken.")
            con.execute(
                "UPDATE au_users SET username = ?, display_name = ?, profile_completed = 1, updated_at = ? WHERE user_id = ?",
                (uname, safe_display, now, uid),
            )
            self._log_admin_action(
                con,
                admin_email,
                "user_rename",
                uid,
                {"old_username": target.get("username"), "username": uname, "display_name": safe_display},
                now,
            )
        return {"ok": True, "user": self.get_user_bundle(uid)}

    def admin_ban_user(
        self,
        admin_email: str,
        user_id: str,
        banned: bool,
        reason: str = "",
    ) -> Dict[str, Any]:
        uid = (user_id or "").strip()
        clean_reason = str(filter_pg13_text(reason or "", allow_empty=True)["clean_text"]).strip()[:180]
        now = time.time()
        with self._conn(exclusive=True) as con:
            target = con.execute("SELECT user_id, email FROM au_users WHERE user_id = ?", (uid,)).fetchone()
            if not target:
                raise ValueError("Target user not found.")
            if banned and is_admin_email(target.get("email")):
                raise ValueError("Cannot ban the primary administrator account.")
            con.execute(
                "UPDATE au_users SET banned = ?, ban_reason = ?, updated_at = ? WHERE user_id = ?",
                (1 if banned else 0, clean_reason if banned else "", now, uid),
            )
            self._log_admin_action(
                con,
                admin_email,
                "user_ban" if banned else "user_unban",
                uid,
                {"banned": bool(banned), "reason": clean_reason},
                now,
            )
        return {"ok": True, "user": self.get_user_bundle(uid)}

    def admin_adjust_currency(
        self,
        admin_email: str,
        user_id: str,
        mode: str,
        zencoins: int = 0,
        au_coins: int = 0,
        reason: str = "",
    ) -> Dict[str, Any]:
        uid = (user_id or "").strip()
        m = (mode or "add").strip().lower()
        if m not in ("add", "set"):
            raise ValueError("Mode must be 'add' or 'set'.")
        zc_val = int(zencoins)
        au_val = int(au_coins)
        clean_reason = str(filter_pg13_text(reason or "admin_adjustment", allow_empty=True)["clean_text"]).strip()[:80] or "admin_adjustment"
        now = time.time()
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT zencoins, au_coins FROM au_users WHERE user_id = ?", (uid,)).fetchone()
            if not row:
                raise ValueError("Target user not found.")
            cur_zc = int(row["zencoins"] or 0)
            cur_au = int(row["au_coins"] or 0)
            if m == "set":
                new_zc = max(0, zc_val)
                new_au = max(0, au_val)
            else:
                new_zc = max(0, cur_zc + zc_val)
                new_au = max(0, cur_au + au_val)
            delta_zc = new_zc - cur_zc
            delta_au = new_au - cur_au
            con.execute(
                "UPDATE au_users SET zencoins = ?, au_coins = ?, updated_at = ? WHERE user_id = ?",
                (new_zc, new_au, now, uid),
            )
            if delta_zc != 0:
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, meta_json, created_at) VALUES (?, ?, 'zencoins', ?, ?, ?, ?)",
                    ("tx_" + secrets.token_hex(6), uid, delta_zc, f"admin_{m}:{clean_reason}", json.dumps({"admin": admin_email}), now),
                )
            if delta_au != 0:
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, meta_json, created_at) VALUES (?, ?, 'au_coins', ?, ?, ?, ?)",
                    ("tx_" + secrets.token_hex(6), uid, delta_au, f"admin_{m}:{clean_reason}", json.dumps({"admin": admin_email}), now),
                )
            self._log_admin_action(
                con,
                admin_email,
                "user_currency",
                uid,
                {"mode": m, "delta_zc": delta_zc, "delta_au": delta_au, "zencoins": new_zc, "au_coins": new_au, "reason": clean_reason},
                now,
            )
        return {"ok": True, "user": self.get_user_bundle(uid)}

    def admin_topup_test_wallet(self, admin_email: str, target_user_id: str) -> Dict[str, Any]:
        uid = (target_user_id or "").strip()
        budget = compute_10x_catalog_budget()
        now = time.time()
        with self._conn(exclusive=True) as con:
            row = con.execute("SELECT zencoins, au_coins FROM au_users WHERE user_id = ?", (uid,)).fetchone()
            if not row:
                raise ValueError("Target user not found.")
            cur_zc = int(row["zencoins"] or 0)
            cur_au = int(row["au_coins"] or 0)
            new_zc = max(cur_zc, budget["zencoins"])
            new_au = max(cur_au, budget["au_coins"])
            delta_zc = new_zc - cur_zc
            delta_au = new_au - cur_au
            con.execute(
                "UPDATE au_users SET zencoins = ?, au_coins = ?, updated_at = ? WHERE user_id = ?",
                (new_zc, new_au, now, uid),
            )
            if delta_zc > 0:
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'zencoins', ?, 'admin_10x_test_topup', ?)",
                    ("tx_" + secrets.token_hex(6), uid, delta_zc, now),
                )
            if delta_au > 0:
                con.execute(
                    "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, created_at) VALUES (?, ?, 'au_coins', ?, 'admin_10x_test_topup', ?)",
                    ("tx_" + secrets.token_hex(6), uid, delta_au, now),
                )
            self._log_admin_action(
                con,
                admin_email,
                "topup_test_wallet",
                uid,
                {"budget": budget, "delta_zc": delta_zc, "delta_au": delta_au},
                now,
            )
        return {"ok": True, "budget": budget, "user": self.get_user_bundle(uid)}

    # ---------- Tripo Image-to-3D User Avatar Generation ----------
    def _get_tripo_key(self) -> str:
        key = os.getenv("TRIPO_API_KEY", "").strip()
        if not key and (WORKSPACE_ROOT / ".env").exists():
            for line in (WORKSPACE_ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("TRIPO_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        return key

    def upload_custom_avatar_file(self, user_id: str, file_bytes: bytes, filename: str) -> Dict[str, Any]:
        """Validate uploaded .glb (<=16 MB) or PNG/JPG (Tripo Image-to-3D, <=8 MB) and charge 35 AU Coins upon success."""
        fee_au = CUSTOM_AVATAR_UPLOAD_FEE_AU
        with self._conn() as con:
            u = con.execute("SELECT au_coins FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not u:
                raise ValueError("User not found.")
            if int(u["au_coins"]) < fee_au:
                raise ValueError(f"Uploading a custom character requires {fee_au} AU Coins.")

        if file_bytes.startswith(b"glTF"):
            if not (MIN_AVATAR_BYTES <= len(file_bytes) <= MAX_AVATAR_GLB_BYTES):
                raise ValueError("Custom .glb character must be between 32 bytes and 16 MB.")
            if len(file_bytes) < 20:
                raise ValueError("Uploaded .glb header is truncated.")
            version, declared_len = struct.unpack("<II", file_bytes[4:12])
            if version != 2 or declared_len != len(file_bytes):
                raise ValueError("Invalid or corrupt glTF 2.0 binary (.glb) file.")
            glb_bytes = file_bytes
            task_id = "direct_glb"
        else:
            if not (MIN_AVATAR_BYTES <= len(file_bytes) <= MAX_AVATAR_BYTES):
                raise ValueError("Avatar file must be between 32 bytes and 8 MB for PNG/JPG (16 MB for .glb).")
            if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                ext, mime = "png", "image/png"
            elif file_bytes.startswith(b"\xff\xd8\xff"):
                ext, mime = "jpg", "image/jpeg"
            else:
                raise ValueError("Upload a valid 3D .glb model or full-body PNG/JPG character image.")
            glb_bytes, task_id = self._generate_glb_via_tripo(file_bytes, ext, mime, filename)

        try:
            PUBLIC_MODELS_DIR.mkdir(parents=True, exist_ok=True)
            (PUBLIC_MODELS_DIR / "avatars").mkdir(parents=True, exist_ok=True)
            out_path = PUBLIC_MODELS_DIR / "avatars" / f"{user_id}.glb"
            out_path.write_bytes(glb_bytes)
            if (WORKSPACE_ROOT / "frontend" / "dist").exists():
                dist_av = DIST_MODELS_DIR / "avatars"
                dist_av.mkdir(parents=True, exist_ok=True)
                shutil.copy2(out_path, dist_av / f"{user_id}.glb")
        except Exception:
            pass

        now = time.time()
        rel_url = f"/api/au-city/avatar/file/{user_id}.glb?v={int(now)}"
        with self._conn(exclusive=True) as con:
            cur_u = con.execute("SELECT au_coins, avatar_appearance_json FROM au_users WHERE user_id = ?", (user_id,)).fetchone()
            if not cur_u or int(cur_u["au_coins"]) < fee_au:
                raise ValueError(f"Uploading a custom character requires {fee_au} AU Coins.")
            res = con.execute(
                "UPDATE au_users SET au_coins = au_coins - ?, avatar_glb_url = ?, updated_at = ? WHERE user_id = ? AND au_coins >= ?",
                (fee_au, rel_url, now, user_id, fee_au),
            )
            if res.rowcount == 0:
                raise ValueError(f"Uploading a custom character requires {fee_au} AU Coins.")
            con.execute(
                "INSERT INTO au_ledger (tx_id, user_id, currency, delta, reason, meta_json, created_at) VALUES (?, ?, 'au_coins', ?, 'custom_avatar_upload', ?, ?)",
                ("tx_" + secrets.token_hex(6), user_id, -fee_au, json.dumps({"task_id": task_id, "bytes": len(glb_bytes)}), now),
            )
            try:
                raw_app = json.loads(cur_u.get("avatar_appearance_json") or "{}")
            except Exception:
                raw_app = {}
            clean_app = sanitize_avatar_appearance(None, base=raw_app if isinstance(raw_app, dict) else None)
            clean_app["identity_initialized"] = True
            con.execute(
                "UPDATE au_users SET avatar_appearance_json = ? WHERE user_id = ?",
                (json.dumps(clean_app), user_id),
            )
            con.upsert(
                "au_avatar_blobs",
                {
                    "user_id": user_id,
                    "filename": f"{user_id}.glb",
                    "content_type": "model/gltf-binary",
                    "data": self._db.adapt_blob(glb_bytes),
                    "updated_at": now,
                },
                ("user_id",),
            )
        return {
            "ok": True,
            "cost_au": fee_au,
            "task_id": task_id,
            "avatar_glb_url": rel_url,
            "user": self.get_user_bundle(user_id),
        }

    def generate_user_avatar_from_image(self, user_id: str, image_bytes: bytes, filename: str) -> Dict[str, Any]:
        """Backward-compatible alias delegating image/GLB uploads to upload_custom_avatar_file."""
        return self.upload_custom_avatar_file(user_id, image_bytes, filename)

    def _generate_glb_via_tripo(self, image_bytes: bytes, ext: str, mime: str, filename: str) -> Tuple[bytes, str]:
        """Upload PNG/JPG to Tripo OpenAPI v2, run auto-rig, and return (glb_bytes, task_id)."""
        token = self._get_tripo_key()
        if not token:
            raise RuntimeError("TRIPO_API_KEY is not configured on the server (you can still upload a .glb file directly).")

        boundary = f"----AUBoundary{secrets.token_hex(8)}"
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(filename or f"avatar.{ext}").name) or f"avatar.{ext}"
        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode())
        body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
        body.extend(image_bytes)
        body.extend(f"\r\n--{boundary}--\r\n".encode())

        up_req = urllib.request.Request(
            f"{TRIPO_BASE_URL}/upload",
            data=bytes(body),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": TRIPO_USER_AGENT,
            },
        )
        with urllib.request.urlopen(up_req, timeout=30) as resp:
            up_data = json.loads(resp.read().decode("utf-8"))
        if up_data.get("code") != 0:
            raise RuntimeError(f"Tripo upload failed: {up_data.get('message')}")
        image_token = up_data["data"]["image_token"]

        task_payload = {
            "type": "image_to_model",
            "model_version": "v2.5-20250123",
            "file": {"type": ext, "file_token": image_token},
            "texture": True,
            "pbr": True,
            "texture_quality": "detailed",
            "face_limit": 18000,
            "auto_size": True,
        }
        task_req = urllib.request.Request(
            f"{TRIPO_BASE_URL}/task",
            data=json.dumps(task_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": TRIPO_USER_AGENT,
            },
        )
        with urllib.request.urlopen(task_req, timeout=30) as resp:
            t_data = json.loads(resp.read().decode("utf-8"))
        if t_data.get("code") != 0:
            raise RuntimeError(f"Tripo avatar task failed: {t_data.get('message')}")
        task_id = t_data["data"]["task_id"]

        model_url = None
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(2.5)
            p_req = urllib.request.Request(
                f"{TRIPO_BASE_URL}/task/{task_id}",
                headers={"Authorization": f"Bearer {token}", "User-Agent": TRIPO_USER_AGENT},
            )
            with urllib.request.urlopen(p_req, timeout=15) as resp:
                p_data = json.loads(resp.read().decode("utf-8"))
            if p_data.get("code") == 0:
                st = p_data["data"].get("status")
                if st == "success":
                    out = p_data["data"].get("output", {})
                    model_url = out.get("pbr_model") or out.get("model") or out.get("base_model")
                    break
                if st in ("failed", "cancelled", "unknown"):
                    raise RuntimeError(f"Tripo avatar status: {st}")

        if not model_url:
            raise RuntimeError("Tripo avatar generation timed out.")

        # Auto-rig newly generated humanoid avatars via Tripo animate_prerigcheck + animate_rig
        # so uploaded user avatars also get the full 43-bone skeleton for heroic anime running.
        try:
            pre_req = urllib.request.Request(
                f"{TRIPO_BASE_URL}/task",
                data=json.dumps({"type": "animate_prerigcheck", "original_model_task_id": task_id}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": TRIPO_USER_AGENT,
                },
            )
            with urllib.request.urlopen(pre_req, timeout=20) as pre_resp:
                pre_data = json.loads(pre_resp.read().decode("utf-8"))
            if pre_data.get("code") == 0 and pre_data.get("data", {}).get("task_id"):
                pre_tid = pre_data["data"]["task_id"]
                rig_ok = False
                for _ in range(18):
                    time.sleep(2.0)
                    chk = urllib.request.Request(
                        f"{TRIPO_BASE_URL}/task/{pre_tid}",
                        headers={"Authorization": f"Bearer {token}", "User-Agent": TRIPO_USER_AGENT},
                    )
                    with urllib.request.urlopen(chk, timeout=15) as chk_r:
                        cd = json.loads(chk_r.read().decode("utf-8"))
                    st = cd.get("data", {}).get("status")
                    if st == "success":
                        rig_ok = bool(cd.get("data", {}).get("output", {}).get("riggable", True))
                        break
                    if st in ("failed", "cancelled", "unknown"):
                        break
                if rig_ok:
                    rig_req = urllib.request.Request(
                        f"{TRIPO_BASE_URL}/task",
                        data=json.dumps({
                            "type": "animate_rig",
                            "original_model_task_id": task_id,
                            "out_format": "glb",
                            "spec": "tripo",
                        }).encode("utf-8"),
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "User-Agent": TRIPO_USER_AGENT,
                        },
                    )
                    with urllib.request.urlopen(rig_req, timeout=20) as rig_resp:
                        rig_d = json.loads(rig_resp.read().decode("utf-8"))
                    rig_tid = rig_d.get("data", {}).get("task_id") if rig_d.get("code") == 0 else None
                    if rig_tid:
                        for _ in range(45):
                            time.sleep(2.5)
                            r_chk = urllib.request.Request(
                                f"{TRIPO_BASE_URL}/task/{rig_tid}",
                                headers={"Authorization": f"Bearer {token}", "User-Agent": TRIPO_USER_AGENT},
                            )
                            with urllib.request.urlopen(r_chk, timeout=15) as r_r:
                                rd = json.loads(r_r.read().decode("utf-8"))
                            rst = rd.get("data", {}).get("status")
                            if rst == "success":
                                rout = rd.get("data", {}).get("output", {})
                                rigged_url = rout.get("model") or rout.get("pbr_model") or rout.get("base_model")
                                if rigged_url:
                                    model_url = rigged_url
                                break
                            if rst in ("failed", "cancelled", "unknown"):
                                break
        except Exception:
            pass

        glb_bytes = urllib.request.urlopen(
            urllib.request.Request(model_url, headers={"User-Agent": TRIPO_USER_AGENT}),
            timeout=45,
        ).read()
        if not glb_bytes.startswith(b"glTF"):
            raise RuntimeError("Downloaded Tripo model is not a valid binary glTF (.glb).")
        return glb_bytes, task_id

    def get_tripo_manifest(self) -> Dict[str, Any]:
        if TRIPO_MANIFEST_PATH.is_file():
            try:
                return json.loads(TRIPO_MANIFEST_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"assets": {}, "count": 0}
