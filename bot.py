#!/usr/bin/env python3
"""
Telegram Bot - Téléchargeur Instagram & TikTok
Photos / Stories / Highlights Instagram  +  Vidéos & Photos TikTok
"""

import os
import re
import json
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode, ChatAction
import instaloader
import yt_dlp

# ─── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "VOTRE_TOKEN_ICI")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ─── Regex ───────────────────────────────────────────────────────────────────
INSTAGRAM_PROFILE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?(?:\?.*)?$"
)
INSTAGRAM_POST_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)"
)
TIKTOK_PROFILE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)/?(?:\?.*)?$"
)
TIKTOK_VIDEO_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:vm\.)?tiktok\.com/"
    r"(?:@[^/]+/video/|v/|embed/v2/)?(\d{5,})"
)

# Extensions médias
IMG_EXT  = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT  = {".mp4", ".mov", ".mkv", ".webm"}
MEDIA_EXT = IMG_EXT | VID_EXT

# Credentials Instagram par user Telegram (mémoire — OK pour usage solo/famille)
user_ig_creds: dict[int, tuple[str, str]] = {}

# ─── Messages ────────────────────────────────────────────────────────────────
MSG_WELCOME = (
    "🤖 *Bienvenue sur le Bot Téléchargeur !*\n\n"
    "📷 *Instagram* :\n"
    "• Photos & carrousels\n"
    "• Stories actives (24h)\n"
    "• Stories à la Une (Highlights)\n\n"
    "🎵 *TikTok* :\n"
    "• Vidéos (sans watermark)\n"
    "• Photos/slideshows → envoyées en photo\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "*Envoie simplement un lien :*\n"
    "`https://instagram.com/username`\n"
    "`https://tiktok.com/@username`\n"
    "`https://tiktok.com/@user/video/123`\n\n"
    "⚠️ Comptes privés Instagram → `/login username password`"
)

# ─── Helpers ─────────────────────────────────────────────────────────────────
def detect_url(url: str) -> tuple[str, str]:
    if INSTAGRAM_POST_RE.search(url):
        return "instagram", "post"
    if INSTAGRAM_PROFILE_RE.search(url):
        return "instagram", "profile"
    if TIKTOK_VIDEO_RE.search(url):
        return "tiktok", "video"
    if TIKTOK_PROFILE_RE.search(url):
        return "tiktok", "profile"
    return "unknown", "unknown"

def human_size(b: int) -> str:
    for u in ("B","KB","MB","GB"):
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

async def typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    except Exception:
        pass

# ─── Instagram ────────────────────────────────────────────────────────────────
class InstagramDownloader:
    def __init__(self):
        self.L = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            filename_pattern="{date_utc:%Y%m%d_%H%M%S}_{shortcode}",
        )

    def login(self, username: str, password: str) -> bool:
        try:
            self.L.login(username, password)
            logger.info(f"IG login OK: {username}")
            return True
        except Exception as e:
            logger.error(f"IG login failed: {e}")
            return False

    # ── Posts (photos uniquement, pas reels) ──────────────────────────────────
    def download_photos(self, username: str, tmpdir: str) -> list[Path]:
        """Télécharge tous les posts PHOTO du profil (exclut les vidéos/reels)."""
        try:
            profile = instaloader.Profile.from_username(self.L.context, username)
            out = Path(tmpdir) / "photos"
            out.mkdir(exist_ok=True)
            for post in profile.get_posts():
                # is_video = True pour les reels/vidéos → on skip
                if post.is_video:
                    continue
                self.L.download_post(post, target=str(out))
            # Retourne uniquement les images
            return sorted(f for f in out.rglob("*") if f.suffix.lower() in IMG_EXT)
        except Exception as e:
            logger.error(f"IG photos error: {e}")
            return []

    # ── Stories actives ───────────────────────────────────────────────────────
    def download_stories(self, username: str, tmpdir: str) -> list[Path]:
        try:
            profile = instaloader.Profile.from_username(self.L.context, username)
            out = Path(tmpdir) / "stories"
            out.mkdir(exist_ok=True)
            self.L.download_stories(userids=[profile.userid], filename_target=str(out))
            return sorted(f for f in out.rglob("*") if f.suffix.lower() in MEDIA_EXT)
        except Exception as e:
            logger.error(f"IG stories error: {e}")
            return []

    # ── Highlights ────────────────────────────────────────────────────────────
    def download_highlights(self, username: str, tmpdir: str) -> list[Path]:
        try:
            profile = instaloader.Profile.from_username(self.L.context, username)
            out = Path(tmpdir) / "highlights"
            out.mkdir(exist_ok=True)
            for hl in self.L.get_highlights(profile):
                hl_dir = out / re.sub(r'[^\w]', '_', hl.title)
                hl_dir.mkdir(exist_ok=True)
                for item in hl.get_items():
                    self.L.download_storyitem(item, target=str(hl_dir))
            return sorted(f for f in out.rglob("*") if f.suffix.lower() in MEDIA_EXT)
        except Exception as e:
            logger.error(f"IG highlights error: {e}")
            return []

    # ── Stories + Highlights combinés ────────────────────────────────────────
    def download_all_stories(self, username: str, tmpdir: str) -> list[Path]:
        s = self.download_stories(username, tmpdir)
        h = self.download_highlights(username, tmpdir)
        return s + h

