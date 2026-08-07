"""Сборка книги с использованием LuaLaTeX.

Возможности:
    * По умолчанию выполняется два прохода LuaLaTeX.
    * Перед сборкой локальная папка со стилями (опционально) обновляется из внешнего репозитория. Если внешняя папка со стилями отсутствует, используются локальные стили.
    * При ошибке на любом проходе сборка немедленно прекращается.
    * Вывод LuaLaTeX скрывается, вместо него показывается индикатор прогресса.
    * Из файла журнала извлекаются ошибки и полезные предупреждения.
    * Флаг --clean выполняет только очистку вспомогательных файлов без сборки.

Использование:
    python3 build.py
    python3 build.py --clean
    python3 build.py --open
    python3 build.py --passes N
    python3 build.py --verbose
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple


# =============================================================================
# Основные настройки проекта
# =============================================================================

# Заголовок, выводимый в начале работы скрипта.
BUILD_TITLE = "Сборка книги"

# Корневая папка проекта определяется по расположению данного файла.
PROJECT_DIR = Path(__file__).resolve().parent

# Главный LaTeX-файл книги.
TEX_FILE = "book.tex"

# Локальная папка со стилями, используемая при сборке.
LOCAL_STYLE_DIR = Path("style")

# Внешняя папка со стилями из репозитория шаблона.
# Относительный путь отсчитывается от корневой папки проекта.
EXTERNAL_STYLE_DIR = Path("../_repo/faimatantemplate/style")


# =============================================================================
# Сообщения о синхронизации стилей
# =============================================================================

# Сообщение при успешном копировании стилей из внешнего репозитория.
STYLE_SYNC_SUCCESS_MESSAGE = (
    "Внешняя папка со стилями найдена: локальные стили обновлены."
)

# Сообщение при отсутствии внешней папки со стилями.
STYLE_SYNC_FALLBACK_MESSAGE = (
    "Внешняя папка со стилями не найдена: используются локальные стили."
)

# Сообщение при отсутствии как внешней, так и локальной папки со стилями.
STYLE_LOCAL_MISSING_MESSAGE = (
    "Локальная папка со стилями отсутствует: сборка невозможна."
)

# Сообщение при ошибке копирования внешней папки со стилями.
STYLE_SYNC_ERROR_MESSAGE = (
    "Не удалось обновить локальную папку со стилями."
)


# =============================================================================
# Настройки LuaLaTeX
# =============================================================================

# Имя исполняемого файла LuaLaTeX.
LUALATEX = "lualatex"

# Аргументы, передаваемые LuaLaTeX при каждом проходе.
LUALATEX_ARGS = [
    "-synctex=1",
    "-interaction=nonstopmode",
    "-file-line-error",
    "-halt-on-error",
    "-shell-escape",
]

# Число проходов LuaLaTeX по умолчанию.
DEFAULT_PASSES = 2


# =============================================================================
# Настройки очистки
# =============================================================================

# Расширения вспомогательных файлов, удаляемых при запуске с флагом --clean.
AUX_EXTS = (
    ".aux",
    ".log",
    ".toc",
    ".out",
    ".synctex.gz",
    ".lof",
    ".lot",
    ".tdo",
    ".fls",
    ".fdb_latexmk",
    ".bbl",
    ".blg",
    ".nav",
    ".snm",
    ".vrb",
)


# =============================================================================
# Настройки вывода диагностических сообщений
# =============================================================================

# Максимальное число предупреждений каждого вида, выводимых на экран.
WARN_SAMPLE_LIMIT = 5


# =============================================================================
# Настройки терминала
# =============================================================================

# ANSI-последовательности оформления текста.
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
WHITE = "\033[37m"

# Возможности текущего терминала.
IS_TTY = sys.stdout.isatty()
USE_COLOR = IS_TTY
UTF = "utf" in (sys.stdout.encoding or "").lower()


# =============================================================================
# Вспомогательные пути
# =============================================================================

TEX_PATH = PROJECT_DIR / TEX_FILE
LOG_PATH = TEX_PATH.with_suffix(".log")
PDF_PATH = TEX_PATH.with_suffix(".pdf")


# =============================================================================
# Вспомогательные функции терминала
# =============================================================================

def c(code: str, text: str) -> str:
    """Оформляет текст ANSI-последовательностью, если терминал это допускает."""
    return f"{code}{text}{RESET}" if USE_COLOR else text


OK_MARK = c(GREEN + BOLD, "✓") if UTF else c(GREEN + BOLD, "+")
FAIL_MARK = c(RED + BOLD, "✗") if UTF else c(RED + BOLD, "x")
SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if UTF else "|/-\\"


def header(text: str) -> None:
    """Выводит заголовок раздела."""
    bar = "=" * 60
    print(f"\n{c(CYAN + BOLD, bar)}")
    print(c(CYAN + BOLD, f"  {text}"))
    print(f"{c(CYAN + BOLD, bar)}\n")


def human_size(size: float) -> str:
    """Преобразует размер файла в удобный для чтения формат."""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            if unit == "Б":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


# =============================================================================
# Уведомления операционной системы
# =============================================================================

def notify(title: str, message: str, success: bool) -> None:
    """Показывает системное уведомление, если ОС предоставляет такую возможность."""
    try:
        if sys.platform == "darwin":
            sound = "Glass" if success else "Basso"
            script = (
                f'display notification "{message}" '
                f'with title "{title}" sound name "{sound}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=5,
                check=False,
            )
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            urgency = "normal" if success else "critical"
            subprocess.run(
                ["notify-send", "-u", urgency, title, message],
                capture_output=True,
                timeout=5,
                check=False,
            )
    except Exception:
        pass


# =============================================================================
# Работа с файловой системой
# =============================================================================

def resolve_project_path(path: Path) -> Path:
    """Преобразует путь относительно корня проекта в абсолютный путь."""
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_DIR / path).resolve()


def remove_path(path: Path) -> None:
    """Удаляет файл, символическую ссылку или папку."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def clean_aux() -> None:
    """Удаляет вспомогательные файлы сборки."""
    stem = TEX_PATH.stem
    removed = 0

    for extension in AUX_EXTS:
        path = PROJECT_DIR / f"{stem}{extension}"
        if path.exists() or path.is_symlink():
            remove_path(path)
            removed += 1

    print(c(DIM, f"  Удалено вспомогательных файлов: {removed}\n"))


