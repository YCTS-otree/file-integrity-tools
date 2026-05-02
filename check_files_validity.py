import argparse
import concurrent.futures
import csv
import json
import os
import struct
import subprocess
import sys
import tarfile
import zipfile
import gzip
import bz2
import lzma
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".webm",
    ".mpeg", ".mpg", ".m2ts", ".mts", ".ts", ".3gp", ".3g2", ".rm",
    ".rmvb", ".vob", ".ogv", ".divx", ".asf", ".f4v", ".mxf"
}

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png"
}

CODE_EXTS = {
    ".py", ".pyw", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".java", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css",
    ".scss", ".less", ".json", ".jsonc", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".md", ".txt", ".csv",
    ".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".psm1",
    ".go", ".rs", ".swift", ".kt", ".kts", ".php", ".rb", ".lua",
    ".r", ".m", ".mm", ".sql", ".v", ".sv", ".vhd", ".vhdl",
    ".asm", ".s", ".S", ".cs", ".fs", ".fsx", ".dart", ".vue",
    ".gradle", ".make", ".mk", ".cmake", ".dockerfile"
}

ARCHIVE_EXTS = {
    ".zip", ".jar", ".apk", ".odt", ".ods", ".odp",
    ".tar", ".tgz", ".tar.gz", ".gz", ".bz2", ".xz", ".lzma",
    ".7z", ".rar"
}

OFFICE_XML_EXTS = {".docx", ".xlsx", ".pptx"}


@dataclass
class CheckResult:
    path: str
    kind: str
    ok: bool
    detail: str
    size: int


def suffixes_lower(path: Path):
    return [s.lower() for s in path.suffixes]


def full_ext(path: Path) -> str:
    """
    处理 .tar.gz 这种双后缀。
    """
    suffixes = suffixes_lower(path)
    if len(suffixes) >= 2:
        two = "".join(suffixes[-2:])
        if two in {".tar.gz", ".tar.bz2", ".tar.xz"}:
            return two
    return path.suffix.lower()


def is_code_file(path: Path) -> bool:
    name = path.name.lower()

    if name in {
        "makefile", "dockerfile", "cmakelists.txt", "requirements.txt",
        "pipfile", "gemfile", "cargo.toml", "go.mod", "go.sum"
    }:
        return True

    return path.suffix.lower() in CODE_EXTS


def is_archive_file(path: Path) -> bool:
    ext = full_ext(path)
    return ext in ARCHIVE_EXTS or path.suffix.lower() in ARCHIVE_EXTS


def check_jpeg(path: Path) -> tuple[bool, str]:
    """
    浅层 JPEG 检查：
    - SOI FF D8
    - 文件末尾 EOI FF D9
    - 尝试解析 SOF marker 获取尺寸
    不完整解码，速度快，读盘少。
    """
    try:
        size = path.stat().st_size
        if size < 4:
            return False, "JPEG too small"

        with path.open("rb") as f:
            head = f.read(2)
            if head != b"\xff\xd8":
                return False, "missing JPEG SOI"

            f.seek(-2, os.SEEK_END)
            tail = f.read(2)
            if tail != b"\xff\xd9":
                return False, "missing JPEG EOI, file may be truncated"

            f.seek(2)
            width = height = None

            while f.tell() < size:
                byte = f.read(1)
                if not byte:
                    break

                if byte != b"\xff":
                    continue

                while True:
                    marker_byte = f.read(1)
                    if not marker_byte:
                        break
                    if marker_byte != b"\xff":
                        break

                if not marker_byte:
                    break

                marker = marker_byte[0]

                # standalone markers
                if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                    continue

                seg_len_bytes = f.read(2)
                if len(seg_len_bytes) != 2:
                    return False, "truncated JPEG segment length"

                seg_len = struct.unpack(">H", seg_len_bytes)[0]
                if seg_len < 2:
                    return False, "invalid JPEG segment length"

                # SOF0/SOF2 等，包含尺寸
                if marker in {
                    0xC0, 0xC1, 0xC2, 0xC3,
                    0xC5, 0xC6, 0xC7,
                    0xC9, 0xCA, 0xCB,
                    0xCD, 0xCE, 0xCF
                }:
                    data = f.read(min(seg_len - 2, 7))
                    if len(data) < 7:
                        return False, "truncated JPEG SOF segment"
                    height = struct.unpack(">H", data[1:3])[0]
                    width = struct.unpack(">H", data[3:5])[0]
                    if width <= 0 or height <= 0:
                        return False, "invalid JPEG dimensions"
                    return True, f"JPEG header OK, {width}x{height}"

                # SOS 后面是压缩数据，浅层解析到这里基本可以认为结构成立
                if marker == 0xDA:
                    if width and height:
                        return True, f"JPEG header OK, {width}x{height}"
                    return True, "JPEG header OK, reached SOS"

                f.seek(seg_len - 2, os.SEEK_CUR)

            if width and height:
                return True, f"JPEG header OK, {width}x{height}"

            return True, "JPEG SOI/EOI OK, dimensions not found"

    except Exception as e:
        return False, f"JPEG check error: {e}"