# ─── TikTok ───────────────────────────────────────────────────────────────────
class TikTokDownloader:
    """
    Télécharge vidéos ET photos TikTok.
    - Vidéos  → fichiers .mp4
    - Slideshows (photos) → yt-dlp les sort nativement en .jpg quand
      le format 'slides' est disponible ; sinon on extrait les frames
      via le post-processor 'SponsorBlock' + métadonnées JSON.
    """

    def _base_opts(self, outdir: str) -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
            # Priorité : slides (images brutes) > mp4 sans watermark > best
            "format": "slides/bestvideo[vcodec!*=av01]+bestaudio/best",
            "outtmpl": f"{outdir}/%(upload_date)s_%(id)s.%(ext)s",
            # Écrire les métadonnées JSON pour détecter le type après DL
            "writeinfojson": True,
            # Merge audio+vidéo si nécessaire
            "merge_output_format": "mp4",
            # Pas de watermark TikTok
            "extractor_args": {"tiktok": {"webpage_download": ["true"]}},
        }

    def _collect_media(self, outdir: str) -> list[Path]:
        """
        Parcourt le dossier de sortie.
        Pour chaque fichier, vérifie le JSON associé :
        - Si "images" dans le JSON → c'est un slideshow → garde les .jpg/.png
        - Sinon → c'est une vidéo → garde le .mp4
        Évite les doublons et les fichiers système.
        """
        result: list[Path] = []
        seen_ids: set[str] = set()

        for json_file in Path(outdir).glob("*.json"):
            try:
                info = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            vid_id = info.get("id", "")
            if vid_id in seen_ids:
                continue
            seen_ids.add(vid_id)

            is_photo_post = bool(info.get("images"))  # slideshow TikTok

            if is_photo_post:
                # yt-dlp nomme les images <date>_<id>_<n>.jpg
                imgs = sorted(Path(outdir).glob(f"*{vid_id}*.jpg")) + \
                       sorted(Path(outdir).glob(f"*{vid_id}*.jpeg")) + \
                       sorted(Path(outdir).glob(f"*{vid_id}*.png")) + \
                       sorted(Path(outdir).glob(f"*{vid_id}*.webp"))
                result.extend(imgs)
            else:
                vids = sorted(Path(outdir).glob(f"*{vid_id}*.mp4"))
                result.extend(vids)

        # Fallback : si aucun JSON traité, prendre tous les médias
        if not result:
            result = sorted(
                f for f in Path(outdir).glob("*")
                if f.suffix.lower() in MEDIA_EXT
            )
        return result

    def download_video(self, url: str, tmpdir: str) -> list[Path]:
        opts = self._base_opts(tmpdir)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            return self._collect_media(tmpdir)
        except Exception as e:
            logger.error(f"TikTok video DL error: {e}")
            return []

    def download_profile(self, username: str, tmpdir: str, limit: int = 50) -> list[Path]:
        opts = {
            **self._base_opts(tmpdir),
            "playlistend": limit,
        }
        url = f"https://www.tiktok.com/@{username}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            return self._collect_media(tmpdir)
        except Exception as e:
            logger.error(f"TikTok profile DL error: {e}")
            return []

