"""
Сборка книги (LuaLaTeX).

Возможности:
    * По умолчанию выполняется 2 прохода (сборка + подхват ссылок/TOC).
    * При ошибке на проходе — немедленный выход без повторов.
    * Вывод lualatex скрыт; показывается индикатор прогресса
      (спиннер, текущая страница, прошедшее время).
    * Из .log извлекаются ошибки и «полезные» предупреждения:
      overfull/underfull box, неопределённые ссылки,
      отсутствующие файлы, нет глифа в шрифте, дублирующиеся метки.

Использование:
    python3 build.py [--clean] [--open] [--passes N] [--verbose]

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
from typing import List

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

TEX_FILE = "book.tex"
LUALATEX = "lualatex"
LUALATEX_ARGS = [
    "-synctex=1",
    "-interaction=nonstopmode",
    "-file-line-error",
    "-halt-on-error",
    "-shell-escape",
]
DEFAULT_PASSES = 2
WARN_SAMPLE_LIMIT = 5

AUX_EXTS = (
    ".aux", ".log", ".toc", ".out", ".synctex.gz",
    ".lof", ".lot", ".tdo", ".fls", ".fdb_latexmk",
    ".bbl", ".blg", ".nav", ".snm", ".vrb",
)

# ---------------------------------------------------------------------------
# Терминал / цвета
# ---------------------------------------------------------------------------

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, CYAN, WHITE = (
    "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[37m"
)

IS_TTY    = sys.stdout.isatty()
USE_COLOR = IS_TTY
UTF       = "utf" in (sys.stdout.encoding or "").lower()


def c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if USE_COLOR else text


OK_MARK     = c(GREEN + BOLD, "✓") if UTF else c(GREEN + BOLD, "+")
FAIL_MARK   = c(RED + BOLD,   "✗") if UTF else c(RED + BOLD,   "x")
SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   if UTF else "|/-\\"


def header(text: str) -> None:
    bar = "=" * 60
    print(f"\n{c(CYAN + BOLD, bar)}")
    print(c(CYAN + BOLD, f"  {text}"))
    print(f"{c(CYAN + BOLD, bar)}\n")


def human_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


# ---------------------------------------------------------------------------
# Уведомления ОС
# ---------------------------------------------------------------------------

def notify(title: str, message: str, success: bool) -> None:
    try:
        if sys.platform == "darwin":
            sound = "Glass" if success else "Basso"
            script = (
                f'display notification "{message}" '
                f'with title "{title}" sound name "{sound}"'
            )
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=5)
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            urgency = "normal" if success else "critical"
            subprocess.run(["notify-send", "-u", urgency, title, message],
                           capture_output=True, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Очистка вспомогательных файлов
# ---------------------------------------------------------------------------

def clean_aux() -> None:
    stem = Path(TEX_FILE).stem
    removed = 0
    for ext in AUX_EXTS:
        p = Path(f"{stem}{ext}")
        if p.exists():
            p.unlink()
            removed += 1
    print(c(DIM, f"  Удалено вспомогательных файлов: {removed}\n"))


# ---------------------------------------------------------------------------
# Разбор .log
# ---------------------------------------------------------------------------

@dataclass
class LogError:
    message: str
    file: str = ""
    line: str = ""
    context: List[str] = field(default_factory=list)


@dataclass
class LogWarning:
    kind: str
    message: str
    line: str = ""
    details: str = ""


RE_FILELINE    = re.compile(r"^(.+?\.\w+):(\d+):\s*(.+)$")
RE_BANG        = re.compile(r"^!\s+(.+)$")
RE_BOX         = re.compile(r"^(Overfull|Underfull)\s+\\([hv])box\b(.*)")
RE_BOX_AMOUNT  = re.compile(r"\(([\d.]+)pt too (\w+)\)")
RE_BOX_BADNESS = re.compile(r"\(badness\s+(\d+)\)")
RE_AT_LINES    = re.compile(r"at lines?\s+(\d+)(?:--(\d+))?")
RE_UNDEF_REF   = re.compile(
    r"(?:LaTeX|Package\s+\S+)\s+Warning:\s+"
    r"(Reference|Citation)\s+[`']([^']+)'\s+.*?undefined",
    re.IGNORECASE,
)
RE_MULTI_LABEL = re.compile(
    r"LaTeX Warning:\s+Label\s+[`']([^']+)'\s+multiply defined",
    re.IGNORECASE,
)
RE_MISS_CHAR   = re.compile(
    r"Missing character:\s+There is no\s+(\S+)\s+.+?\s+in font\s+(.+?)!?$"
)
RE_FILE_MISS   = re.compile(
    r"(?:LaTeX Error:\s+)?File\s+[`']([^']+)'\s+not found"
)

RE_PAGE = re.compile(r"\[(\d+)[\s\]\{]")


def parse_log(log_path: Path):
    if not log_path.is_file():
        return [], []

    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    errors:   List[LogError]   = []
    warnings: List[LogWarning] = []
    seen_err, seen_warn = set(), set()

    def add_warn(kind: str, message: str, line: str = "", details: str = ""):
        key = (kind, message, line, details)
        if key in seen_warn:
            return
        seen_warn.add(key)
        warnings.append(LogWarning(kind, message, line, details))

    i = 0
    while i < len(lines):
        line = lines[i]

        # ---- Ошибки ---------------------------------------------------------
        m_fl = RE_FILELINE.match(line)
        m_bg = None if m_fl else RE_BANG.match(line)
        if m_fl or m_bg:
            if m_fl:
                file_, line_no, msg = m_fl.group(1), m_fl.group(2), m_fl.group(3).strip()
            else:
                file_, line_no, msg = "", "", m_bg.group(1).strip()

            ctx = []
            j = i + 1
            while j < len(lines) and j <= i + 4:
                cl = lines[j]
                if cl.startswith("!") or RE_FILELINE.match(cl):
                    break
                ctx.append(cl)
                j += 1

            key = (file_, line_no, msg)
            if key not in seen_err:
                seen_err.add(key)
                errors.append(LogError(message=msg, file=file_,
                                       line=line_no, context=ctx))
            i = j
            continue

        # ---- Overfull / Underfull ------------------------------------------
        mbox = RE_BOX.match(line)
        if mbox:
            kind = mbox.group(1).lower()
            box  = mbox.group(2)

            # Склеим возможное продолжение сообщения
            buf = line
            k = i + 1
            while k < len(lines) and k < i + 4 and lines[k].strip():
                nxt = lines[k]
                if RE_BOX.match(nxt) or RE_FILELINE.match(nxt) or nxt.startswith("!"):
                    break
                buf += " " + nxt
                k += 1

            mal = RE_AT_LINES.search(buf)
            lr  = ""
            if mal:
                lr = mal.group(1) + (f"–{mal.group(2)}" if mal.group(2) else "")

            mam = RE_BOX_AMOUNT.search(buf)
            mbn = RE_BOX_BADNESS.search(buf)
            details = ""
            if mam:
                details = f"{mam.group(1)}pt too {mam.group(2)}"
            elif mbn:
                details = f"badness {mbn.group(1)}"

            add_warn(kind, f"\\{box}box", line=lr, details=details)
            i += 1
            continue

        # ---- Неопределённые ссылки / цитаты --------------------------------
        mu = RE_UNDEF_REF.search(line)
        if mu:
            add_warn("ref", f"{mu.group(1)} '{mu.group(2)}' не определена")
            i += 1
            continue

        # ---- Дублирующиеся метки -------------------------------------------
        md = RE_MULTI_LABEL.search(line)
        if md:
            add_warn("dup", f"Метка '{md.group(1)}' определена несколько раз")
            i += 1
            continue

        # ---- Нет глифа в шрифте --------------------------------------------
        mc = RE_MISS_CHAR.search(line)
        if mc:
            add_warn("char", f"Нет глифа {mc.group(1)} в шрифте {mc.group(2)}")
            i += 1
            continue

        # ---- Не найден файл ------------------------------------------------
        mf = RE_FILE_MISS.search(line)
        if mf:
            add_warn("file", f"Не найден файл: {mf.group(1)}")
            i += 1
            continue

        i += 1

    return errors, warnings


# ---------------------------------------------------------------------------
# Печать ошибок и предупреждений
# ---------------------------------------------------------------------------

def print_errors(errors: List[LogError]) -> None:
    bar = "=" * 60
    print(f"\n{c(RED + BOLD, bar)}")
    print(c(RED + BOLD, f"  Найдено ошибок: {len(errors)}"))
    print(f"{c(RED + BOLD, bar)}\n")

    for idx, err in enumerate(errors, 1):
        loc = ""
        if err.file:
            loc += c(CYAN, err.file)
        if err.line:
            loc += c(DIM, ":") + c(YELLOW, err.line)
        print(f"  {c(RED + BOLD, f'[{idx}]')} {loc}")
        print(f"      {c(WHITE, err.message)}")
        for cl in err.context:
            s = cl.strip()
            if s:
                print(f"      {c(DIM, s)}")
        print()


WARN_LABELS = {
    "overfull":  "Переполнение (overfull)",
    "underfull": "Недополнение (underfull)",
    "ref":       "Неопределённые ссылки",
    "dup":       "Дублирующиеся метки",
    "file":      "Отсутствующие файлы",
    "char":      "Нет глифа в шрифте",
}
WARN_ORDER = ("file", "ref", "dup", "char", "overfull", "underfull")


def print_warnings(warnings: List[LogWarning],
                   limit: int = WARN_SAMPLE_LIMIT) -> None:
    if not warnings:
        return

    groups = {}
    for w in warnings:
        groups.setdefault(w.kind, []).append(w)

    print(c(YELLOW + BOLD, f"  Предупреждения ({len(warnings)}):"))
    for kind in list(WARN_ORDER) + [k for k in groups if k not in WARN_ORDER]:
        items = groups.get(kind, [])
        if not items:
            continue
        label = WARN_LABELS.get(kind, kind.capitalize())
        print(f"  {c(YELLOW, '▸')} {c(BOLD, label)} — {len(items)}")
        for w in items[:limit]:
            tags = []
            if w.line:
                tags.append(c(CYAN, f"стр. {w.line}"))
            if w.details:
                tags.append(c(DIM, w.details))
            tail = f"  [{' · '.join(tags)}]" if tags else ""
            print(f"      {c(YELLOW, '·')} {w.message}{tail}")
        if len(items) > limit:
            print(c(DIM, f"      … ещё {len(items) - limit}"))
    print()


def print_success(elapsed: float) -> None:
    bar = "=" * 60
    print(f"\n{c(GREEN + BOLD, bar)}")
    print(c(GREEN + BOLD, f"  Сборка завершена успешно ({elapsed:.1f} с)"))
    pdf = Path(TEX_FILE).with_suffix(".pdf")
    if pdf.exists():
        print(c(GREEN, f"  PDF: {pdf}  ({human_size(pdf.stat().st_size)})"))
    print(f"{c(GREEN + BOLD, bar)}\n")


# ---------------------------------------------------------------------------
# Запуск lualatex с индикатором прогресса
# ---------------------------------------------------------------------------

def _draw_status(label: str, marker: str, page: int, elapsed: float) -> None:
    if not IS_TTY:
        return
    page_part = c(WHITE, f"стр. {page:>4}") if page else c(DIM, "стр. ----")
    time_part = c(DIM, f"{elapsed:5.1f} с")
    line = f"  {c(CYAN + BOLD, label)}  {marker}  {page_part}  {time_part}"
    sys.stdout.write("\r\033[K" + line)
    sys.stdout.flush()


def run_lualatex(pass_num: int, total: int, verbose: bool = False):
    cmd   = [LUALATEX, *LUALATEX_ARGS, TEX_FILE]
    label = f"Проход {pass_num}/{total}"
    t0    = time.time()

    # Verbose: отдаём stdout/stderr как есть.
    if verbose:
        print(f"  {c(CYAN + BOLD, label)}  {c(DIM, ' '.join(cmd))}")
        rc      = subprocess.call(cmd)
        elapsed = time.time() - t0
        mark    = OK_MARK if rc == 0 else FAIL_MARK
        print(f"  {c(CYAN + BOLD, label)}  {mark}  "
              f"{c(DIM, f'({elapsed:.1f} с)')}\n")
        return rc, elapsed, 0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        errors="replace",
    )

    state = {"page": 0, "done": False}

    def spinner_loop():
        i = 0
        while not state["done"]:
            _draw_status(label,
                         c(YELLOW, SPIN_FRAMES[i % len(SPIN_FRAMES)]),
                         state["page"], time.time() - t0)
            i += 1
            time.sleep(0.1)

    thread = None
    if IS_TTY:
        thread = threading.Thread(target=spinner_loop, daemon=True)
        thread.start()

    try:
        for raw in proc.stdout:
            for m in RE_PAGE.findall(raw):
                try:
                    p = int(m)
                    if p > state["page"]:
                        state["page"] = p
                except ValueError:
                    pass
    except KeyboardInterrupt:
        state["done"] = True
        if thread:
            thread.join(timeout=0.5)
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        if IS_TTY:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        raise

    proc.wait()
    state["done"] = True
    if thread:
        thread.join(timeout=0.5)

    elapsed = time.time() - t0
    mark    = OK_MARK if proc.returncode == 0 else FAIL_MARK
    if IS_TTY:
        _draw_status(label, mark, state["page"], elapsed)
        print()
    else:
        plain = "OK" if proc.returncode == 0 else "FAIL"
        print(f"  {label}  {plain}  стр. {state['page']}  {elapsed:.1f} с")
    return proc.returncode, elapsed, state["page"]


# ---------------------------------------------------------------------------
# Открытие PDF
# ---------------------------------------------------------------------------

def open_pdf() -> None:
    pdf = Path(TEX_FILE).with_suffix(".pdf")
    if not pdf.exists():
        return
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(pdf)], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(pdf)], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(str(pdf))  # noqa
    except Exception:
        pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка книги через LuaLaTeX")
    parser.add_argument("--clean", action="store_true",
                        help="удалить вспомогательные файлы перед сборкой")
    parser.add_argument("--open", dest="open_pdf", action="store_true",
                        help="открыть PDF после успешной сборки")
    parser.add_argument("--passes", type=int, default=DEFAULT_PASSES,
                        help=f"число проходов (по умолчанию {DEFAULT_PASSES})")
    parser.add_argument("--verbose", action="store_true",
                        help="не скрывать вывод lualatex")
    args = parser.parse_args()

    if args.passes < 1:
        print(c(RED, "  --passes должно быть >= 1"))
        return 1

    if IS_TTY:
        os.system("clear" if os.name != "nt" else "cls")
    header("Сборка книги")

    if not os.path.isfile(TEX_FILE):
        print(c(RED, f"  Файл {TEX_FILE} не найден."))
        return 1

    if shutil.which(LUALATEX) is None:
        print(c(RED, f"  Компилятор '{LUALATEX}' не найден в PATH."))
        return 1

    if args.clean:
        clean_aux()

    log_path = Path(TEX_FILE).with_suffix(".log")
    pdf_path = Path(TEX_FILE).with_suffix(".pdf")
    total_elapsed = 0.0
    warnings: List[LogWarning] = []

    for p in range(1, args.passes + 1):
        try:
            rc, elapsed, _ = run_lualatex(p, args.passes, verbose=args.verbose)
        except KeyboardInterrupt:
            print(c(YELLOW, "\n  Прервано пользователем."))
            return 130

        total_elapsed += elapsed
        errors, warnings = parse_log(log_path)

        if rc != 0 or errors:
            if not errors:
                errors = [LogError(
                    message=f"lualatex завершился с кодом {rc} "
                            f"(подробности в {log_path})",
                )]
            print_errors(errors)
            print_warnings(warnings)
            notify("LaTeX: ошибки сборки",
                   f"Найдено ошибок: {len(errors)}", success=False)
            return 2

    if not pdf_path.exists():
        print(c(RED, f"\n  PDF {pdf_path} не был создан."))
        notify("LaTeX: PDF не создан", str(pdf_path), success=False)
        return 2

    print_success(total_elapsed)
    print_warnings(warnings)

    notify("LaTeX: сборка завершена", "Всё прошло успешно", success=True)

    if args.open_pdf:
        open_pdf()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(c(YELLOW, "\n  Прервано пользователем."))
        sys.exit(130)