def check_png(path: Path, deep_png: bool = False) -> tuple[bool, str]:
    """
    浅层 PNG 检查：
    - 签名
    - IHDR
    - 宽高
    可选 deep_png 时会扫描 chunk 并校验 CRC，会读完整 PNG。
    """
    try:
        with path.open("rb") as f:
            sig = f.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return False, "bad PNG signature"

            raw = f.read(25)
            if len(raw) < 25:
                return False, "truncated PNG IHDR"

            length = struct.unpack(">I", raw[0:4])[0]
            ctype = raw[4:8]

            if length != 13 or ctype != b"IHDR":
                return False, "missing or invalid PNG IHDR"

            width, height = struct.unpack(">II", raw[8:16])
            if width <= 0 or height <= 0:
                return False, "invalid PNG dimensions"

            if not deep_png:
                return True, f"PNG header OK, {width}x{height}"

            import zlib

            # raw 已经包含 IHDR data + CRC
            ihdr_data = raw[8:21]
            ihdr_crc_read = struct.unpack(">I", raw[21:25])[0]
            ihdr_crc_calc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
            if ihdr_crc_read != ihdr_crc_calc:
                return False, "PNG IHDR CRC mismatch"

            seen_iend = False

            while True:
                len_bytes = f.read(4)
                if not len_bytes:
                    break
                if len(len_bytes) != 4:
                    return False, "truncated PNG chunk length"

                clen = struct.unpack(">I", len_bytes)[0]
                ctype = f.read(4)
                if len(ctype) != 4:
                    return False, "truncated PNG chunk type"

                data = f.read(clen)
                if len(data) != clen:
                    return False, f"truncated PNG chunk {ctype!r}"

                crc_bytes = f.read(4)
                if len(crc_bytes) != 4:
                    return False, f"truncated PNG CRC for chunk {ctype!r}"

                crc_read = struct.unpack(">I", crc_bytes)[0]
                crc_calc = zlib.crc32(ctype + data) & 0xFFFFFFFF
                if crc_read != crc_calc:
                    return False, f"PNG CRC mismatch in chunk {ctype.decode(errors='replace')}"

                if ctype == b"IEND":
                    seen_iend = True
                    break

            if not seen_iend:
                return False, "missing PNG IEND"

            return True, f"PNG chunks OK, {width}x{height}"

    except Exception as e:
        return False, f"PNG check error: {e}"


def check_image(path: Path, deep_png: bool = False) -> tuple[bool, str]:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return check_jpeg(path)
    if ext == ".png":
        return check_png(path, deep_png=deep_png)
    return False, "unsupported image type"


