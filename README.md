# 文件校验和比对工具

这个项目包含几个用于**快速检查文件有效性**和**比对目录文件列表一致性**的小工具。工具默认尽量采用轻量检查方式：优先读取文件头、容器目录或元数据，避免对大量文件做完整解码或哈希计算，从而降低磁盘 I/O 和 CPU 压力。

## 文件说明

| 文件 | 功能 | 适用场景 |
| --- | --- | --- |
| `check_files_validity.py` | 批量检查图片、视频、代码/文本、Office OOXML、压缩包等文件是否明显损坏 | 扫描目录，快速找出坏图、坏视频、无法解码文本、异常压缩包等 |
| `compare_folders.py` | 使用 Everything CLI（`es.exe`）快速比对两个目录的相对路径/文件名，可选比较大小和修改时间，并限制明细显示条数 | 对比备份目录、迁移目录、网盘同步目录是否缺文件 |
| `es.exe` | Everything 命令行工具，由 `compare_folders.py` 调用 | Windows 上依赖 Everything 索引来快速枚举文件 |

## 环境要求

- Python 3.9+（脚本使用了 `tuple[...]` 类型标注）。
- Windows 用户可直接使用项目内的 `es.exe` 配合 `compare_folders.py`。
- 使用 `check_files_validity.py` 检查视频时，需要系统 `PATH` 中存在 `ffprobe`；启用 `--deep-video` 时还需要 `ffmpeg`。
- 使用 `compare_folders.py` 前，请确保 Everything 已安装、正在运行，并且目标磁盘/目录已经被索引。

## `check_files_validity.py`：文件有效性检查

### 支持的文件类型

- 图片：`.jpg`、`.jpeg`、`.png`
- 视频：`.mp4`、`.mov`、`.mkv`、`.avi`、`.flv`、`.wmv`、`.webm` 等常见视频格式
- 代码/文本：`.py`、`.js`、`.ts`、`.html`、`.css`、`.json`、`.xml`、`.yaml`、`.md`、`.txt` 等
- Office Open XML：`.docx`、`.xlsx`、`.pptx`
- 压缩包：`.zip`、`.jar`、`.apk`、`.odt`、`.ods`、`.odp`、`.tar`、`.gz`、`.bz2`、`.xz`、`.7z`、`.rar` 等

### 检查方式概览

- JPEG：检查 SOI/EOI 标记并尝试读取尺寸。
- PNG：默认检查签名和 IHDR；使用 `--deep-png` 时会扫描 chunk 并校验 CRC。
- 视频：默认调用 `ffprobe` 读取容器和流信息；使用 `--deep-video` 时调用 `ffmpeg` 额外解码前 1 秒。
- 文本/代码：尝试用 UTF-8、UTF-8 BOM、GB18030、UTF-16 等编码解码，并检查异常控制字符比例。
- Office OOXML：对 `.docx/.xlsx/.pptx` 做轻量 ZIP 容器和关键 XML 入口检查，不完整解压。
- 压缩包：ZIP 类读取 central directory；TAR 读取成员表；GZIP/BZ2/XZ 只解压少量样本；7z/RAR 只做魔数检查。

### 基本用法

```bash
python check_files_validity.py <目录>
```

递归检查子目录：

```bash
python check_files_validity.py -r <目录>
```

只显示异常文件：

```bash
python check_files_validity.py --only-bad -r <目录>
```

导出 CSV 和 JSON 报告：

```bash
python check_files_validity.py -r <目录> --csv result.csv --json result.json
```

提高并发数（SSD 可适当提高，机械硬盘建议较低）：

```bash
python check_files_validity.py -r <目录> --workers 4
```

深度检查 PNG 和视频：

```bash
python check_files_validity.py -r <目录> --deep-png --deep-video
```

限制文本文件最大完整解码大小（单位 MB）：

```bash
python check_files_validity.py -r <目录> --max-text-mb 128
```

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `-r`, `--recursive` | 递归检查子目录 |
| `--workers N` | 并发线程数，默认 `2` |
| `--timeout N` | 单个视频检查超时时间，默认 `20` 秒 |
| `--deep-video` | 用 `ffmpeg` 解码视频前 1 秒，检查更严格但更慢 |
| `--deep-png` | 完整扫描 PNG chunk 并校验 CRC，检查更严格但会读取完整 PNG |
| `--max-text-mb N` | 文本/代码文件最大完整解码大小，默认 `64` MB |
| `--only-bad` | 控制台只显示异常文件 |
| `--csv PATH` | 导出 CSV 报告 |
| `--json PATH` | 导出 JSON 报告 |

### 退出码

