# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    ApplicationHandlerStop,
    filters,
)

# === ВСТАВЛЕННЫЙ ТОКЕН БОТА ===
BOT_TOKEN = "7427775003:AAHIHeZiiHJXoGXLdFjS3qCTbbaeLyzn1FU"

# Сайт для удалённого управления ПК по запросу пользователя
REMOTE_CONTROL_URL = "https://app.getscreen.me"
REMOTE_CONTROL_MESSAGE = (
    "Для удалённого доступа используйте сервис Getscreen.me, который даёт доступ по"
    " ссылке без авторизации.\n"
    "1. Откройте ссылку: {url}\n"
    "2. Скачайте и запустите мини-программу «Поделиться экраном».\n"
    "3. Сервис выдаст готовую ссылку — передайте её тому, кто будет подключаться.\n"
    "4. Перейдя по ссылке, пользователь сразу увидит экран и сможет управлять им,"
    " авторизация не требуется."
)

# === ЛИМИТЫ ===
# Пользовательский лимит: 100 МБ (проверяем заранее и уведомляем)
USER_DOWNLOAD_LIMIT = 100 * 1024 * 1024
# Техническое ограничение Telegram для getFile: около 20 МБ — это серверное ограничение Telegram.
# Мы предупреждаем об этом отдельно при попытке скачать такой файл.
TG_GETFILE_HARD_LIMIT = 19 * 1024 * 1024
# Лимит на отправку ботом документов: ~50 МБ (оценка)
TG_UPLOAD_LIMIT = 49 * 1024 * 1024

# === КУЛДАУН (CD) — 1 сообщение/сек на пользователя ===
COOLDOWN_SECONDS = 1.0
_LAST_MSG_TS: Dict[int, float] = {}

# === КУЛДАУН НА ИСХОДЯЩИЕ СООБЩЕНИЯ ===
BOT_SEND_COOLDOWN_SECONDS = 1.0
_BOT_LAST_SEND_TS: float = 0.0
_BOT_SEND_LOCK = asyncio.Lock()

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
)
logger = logging.getLogger("file-merger-bot")

ENCODINGS_TRY = ["utf-8", "utf-8-sig", "cp1251", "windows-1251", "latin-1"]


def read_text_best_effort_bytes(data: bytes) -> Tuple[str, str]:
    last_err: Optional[Exception] = None
    for enc in ENCODINGS_TRY:
        try:
            return data.decode(enc, errors="strict"), enc
        except Exception as e:
            last_err = e
            continue
    return data.decode("utf-8", errors="replace"), f"fallback(replace): {last_err}"


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding, errors="ignore")


def sanitize_basename(name: str, default: str = "merged") -> str:
    name = name.strip() if name else default
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or default


def merge_contents(text1: str, text2: str, src1: str, src2: str) -> str:
    header = (
        "# -*- coding: utf-8 -*-\n"
        "# === СКЛЕЕННЫЙ ФАЙЛ ===\n"
        f"# Источники:\n# 1) {src1}\n# 2) {src2}\n\n"
    )
    body1 = f"# ---- НАЧАЛО ФАЙЛА 1 ----\n\n{text1}\n\n# ---- КОНЕЦ ФАЙЛА 1 ----\n\n"
    body2 = f"# ---- НАЧАЛО ФАЙЛА 2 ----\n\n{text2}\n\n# ---- КОНЕЦ ФАЙЛА 2 ----\n"
    return header + body1 + body2


def _pyinstaller_allowed_icon_suffixes() -> Tuple[str, ...]:
    if sys.platform.startswith("win"):
        return (".ico",)
    if sys.platform == "darwin":
        return (".icns",)
    return (".ico", ".icns")