def sync_styles() -> bool:
    """Обновляет локальную папку со стилями из внешнего репозитория."""
    external_dir = resolve_project_path(EXTERNAL_STYLE_DIR)
    local_dir = resolve_project_path(LOCAL_STYLE_DIR)

    if not external_dir.is_dir():
        print(c(YELLOW, f"  {STYLE_SYNC_FALLBACK_MESSAGE}"))
        print(c(DIM, f"  Внешняя папка: {external_dir}"))

        if not local_dir.is_dir():
            print(c(RED + BOLD, f"\n  {STYLE_LOCAL_MISSING_MESSAGE}"))
            print(c(DIM, f"  Локальная папка: {local_dir}\n"))
            return False

        print(c(DIM, f"  Локальная папка: {local_dir}\n"))
        return True

    try:
        if external_dir == local_dir:
            print(c(GREEN, f"  {STYLE_SYNC_SUCCESS_MESSAGE}"))
            print(c(DIM, f"  Папка со стилями: {local_dir}\n"))
            return True

        if local_dir.exists() or local_dir.is_symlink():
            remove_path(local_dir)

        shutil.copytree(external_dir, local_dir)
    except OSError as error:
        print(c(RED + BOLD, f"  {STYLE_SYNC_ERROR_MESSAGE}"))
        print(c(DIM, f"  Источник: {external_dir}"))
        print(c(DIM, f"  Назначение: {local_dir}"))
        print(c(RED, f"  Причина: {error}\n"))
        return False

    print(c(GREEN, f"  {STYLE_SYNC_SUCCESS_MESSAGE}"))
    print(c(DIM, f"  Источник: {external_dir}"))
    print(c(DIM, f"  Назначение: {local_dir}\n"))
    return True


# =============================================================================
# Структуры диагностических сообщений
# =============================================================================

@dataclass
class LogError:
    """Ошибка, найденная в журнале LuaLaTeX."""

    message: str
    file: str = ""
    line: str = ""
    context: List[str] = field(default_factory=list)


@dataclass
class LogWarning:
    """Предупреждение, найденное в журнале LuaLaTeX."""

    kind: str
    message: str
    line: str = ""
    details: str = ""