def decode_bytes_best_effort(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_cmd(args, timeout: int) -> tuple[int, str, str]:
    p = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        timeout=timeout,
        stdin=subprocess.DEVNULL
    )
    return p.returncode, decode_bytes_best_effort(p.stdout), decode_bytes_best_effort(p.stderr)


def check_video(path: Path, deep_video: bool = False, timeout: int = 20) -> tuple[bool, str]:
    """
    默认使用 ffprobe 读取容器和流信息，不完整解码。
    deep_video=True 时，用 ffmpeg 解码前 1 秒，更慢但能发现一部分码流错误。
    """
    try:
        ffprobe_cmd = [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_entries",
            "format=format_name,duration,nb_streams:stream=codec_type,codec_name,width,height,duration",
            str(path)
        ]

        code, out, err = run_cmd(ffprobe_cmd, timeout=timeout)

        if code != 0:
            msg = err.strip() or out.strip() or "ffprobe failed"
            return False, msg[:500]

        try:
            info = json.loads(out)
        except Exception:
            return False, "ffprobe returned non-json output"

        streams = info.get("streams", [])
        vstreams = [s for s in streams if s.get("codec_type") == "video"]

        if not streams:
            return False, "no media stream found"

        if not vstreams:
            return False, "no video stream found"

        v0 = vstreams[0]
        codec = v0.get("codec_name", "unknown")
        width = v0.get("width", "?")
        height = v0.get("height", "?")
        duration = info.get("format", {}).get("duration") or v0.get("duration") or "?"

        if deep_video:
            ffmpeg_cmd = [
                "ffmpeg",
                "-v", "error",
                "-xerror",
                "-nostdin",
                "-i", str(path),
                "-map", "0:v:0",
                "-t", "1",
                "-f", "null",
                "-"
            ]
            code2, out2, err2 = run_cmd(ffmpeg_cmd, timeout=timeout)
            if code2 != 0:
                msg = err2.strip() or out2.strip() or "ffmpeg decode check failed"
                return False, msg[:500]

            return True, f"video metadata OK, first 1s decode OK, {codec}, {width}x{height}, duration={duration}"

        return True, f"video metadata OK, {codec}, {width}x{height}, duration={duration}"

    except subprocess.TimeoutExpired:
        return False, f"video check timeout after {timeout}s"
    except FileNotFoundError:
        return False, "ffprobe/ffmpeg not found in PATH"
    except Exception as e:
        return False, f"video check error: {e}"