def run_pyinstaller(
    merged_py: Path, out_dir: Path, base: str, windowed: bool, icon_path: Optional[Path]
) -> Tuple[Optional[Path], str]:
    """Сборка exe через PyInstaller. Возвращает путь к exe и лог."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dist_dir = out_dir / "dist"
    build_dir = out_dir / "build"
    spec_dir = out_dir / "spec"
    dist_dir.mkdir(exist_ok=True)
    build_dir.mkdir(exist_ok=True)
    spec_dir.mkdir(exist_ok=True)

    py = sys.executable or "python"
    cmd = [py, "-m", "PyInstaller", "--onefile", "--clean", "--noconfirm", "--name", base]
    if windowed:
        cmd.append("--noconsole" if sys.platform.startswith("win") else "--windowed")

    allowed_suffixes = _pyinstaller_allowed_icon_suffixes()
    icon_warning: Optional[str] = None
    if icon_path and icon_path.exists():
        if icon_path.suffix.lower() in allowed_suffixes:
            cmd.extend(["--icon", str(icon_path)])
        else:
            allowed_fmt = ", ".join(allowed_suffixes)
            icon_warning = (
                f"Иконка {icon_path.name} имеет неподдерживаемое расширение {icon_path.suffix}."
                f" PyInstaller использует только: {allowed_fmt}. Иконка пропущена."
            )
            logger.warning(icon_warning)
    cmd.extend(
        [
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(build_dir),
            "--specpath",
            str(spec_dir),
            str(merged_py),
        ]
    )

    log_lines: List[str] = ["Команда:", " ".join(cmd), "\n"]
    if icon_warning:
        log_lines.append(icon_warning)
    try:
        with subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        ) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                log_lines.append(line.rstrip())
        log_lines.append("PyInstaller завершён.")
    except FileNotFoundError:
        log_lines.append("PyInstaller не найден. Установите: pip install pyinstaller")
        return None, "\n".join(log_lines)
    except Exception as e:
        log_lines.append(f"Ошибка PyInstaller: {e}")
        return None, "\n".join(log_lines)

    exe_name = base + (".exe" if sys.platform.startswith("win") else "")
    exe_path = (out_dir / "dist" / exe_name)
    if exe_path.exists():
        return exe_path, "\n".join(log_lines)
    candidates = sorted((out_dir / "dist").glob(base + "*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return (candidates[0] if candidates else None), "\n".join(log_lines)


@dataclass
class PendingMerge:
    base_name: str = "merged"
    windowed: bool = True
    files: List[Tuple[str, bytes]] = field(default_factory=list)
    icon: Optional[Tuple[str, bytes]] = None  # .ico/.icns/.png
    awaiting_icon: bool = False               # режим «ждем иконку»

    def add_file(self, filename: str, data: bytes) -> None:
        if self.awaiting_icon:
            self.icon = (filename, data)
            self.awaiting_icon = False
            return
        if filename.lower().endswith((".ico", ".icns", ".png")) and self.icon is None:
            self.icon = (filename, data)
        else:
            self.files.append((filename, data))
            if len(self.files) > 2:
                # Храним только два последних файла для склейки
                self.files = self.files[-2:]

    def ready(self) -> bool:
        return len(self.files) >= 2


# Состояния пользователей: user_id -> PendingMerge
STATES: Dict[int, PendingMerge] = {}


# === ВСПОМОГАТЕЛЬНОЕ: клавиатуры ===
def build_menu_kb(state: PendingMerge) -> InlineKeyboardMarkup:
    """Формирует inline-клавиатуру со всеми действиями."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📂 Загрузить файлы", callback_data="files_prompt"),
                InlineKeyboardButton("🚀 Собрать", callback_data="merge_now"),
            ],
            [
                InlineKeyboardButton("🖼 Сменить иконку", callback_data="icon_change"),
                InlineKeyboardButton("🧹 Удалить иконку", callback_data="icon_clear"),
            ],
            [
                InlineKeyboardButton("🖥 Удалённое управление", url=REMOTE_CONTROL_URL),
                InlineKeyboardButton("ℹ️ Статус", callback_data="state"),
            ],
            [
                InlineKeyboardButton("🔁 Сброс", callback_data="reset"),
            ],
        ]
    )


def state_summary(state: PendingMerge) -> str:
    return (
        f"Имя результата: {state.base_name}\n"
        f"Иконка: {'есть' if state.icon else 'нет'}\n"
        f"Оконный режим (windowed): {'включён' if state.windowed else 'выключен'}\n"
        f"Файлов для склейки: {len(state.files)} / 2"
    )


# === ВСПОМОГАТЕЛЬНОЕ: отправка с учётом кулдауна ===
async def _send_with_bot_cooldown(factory: Callable[[], Awaitable[Any]]) -> Any:
    global _BOT_LAST_SEND_TS
    async with _BOT_SEND_LOCK:
        now = time.time()
        delay = BOT_SEND_COOLDOWN_SECONDS - (now - _BOT_LAST_SEND_TS)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await factory()
        finally:
            _BOT_LAST_SEND_TS = time.time()


async def reply_text_cd(message: Optional[Message], text: str, **kwargs: Any) -> Any:
    if not message:
        return None
    return await _send_with_bot_cooldown(lambda: message.reply_text(text, **kwargs))


