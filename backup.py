"""Sudo-only project and database backup/restore support."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, SUDO_USER_ID

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", Path(__file__).resolve().parent)).resolve()
BACKUP_TZ = ZoneInfo(os.getenv("BACKUP_TIMEZONE", "Asia/Tehran"))
MAX_RESTORE_BYTES = 250 * 1024 * 1024
_restore_waiting: set[int] = set()


def _run_backup_sync(output: Path):
    with tempfile.TemporaryDirectory(prefix="tisa_backup_") as temp:
        temp_path = Path(temp)
        project_path = temp_path / "project"
        shutil.copytree(
            PROJECT_ROOT,
            project_path,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.zip", ".venv", "venv"),
        )
        dump_path = temp_path / "database.sql"
        env = os.environ.copy()
        env["MYSQL_PWD"] = DB_PASSWORD
        command = [
            "mysqldump", "--single-transaction", "--routines", "--triggers",
            "-h", DB_HOST, "-P", str(DB_PORT), "-u", DB_USER, DB_NAME,
        ]
        with dump_path.open("wb") as dump:
            result = subprocess.run(command, stdout=dump, stderr=subprocess.PIPE, env=env, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"mysqldump failed: {result.stderr.decode(errors='replace')[:1000]}")

        manifest = {
            "format": "tisa-manager-backup",
            "version": 1,
            "created_at": datetime.now(BACKUP_TZ).isoformat(),
            "project_root": str(PROJECT_ROOT),
            "database": DB_NAME,
        }
        (temp_path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for item in (temp_path / "project").rglob("*"):
                if item.is_file():
                    archive.write(item, Path("project") / item.relative_to(project_path))
            archive.write(dump_path, "database.sql")
            archive.write(temp_path / "manifest.json", "manifest.json")


def _safe_extract(zip_path: Path, destination: Path):
    with zipfile.ZipFile(zip_path) as archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > MAX_RESTORE_BYTES:
            raise ValueError("backup is too large")
        names = archive.namelist()
        if "manifest.json" not in names or "database.sql" not in names or not any(n.startswith("project/") for n in names):
            raise ValueError("unsupported backup structure")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != "tisa-manager-backup" or manifest.get("version") != 1:
            raise ValueError("unsupported backup version")
        for name in names:
            target = (destination / name).resolve()
            if not str(target).startswith(str(destination.resolve()) + os.sep):
                raise ValueError("unsafe path in backup")
        archive.extractall(destination)


def _run_restore_sync(zip_path: Path):
    with tempfile.TemporaryDirectory(prefix="tisa_restore_") as temp:
        extracted = Path(temp)
        _safe_extract(zip_path, extracted)
        env = os.environ.copy()
        env["MYSQL_PWD"] = DB_PASSWORD
        command = ["mysql", "-h", DB_HOST, "-P", str(DB_PORT), "-u", DB_USER, DB_NAME]
        with (extracted / "database.sql").open("rb") as dump:
            result = subprocess.run(command, stdin=dump, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"mysql restore failed: {result.stderr.decode(errors='replace')[:1000]}")

        restored_project = extracted / "project"
        for source in restored_project.rglob("*"):
            relative = source.relative_to(restored_project)
            if relative.parts[0] in {".git", ".env"}:
                continue
            target = PROJECT_ROOT / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


async def create_backup() -> Path:
    fd, filename = tempfile.mkstemp(prefix="tisa_backup_", suffix=".zip")
    os.close(fd)
    output = Path(filename)
    await asyncio.to_thread(_run_backup_sync, output)
    return output


async def restore_backup(zip_path: Path):
    await asyncio.to_thread(_run_restore_sync, zip_path)


async def send_backup(bot, chat_id: int):
    path = await create_backup()
    try:
        with path.open("rb") as archive:
            await bot.send_document(chat_id=chat_id, document=archive, caption="🗄️ پشتیبان کامل پروژه و پایگاه‌داده")
    finally:
        path.unlink(missing_ok=True)


async def handle_backup(update, context):
    query = update.callback_query
    if query.from_user.id != SUDO_USER_ID:
        await query.answer("❌ فقط sudo دسترسی دارد.", show_alert=True)
        return
    await query.answer("⏳ در حال ساخت پشتیبان...")
    await query.edit_message_text("⏳ در حال ساخت پشتیبان کامل پروژه و پایگاه‌داده...")
    try:
        await send_backup(context.bot, SUDO_USER_ID)
        await query.message.reply_text("✅ پشتیبان ارسال شد.")
    except Exception as exc:
        logger.exception("Backup failed")
        await query.message.reply_text(f"❌ ساخت پشتیبان ناموفق بود: {exc}")


async def handle_restore(update, context):
    query = update.callback_query
    if query.from_user.id != SUDO_USER_ID:
        await query.answer("❌ فقط sudo دسترسی دارد.", show_alert=True)
        return
    _restore_waiting.add(query.from_user.id)
    await query.answer()
    await query.edit_message_text("♻️ فایل ZIP پشتیبان را ارسال کنید. ساختار آن قبل از بازیابی بررسی می‌شود.\n\nبرای لغو /cancel را بفرستید.")


def cancel_restore(user_id: int):
    _restore_waiting.discard(user_id)


async def handle_restore_document(update, context) -> bool:
    user_id = update.effective_user.id
    if user_id not in _restore_waiting or user_id != SUDO_USER_ID:
        return False
    if not update.message or not update.message.document:
        return False
    document = update.message.document
    if not (document.file_name or "").lower().endswith(".zip"):
        await update.message.reply_text("❌ فقط فایل ZIP پشتیبان پذیرفته می‌شود.")
        return True
    _restore_waiting.discard(user_id)
    fd, filename = tempfile.mkstemp(prefix="tisa_restore_upload_", suffix=".zip")
    os.close(fd)
    temp_path = Path(filename)
    try:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(str(temp_path))
        await update.message.reply_text("⏳ در حال بررسی و بازیابی پروژه و پایگاه‌داده...")
        await restore_backup(temp_path)
        await update.message.reply_text("✅ بازیابی کامل شد. برای اعمال کدهای جدید، ربات باید ری‌استارت شود.")
    except Exception as exc:
        logger.exception("Restore failed")
        await update.message.reply_text(f"❌ بازیابی انجام نشد: {exc}")
    finally:
        temp_path.unlink(missing_ok=True)
    return True


async def nightly_backup(context):
    try:
        await send_backup(context.bot, SUDO_USER_ID)
        logger.info("Nightly backup sent to sudo user %s", SUDO_USER_ID)
    except Exception:
        logger.exception("Nightly backup failed")