# =============================================================================
# Регулярные выражения для разбора журнала
# =============================================================================

RE_FILELINE = re.compile(r"^(.+?\.\w+):(\d+):\s*(.+)$")
RE_BANG = re.compile(r"^!\s+(.+)$")

RE_BOX = re.compile(r"^(Overfull|Underfull)\s+\\([hv])box\b(.*)")
RE_BOX_AMOUNT = re.compile(r"\(([\d.]+)pt too (\w+)\)")
RE_BOX_BADNESS = re.compile(r"\(badness\s+(\d+)\)")
RE_AT_LINES = re.compile(r"at lines?\s+(\d+)(?:--(\d+))?")

RE_UNDEF_REF = re.compile(
    r"(?:LaTeX|Package\s+\S+)\s+Warning:\s+"
    r"(Reference|Citation)\s+[`']([^']+)'\s+.*?undefined",
    re.IGNORECASE,
)

RE_MULTI_LABEL = re.compile(
    r"LaTeX Warning:\s+Label\s+[`']([^']+)'\s+multiply defined",
    re.IGNORECASE,
)

RE_MISS_CHAR = re.compile(
    r"Missing character:\s+There is no\s+(\S+)\s+.+?\s+in font\s+(.+?)!?$"
)

RE_FILE_MISS = re.compile(
    r"(?:LaTeX Error:\s+)?File\s+[`']([^']+)'\s+not found"
)

RE_PAGE = re.compile(r"\[(\d+)[\s\]\{]")


# =============================================================================
# Разбор журнала LuaLaTeX
# =============================================================================

def parse_log(log_path: Path) -> Tuple[List[LogError], List[LogWarning]]:
    """Извлекает ошибки и полезные предупреждения из журнала LuaLaTeX."""
    if not log_path.is_file():
        return [], []

    with log_path.open(encoding="utf-8", errors="replace") as file:
        lines = file.read().splitlines()

    errors: List[LogError] = []
    warnings: List[LogWarning] = []

    seen_errors = set()
    seen_warnings = set()

    def add_warning(
        kind: str,
        message: str,
        line: str = "",
        details: str = "",
    ) -> None:
        key = (kind, message, line, details)
        if key in seen_warnings:
            return

        seen_warnings.add(key)
        warnings.append(
            LogWarning(
                kind=kind,
                message=message,
                line=line,
                details=details,
            )
        )

    index = 0

    while index < len(lines):
        line = lines[index]

        # ---------------------------------------------------------------------
        # Ошибки
        # ---------------------------------------------------------------------

        fileline_match = RE_FILELINE.match(line)
        bang_match = None if fileline_match else RE_BANG.match(line)

        if fileline_match or bang_match:
            if fileline_match:
                file_name = fileline_match.group(1)
                line_number = fileline_match.group(2)
                message = fileline_match.group(3).strip()
            else:
                file_name = ""
                line_number = ""
                message = bang_match.group(1).strip()

            context = []
            context_index = index + 1

            while context_index < len(lines) and context_index <= index + 4:
                context_line = lines[context_index]

                if context_line.startswith("!") or RE_FILELINE.match(context_line):
                    break

                context.append(context_line)
                context_index += 1

            key = (file_name, line_number, message)

            if key not in seen_errors:
                seen_errors.add(key)
                errors.append(
                    LogError(
                        message=message,
                        file=file_name,
                        line=line_number,
                        context=context,
                    )
                )

            index = context_index
            continue

        # ---------------------------------------------------------------------
        # Переполненные и недополненные блоки
        # ---------------------------------------------------------------------

        box_match = RE_BOX.match(line)

        if box_match:
            kind = box_match.group(1).lower()
            box_type = box_match.group(2)

            buffer = line
            continuation_index = index + 1

            while (
                continuation_index < len(lines)
                and continuation_index < index + 4
                and lines[continuation_index].strip()
            ):
                next_line = lines[continuation_index]

                if (
                    RE_BOX.match(next_line)
                    or RE_FILELINE.match(next_line)
                    or next_line.startswith("!")
                ):
                    break

                buffer += " " + next_line
                continuation_index += 1

            lines_match = RE_AT_LINES.search(buffer)
            line_range = ""

            if lines_match:
                line_range = lines_match.group(1)
                if lines_match.group(2):
                    line_range += f"–{lines_match.group(2)}"

            amount_match = RE_BOX_AMOUNT.search(buffer)
            badness_match = RE_BOX_BADNESS.search(buffer)
            details = ""

            if amount_match:
                details = f"{amount_match.group(1)}pt too {amount_match.group(2)}"
            elif badness_match:
                details = f"badness {badness_match.group(1)}"

            add_warning(
                kind=kind,
                message=f"\\{box_type}box",
                line=line_range,
                details=details,
            )

            index += 1
            continue

        # ---------------------------------------------------------------------
        # Неопределённые ссылки и цитаты
        # ---------------------------------------------------------------------

        undefined_reference_match = RE_UNDEF_REF.search(line)

        if undefined_reference_match:
            add_warning(
                "ref",
                (
                    f"{undefined_reference_match.group(1)} "
                    f"'{undefined_reference_match.group(2)}' не определена"
                ),
            )
            index += 1
            continue

        # ---------------------------------------------------------------------
        # Дублирующиеся метки
        # ---------------------------------------------------------------------

        duplicate_label_match = RE_MULTI_LABEL.search(line)

        if duplicate_label_match:
            add_warning(
                "dup",
                (
                    f"Метка '{duplicate_label_match.group(1)}' "
                    f"определена несколько раз"
                ),
            )
            index += 1
            continue

        # ---------------------------------------------------------------------
        # Отсутствующие глифы
        # ---------------------------------------------------------------------

        missing_character_match = RE_MISS_CHAR.search(line)

        if missing_character_match:
            add_warning(
                "char",
                (
                    f"Нет глифа {missing_character_match.group(1)} "
                    f"в шрифте {missing_character_match.group(2)}"
                ),
            )
            index += 1
            continue

        # ---------------------------------------------------------------------
        # Отсутствующие файлы
        # ---------------------------------------------------------------------

        missing_file_match = RE_FILE_MISS.search(line)

        if missing_file_match:
            add_warning(
                "file",
                f"Не найден файл: {missing_file_match.group(1)}",
            )
            index += 1
            continue

        index += 1

    return errors, warnings