- `0`：没有发现异常，或命令正常完成且无坏文件。
- `1`：发现异常文件。
- `2`：输入目录不存在或不是目录。

## `compare_folders.py`：目录一致性比对

`compare_folders.py` 通过项目中的 `es.exe` 调用 Everything 索引来快速获取文件列表。默认只比较两个目录中的**相对路径 + 文件名**，不读取文件内容，也不计算哈希。

### 基本用法

```bash
python compare_folders.py <FolderA> <FolderB>
```

示例：

```bash
python compare_folders.py "E:\备份\家庭照片备份" "D:\Home_NetDisk\各种备份\家庭照片备份"
```

同时检查文件大小：

```bash
python compare_folders.py "E:\A" "D:\B" -s
```

同时检查修改时间：

```bash
python compare_folders.py "E:\A" "D:\B" -t
```

同时检查大小和修改时间：

```bash
python compare_folders.py "E:\A" "D:\B" -s -t
```

设置修改时间容差为 5 秒：

```bash
python compare_folders.py "E:\A" "D:\B" -t --time-tolerance 5
```

限制每个异常分组最多显示 20 条明细（默认 64 条）：

```bash
python compare_folders.py "E:\A" "D:\B" -s -t --max-display 20
```

只看最终报表，不显示各分组明细：

```bash
python compare_folders.py "E:\A" "D:\B" -s -t --max-display 0
```

指定完整 TXT/JSON 报告保存目录（默认当前目录）：

```bash
python compare_folders.py "E:\A" "D:\B" -s -t --report-dir reports
```

输出 Everything 查询调试信息：

```bash
python compare_folders.py "E:\A" "D:\B" --debug
```

查看帮助：

```bash
python compare_folders.py -h
```

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `-h`, `-help`, `--help` | 显示帮助信息 |
| `-s` | 比较文件大小 |
| `-t` | 比较修改时间 |
| `--time-tolerance SECONDS` | 修改时间容差，默认 `2` 秒 |
| `--max-display N` | 每个明细分组最多显示 `N` 条，默认 `64` 条；设为 `0` 表示只看报表不显示明细 |
| `--report-dir DIR` | 完整 TXT/JSON 报告保存目录，默认当前目录 |
| `--debug` | 输出 Everything 查询调试信息 |

### 结果解释

- `Only in A` / `仅 A 存在`：只在 A 目录中发现，B 目录没有对应相对路径。
- `Only in B` / `仅 B 存在`：只在 B 目录中发现，A 目录没有对应相对路径。
- `Size mismatch` / `大小不同`：相对路径相同，但文件大小不同，需要使用 `-s` 才会检查。
- `Modified time mismatch` / `修改时间不同`：相对路径相同，但修改时间差超过容差，需要使用 `-t` 才会检查。
- `Metadata unavailable` / `元数据读取失败`：相对路径相同，但脚本无法读取至少一侧文件的大小/修改时间；这通常是 Everything 索引里的路径已经失效，或路径字符在当前 Windows 代码页下无法正确还原。此类条目不会再被误算为 `Size mismatch` 或 `Modified time mismatch`。

> 注意：这个脚本不计算哈希，因此不能证明文件内容 100% 完全一致。如果需要严格校验，建议先用本工具筛出可疑文件，再对可疑文件单独计算哈希。运行时会用单行刷新显示已统计/已比对的文件数量；执行完成后会输出控制台摘要，并在本地保存完整 TXT/JSON 报告，控制台明细默认每组最多显示 64 条。直接传入 `G:`、`E:` 这类盘符时，脚本会自动按盘符根目录（例如 `G:\`、`E:\`）处理。

## 常见问题

### 为什么 `.docx/.xlsx/.pptx` 看起来像 ZIP？

现代 Office 文件（`.docx/.xlsx/.pptx`）本质上是 Office Open XML（OOXML）容器，底层确实使用 ZIP 结构保存多个 XML 和资源文件。因此工具会用 ZIP 容器方式做轻量校验，但会把它们归类为 `office`，并检查对应的关键入口文件。

### 为什么视频错误里有中文路径乱码？

视频检查依赖外部的 `ffprobe/ffmpeg`。这些工具在 Windows 控制台下输出编码可能受系统代码页影响。脚本会优先使用 UTF-8 解码，再回退到 GB18030/CP936，以尽量减少中文路径乱码。

### 为什么有些检查只是“粗略检查”？

项目目标是快速扫描大量文件并发现明显损坏。完整解码或完整哈希会显著增加耗时和磁盘读取量。因此默认使用轻量策略；如果需要更严格检查，可以对 PNG 使用 `--deep-png`，对视频使用 `--deep-video`，或对筛出的可疑文件再做专项校验。
