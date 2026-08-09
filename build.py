"""Сборка книги с использованием LuaLaTeX.

Возможности:
    * По исходным LaTeX-файлам автоматически формируется карта блоков map.md.
    * Флаг --map выполняет только формирование карты без сборки документа.
    * По умолчанию выполняется два прохода LuaLaTeX.
    * Перед сборкой локальная папка со стилями (опционально) обновляется из внешнего репозитория.
    * Если внешняя папка со стилями отсутствует, используются локальные стили.
    * При ошибке на любом проходе сборка немедленно прекращается.
    * Вывод LuaLaTeX скрывается, вместо него показывается индикатор прогресса.
    * Из файла журнала извлекаются ошибки и полезные предупреждения.
    * Флаг --clean выполняет только очистку вспомогательных файлов без сборки.

Использование:
    python3 build.py
    python3 build.py --clean
    python3 build.py --map
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
from typing import Dict, List, Optional, Tuple


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
# Настройки карты содержания
# =============================================================================

# Заголовок первого уровня в автоматически формируемой карте.
CONTENT_MAP_TITLE = "Содержание книги"

# Файл автоматически формируемой карты.
CONTENT_MAP_FILE = "map.md"

# Команды блоков и их сигнатуры:
# mandatory — число обязательных аргументов;
# trailing_optional — максимальное число необязательных аргументов после них;
# title_index — индекс аргумента с заголовком блока;
# initial_options — допускается ли необязательный аргумент с параметрами блока.
BLOCK_SIGNATURES: Dict[str, Dict[str, object]] = {
    "defi": {
        "mandatory": 3,
        "trailing_optional": 2,
        "title_index": 1,
        "initial_options": True,
    },
    "stat": {
        "mandatory": 3,
        "trailing_optional": 2,
        "title_index": 1,
        "initial_options": True,
    },
    "lemm": {
        "mandatory": 3,
        "trailing_optional": 2,
        "title_index": 1,
        "initial_options": True,
    },
    "theo": {
        "mandatory": 3,
        "trailing_optional": 2,
        "title_index": 1,
        "initial_options": True,
    },
    "conc": {
        "mandatory": 3,
        "trailing_optional": 2,
        "title_index": 1,
        "initial_options": True,
    },
    "exam": {
        "mandatory": 3,
        "trailing_optional": 2,
        "title_index": 1,
        "initial_options": True,
    },
    "note": {
        "mandatory": 3,
        "trailing_optional": 0,
        "title_index": 1,
        "initial_options": True,
    },
    "algo": {
        "mandatory": 3,
        "trailing_optional": 2,
        "title_index": 1,
        "initial_options": True,
    },
    "code": {
        "mandatory": 3,
        "trailing_optional": 1,
        "title_index": 1,
        "initial_options": False,
    },
    "task": {
        "mandatory": 2,
        "trailing_optional": 2,
        "title_index": None,
        "initial_options": False,
    },
}


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
CONTENT_MAP_PATH = PROJECT_DIR / CONTENT_MAP_FILE


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
# Структуры карты содержания
# =============================================================================

@dataclass
class ContentMapBlock:
    """Блок учебника, добавляемый в карту содержания."""

    kind: str
    label: str
    title: str = ""
    optional_mark: str = ""


@dataclass
class ContentMapSubsection:
    """Подраздел четвёртого уровня в карте содержания."""

    title: str
    blocks: List[ContentMapBlock] = field(default_factory=list)


@dataclass
class ContentMapSection:
    """Раздел главы в карте содержания."""

    title: str
    number: str = ""
    blocks: List[ContentMapBlock] = field(default_factory=list)
    subsections: List[ContentMapSubsection] = field(default_factory=list)


@dataclass
class ContentMapChapter:
    """Глава книги в карте содержания."""

    title: str
    number: str = ""
    blocks: List[ContentMapBlock] = field(default_factory=list)
    sections: List[ContentMapSection] = field(default_factory=list)


@dataclass
class ContentMapDocument:
    """Полная древовидная структура карты содержания."""

    blocks: List[ContentMapBlock] = field(default_factory=list)
    chapters: List[ContentMapChapter] = field(default_factory=list)


@dataclass
class ContentMapLabelWarning:
    """Предупреждение о некорректной метке блока."""

    source: str
    line: int
    message: str


class ContentMapError(RuntimeError):
    """Ошибка при построении карты содержания."""


# =============================================================================
# Разбор LaTeX для построения карты содержания
# =============================================================================

RE_TEX_COMMAND = re.compile(r"\\([A-Za-z@]+)")
RE_BEGIN_DOCUMENT = re.compile(r"\\begin\s*\{\s*document\s*\}")
RE_END_DOCUMENT = re.compile(r"\\end\s*\{\s*document\s*\}")


def normalize_tex_text(text: str) -> str:
    """Сворачивает пробельные символы LaTeX-фрагмента в одиночные пробелы."""
    return re.sub(r"\s+", " ", text).strip()


def skip_whitespace(text: str, position: int) -> int:
    """Пропускает пробельные символы, начиная с заданной позиции."""
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def skip_tex_verb(text: str, position: int) -> Optional[int]:
    """Возвращает позицию после конструкции \\verb или None, если её нет."""
    if not text.startswith(r"\verb", position):
        return None

    command_end = position + len(r"\verb")

    if command_end < len(text) and text[command_end].isalpha():
        return None

    if command_end < len(text) and text[command_end] == "*":
        command_end += 1

    if command_end >= len(text):
        return None

    delimiter = text[command_end]

    if delimiter.isspace():
        return None

    closing_position = text.find(delimiter, command_end + 1)

    if closing_position == -1:
        return len(text)

    return closing_position + 1


def is_escaped(text: str, position: int) -> bool:
    """Проверяет, экранирован ли символ в заданной позиции обратной косой чертой."""
    backslashes = 0
    index = position - 1

    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1

    return backslashes % 2 == 1


def strip_tex_comments(text: str) -> str:
    """Удаляет LaTeX-комментарии, сохраняя переводы строк."""
    result: List[str] = []
    position = 0

    while position < len(text):
        verb_end = skip_tex_verb(text, position)

        if verb_end is not None:
            result.append(text[position:verb_end])
            position = verb_end
            continue

        if text[position] == "%" and not is_escaped(text, position):
            newline_position = text.find("\n", position)

            if newline_position == -1:
                break

            result.append("\n")
            position = newline_position + 1
            continue

        result.append(text[position])
        position += 1

    return "".join(result)


def read_tex_group(
    text: str,
    position: int,
    opener: str,
    closer: str,
) -> Tuple[str, int]:
    """Читает сбалансированную группу в фигурных или квадратных скобках."""
    position = skip_whitespace(text, position)

    if position >= len(text) or text[position] != opener:
        raise ValueError(f"ожидался символ {opener!r}")

    content_start = position + 1
    index = content_start

    if opener == "{":
        depth = 1

        while index < len(text):
            verb_end = skip_tex_verb(text, index)

            if verb_end is not None:
                index = verb_end
                continue

            if text[index] == "\\":
                if index + 1 < len(text) and text[index + 1] in "{}[]%\\":
                    index += 2
                else:
                    index += 1
                continue

            if text[index] == opener:
                depth += 1
            elif text[index] == closer:
                depth -= 1

                if depth == 0:
                    return text[content_start:index], index + 1

            index += 1

        raise ValueError("не найдена закрывающая фигурная скобка")

    square_depth = 1
    brace_depth = 0

    while index < len(text):
        verb_end = skip_tex_verb(text, index)

        if verb_end is not None:
            index = verb_end
            continue

        if text[index] == "\\":
            if index + 1 < len(text) and text[index + 1] in "{}[]%\\":
                index += 2
            else:
                index += 1
            continue

        if text[index] == "{":
            brace_depth += 1
        elif text[index] == "}" and brace_depth > 0:
            brace_depth -= 1
        elif brace_depth == 0 and text[index] == opener:
            square_depth += 1
        elif brace_depth == 0 and text[index] == closer:
            square_depth -= 1

            if square_depth == 0:
                return text[content_start:index], index + 1

        index += 1

    raise ValueError("не найдена закрывающая квадратная скобка")


def split_top_level_commas(text: str) -> List[str]:
    """Разделяет строку по запятым, находящимся вне фигурных скобок."""
    parts: List[str] = []
    start = 0
    depth = 0

    for index, character in enumerate(text):
        if character == "{" and not is_escaped(text, index):
            depth += 1
        elif character == "}" and not is_escaped(text, index):
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1

    parts.append(text[start:].strip())
    return [part for part in parts if part]


def integer_to_letters(value: int) -> str:
    """Преобразует положительное число в буквенное обозначение приложения."""
    if value < 1:
        return "A"

    result = ""

    while value > 0:
        value -= 1
        result = chr(ord("A") + value % 26) + result
        value //= 26

    return result


def detect_optional_mark(options: str) -> str:
    """Определяет отметку дополнительного блока или необязательного доказательства."""
    if not options.strip():
        return ""

    optional_parts: List[str] = []

    for option in split_top_level_commas(options):
        compact_option = re.sub(r"\s+", "", option)

        if compact_option == "optional":
            return "доп материал"

        if not compact_option.startswith("optional="):
            continue

        value = compact_option.split("=", 1)[1]

        if value.startswith("{") and value.endswith("}"):
            value = value[1:-1]

        optional_parts.extend(
            part
            for part in value.split(",")
            if part
        )

    if "block" in optional_parts:
        return "доп материал"

    if "proof" in optional_parts:
        return "без док-ва"

    return ""


class ContentMapParser:
    """Рекурсивно разбирает структуру книги и формирует дерево карты."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.resolve()
        self.document = ContentMapDocument()

        self.current_chapter: Optional[ContentMapChapter] = None
        self.current_section: Optional[ContentMapSection] = None
        self.current_subsection: Optional[ContentMapSubsection] = None

        self.chapter_counter = 0
        self.section_counter = 0
        self.appendix_mode = False

        self.active_files: List[Path] = []
        self.block_count = 0

        self.label_warnings: List[ContentMapLabelWarning] = []

    def parse(self, main_file: Path) -> ContentMapDocument:
        """Разбирает главный файл книги."""
        self._parse_file(main_file.resolve(), document_only=True)
        return self.document

    def _parse_file(self, path: Path, document_only: bool = False) -> None:
        """Разбирает один LaTeX-файл и рекурсивно обрабатывает его включения."""
        if path in self.active_files:
            chain = " -> ".join(str(item) for item in [*self.active_files, path])
            raise ContentMapError(
                f"обнаружено циклическое включение файлов: {chain}"
            )

        if not path.is_file():
            raise ContentMapError(f"не найден LaTeX-файл: {path}")

        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ContentMapError(
                f"не удалось прочитать файл {path}: {error}"
            ) from error

        text = strip_tex_comments(raw_text)

        if document_only:
            begin_match = RE_BEGIN_DOCUMENT.search(text)

            if begin_match is None:
                raise ContentMapError(
                    f"в главном файле {path} не найдено окружение document"
                )

            text = text[begin_match.end():]
            end_match = RE_END_DOCUMENT.search(text)

            if end_match is not None:
                text = text[:end_match.start()]

        self.active_files.append(path)

        try:
            self._parse_text(text, path)
        finally:
            self.active_files.pop()

    def _parse_text(self, text: str, source_path: Path) -> None:
        """Последовательно разбирает команды в LaTeX-тексте."""
        position = 0

        while position < len(text):
            command_position = text.find("\\", position)

            if command_position == -1:
                break

            verb_end = skip_tex_verb(text, command_position)

            if verb_end is not None:
                position = verb_end
                continue

            command_match = RE_TEX_COMMAND.match(text, command_position)

            if command_match is None:
                position = command_position + 1
                continue

            command = command_match.group(1)
            cursor = command_match.end()

            if command in {"input", "include"}:
                position = self._parse_input(
                    text,
                    cursor,
                    source_path,
                    command_position,
                )
                continue

            if command == "appendix":
                self.appendix_mode = True
                self.chapter_counter = 0
                self.section_counter = 0
                position = cursor
                continue

            if command == "chapter":
                starred, title, position = self._parse_heading(
                    text,
                    cursor,
                    source_path,
                    command_position,
                )
                self._start_chapter(title, starred)
                continue

            if command == "section":
                starred, title, position = self._parse_heading(
                    text,
                    cursor,
                    source_path,
                    command_position,
                )
                self._start_section(title, starred, source_path, command_position)
                continue

            if command == "subsection":
                _, title, position = self._parse_heading(
                    text,
                    cursor,
                    source_path,
                    command_position,
                )
                self._start_subsection(title, source_path, command_position)
                continue

            if command in BLOCK_SIGNATURES:
                position = self._parse_block(
                    command,
                    text,
                    cursor,
                    source_path,
                    command_position,
                )
                continue

            position = cursor

    def _parse_heading(
        self,
        text: str,
        cursor: int,
        source_path: Path,
        command_position: int,
    ) -> Tuple[bool, str, int]:
        """Разбирает заголовок chapter, section или subsection."""
        cursor = skip_whitespace(text, cursor)
        starred = False

        if cursor < len(text) and text[cursor] == "*":
            starred = True
            cursor += 1

        cursor = skip_whitespace(text, cursor)

        try:
            if cursor < len(text) and text[cursor] == "[":
                _, cursor = read_tex_group(text, cursor, "[", "]")

            title, cursor = read_tex_group(text, cursor, "{", "}")
        except ValueError as error:
            self._raise_parse_error(
                source_path,
                text,
                command_position,
                str(error),
            )

        return starred, normalize_tex_text(title), cursor

    def _parse_input(
        self,
        text: str,
        cursor: int,
        source_path: Path,
        command_position: int,
    ) -> int:
        """Разбирает команду input или include и обрабатывает указанный файл."""
        try:
            input_name, cursor = read_tex_group(text, cursor, "{", "}")
        except ValueError as error:
            self._raise_parse_error(
                source_path,
                text,
                command_position,
                str(error),
            )

        input_path = self._resolve_input_path(
            normalize_tex_text(input_name),
            source_path,
        )
        self._parse_file(input_path)
        return cursor

    def _resolve_input_path(
        self,
        input_name: str,
        source_path: Path,
    ) -> Path:
        """Разрешает путь к файлу, указанному в input или include."""
        if not input_name:
            raise ContentMapError(
                f"в файле {source_path} указана пустая команда input"
            )

        if "\\" in input_name:
            raise ContentMapError(
                f"динамический путь в команде input не поддерживается: {input_name}"
            )

        raw_path = Path(input_name)

        if raw_path.suffix == "":
            raw_path = raw_path.with_suffix(".tex")

        candidates: List[Path] = []

        if raw_path.is_absolute():
            candidates.append(raw_path)
        else:
            candidates.append(self.project_dir / raw_path)
            candidates.append(source_path.parent / raw_path)

        for candidate in candidates:
            resolved_candidate = candidate.resolve()

            if resolved_candidate.is_file():
                return resolved_candidate

        candidate_list = ", ".join(str(candidate.resolve()) for candidate in candidates)
        raise ContentMapError(
            f"не найден файл из команды input: {input_name}; проверены пути: {candidate_list}"
        )

    def _parse_block(
        self,
        command: str,
        text: str,
        cursor: int,
        source_path: Path,
        command_position: int,
    ) -> int:
        """Разбирает один блок учебника."""
        signature = BLOCK_SIGNATURES[command]

        mandatory_count = int(signature["mandatory"])
        trailing_optional_count = int(signature["trailing_optional"])
        title_index = signature["title_index"]
        initial_options_allowed = bool(signature["initial_options"])

        cursor = skip_whitespace(text, cursor)
        starred = False

        if cursor < len(text) and text[cursor] == "*":
            starred = True
            cursor += 1

        cursor = skip_whitespace(text, cursor)
        options = ""

        try:
            if (
                initial_options_allowed
                and cursor < len(text)
                and text[cursor] == "["
            ):
                options, cursor = read_tex_group(text, cursor, "[", "]")

            arguments: List[str] = []

            for _ in range(mandatory_count):
                argument, cursor = read_tex_group(text, cursor, "{", "}")
                arguments.append(argument)

            for _ in range(trailing_optional_count):
                optional_cursor = skip_whitespace(text, cursor)

                if (
                    optional_cursor >= len(text)
                    or text[optional_cursor] != "["
                ):
                    break

                _, cursor = read_tex_group(
                    text,
                    optional_cursor,
                    "[",
                    "]",
                )
        except ValueError as error:
            self._raise_parse_error(
                source_path,
                text,
                command_position,
                f"не удалось разобрать блок \\{command}: {error}",
            )

        label = normalize_tex_text(arguments[0])
        title = ""

        if isinstance(title_index, int):
            title = normalize_tex_text(arguments[title_index])

        self._validate_block_label(
            command=command,
            label=label,
            starred=starred,
            source_path=source_path,
            text=text,
            command_position=command_position,
        )

        block = ContentMapBlock(
            kind=command,
            label=label,
            title=title,
            optional_mark=detect_optional_mark(options),
        )

        self._add_block(block)
        self.block_count += 1
        return cursor

    def _start_chapter(self, title: str, starred: bool) -> None:
        """Добавляет в дерево новую главу."""
        number = ""

        if not starred:
            self.chapter_counter += 1
            self.section_counter = 0

            if self.appendix_mode:
                number = integer_to_letters(self.chapter_counter)
            else:
                number = str(self.chapter_counter)

        chapter = ContentMapChapter(
            title=title,
            number=number,
        )

        self.document.chapters.append(chapter)
        self.current_chapter = chapter
        self.current_section = None
        self.current_subsection = None

    def _start_section(
        self,
        title: str,
        starred: bool,
        source_path: Path,
        command_position: int,
    ) -> None:
        """Добавляет в текущую главу новый раздел."""
        if self.current_chapter is None:
            raise ContentMapError(
                f"раздел вне главы: {source_path}:{self._line_number(source_path, command_position)}"
            )

        number = ""

        if not starred:
            self.section_counter += 1

            if self.appendix_mode:
                chapter_number = integer_to_letters(self.chapter_counter)
            else:
                chapter_number = str(self.chapter_counter)

            number = f"{chapter_number}.{self.section_counter}"

        section = ContentMapSection(
            title=title,
            number=number,
        )

        self.current_chapter.sections.append(section)
        self.current_section = section
        self.current_subsection = None

    def _start_subsection(
        self,
        title: str,
        source_path: Path,
        command_position: int,
    ) -> None:
        """Добавляет в текущий раздел новый подраздел."""
        if self.current_section is None:
            raise ContentMapError(
                f"подраздел вне раздела: {source_path}:{self._line_number(source_path, command_position)}"
            )

        subsection = ContentMapSubsection(title=title)
        self.current_section.subsections.append(subsection)
        self.current_subsection = subsection

    def _add_block(self, block: ContentMapBlock) -> None:
        """Добавляет блок в текущий элемент иерархии."""
        if self.current_subsection is not None:
            self.current_subsection.blocks.append(block)
        elif self.current_section is not None:
            self.current_section.blocks.append(block)
        elif self.current_chapter is not None:
            self.current_chapter.blocks.append(block)
        else:
            self.document.blocks.append(block)

    def _validate_block_label(
        self,
        command: str,
        label: str,
        starred: bool,
        source_path: Path,
        text: str,
        command_position: int,
    ) -> None:
        """Проверяет соответствие метки типу блока."""
        line_number = text.count("\n", 0, command_position) + 1

        try:
            source_name = source_path.relative_to(self.project_dir).as_posix()
        except ValueError:
            source_name = str(source_path)

        if starred:
            if label:
                self.label_warnings.append(
                    ContentMapLabelWarning(
                        source=source_name,
                        line=line_number,
                        message=(
                            f"У звёздного блока \\{command} метка должна быть пустой; "
                            f"указана метка <<{label}>>."
                        ),
                    )
                )

            return

        expected_pattern = rf"{re.escape(command)}:[^\s]+"

        if re.fullmatch(expected_pattern, label):
            return

        if command == 'task':
            return

        if not label:
            message = (
                f"У блока \\{command} отсутствует метка; "
                f"ожидается метка вида <<{command}:имя>>."
            )
        else:
            message = (
                f"Метка блока \\{command} должна иметь вид <<{command}:имя>>; "
                f"указана метка <<{label}>>."
            )

        self.label_warnings.append(
            ContentMapLabelWarning(
                source=source_name,
                line=line_number,
                message=message,
            )
        )
    
    def _raise_parse_error(
        self,
        source_path: Path,
        text: str,
        position: int,
        message: str,
    ) -> None:
        """Возбуждает исключение с указанием файла и строки."""
        line_number = text.count("\n", 0, position) + 1
        raise ContentMapError(
            f"{source_path}:{line_number}: {message}"
        )

    @staticmethod
    def _line_number(source_path: Path, position: int) -> int:
        """Возвращает приблизительный номер строки для структурной ошибки."""
        try:
            text = source_path.read_text(encoding="utf-8")
        except OSError:
            return 0

        return text.count("\n", 0, position) + 1