# =============================================================================
# Вывод ошибок и предупреждений
# =============================================================================

def print_errors(errors: List[LogError]) -> None:
    """Выводит найденные ошибки."""
    bar = "=" * 60

    print(f"\n{c(RED + BOLD, bar)}")
    print(c(RED + BOLD, f"  Найдено ошибок: {len(errors)}"))
    print(f"{c(RED + BOLD, bar)}\n")

    for index, error in enumerate(errors, 1):
        location = ""

        if error.file:
            location += c(CYAN, error.file)

        if error.line:
            location += c(DIM, ":") + c(YELLOW, error.line)

        print(f"  {c(RED + BOLD, f'[{index}]')} {location}")
        print(f"      {c(WHITE, error.message)}")

        for context_line in error.context:
            stripped_line = context_line.strip()
            if stripped_line:
                print(f"      {c(DIM, stripped_line)}")

        print()


WARN_LABELS = {
    "overfull": "Переполнение (overfull)",
    "underfull": "Недополнение (underfull)",
    "ref": "Неопределённые ссылки",
    "dup": "Дублирующиеся метки",
    "file": "Отсутствующие файлы",
    "char": "Нет глифа в шрифте",
}

WARN_ORDER = (
    "file",
    "ref",
    "dup",
    "char",
    "overfull",
    "underfull",
)


def print_warnings(
    warnings: List[LogWarning],
    limit: int = WARN_SAMPLE_LIMIT,
) -> None:
    """Выводит найденные предупреждения, сгруппированные по типам."""
    if not warnings:
        return

    groups = {}

    for warning in warnings:
        groups.setdefault(warning.kind, []).append(warning)

    print(c(YELLOW + BOLD, f"  Предупреждения ({len(warnings)}):"))

    ordered_kinds = list(WARN_ORDER)
    ordered_kinds.extend(
        kind for kind in groups if kind not in WARN_ORDER
    )

    for kind in ordered_kinds:
        items = groups.get(kind, [])

        if not items:
            continue

        label = WARN_LABELS.get(kind, kind.capitalize())

        print(f"  {c(YELLOW, '▸')} {c(BOLD, label)} — {len(items)}")

        for warning in items[:limit]:
            tags = []

            if warning.line:
                tags.append(c(CYAN, f"стр. {warning.line}"))

            if warning.details:
                tags.append(c(DIM, warning.details))

            tail = f"  [{' · '.join(tags)}]" if tags else ""

            print(f"      {c(YELLOW, '·')} {warning.message}{tail}")

        if len(items) > limit:
            print(c(DIM, f"      … ещё {len(items) - limit}"))

    print()


