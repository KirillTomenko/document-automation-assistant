"""Restricted paths and fresh byte copies for the offline backup format."""

import os
import re
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath


class BackupError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def relative_path(value):
    if (
        not isinstance(value, str)
        or not value
        or value == "."
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or any(c in value for c in ':<>"|?*')
    ):
        raise BackupError("unsafe_path", "Недопустимый путь в копии.")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise BackupError(
            "unsafe_path", "Путь должен быть безопасным относительным именем."
        )
    for part in path.parts:
        if (
            part in (".", "..")
            or part.endswith((".", " "))
            or re.fullmatch(
                r"(?i)(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?",
                part,
            )
        ):
            raise BackupError("unsafe_path", "Недопустимый компонент пути в копии.")
    return path


def _reparse(path, info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def directory(path):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if _reparse(part, info) or not stat.S_ISDIR(info.st_mode):
            raise BackupError(
                "unsafe_path",
                "Каталог не должен содержать символические ссылки или junctions.",
            )
    if path.resolve() != path:
        raise BackupError(
            "unsafe_path", "Каталог должен иметь однозначный локальный путь."
        )
    return path


def new_target(value):
    path = Path(os.path.abspath(value))
    directory(path.parent)
    if os.path.lexists(path):
        raise BackupError(
            "target_exists",
            "Укажите новый ещё не существующий каталог; существующие каталоги не изменяются.",
        )
    if any(p.casefold() == ".git" for p in path.parts):
        raise BackupError(
            "unsafe_path", "Каталог Git не является местом хранения данных."
        )
    return path


def regular(path):
    info = path.lstat()
    if _reparse(path, info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError(
            "unsafe_file",
            "Допускаются только обычные файлы без символических и жёстких ссылок.",
        )
    return info


def file_path(root, name):
    path = root.joinpath(*relative_path(name).parts)
    directory(path.parent)
    regular(path)
    return path


def checksum(path):
    info = regular(path)
    digest, size = sha256(), 0
    with path.open("rb") as file:
        opened = os.fstat(file.fileno())
        if (info.st_dev, info.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or opened.st_nlink != 1:
            raise BackupError("file_changed", "Файл изменился во время проверки.")
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    if size != info.st_size:
        raise BackupError("file_changed", "Размер файла изменился во время проверки.")
    return size, digest.hexdigest()


def copy_file(source, target):
    info = regular(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    directory(target.parent)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        opened = os.fstat(incoming.fileno())
        if (info.st_dev, info.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or opened.st_nlink != 1:
            raise BackupError(
                "file_changed", "Исходный файл изменился во время копирования."
            )
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    regular(target)


def check_tree(root, expected):
    expected = set(expected)
    expected_dirs = {
        str(parent)
        for name in expected
        for parent in relative_path(name).parents
        if str(parent) != "."
    }
    files, dirs = set(), set()

    def visit(path):
        directory(path)
        for child in path.iterdir():
            info = child.lstat()
            if _reparse(child, info):
                raise BackupError("unsafe_file", "Ссылки в резервной копии запрещены.")
            name = child.relative_to(root).as_posix()
            relative_path(name)
            if stat.S_ISDIR(info.st_mode):
                if name not in expected_dirs:
                    raise BackupError(
                        "file_inventory_mismatch",
                        "Обнаружен незарегистрированный каталог.",
                    )
                dirs.add(name)
                visit(child)
            else:
                if name not in expected:
                    raise BackupError(
                        "file_inventory_mismatch",
                        "Обнаружен незарегистрированный файл.",
                    )
                regular(child)
                files.add(name)

    visit(root)
    if files != expected or dirs != expected_dirs:
        raise BackupError(
            "file_inventory_mismatch",
            "Состав файлов или каталогов не соответствует зарегистрированным данным.",
        )


def publish(stage, target):
    # No overwrite: NTFS rename refuses an existing target. Linux uses the
    # equivalent RENAME_NOREPLACE; unsupported platforms stop safely.
    if os.path.lexists(target):
        raise BackupError(
            "target_exists",
            "Целевой каталог появился во время операции; он не изменён.",
        )
    if os.name == "nt":
        os.rename(stage, target)
    else:
        import ctypes

        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise BackupError(
                "publication_unsupported",
                "Безопасная публикация каталога на этой платформе не поддерживается.",
            )
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1):
            raise OSError(ctypes.get_errno(), "Directory publication failed")