# ─── Instances globales ───────────────────────────────────────────────────────
ig = InstagramDownloader()
tt = TikTokDownloader()

# ─── Envoi de fichiers ────────────────────────────────────────────────────────
async def send_files(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    files: list[Path],
    label: str,
):
    chat_id = update.effective_chat.id
    media = [f for f in files if f.is_file() and f.stat().st_size > 0
             and f.suffix.lower() in MEDIA_EXT]

    if not media:
        await update.effective_message.reply_text(
            f"⚠️ Aucun média trouvé pour *{label}*.\n"
            "Vérifie que le compte est public ou connecte-toi avec `/login`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.effective_message.reply_text(
        f"📦 *{len(media)} fichier(s)* trouvé(s) — envoi en cours…",
        parse_mode=ParseMode.MARKDOWN,
    )

    sent = skipped = 0
    for f in media:
        await typing(context, chat_id)
        size = f.stat().st_size
        if size > 49 * 1024 * 1024:
            await update.effective_message.reply_text(
                f"⚠️ `{f.name}` trop volumineux ({human_size(size)}) — ignoré.",
                parse_mode=ParseMode.MARKDOWN,
            )
            skipped += 1
            continue
        try:
            ext = f.suffix.lower()
            with open(f, "rb") as fh:
                cap = f"`{f.name}`"
                if ext in IMG_EXT:
                    await context.bot.send_photo(
                        chat_id=chat_id, photo=fh,
                        caption=cap, parse_mode=ParseMode.MARKDOWN,
                    )
                elif ext in VID_EXT:
                    await context.bot.send_video(
                        chat_id=chat_id, video=fh,
                        caption=cap, parse_mode=ParseMode.MARKDOWN,
                        supports_streaming=True,
                    )
            sent += 1
        except Exception as e:
            logger.warning(f"send error {f.name}: {e}")
            skipped += 1

    emoji = "✅" if skipped == 0 else "⚠️"
    msg = f"{emoji} *{label}* — {sent} envoyé(s)"
    if skipped:
        msg += f", {skipped} ignoré(s)"
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ─── Menus inline ─────────────────────────────────────────────────────────────
async def show_instagram_menu(update: Update, username: str):
    kb = [
        [InlineKeyboardButton("📸 Photos du profil (tous)", callback_data=f"ig_photos|{username}")],
        [InlineKeyboardButton("📖 Stories actives (24h)", callback_data=f"ig_stories|{username}")],
        [InlineKeyboardButton("⭐ Stories à la Une", callback_data=f"ig_highlights|{username}")],
        [InlineKeyboardButton("🔁 Stories + Highlights", callback_data=f"ig_allstories|{username}")],
    ]
    await update.effective_message.reply_text(
        f"📷 *Profil Instagram :* `@{username}`\n\nQue veux-tu télécharger ?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )

async def show_tiktok_menu(update: Update, username: str):
    kb = [
        [InlineKeyboardButton("🎬 Toutes les vidéos (50 max)", callback_data=f"tt_profile|{username}")],
    ]
    await update.effective_message.reply_text(
        f"🎵 *Profil TikTok :* `@{username}`\n\nQue veux-tu télécharger ?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )

# ─── Handlers commandes ───────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MSG_WELCOME, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MSG_WELCOME, parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ig_st = "✅ Connecté" if uid in user_ig_creds else "❌ Non configuré"
    await update.message.reply_text(
        f"🟢 *Bot actif*\n\nInstagram : {ig_st}\nTikTok : ✅ Prêt\n"
        f"Heure : `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Usage : `/login username password`\n⚠️ Utilise un compte secondaire !",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    username, password = args
    await update.message.reply_text("⏳ Connexion…")
    ok = await asyncio.get_event_loop().run_in_executor(None, ig.login, username, password)
    if ok:
        user_ig_creds[update.effective_user.id] = (username, password)
        await update.message.reply_text("✅ Connecté à Instagram !")
    else:
        await update.message.reply_text("❌ Échec. Vérifie tes identifiants.")

# ─── Handler message (lien) ───────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    platform, url_type = detect_url(text)
    loop = asyncio.get_event_loop()

    if platform == "instagram":
        if url_type == "post":
            tmpdir = tempfile.mkdtemp()
            try:
                await update.message.reply_text("⏳ Téléchargement du post…")
                files = await loop.run_in_executor(None, ig.download_photos, "", tmpdir)
                # Pour un post unique, on utilise download_post direct
                files2 = await loop.run_in_executor(
                    None, lambda: _dl_ig_post(text, tmpdir)
                )
                await send_files(update, context, files2, "Post Instagram")
            finally:
                await asyncio.sleep(3)
                shutil.rmtree(tmpdir, ignore_errors=True)
        elif url_type == "profile":
            m = INSTAGRAM_PROFILE_RE.search(text)
            if m:
                await show_instagram_menu(update, m.group(1))

    elif platform == "tiktok":
        if url_type == "video":
            tmpdir = tempfile.mkdtemp()
            try:
                await update.message.reply_text("⏳ Téléchargement TikTok…")
                files = await loop.run_in_executor(None, tt.download_video, text, tmpdir)
                await send_files(update, context, files, "TikTok")
            finally:
                await asyncio.sleep(3)
                shutil.rmtree(tmpdir, ignore_errors=True)
        elif url_type == "profile":
            m = TIKTOK_PROFILE_RE.search(text)
            if m:
                await show_tiktok_menu(update, m.group(1))

    else:
        await update.message.reply_text(
            "❓ Lien non reconnu.\n\nExemples acceptés :\n"
            "• `https://instagram.com/username`\n"
            "• `https://tiktok.com/@username`\n"
            "• `https://tiktok.com/@user/video/123`",
            parse_mode=ParseMode.MARKDOWN,
        )

def _dl_ig_post(url: str, tmpdir: str) -> list[Path]:
    """Télécharge un post Instagram unique (photo ou carrousel, pas reel)."""
    try:
        m = INSTAGRAM_POST_RE.search(url)
        if not m:
            return []
        post = instaloader.Post.from_shortcode(ig.L.context, m.group(1))
        ig.L.download_post(post, target=tmpdir)
        return sorted(f for f in Path(tmpdir).rglob("*") if f.suffix.lower() in IMG_EXT)
    except Exception as e:
        logger.error(f"IG post error: {e}")
        return []

# ─── Callback handler ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, username = query.data.split("|", 1)
    tmpdir = tempfile.mkdtemp()
    loop = asyncio.get_event_loop()

    try:
        if action == "ig_photos":
            await query.edit_message_text(f"⏳ Téléchargement photos `@{username}`…", parse_mode=ParseMode.MARKDOWN)
            files = await loop.run_in_executor(None, ig.download_photos, username, tmpdir)
            await send_files(update, context, files, f"Photos @{username}")

        elif action == "ig_stories":
            await query.edit_message_text(f"⏳ Stories actives `@{username}`…", parse_mode=ParseMode.MARKDOWN)
            files = await loop.run_in_executor(None, ig.download_stories, username, tmpdir)
            await send_files(update, context, files, f"Stories @{username}")

        elif action == "ig_highlights":
            await query.edit_message_text(f"⏳ Highlights `@{username}`…", parse_mode=ParseMode.MARKDOWN)
            files = await loop.run_in_executor(None, ig.download_highlights, username, tmpdir)
            await send_files(update, context, files, f"Highlights @{username}")

        elif action == "ig_allstories":
            await query.edit_message_text(f"⏳ Stories + Highlights `@{username}`…", parse_mode=ParseMode.MARKDOWN)
            files = await loop.run_in_executor(None, ig.download_all_stories, username, tmpdir)
            await send_files(update, context, files, f"Stories+HL @{username}")

        elif action == "tt_profile":
            await query.edit_message_text(f"⏳ Téléchargement TikTok `@{username}`…", parse_mode=ParseMode.MARKDOWN)
            files = await loop.run_in_executor(None, tt.download_profile, username, tmpdir)
            await send_files(update, context, files, f"TikTok @{username}")

    finally:
        await asyncio.sleep(3)
        shutil.rmtree(tmpdir, ignore_errors=True)

# ─── Error handler ────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Erreur bot:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("❌ Erreur inattendue, réessaie.")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    if BOT_TOKEN == "VOTRE_TOKEN_ICI":
        logger.error("❌ TELEGRAM_BOT_TOKEN non défini !")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    logger.info("🤖 Bot démarré — polling actif 24/7")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