def print_success(elapsed: float) -> None:
    """Выводит сообщение об успешной сборке."""
    bar = "=" * 60

    print(f"\n{c(GREEN + BOLD, bar)}")
    print(c(GREEN + BOLD, f"  Сборка завершена успешно ({elapsed:.1f} с)"))

    if PDF_PATH.exists():
        relative_pdf_path = PDF_PATH.relative_to(PROJECT_DIR)
        print(
            c(
                GREEN,
                (
                    f"  PDF: {relative_pdf_path} "
                    f"({human_size(PDF_PATH.stat().st_size)})"
                ),
            )
        )

    print(f"{c(GREEN + BOLD, bar)}\n")


# =============================================================================
# Запуск LuaLaTeX
# =============================================================================

def draw_status(
    label: str,
    marker: str,
    page: int,
    elapsed: float,
) -> None:
    """Обновляет строку состояния текущего прохода LuaLaTeX."""
    if not IS_TTY:
        return

    if page:
        page_part = c(WHITE, f"стр. {page:>4}")
    else:
        page_part = c(DIM, "стр. ----")

    time_part = c(DIM, f"{elapsed:5.1f} с")
    line = f"  {c(CYAN + BOLD, label)}  {marker}  {page_part}  {time_part}"

    sys.stdout.write("\r\033[K" + line)
    sys.stdout.flush()


def run_lualatex(
    pass_num: int,
    total: int,
    verbose: bool = False,
) -> Tuple[int, float, int]:
    """Выполняет один проход LuaLaTeX."""
    command = [LUALATEX, *LUALATEX_ARGS, TEX_FILE]
    label = f"Проход {pass_num}/{total}"
    started_at = time.time()

    # -------------------------------------------------------------------------
    # Режим с непосредственным выводом LuaLaTeX
    # -------------------------------------------------------------------------

    if verbose:
        print(
            f"  {c(CYAN + BOLD, label)}  "
            f"{c(DIM, ' '.join(command))}"
        )

        return_code = subprocess.call(
            command,
            cwd=PROJECT_DIR,
        )

        elapsed = time.time() - started_at
        mark = OK_MARK if return_code == 0 else FAIL_MARK

        print(
            f"  {c(CYAN + BOLD, label)}  {mark}  "
            f"{c(DIM, f'({elapsed:.1f} с)')}\n"
        )

        return return_code, elapsed, 0

    # -------------------------------------------------------------------------
    # Обычный режим со скрытым выводом и индикатором прогресса
    # -------------------------------------------------------------------------

    process = subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        errors="replace",
    )

    state = {
        "page": 0,
        "done": False,
    }

    def spinner_loop() -> None:
        frame_index = 0

        while not state["done"]:
            draw_status(
                label,
                c(YELLOW, SPIN_FRAMES[frame_index % len(SPIN_FRAMES)]),
                state["page"],
                time.time() - started_at,
            )

            frame_index += 1
            time.sleep(0.1)

    spinner_thread = None

    if IS_TTY:
        spinner_thread = threading.Thread(
            target=spinner_loop,
            daemon=True,
        )
        spinner_thread.start()

    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                for page_match in RE_PAGE.findall(raw_line):
                    try:
                        page = int(page_match)
                    except ValueError:
                        continue

                    if page > state["page"]:
                        state["page"] = page

    except KeyboardInterrupt:
        state["done"] = True

        if spinner_thread:
            spinner_thread.join(timeout=0.5)

        try:
            process.terminate()
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

        if IS_TTY:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        raise

    process.wait()

    state["done"] = True

    if spinner_thread:
        spinner_thread.join(timeout=0.5)

    elapsed = time.time() - started_at
    mark = OK_MARK if process.returncode == 0 else FAIL_MARK

    if IS_TTY:
        draw_status(label, mark, state["page"], elapsed)
        print()
    else:
        plain_mark = "OK" if process.returncode == 0 else "FAIL"
        print(
            f"  {label}  {plain_mark}  "
            f"стр. {state['page']}  {elapsed:.1f} с"
        )

    return process.returncode, elapsed, state["page"]


