import argparse
import json
import subprocess
import sys
import locale
from datetime import datetime
from pathlib import Path


ES_EXE = Path(__file__).with_name("es.exe")
DEFAULT_MAX_DISPLAY = 64
PROGRESS_STEP = 1000


HELP_TEXT = r"""
快速比对两个文件夹内容一致性 / Fast folder consistency comparison

说明 / Description:
  使用 Everything CLI(es.exe) 通过文件系统索引获取文件列表。
  默认只比对“相对路径 + 文件名”，不读取文件内容，不计算哈希。

  Uses Everything CLI(es.exe) to get file lists from filesystem index.
  By default, only compares "relative path + filename".
  It does not read file contents or calculate hashes.

用法 / Usage:
  python compare_folders.py <FolderA> <FolderB> [options]

示例 / Examples:
  python compare_folders.py "E:\备份\家庭照片备份" "D:\Home_NetDisk\各种备份\家庭照片备份"

  python compare_folders.py "E:\A" "D:\B" -s

  python compare_folders.py "E:\A" "D:\B" -s -t

  python compare_folders.py "E:\A" "D:\B" -t --time-tolerance 5

选项 / Options:
  -h, -help, --help
      显示帮助信息。
      Show this help message.

  -s
      校验文件大小。
      Compare file size.

  -t
      校验修改时间。
      Compare last modified time.

  --time-tolerance SECONDS
      修改时间容差，单位秒，默认 2 秒。
      Time tolerance in seconds. Default: 2 seconds.

  --max-display N
      每个明细分组最多显示 N 条，默认 64 条。设为 0 表示不显示明细。
      Maximum displayed entries per detail group. Default: 64. Use 0 to hide details.

  --report-dir DIR
      完整 TXT/JSON 报告保存目录，默认当前目录。
      Directory for full TXT/JSON reports. Default: current directory.

  --debug
      输出 Everything 查询调试信息。
      Show Everything query debug information.

结果解释 / Result meaning:
  仅 A 存在 / Only in A:
      A 文件夹中存在，但 B 文件夹中不存在。

  仅 B 存在 / Only in B:
      B 文件夹中存在，但 A 文件夹中不存在。

  大小不同 / Size mismatch:
      相对路径相同，但文件大小不同。需要使用 -s。

  修改时间不同 / Modified time mismatch:
      相对路径相同，但修改时间超出容差。需要使用 -t。

注意 / Notes:
  1. 本工具不计算哈希，因此不能证明文件内容 100% 相同。
  2. -s 和 -t 只检查元数据，不读取文件内容。
  3. 每个明细分组默认最多显示 64 条；完整明细会保存到本地 TXT 和 JSON 报告。
  4. 盘符根目录请使用 G: 或 G:\，脚本会自动规范化为 G:\。
  5. 如果需要绝对严谨校验，应在筛出可疑文件后再单独计算哈希。
  6. 请确保 Everything 已经索引目标磁盘。
"""


def normalize_folder_arg(folder: str) -> Path:
    text = str(folder).strip().strip('"')
    if len(text) == 2 and text[1] == ":" and text[0].isalpha():
        text += "\\"
    return Path(text).resolve()


def decode_es_output(raw: bytes) -> str:
    encodings = [
        "utf-8-sig",
        locale.getpreferredencoding(False),
        "mbcs",
        "gbk",
        "cp936",
        "utf-8",
    ]

    tried = set()

    for enc in encodings:
        if not enc or enc.lower() in tried:
            continue

        tried.add(enc.lower())

        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue

        if "�" not in text:
            return text

    return raw.decode(locale.getpreferredencoding(False), errors="replace")


def normalize_relpath(path: Path) -> str:
    """
    Windows 默认大小写不敏感，因此使用 casefold。
    """
    return str(path).replace("/", "\\").casefold()


def get_file_meta(path: Path, check_size: bool, check_time: bool):
    """
    只读取文件元数据，不读取文件内容。
    """
    size = None
    mtime_ns = None

    if not check_size and not check_time:
        return size, mtime_ns

    try:
        st = path.stat()
    except OSError:
        return size, mtime_ns

    if check_size:
        size = st.st_size

    if check_time:
        mtime_ns = st.st_mtime_ns

    return size, mtime_ns


def print_progress(message: str):
    print(f"\r{message}", end="", flush=True)


def finish_progress():
    print()


