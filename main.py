# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
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
from typing import Dict, List, Optional, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
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
    if icon_path and icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])
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

    def ready(self) -> bool:
        return len(self.files) >= 2


# Состояния пользователей: user_id -> PendingMerge
STATES: Dict[int, PendingMerge] = {}


# === ВСПОМОГАТЕЛЬНОЕ: клавиатуры ===
def build_menu_kb(state: PendingMerge) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🖼 Сменить иконку", callback_data="icon_change"),
                InlineKeyboardButton("🧹 Удалить иконку", callback_data="icon_clear"),
            ],
            [
                InlineKeyboardButton("🪟 Переключить windowed", callback_data="toggle_window"),
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
    await update.message.reply_text(
        "Команда: /merge [base=myapp] [windowed=1|0]\n"
        "Пожалуйста, пришлите два файла для склейки. Можно прислать иконку (.ico/.icns/.png). "
        "На выходе бот соберёт .exe и пришлёт результат.",
        reply_markup=build_menu_kb(STATES.get(update.effective_user.id, PendingMerge())),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    STATES.pop(uid, None)
    await update.message.reply_text("Состояние сброшено.", reply_markup=build_menu_kb(PendingMerge()))


async def cmd_merge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    text = update.message.text or "/merge"
    opts = _parse_options(text)
    state = PendingMerge(
        base_name=sanitize_basename(opts.get("base", "merged")),
        windowed=(opts.get("windowed", "1") == "1"),
    )
    STATES[uid] = state
    await update.message.reply_text(
        "Готово. Пожалуйста, пришлите два файла.\n" + state_summary(state),
        reply_markup=build_menu_kb(state),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    state = STATES.get(uid) or PendingMerge()
    STATES[uid] = state

    data = q.data or ""
    if data == "icon_change":
        state.awaiting_icon = True
        await q.edit_message_text(
            "Режим смены иконки включён. Пожалуйста, пришлите файл иконки (.ico/.icns/.png).",
            reply_markup=build_menu_kb(state),
        )
    elif data == "icon_clear":
        state.icon = None
        state.awaiting_icon = False
        await q.edit_message_text("Иконка удалена.", reply_markup=build_menu_kb(state))
    elif data == "toggle_window":
        state.windowed = not state.windowed
        await q.edit_message_text(
            f"Оконный режим: {'включён' if state.windowed else 'выключен'}.",
            reply_markup=build_menu_kb(state),
        )
    elif data == "state":
        await q.edit_message_text(state_summary(state), reply_markup=build_menu_kb(state))
    elif data == "reset":
        STATES.pop(uid, None)
        await q.edit_message_text("Состояние сброшено. Пожалуйста, начните заново: /merge", reply_markup=build_menu_kb(PendingMerge()))
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
        await update.message.reply_text(
            f"Вы отправили слишком большой файл ({mb:.1f} МБ). У вас лимит 100 МБ на файл."
        )
        return

    # 2) Уведомление о жёстком лимите Telegram (около 20 МБ для getFile)
    if doc.file_size and doc.file_size > TG_GETFILE_HARD_LIMIT:
        await update.message.reply_text(
            "Файл больше ≈20 МБ. Telegram не позволяет ботам скачивать такие файлы через API. "
            "Даже при лимите 100 МБ это ограничение нельзя обойти."
        )
        return

    await update.message.reply_text(f"Принимаю «{fname}»…")
    try:
        file = await doc.get_file()
        bio = BytesIO()
        await file.download_to_memory(out=bio)
        data = bio.getvalue()
    except BadRequest as e:
        logger.warning("BadRequest on get_file: %s", e)
        await update.message.reply_text("Не удалось скачать файл. Пожалуйста, попробуйте меньший файл.")
        return
    except Exception as e:
        logger.exception("Ошибка скачивания файла")
        await update.message.reply_text(f"Не удалось скачать «{fname}»: {e}")
        return

    if not isinstance(data, (bytes, bytearray)):
        await update.message.reply_text("Не удалось получить содержимое файла.")
        return

    # Сохранение как иконка/обычный файл с учётом режима
    before_files = len(state.files)
    state.add_file(fname, bytes(data))

    if state.awaiting_icon:
        # (на всякий случай, не должно сработать — awaiting_icon сбрасывается в add_file)
        await update.message.reply_text("Ожидаю иконку.", reply_markup=build_menu_kb(state))
        return

    if len(state.files) > before_files:
        await update.message.reply_text(
            f"Файл принят: {fname}. Всего файлов: {len(state.files)}.",
            reply_markup=build_menu_kb(state),
        )
    else:
        await update.message.reply_text(
            f"Иконка обновлена: {fname}.", reply_markup=build_menu_kb(state)
        )

    if state.ready():
        await _perform_merge(update, context, state)
        STATES.pop(uid, None)


async def _perform_merge(update: Update, context: ContextTypes.DEFAULT_TYPE, state: PendingMerge) -> None:
    uid = update.effective_user.id
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("out") / str(uid) / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    (n1, b1), (n2, b2) = state.files[0], state.files[1]

    await update.message.reply_text("Склеиваю файлы…")
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
            await update.message.reply_document(
                document=f, filename=merged_py.name, caption=f"*_merged.py (кодировки: {enc1}, {enc2})", parse_mode=ParseMode.MARKDOWN
            )
        with pyinstall_path.open("rb") as f:
            await update.message.reply_document(
                document=f, filename=pyinstall_path.name, caption="*.pyinstall"
            )
    except Exception as e:
        logger.exception("Ошибка отправки текстовых артефактов")
        await update.message.reply_text(f"Ошибка отправки промежуточных файлов: {e}")

    await update.message.reply_text("Собираю .exe через PyInstaller…")

    # Сборка EXE — всегда включена
    icon_path: Optional[Path] = None
    if state.icon:
        icon_name, icon_bytes = state.icon
        icon_path = out_dir / icon_name
        icon_path.write_bytes(icon_bytes)

    exe_path, log = run_pyinstaller(
        merged_py=merged_py, out_dir=out_dir, base=state.base_name, windowed=state.windowed, icon_path=icon_path
    )
    log_file = out_dir / "pyinstaller.log"
    log_file.write_text(log, encoding="utf-8")

    try:
        with log_file.open("rb") as f:
            await update.message.reply_document(document=f, filename=log_file.name, caption="Лог сборки")
    except Exception as e:
        logger.warning("Не удалось отправить лог сборки: %s", e)

    if not exe_path or not exe_path.exists():
        await update.message.reply_text("Не удалось собрать исполняемый файл. Проверьте лог.")
        return

    try:
        size = exe_path.stat().st_size
        if size <= TG_UPLOAD_LIMIT:
            with exe_path.open("rb") as f:
                await update.message.reply_document(document=f, filename=exe_path.name, caption="Готовый .exe")
        else:
            await update.message.reply_text("EXE крупный, упаковываю в ZIP…")
            zip_path = exe_path.with_suffix(".zip")
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                zf.write(exe_path, arcname=exe_path.name)
            if zip_path.stat().st_size <= TG_UPLOAD_LIMIT:
                with zip_path.open("rb") as f:
                    await update.message.reply_document(document=f, filename=zip_path.name, caption="Готовый .exe (ZIP)")
            else:
                await update.message.reply_text(
                    "EXE собран, но слишком большой для отправки через Telegram, даже в ZIP. "
                    f"Путь к файлу на сервере: {exe_path}"
                )
    except Exception as e:
        logger.exception("Ошибка при отправке exe")
        await update.message.reply_text(f"EXE собран, но не удалось отправить: {e}\nПуть к файлу: {exe_path}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    logger.exception("Исключение при обработке апдейта", exc_info=err)
    try:
        if isinstance(update, Update) and update.effective_message:
            msg = "Произошла ошибка."
            if isinstance(err, BadRequest) and "File is too big" in str(err):
                msg = ("Файл слишком большой для скачивания ботом через API Telegram (жёсткий лимит около 20 МБ). "
                       "Пожалуйста, используйте файл меньшего размера.")
            await update.effective_message.reply_text(msg)
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