# =============================================================================
# Формирование Markdown-карты
# =============================================================================

def append_map_heading(
    lines: List[str],
    level: int,
    title: str,
    blank_lines: int,
) -> None:
    """Добавляет Markdown-заголовок с заданным числом пустых строк перед ним."""
    if lines:
        lines.extend([""] * blank_lines)

    lines.append(f"{'#' * level} {title}")


def append_map_block(
    lines: List[str],
    block: ContentMapBlock,
) -> None:
    """Добавляет в Markdown-карту описание одного блока."""
    if block.label:
        lines.append(f"- {block.label}")
    else:
        lines.append(f"- {block.kind}: [без метки]")

    if block.title:
        lines.append(f"    > {block.title}")

    if block.optional_mark:
        lines.append(f"    > [* {block.optional_mark}]")


def render_content_map(document: ContentMapDocument) -> str:
    """Преобразует дерево карты содержания в Markdown."""
    lines = [f"# {CONTENT_MAP_TITLE}"]

    for block in document.blocks:
        append_map_block(lines, block)

    for chapter in document.chapters:
        chapter_title = chapter.title

        if chapter.number:
            chapter_title = f"{chapter.number}. {chapter_title}"

        append_map_heading(
            lines,
            level=2,
            title=chapter_title,
            blank_lines=3,
        )

        for block in chapter.blocks:
            append_map_block(lines, block)

        for section in chapter.sections:
            section_title = section.title

            if section.number:
                section_title = f"{section.number}. {section_title}"

            append_map_heading(
                lines,
                level=3,
                title=section_title,
                blank_lines=2,
            )

            for block in section.blocks:
                append_map_block(lines, block)

            for subsection in section.subsections:
                append_map_heading(
                    lines,
                    level=4,
                    title=subsection.title,
                    blank_lines=1,
                )

                for block in subsection.blocks:
                    append_map_block(lines, block)

    return "\n".join(lines) + "\n"