def detect_encoding_by_bom_or_nul(data: bytes) -> Optional[str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16"

    if b"\x00" in data[:4096]:
        even_nul = data[:4096:2].count(0)
        odd_nul = data[1:4096:2].count(0)
        if even_nul > 10 or odd_nul > 10:
            return "utf-16"

    return None


def check_code_text(path: Path, max_text_mb: int = 64) -> tuple[bool, str]:
    """
    代码文件检查：
    - 默认读取完整文件并尝试解码
    - 主要目标是判断“能否作为文本解析”，不是检查语法是否正确
    """
    try:
        size = path.stat().st_size
        limit = max_text_mb * 1024 * 1024

        if size > limit:
            return False, f"text file too large for full decode: {size} bytes > {limit} bytes"

        data = path.read_bytes()

        if not data:
            return True, "empty text file"

        if b"\x00" in data[:4096]:
            enc = detect_encoding_by_bom_or_nul(data)
            if not enc:
                return False, "contains NUL bytes, likely not plain text"
        else:
            enc = detect_encoding_by_bom_or_nul(data)

        encodings = []
        if enc:
            encodings.append(enc)

        encodings.extend([
            "utf-8",
            "utf-8-sig",
            "gb18030",
            "utf-16",
        ])

        tried = []
        for encoding in dict.fromkeys(encodings):
            try:
                text = data.decode(encoding)
                # 粗略检查控制字符比例
                if text:
                    bad_controls = sum(
                        1 for ch in text
                        if ord(ch) < 32 and ch not in "\r\n\t"
                    )
                    ratio = bad_controls / max(len(text), 1)
                    if ratio > 0.01:
                        return False, f"decoded as {encoding}, but too many control chars"
                return True, f"text decode OK, encoding={encoding}"
            except UnicodeDecodeError:
                tried.append(encoding)

        return False, "cannot decode as text, tried: " + ", ".join(tried)

    except Exception as e:
        return False, f"text check error: {e}"



def check_office_xml(path: Path) -> tuple[bool, str]:
    """
    Office Open XML 文件（docx/xlsx/pptx）快速检查：
    - 先判断 ZIP 容器
    - 检查关键 XML 入口是否存在
    - 避免完整解压，提高速度
    """
    try:
        low_name = path.name.lower()
        if low_name.startswith("~$"):
            return True, "office temp lock file skipped"

        if not zipfile.is_zipfile(path):
            return False, "not a valid OOXML ZIP container"

        required = {
            ".docx": ("[Content_Types].xml", "word/document.xml"),
            ".xlsx": ("[Content_Types].xml", "xl/workbook.xml"),
            ".pptx": ("[Content_Types].xml", "ppt/presentation.xml"),
        }
        ext = path.suffix.lower()
        must_have = required.get(ext, ("[Content_Types].xml",))

        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            missing = [n for n in must_have if n not in names]
            if missing:
                return False, f"OOXML container missing entries: {', '.join(missing)}"

        return True, f"OOXML container OK, entries={len(names)}"

    except Exception as e:
        return False, f"office check error: {e}"

def check_archive(path: Path) -> tuple[bool, str]:
    """
    压缩文件粗略检查：
    - zip 系：读取 central directory，不 testzip，避免完整读出所有文件
    - tar：读取成员表
    - gz/bz2/xz：尝试解压少量数据
    - 7z/rar：仅检查魔数
    """
    try:
        ext = full_ext(path)
        low_name = path.name.lower()

        if ext in {".zip", ".jar", ".apk", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
            if not zipfile.is_zipfile(path):
                return False, "not a valid ZIP container"

            with zipfile.ZipFile(path, "r") as zf:
                infos = zf.infolist()
                return True, f"ZIP central directory OK, entries={len(infos)}"

        if ext in {".tar", ".tar.gz", ".tar.bz2", ".tar.xz"}:
            with tarfile.open(path, "r:*") as tf:
                members = tf.getmembers()
                return True, f"TAR member table OK, entries={len(members)}"

        if ext == ".gz" or low_name.endswith(".tgz"):
            with gzip.open(path, "rb") as f:
                f.read(1024)
            return True, "GZIP header/decompress sample OK"

        if ext == ".bz2":
            with bz2.open(path, "rb") as f:
                f.read(1024)
            return True, "BZ2 header/decompress sample OK"

        if ext in {".xz", ".lzma"}:
            with lzma.open(path, "rb") as f:
                f.read(1024)
            return True, "XZ/LZMA header/decompress sample OK"

        if ext == ".7z":
            with path.open("rb") as f:
                magic = f.read(6)
            if magic == b"7z\xbc\xaf\x27\x1c":
                return True, "7z magic OK, rough check only"
            return False, "bad 7z magic"

        if ext == ".rar":
            with path.open("rb") as f:
                magic = f.read(8)
            if magic.startswith(b"Rar!\x1a\x07"):
                return True, "RAR magic OK, rough check only"
            return False, "bad RAR magic"

        return False, "unsupported archive type"

    except Exception as e:
        return False, f"archive check error: {e}"


def classify(path: Path) -> str:
    ext = path.suffix.lower()

    if ext in IMAGE_EXTS:
        return "image"

    if ext in VIDEO_EXTS:
        return "video"

    if ext in OFFICE_XML_EXTS:
        return "office"

    if is_archive_file(path):
        return "archive"

    if is_code_file(path):
        return "code"

    return "unknown"


def check_one(path: Path, args) -> CheckResult:
    try:
        size = path.stat().st_size
    except Exception:
        return CheckResult(str(path), "unknown", False, "cannot stat file", -1)

    kind = classify(path)

    if kind == "image":
        ok, detail = check_image(path, deep_png=args.deep_png)
    elif kind == "video":
        ok, detail = check_video(
            path,
            deep_video=args.deep_video,
            timeout=args.timeout
        )
    elif kind == "code":
        ok, detail = check_code_text(
            path,
            max_text_mb=args.max_text_mb
        )
    elif kind == "office":
        ok, detail = check_office_xml(path)
    elif kind == "archive":
        ok, detail = check_archive(path)
    else:
        ok, detail = True, "skipped unsupported file type"

    return CheckResult(str(path), kind, ok, detail, size)


def iter_files(root: Path, recursive: bool):
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in filenames:
                yield Path(dirpath) / name
    else:
        for p in root.iterdir():
            if p.is_file():
                yield p


def write_csv(results, output_path: Path):
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["path", "kind", "ok", "detail", "size"]
        )
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def write_json(results, output_path: Path):
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(
            [asdict(r) for r in results],
            f,
            ensure_ascii=False,
            indent=2
        )


def main():
    parser = argparse.ArgumentParser(
        description="Check validity of videos, jpg/png images, code text files, Office files, and archives."
    )

    parser.add_argument(
        "directory",
        help="要检查的目录"
    )

    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        help="递归检查子目录"
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="并发线程数。机械硬盘建议 1~2，SSD 可适当提高。默认 2"
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="单个视频 ffprobe/ffmpeg 超时时间，单位秒。默认 20"
    )

    parser.add_argument(
        "--deep-video",
        action="store_true",
        help="额外用 ffmpeg 解码视频前 1 秒，能发现部分码流损坏，但更慢"
    )

    parser.add_argument(
        "--deep-png",
        action="store_true",
        help="完整扫描 PNG chunk 并校验 CRC，会读取完整 PNG"
    )

    parser.add_argument(
        "--max-text-mb",
        type=int,
        default=64,
        help="代码/文本文件最大完整解码大小，单位 MB。默认 64"
    )

    parser.add_argument(
        "--only-bad",
        action="store_true",
        help="控制台只显示异常文件"
    )

    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="导出 CSV 路径"
    )

    parser.add_argument(
        "--json",
        type=str,
        default=None,
        help="导出 JSON 路径"
    )

    args = parser.parse_args()

    root = Path(args.directory)
    if not root.exists() or not root.is_dir():
        print(f"目录不存在或不是目录: {root}", file=sys.stderr)
        sys.exit(2)

    files = list(iter_files(root, args.recursive))

    if not files:
        print("没有找到文件。")
        return

    results = []
    total = len(files)
    bad_count = 0

    print(f"开始检查：{root}")
    print(f"文件数：{total}")
    print(f"并发数：{args.workers}")
    print("-" * 80)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {
            executor.submit(check_one, p, args): p
            for p in files
        }

        done = 0

        for future in concurrent.futures.as_completed(future_map):
            done += 1

            try:
                result = future.result()
            except Exception as e:
                p = future_map[future]
                result = CheckResult(str(p), "unknown", False, f"worker error: {e}", -1)

            results.append(result)

            if not result.ok:
                bad_count += 1

            if not args.only_bad or not result.ok:
                status = "OK " if result.ok else "BAD"
                print(f"[{done}/{total}] [{status}] [{result.kind}] {result.path}")
                print(f"    {result.detail}")

    print("-" * 80)
    print(f"检查完成：总计 {total} 个文件，异常 {bad_count} 个。")

    if args.csv:
        write_csv(results, Path(args.csv))
        print(f"CSV 已导出：{args.csv}")

    if args.json:
        write_json(results, Path(args.json))
        print(f"JSON 已导出：{args.json}")

    if bad_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()