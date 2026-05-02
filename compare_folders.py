import subprocess
import sys
from pathlib import Path
from collections import Counter


ES_EXE = Path(__file__).with_name("es.exe")


def query_everything(folder: str):
    """
    使用 Everything 索引查询某文件夹下的所有文件。
    默认只返回文件，不返回文件夹。
    """
    folder = str(Path(folder).resolve())

    if not ES_EXE.exists():
        raise FileNotFoundError(f"找不到 {ES_EXE}，请把 es.exe 放到脚本同目录")

    # Everything 查询语法：
    #   path:"D:\xxx"   限定路径
    #   file:           只搜索文件
    #
    # -n 0 表示不限制结果数量
    # -path-column 显示完整路径
    cmd = [
        str(ES_EXE),
        "-n", "0",
        "-path-column",
        f'path:"{folder}"',
        "file:"
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Everything 查询失败：\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

    files = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        # es.exe 的 -path-column 输出通常是完整路径
        p = Path(line)

        # 防御性过滤，避免 Everything 查询语法边界导致扫到外面
        try:
            p.relative_to(folder)
        except ValueError:
            continue

        files.append(p)

    return files


def normalize_name(name: str, case_insensitive=True):
    """
    Windows 文件名默认大小写不敏感，因此默认 casefold。
    """
    return name.casefold() if case_insensitive else name


def compare_by_filename(folder_a: str, folder_b: str):
    files_a = query_everything(folder_a)
    files_b = query_everything(folder_b)

    base_a = Path(folder_a).resolve()
    base_b = Path(folder_b).resolve()

    names_a = Counter(normalize_name(str(p.relative_to(base_a))) for p in files_a)
    names_b = Counter(normalize_name(str(p.relative_to(base_b))) for p in files_b)

    only_a = names_a - names_b
    only_b = names_b - names_a

    common = names_a & names_b

    duplicate_a = {k: v for k, v in names_a.items() if v > 1}
    duplicate_b = {k: v for k, v in names_b.items() if v > 1}

    print("========== 对比结果 ==========")
    print(f"A 文件夹文件数: {sum(names_a.values())}")
    print(f"B 文件夹文件数: {sum(names_b.values())}")
    print(f"共同文件名数量: {sum(common.values())}")
    print(f"仅 A 存在的文件名数量: {sum(only_a.values())}")
    print(f"仅 B 存在的文件名数量: {sum(only_b.values())}")
    print()

    if duplicate_a:
        print("========== A 中重复文件名 ==========")
        for name, count in sorted(duplicate_a.items()):
            print(f"{name}  x{count}")
        print()

    if duplicate_b:
        print("========== B 中重复文件名 ==========")
        for name, count in sorted(duplicate_b.items()):
            print(f"{name}  x{count}")
        print()

    if only_a:
        print("========== 仅 A 存在 ==========")
        for name, count in sorted(only_a.items()):
            print(f"{name}" if count == 1 else f"{name}  x{count}")
        print()

    if only_b:
        print("========== 仅 B 存在 ==========")
        for name, count in sorted(only_b.items()):
            print(f"{name}" if count == 1 else f"{name}  x{count}")
        print()

    if not only_a and not only_b:
        print("文件名集合一致。")
        if duplicate_a or duplicate_b:
            print("但注意：存在重复文件名，建议进一步按相对路径或大小比对。")


def main():
    if len(sys.argv) != 3:
        print('用法: python compare_folders.py "D:\\FolderA" "E:\\FolderB"')
        sys.exit(1)

    folder_a = sys.argv[1]
    folder_b = sys.argv[2]

    compare_by_filename(folder_a, folder_b)


main()