def build_content_map() -> Tuple[int, List[ContentMapLabelWarning]]:
    """Строит карту содержания и возвращает число блоков и предупреждения о метках."""
    parser = ContentMapParser(PROJECT_DIR)
    document = parser.parse(TEX_PATH)
    content = render_content_map(document)

    temporary_path = CONTENT_MAP_PATH.with_suffix(
        CONTENT_MAP_PATH.suffix + ".tmp"
    )

    try:
        with temporary_path.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as file:
            file.write(content)
        temporary_path.replace(CONTENT_MAP_PATH)
    except OSError as error:
        try:
            if temporary_path.exists():
                temporary_path.unlink()
        except OSError:
            pass

        raise ContentMapError(
            f"не удалось записать файл {CONTENT_MAP_PATH}: {error}"
        ) from error

    return parser.block_count, parser.label_warnings


def print_content_map_label_warnings(
    warnings: List[ContentMapLabelWarning],
) -> None:
    """Выводит предупреждения о некорректных метках блоков."""
    if not warnings:
        return

    print(
        c(
            RED + BOLD,
            f"  Предупреждения о метках блоков ({len(warnings)}):",
        )
    )

    for warning in warnings:
        print(
            c(
                RED,
                (
                    f"  ▸ {warning.source}:{warning.line}: "
                    f"{warning.message}"
                ),
            )
        )

    print()