async def reply_document_cd(message: Optional[Message], **kwargs: Any) -> Any:
    if not message:
        return None
    return await _send_with_bot_cooldown(lambda: message.reply_document(**kwargs))


# === ХЕНДЛЕР КУЛДАУНА ===
async def check_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
    uid = update.effective_user.id
    now = time.time()
    last = _LAST_MSG_TS.get(uid, 0.0)
    if now - last < COOLDOWN_SECONDS:
        # Ничего не отвечаем, просто глушим обработку
        raise ApplicationHandlerStop
    _LAST_MSG_TS[uid] = now


def _parse_options(text: str) -> Dict[str, str]:
    parts = text.split()
    opts: Dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            opts[k.strip().lower()] = v.strip()
    return opts


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text_cd(
        update.message,
        "Команды: /merge [base=myapp] [windowed=1|0], /reset, /remote\n"
        "Используйте кнопки ниже, чтобы управлять процессом.\n"
        "Лимит на один файл — 100 МБ. Telegram дополнительно ограничивает скачивание ботом файлами ≈20 МБ.",
        reply_markup=build_menu_kb(STATES.get(update.effective_user.id, PendingMerge())),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    STATES.pop(uid, None)
    await reply_text_cd(
        update.message,
        "Состояние сброшено.",
        reply_markup=build_menu_kb(PendingMerge()),
    )


async def cmd_remote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text_cd(
        update.message,
        REMOTE_CONTROL_MESSAGE.format(url=REMOTE_CONTROL_URL),
        disable_web_page_preview=True,
    )


async def cmd_merge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    text = update.message.text or "/merge"
    opts = _parse_options(text)
    state = PendingMerge(
        base_name=sanitize_basename(opts.get("base", "merged")),
        windowed=(opts.get("windowed", "1") == "1"),
    )
    STATES[uid] = state
    await reply_text_cd(
        update.message,
        "Готово. Пожалуйста, пришлите два файла (лимит 100 МБ на каждый).\n" + state_summary(state),
        reply_markup=build_menu_kb(state),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return

    q = update.callback_query

    # Ограничение частоты и для callback-запросов
    if update.effective_user:
        uid = update.effective_user.id
        now = time.time()
        last = _LAST_MSG_TS.get(uid, 0.0)
        if now - last < COOLDOWN_SECONDS:
            await q.answer(
                "Вы нажимаете слишком часто. Подождите чуть-чуть и попробуйте снова.",
                show_alert=True,
            )
            return
        _LAST_MSG_TS[uid] = now

    await q.answer()
    uid = update.effective_user.id
    state = STATES.get(uid) or PendingMerge()
    STATES[uid] = state

    data = q.data or ""
    if data == "files_prompt":
        await q.edit_message_text(
            "Пожалуйста, отправьте два файла для склейки (форматы .py, .txt и т.п.).",
            reply_markup=build_menu_kb(state),
        )
    elif data == "merge_now":
        if state.ready():
            await q.edit_message_text("Запускаю сборку…", reply_markup=build_menu_kb(state))
            await _perform_merge_from_callback(update, context, state)
            STATES.pop(uid, None)
        else:
            await q.edit_message_text(
                "Недостаточно файлов для сборки. Нужно минимум два файла.",
                reply_markup=build_menu_kb(state),
            )
    elif data == "icon_change":
        state.awaiting_icon = True
        await q.edit_message_text(
            "Режим смены иконки включён. Пожалуйста, пришлите файл иконки (.ico/.icns/.png).",
            reply_markup=build_menu_kb(state),
        )
    elif data == "icon_clear":
        state.icon = None
        state.awaiting_icon = False
        await q.edit_message_text("Иконка удалена.", reply_markup=build_menu_kb(state))
    elif data == "state":
        await q.edit_message_text(state_summary(state), reply_markup=build_menu_kb(state))
    elif data == "reset":
        STATES.pop(uid, None)
        await q.edit_message_text(
            "Состояние сброшено. Пожалуйста, начните заново: /merge",
            reply_markup=build_menu_kb(PendingMerge()),
        )
    else:
        await q.edit_message_text("Неизвестное действие.", reply_markup=build_menu_kb(state))


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return

    uid = update.effective_user.id
    state = STATES.get(uid)
    if not state:
        state = PendingMerge()
        STATES[uid] = state

    doc = update.message.document
    fname = doc.file_name or "file"

    # 1) Проверка пользовательского лимита (100 МБ)
    if doc.file_size and doc.file_size > USER_DOWNLOAD_LIMIT:
        mb = doc.file_size / (1024 * 1024)
        await reply_text_cd(
            update.message,
            f"Вы отправили слишком большой файл ({mb:.1f} МБ). У вас лимит 100 МБ на файл.",
        )
        return

    # 2) Уведомление о жёстком лимите Telegram (около 20 МБ для getFile)
    if doc.file_size and doc.file_size > TG_GETFILE_HARD_LIMIT:
        await reply_text_cd(
            update.message,
            "Файл больше ≈20 МБ. Telegram не позволяет ботам скачивать такие файлы через API. "
            "Даже при лимите 100 МБ это ограничение нельзя обойти.",
        )
        return

    await reply_text_cd(update.message, f"Принимаю «{fname}»…")
    try:
        file = await doc.get_file()
        bio = BytesIO()
        await file.download_to_memory(out=bio)
        data = bio.getvalue()
    except BadRequest as e:
        logger.warning("BadRequest on get_file: %s", e)
        await reply_text_cd(
            update.message,
            "Не удалось скачать файл. Пожалуйста, попробуйте меньший файл.",
        )
        return
    except Exception as e:
        logger.exception("Ошибка скачивания файла")
        await reply_text_cd(
            update.message,
            f"Не удалось скачать «{fname}»: {e}",
        )
        return

    if not isinstance(data, (bytes, bytearray)):
        await reply_text_cd(update.message, "Не удалось получить содержимое файла.")
        return

    if len(data) > USER_DOWNLOAD_LIMIT:
        await reply_text_cd(
            update.message,
            "Полученный файл превышает лимит 100 МБ после скачивания. Пожалуйста, отправьте меньший файл.",
        )
        return

    # Сохранение как иконка/обычный файл с учётом режима
    before_files = len(state.files)
    state.add_file(fname, bytes(data))

    if state.awaiting_icon:
        # (на всякий случай, не должно сработать — awaiting_icon сбрасывается в add_file)
        await reply_text_cd(update.message, "Ожидаю иконку.")
        return

    if len(state.files) > before_files:
        await reply_text_cd(
            update.message,
            f"Файл принят: {fname}. Всего файлов: {len(state.files)}.",
        )
    else:
        await reply_text_cd(update.message, f"Иконка обновлена: {fname}.")

    if state.ready():
        await _perform_merge(update, context, state)
        STATES.pop(uid, None)


async def _perform_merge(update: Update, context: ContextTypes.DEFAULT_TYPE, state: PendingMerge) -> None:
    message = update.effective_message
    if not message or not update.effective_user:
        return

    uid = update.effective_user.id
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("out") / str(uid) / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    (n1, b1), (n2, b2) = state.files[0], state.files[1]

    await reply_text_cd(message, "Склеиваю файлы…")
    t1, enc1 = read_text_best_effort_bytes(b1)
    t2, enc2 = read_text_best_effort_bytes(b2)
    merged_text = merge_contents(t1, t2, n1, n2)

    merged_py = out_dir / f"{state.base_name}_merged.py"
    write_text(merged_py, merged_text, encoding="utf-8")

    # Дополнительная копия *.pyinstall (по твоей прошлой схеме)
    pyinstall_path = out_dir / f"{state.base_name}.pyinstall"
    shutil.copyfile(merged_py, pyinstall_path)

    try:
        with merged_py.open("rb") as f:
            await reply_document_cd(
                message,
                document=f,
                filename=merged_py.name,
                caption=f"{merged_py.name} (кодировки: {enc1}, {enc2})",
            )
        with pyinstall_path.open("rb") as f:
            await reply_document_cd(
                message,
                document=f,
                filename=pyinstall_path.name,
                caption="*.pyinstall",
            )
    except Exception as e:
        logger.exception("Ошибка отправки текстовых артефактов")
        await reply_text_cd(message, f"Ошибка отправки промежуточных файлов: {e}")

    await reply_text_cd(message, "Собираю .exe через PyInstaller…")

    # Сборка EXE — всегда включена
    icon_path: Optional[Path] = None
    icon_warning: Optional[str] = None
    if state.icon:
        icon_name, icon_bytes = state.icon
        icon_path = out_dir / icon_name
        icon_path.write_bytes(icon_bytes)
        allowed_suffixes = _pyinstaller_allowed_icon_suffixes()
        if icon_path.suffix.lower() not in allowed_suffixes:
            icon_warning = (
                "Файл иконки отправлен, но PyInstaller поддерживает только форматы "
                f"{', '.join(allowed_suffixes)} на этой платформе. Иконка не будет добавлена."
            )
            try:
                icon_path.unlink()
            except OSError:
                pass
            icon_path = None

    exe_path, log = run_pyinstaller(
        merged_py=merged_py, out_dir=out_dir, base=state.base_name, windowed=state.windowed, icon_path=icon_path
    )
    if icon_warning:
        log = icon_warning + "\n" + log
    log_file = out_dir / "pyinstaller.log"
    log_file.write_text(log, encoding="utf-8")

    if icon_warning:
        await reply_text_cd(message, icon_warning)

    try:
        with log_file.open("rb") as f:
            await reply_document_cd(
                message,
                document=f,
                filename=log_file.name,
                caption="Лог сборки",
            )
    except Exception as e:
        logger.warning("Не удалось отправить лог сборки: %s", e)

    exe_sent = False
    build_failed = False
    if not exe_path or not exe_path.exists():
        await reply_text_cd(message, "Не удалось собрать исполняемый файл. Проверьте лог.")
        build_failed = True
    else:
        try:
            size = exe_path.stat().st_size
            if size <= TG_UPLOAD_LIMIT:
                with exe_path.open("rb") as f:
                    await reply_document_cd(
                        message,
                        document=f,
                        filename=exe_path.name,
                        caption="Готовый .exe",
                    )
                    exe_sent = True
            else:
                await reply_text_cd(message, "EXE крупный, упаковываю в ZIP…")
                zip_path = exe_path.with_suffix(".zip")
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                    zf.write(exe_path, arcname=exe_path.name)
                if zip_path.stat().st_size <= TG_UPLOAD_LIMIT:
                    with zip_path.open("rb") as f:
                        await reply_document_cd(
                            message,
                            document=f,
                            filename=zip_path.name,
                            caption="Готовый .exe (ZIP)",
                        )
                        exe_sent = True
                else:
                    await reply_text_cd(
                        message,
                        "EXE собран, но слишком большой для отправки через Telegram, даже в ZIP. "
                        f"Путь к файлу на сервере: {exe_path}",
                    )
        except Exception as e:
            logger.exception("Ошибка при отправке exe")
            await reply_text_cd(
                message,
                f"EXE собран, но не удалось отправить: {e}\nПуть к файлу: {exe_path}",
            )

    if build_failed:
        summary = "Сборка завершилась с ошибкой. Проверьте лог."
    elif exe_sent:
        summary = "Сборка завершена. Все файлы отправлены."
    else:
        summary = "Сборка завершена. Проверьте отправленные сообщения."

    await reply_text_cd(
        message,
        f"{summary} Используйте кнопки ниже, чтобы продолжить.",
        reply_markup=build_menu_kb(PendingMerge()),
    )


async def _perform_merge_from_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: PendingMerge
) -> None:
    """Отдельный запуск сборки из callback-запроса."""

    await _perform_merge(update, context, state)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    logger.exception("Исключение при обработке апдейта", exc_info=err)
    try:
        if isinstance(update, Update) and update.effective_message:
            msg = "Произошла ошибка."
            if isinstance(err, BadRequest) and "File is too big" in str(err):
                msg = ("Файл слишком большой для скачивания ботом через API Telegram (жёсткий лимит около 20 МБ). "
                       "Пожалуйста, используйте файл меньшего размера.")
            await reply_text_cd(update.effective_message, msg)
    except Exception:
        logger.debug("Не удалось отправить сообщение об ошибке пользователю")


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # Группа 0 — антиспам/кулдаун (останавливает дальнейшие хендлеры при превышении частоты)
    app.add_handler(MessageHandler(filters.ALL, check_cooldown), group=0)

    # Основные хендлеры
    app.add_handler(CommandHandler(["start", "help"], cmd_start), group=1)
    app.add_handler(CommandHandler("reset", cmd_reset), group=1)
    app.add_handler(CommandHandler("merge", cmd_merge), group=1)
    app.add_handler(CommandHandler("remote", cmd_remote), group=1)
    app.add_handler(CallbackQueryHandler(on_callback), group=1)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document), group=1)

    app.add_error_handler(on_error)
    return app


def main() -> None:
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        except Exception:
            pass

    app = build_app()
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