def query_everything_files(folder: str, label: str, check_size=False, check_time=False, debug=False):
    """
    使用 Everything 索引查询某目录下的所有文件。

    -path <path> : 限定目录
    /a-d         : 只返回文件，排除目录
    """
    base = normalize_folder_arg(folder)

    if not ES_EXE.exists():
        raise FileNotFoundError(f"找不到 es.exe: {ES_EXE}")

    cmd = [
        str(ES_EXE),
        "-path", str(base),
        "/a-d",
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout = decode_es_output(result.stdout)
    stderr = decode_es_output(result.stderr)

    if result.returncode != 0:
        raise RuntimeError(
            "es.exe 查询失败 / es.exe query failed:\n"
            f"命令 / Command: {' '.join(cmd)}\n"
            f"STDOUT:\n{stdout}\n"
            f"STDERR:\n{stderr}"
        )

    files = {}
    total_lines = 0
    skipped_not_under_base = 0
    duplicated_paths = 0

    for line in stdout.splitlines():
        line = line.strip().strip('"')
        if not line:
            continue

        total_lines += 1
        if total_lines == 1 or total_lines % PROGRESS_STEP == 0:
            print_progress(f"统计 {label} / Counting {label}: {total_lines} files")

        p = Path(line)

        try:
            rel = p.relative_to(base)
        except ValueError:
            skipped_not_under_base += 1
            continue

        rel_key = normalize_relpath(rel)

        if rel_key in files:
            duplicated_paths += 1
            continue

        size, mtime_ns = get_file_meta(p, check_size, check_time)

        files[rel_key] = {
            "path": p,
            "relative": rel,
            "size": size,
            "mtime_ns": mtime_ns,
        }

    print_progress(f"统计 {label} 完成 / Counting {label} done: {len(files)} files")
    finish_progress()

    if debug:
        print("========== Everything 调试信息 / Everything Debug ==========")
        print(f"目录 / Folder: {base}")
        print(f"命令 / Command: {' '.join(cmd)}")
        print(f"Everything 输出行数 / Output lines: {total_lines}")
        print(f"通过过滤的文件数 / Accepted files: {len(files)}")
        print(f"被过滤掉的行数 / Skipped lines: {skipped_not_under_base}")
        print(f"重复路径数量 / Duplicated paths: {duplicated_paths}")
        print()

    return files


def format_time_ns(ns):
    if ns is None:
        return "N/A"

    return datetime.fromtimestamp(ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")


def file_record(files, rel):
    item = files[rel]
    return {
        "relative": rel,
        "path": str(item["path"]),
        "size": item["size"],
        "mtime": format_time_ns(item["mtime_ns"]),
        "mtime_ns": item["mtime_ns"],
    }


def build_report_data(folder_a, folder_b, a, b, common, only_a, only_b, size_mismatch, time_mismatch, check_size, check_time, time_tolerance):
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "folder_a": str(normalize_folder_arg(folder_a)),
        "folder_b": str(normalize_folder_arg(folder_b)),
        "options": {
            "check_size": check_size,
            "check_time": check_time,
            "time_tolerance_seconds": time_tolerance,
        },
        "summary": {
            "files_in_a": len(a),
            "files_in_b": len(b),
            "common_relative_paths": len(common),
            "only_in_a": len(only_a),
            "only_in_b": len(only_b),
            "size_mismatches": len(size_mismatch),
            "time_mismatches": len(time_mismatch),
        },
        "only_in_a": [file_record(a, rel) for rel in only_a],
        "only_in_b": [file_record(b, rel) for rel in only_b],
        "size_mismatches": [
            {
                "relative": rel,
                "a": file_record(a, rel),
                "b": file_record(b, rel),
            }
            for rel in size_mismatch
        ],
        "time_mismatches": [
            {
                "relative": rel,
                "a": file_record(a, rel),
                "b": file_record(b, rel),
            }
            for rel in time_mismatch
        ],
    }


def append_txt_group(lines, title, entries, formatter):
    lines.append(title)
    if entries:
        for item in entries:
            lines.append(formatter(item))
    else:
        lines.append("(none)")
    lines.append("")


def report_to_text(report):
    summary = report["summary"]
    lines = [
        "========== 完整文件夹比对报告 / Full Folder Comparison Report ==========",
        f"生成时间 / Generated at: {report['generated_at']}",
        f"A: {report['folder_a']}",
        f"B: {report['folder_b']}",
        f"检查大小 / Check size: {report['options']['check_size']}",
        f"检查修改时间 / Check time: {report['options']['check_time']}",
        f"修改时间容差秒 / Time tolerance seconds: {report['options']['time_tolerance_seconds']}",
        "",
        "========== 汇总 / Summary ==========",
        f"A 文件夹文件数 / Files in A: {summary['files_in_a']}",
        f"B 文件夹文件数 / Files in B: {summary['files_in_b']}",
        f"共同相对路径数量 / Common relative paths: {summary['common_relative_paths']}",
        f"仅 A 存在数量 / Only in A: {summary['only_in_a']}",
        f"仅 B 存在数量 / Only in B: {summary['only_in_b']}",
        f"大小不同数量 / Size mismatches: {summary['size_mismatches']}",
        f"修改时间不同数量 / Time mismatches: {summary['time_mismatches']}",
        "",
    ]

    append_txt_group(
        lines,
        "========== 仅 A 存在 / Only in A ==========" ,
        report["only_in_a"],
        lambda item: f"{item['relative']} | path={item['path']} | size={item['size']} | mtime={item['mtime']}",
    )
    append_txt_group(
        lines,
        "========== 仅 B 存在 / Only in B ==========" ,
        report["only_in_b"],
        lambda item: f"{item['relative']} | path={item['path']} | size={item['size']} | mtime={item['mtime']}",
    )
    append_txt_group(
        lines,
        "========== 大小不同 / Size Mismatch ==========" ,
        report["size_mismatches"],
        lambda item: f"{item['relative']} | A={item['a']['size']} bytes | B={item['b']['size']} bytes",
    )
    append_txt_group(
        lines,
        "========== 修改时间不同 / Modified Time Mismatch ==========" ,
        report["time_mismatches"],
        lambda item: f"{item['relative']} | A={item['a']['mtime']} | B={item['b']['mtime']}",
    )

    return "\n".join(lines) + "\n"


def save_full_reports(report, report_dir: Path):
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = report_dir / f"compare_report_{timestamp}.txt"
    json_path = report_dir / f"compare_report_{timestamp}.json"

    txt_path.write_text(report_to_text(report), encoding="utf-8-sig")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return txt_path, json_path


def print_limited_group(title: str, entries, max_display: int, formatter=str):
    if not entries:
        return

    total = len(entries)
    shown = entries[:max_display] if max_display > 0 else []

    print(title)
    for item in shown:
        print(formatter(item))

    hidden = total - len(shown)
    if hidden > 0:
        print(f"... 还有 {hidden} 条未显示 / {hidden} more entries hidden")
    print(f"显示 {len(shown)}/{total} 条 / Showing {len(shown)}/{total} entries")
    print()


def compare_folders(folder_a, folder_b, check_size=False, check_time=False, time_tolerance=2, max_display=DEFAULT_MAX_DISPLAY, report_dir=Path("."), debug=False):
    a = query_everything_files(
        folder_a,
        label="A",
        check_size=check_size,
        check_time=check_time,
        debug=debug,
    )

    b = query_everything_files(
        folder_b,
        label="B",
        check_size=check_size,
        check_time=check_time,
        debug=debug,
    )

    keys_a = set(a.keys())
    keys_b = set(b.keys())

    only_a = sorted(keys_a - keys_b)
    only_b = sorted(keys_b - keys_a)
    common = sorted(keys_a & keys_b)

    size_mismatch = []
    time_mismatch = []

    tolerance_ns = int(time_tolerance * 1_000_000_000)

    for index, rel in enumerate(common, start=1):
        if index == 1 or index % PROGRESS_STEP == 0 or index == len(common):
            print_progress(f"比对 / Comparing: {index} files")

        fa = a[rel]
        fb = b[rel]

        if check_size:
            if fa["size"] is None or fb["size"] is None or fa["size"] != fb["size"]:
                size_mismatch.append(rel)

        if check_time:
            ta = fa["mtime_ns"]
            tb = fb["mtime_ns"]

            if ta is None or tb is None or abs(ta - tb) > tolerance_ns:
                time_mismatch.append(rel)

    if common:
        finish_progress()

    report = build_report_data(
        folder_a,
        folder_b,
        a,
        b,
        common,
        only_a,
        only_b,
        size_mismatch,
        time_mismatch,
        check_size,
        check_time,
        time_tolerance,
    )
    txt_report, json_report = save_full_reports(report, Path(report_dir))

    print("========== 对比结果 / Comparison Result ==========")
    print(f"A 文件夹文件数 / Files in A: {len(a)}")
    print(f"B 文件夹文件数 / Files in B: {len(b)}")
    print(f"共同相对路径数量 / Common relative paths: {len(common)}")
    print(f"仅 A 存在数量 / Only in A: {len(only_a)}")
    print(f"仅 B 存在数量 / Only in B: {len(only_b)}")
    print(f"每组最多显示 / Max displayed per group: {max_display}")
    print(f"完整 TXT 报告 / Full TXT report: {txt_report}")
    print(f"完整 JSON 报告 / Full JSON report: {json_report}")

    if check_size:
        print(f"大小不同数量 / Size mismatches: {len(size_mismatch)}")

    if check_time:
        print(f"修改时间不同数量 / Time mismatches: {len(time_mismatch)}")
        print(f"修改时间容差 / Time tolerance: {time_tolerance} 秒 / seconds")

    print()

    if len(a) == 0:
        print("警告：A 文件查询结果为 0。/ Warning: A returned 0 files.")
        print()

    if len(b) == 0:
        print("警告：B 文件查询结果为 0。/ Warning: B returned 0 files.")
        print()

    print_limited_group("========== 仅 A 存在 / Only in A ==========", only_a, max_display)
    print_limited_group("========== 仅 B 存在 / Only in B ==========", only_b, max_display)

    if check_size:
        print_limited_group(
            "========== 大小不同 / Size Mismatch ==========",
            size_mismatch,
            max_display,
            lambda rel: (
                f"{rel} | "
                f"A={a[rel]['size']} bytes | "
                f"B={b[rel]['size']} bytes"
            ),
        )

    if check_time:
        print_limited_group(
            "========== 修改时间不同 / Modified Time Mismatch ==========",
            time_mismatch,
            max_display,
            lambda rel: (
                f"{rel} | "
                f"A={format_time_ns(a[rel]['mtime_ns'])} | "
                f"B={format_time_ns(b[rel]['mtime_ns'])}"
            ),
        )

    if not only_a and not only_b and not size_mismatch and not time_mismatch and len(a) > 0 and len(b) > 0:
        if check_size and check_time:
            print("相对路径 + 文件名 + 大小 + 修改时间一致。")
            print("Relative path + filename + size + modified time are consistent.")
        elif check_size:
            print("相对路径 + 文件名 + 大小一致。")
            print("Relative path + filename + size are consistent.")
        elif check_time:
            print("相对路径 + 文件名 + 修改时间一致。")
            print("Relative path + filename + modified time are consistent.")
        else:
            print("相对路径 + 文件名一致。")
            print("Relative path + filename are consistent.")

    if len(a) == 0 and len(b) == 0:
        print("不能判定一致：两个目录都返回 0 个文件。")
        print("Cannot determine consistency: both folders returned 0 files.")


def parse_args():
    parser = argparse.ArgumentParser(
        add_help=False,
        formatter_class=argparse.RawTextHelpFormatter,
        usage="python compare_folders.py <FolderA> <FolderB> [options]",
    )

    parser.add_argument("folder_a", nargs="?")
    parser.add_argument("folder_b", nargs="?")

    parser.add_argument("-h", "-help", "--help", action="store_true", dest="help")
    parser.add_argument("-s", action="store_true", dest="check_size")
    parser.add_argument("-t", action="store_true", dest="check_time")
    parser.add_argument("--time-tolerance", type=float, default=2.0)
    parser.add_argument("--max-display", type=int, default=DEFAULT_MAX_DISPLAY)
    parser.add_argument("--report-dir", type=str, default=".")
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    if args.help:
        print(HELP_TEXT)
        sys.exit(0)

    if args.max_display < 0:
        print("--max-display 必须大于等于 0 / --max-display must be >= 0", file=sys.stderr)
        sys.exit(2)

    if not args.folder_a or not args.folder_b:
        print(HELP_TEXT)
        sys.exit(1)

    return args


def main():
    args = parse_args()

    compare_folders(
        args.folder_a,
        args.folder_b,
        check_size=args.check_size,
        check_time=args.check_time,
        time_tolerance=args.time_tolerance,
        max_display=args.max_display,
        report_dir=Path(args.report_dir),
        debug=args.debug,
    )


main()