def update_content_map() -> bool:
    """Формирует карту содержания и выводит сведения о результате."""
    try:
        block_count, label_warnings = build_content_map()
    except ContentMapError as error:
        print(c(RED + BOLD, "  Не удалось сформировать карту содержания."))
        print(c(RED, f"  Причина: {error}\n"))

        notify(
            "LaTeX: ошибка карты содержания",
            "Не удалось сформировать map.md",
            success=False,
        )
        return False

    print(
        c(
            GREEN,
            (
                f"  Карта содержания обновлена: "
                f"{CONTENT_MAP_PATH.relative_to(PROJECT_DIR)} "
                f"(блоков: {block_count})"
            ),
        )
    )
    print()
    print_content_map_label_warnings(label_warnings)
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
        "--map",
        dest="map_only",
        action="store_true",
        help="сформировать карту содержания и завершить работу без сборки",
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
    """Выполняет очистку или сборку книги и карты."""
    args = parse_arguments()

    if IS_TTY:
        os.system("clear" if os.name != "nt" else "cls")

    header(BUILD_TITLE)

    # При флаге --clean выполняется только очистка.
    if args.clean:
        clean_aux()
        return 0

    # При флаге --map выполняется только формирование карты содержания.
    if args.map_only:
        if not update_content_map():
            return 1

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

    if not update_content_map():
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