# =============================================================================
# Открытие PDF
# =============================================================================

def open_pdf() -> None:
    """Открывает собранный PDF в приложении, назначенном операционной системой."""
    if not PDF_PATH.exists():
        return

    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["open", str(PDF_PATH)],
                check=False,
            )
        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["xdg-open", str(PDF_PATH)],
                check=False,
            )
        elif sys.platform.startswith("win"):
            os.startfile(str(PDF_PATH))  # type: ignore[attr-defined]
    except Exception:
        pass


# =============================================================================
# Проверки перед сборкой
# =============================================================================

def check_build_requirements() -> bool:
    """Проверяет наличие главного файла и компилятора LuaLaTeX."""
    if not TEX_PATH.is_file():
        print(c(RED, f"  Файл {TEX_PATH} не найден."))
        return False

    if shutil.which(LUALATEX) is None:
        print(c(RED, f"  Компилятор '{LUALATEX}' не найден в PATH."))
        return False

    return True


# =============================================================================
# Разбор аргументов командной строки
# =============================================================================

def parse_arguments() -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Сборка книги с использованием LuaLaTeX",
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="удалить вспомогательные файлы и завершить работу без сборки",
    )

    parser.add_argument(
        "--open",
        dest="open_pdf",
        action="store_true",
        help="открыть PDF после успешной сборки",
    )

    parser.add_argument(
        "--passes",
        type=int,
        default=DEFAULT_PASSES,
        help=f"число проходов LuaLaTeX (по умолчанию {DEFAULT_PASSES})",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="не скрывать вывод LuaLaTeX",
    )

    return parser.parse_args()


# =============================================================================
# Основная функция
# =============================================================================

def main() -> int:
    """Выполняет очистку или сборку книги."""
    args = parse_arguments()

    if IS_TTY:
        os.system("clear" if os.name != "nt" else "cls")

    header(BUILD_TITLE)

    # При флаге --clean выполняется только очистка.
    if args.clean:
        clean_aux()
        return 0

    if args.passes < 1:
        print(c(RED, "  Значение --passes должно быть не меньше 1."))
        return 1

    if not check_build_requirements():
        return 1

    if not sync_styles():
        notify(
            "LaTeX: ошибка подготовки стилей",
            "Не удалось подготовить папку со стилями",
            success=False,
        )
        return 1

    total_elapsed = 0.0
    warnings: List[LogWarning] = []

    for pass_number in range(1, args.passes + 1):
        try:
            return_code, elapsed, _ = run_lualatex(
                pass_number,
                args.passes,
                verbose=args.verbose,
            )
        except KeyboardInterrupt:
            print(c(YELLOW, "\n  Прервано пользователем."))
            return 130

        total_elapsed += elapsed
        errors, warnings = parse_log(LOG_PATH)

        if return_code != 0 or errors:
            if not errors:
                errors = [
                    LogError(
                        message=(
                            f"LuaLaTeX завершился с кодом {return_code} "
                            f"(подробности находятся в {LOG_PATH})"
                        ),
                    )
                ]

            print_errors(errors)
            print_warnings(warnings)

            notify(
                "LaTeX: ошибки сборки",
                f"Найдено ошибок: {len(errors)}",
                success=False,
            )

            return 2

    if not PDF_PATH.exists():
        print(c(RED, f"\n  PDF-файл {PDF_PATH} не был создан."))

        notify(
            "LaTeX: PDF не создан",
            str(PDF_PATH),
            success=False,
        )

        return 2

    print_success(total_elapsed)
    print_warnings(warnings)

    notify(
        "LaTeX: сборка завершена",
        "Всё прошло успешно",
        success=True,
    )

    if args.open_pdf:
        open_pdf()

    return 0


# =============================================================================
# Точка входа
# =============================================================================

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)

    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(c(YELLOW, "\n  Прервано пользователем."))
        sys.exit(130)