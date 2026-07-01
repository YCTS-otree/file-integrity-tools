#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import shutil
import hashlib
import argparse
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

BUF_SIZE = 8 * 1024 * 1024  # 8MB
MAX_THREADS = 4

def human_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    x = float(n)
    for u in units:
        if x < 1024.0:
            if u == "B":
                return f"{int(x)}{u}"
            return f"{x:.2f}{u}"
        x /= 1024.0
    return f"{x:.2f}EB"

def fmt_eta(sec: float) -> str:
    if sec != sec or sec == float("inf") or sec < 0:
        return "--:--:--"
    sec = int(sec + 0.5)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def walk_files(root: Path, follow_symlinks: bool = False):
    for dirpath, _, filenames in os.walk(root, followlinks=follow_symlinks):
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                if p.is_file():
                    yield p
            except OSError:
                continue

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(BUF_SIZE)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def scan_sizes_multithread(paths, label: str, threads: int, min_size: int):
    """
    多线程 stat，返回 size->list[Path]，并显示进度 done/total
    """
    m = defaultdict(list)
    total = len(paths)
    done = 0

    if total == 0:
        print(f"{label} 扫描进度: 0/0")
        return m

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {ex.submit(_safe_stat_size, p): p for p in paths}
        for fut in as_completed(futs):
            p = futs[fut]
            sz = fut.result()
            if sz is not None and sz >= min_size:
                m[sz].append(p)
            done += 1
            # 只显示 done/total
            if done == total or done % 200 == 0:
                print(f"\r{label} 扫描进度: {done}/{total}", end="", flush=True)
    print()
    return m

def _safe_stat_size(p: Path):
    try:
        return p.stat().st_size
    except OSError:
        return None

def copy_with_progress(src: Path, dst: Path, total_bytes: int, state):
    """
    单线程复制，流式拷贝 + 全局进度显示
    state: dict 保存 copied_bytes, start_time, last_t, last_b, smooth_speed, current_rel
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 再次确认重名：若目标已存在则跳过
    if dst.exists():
        state["skipped_name"] += 1
        return False

    if state["dry_run"]:
        # 演练：按文件大小推进进度
        file_size = src.stat().st_size
        state["copied_bytes"] += file_size
        _print_copy_progress(total_bytes, state, done_file=True, rel=state["current_rel"])
        return True

    copied_this_file = 0
    with src.open("rb") as fsrc, dst.open("wb") as fdst:
        while True:
            chunk = fsrc.read(BUF_SIZE)
            if not chunk:
                break
            fdst.write(chunk)
            copied_this_file += len(chunk)
            state["copied_bytes"] += len(chunk)
            _print_copy_progress(total_bytes, state, done_file=False, rel=state["current_rel"])

    # 尽量保留时间戳/权限等
    try:
        shutil.copystat(src, dst, follow_symlinks=False)
    except Exception:
        pass

    _print_copy_progress(total_bytes, state, done_file=True, rel=state["current_rel"])
    return True

def _print_copy_progress(total_bytes: int, state, done_file: bool, rel: str):
    now = time.monotonic()
    copied = state["copied_bytes"]

    # 限制刷新频率，避免刷屏
    if (now - state["last_print"]) < 0.25 and not done_file:
        return

    dt = now - state["last_t"]
    db = copied - state["last_b"]
    inst_speed = (db / dt) if dt > 0 else 0.0

    if state["smooth_speed"] is None:
        smooth = inst_speed
    else:
        # 平滑一下，否则速度会抖得像电调没调参（你懂的）
        smooth = state["smooth_speed"] * 0.85 + inst_speed * 0.15

    state["smooth_speed"] = max(smooth, 1e-6)
    state["last_t"] = now
    state["last_b"] = copied
    state["last_print"] = now

    pct = (copied / total_bytes * 100.0) if total_bytes > 0 else 100.0
    remain = total_bytes - copied
    eta = remain / state["smooth_speed"] if state["smooth_speed"] > 0 else float("inf")

    line = (
        f"\r复制进度: {pct:6.2f}% | "
        f"速度: {human_bytes(state['smooth_speed'])}/s | "
        f"剩余: {fmt_eta(eta)} | "
        f"当前: {rel}"
    )
    print(line, end="", flush=True)

    if done_file:
        print()  # 文件完成换行

def main():
    ap = argparse.ArgumentParser(
        description="源(s.) -> 目标(t.) 复制工具：先按size过滤再hash去重；重名跳过；复制单线程显示速度/剩余时间（扫描多线程最多4）"
    )
    ap.add_argument("src", help="源目录（s.）")
    ap.add_argument("dst", help="目标目录（t.）")
    ap.add_argument("--dry-run", action="store_true", help="演练模式：不真正复制，只打印进度与统计")
    ap.add_argument("--follow-symlinks", action="store_true", help="遍历时跟随符号链接（默认不跟随）")
    ap.add_argument("--min-size", type=int, default=1, help="忽略小于该字节数的文件（默认=1）")
    ap.add_argument("--threads", type=int, default=4, help="扫描/哈希线程数（最大4，默认4）")
    args = ap.parse_args()

    threads = max(1, min(MAX_THREADS, int(args.threads)))

    src_root = Path(args.src).resolve()
    dst_root = Path(args.dst).resolve()

    if not src_root.exists():
        print(f"源目录不存在: {src_root}")
        return

    if not dst_root.exists():
        print(f"目标目录不存在，将创建: {dst_root}")
        if not args.dry_run:
            dst_root.mkdir(parents=True, exist_ok=True)

    print(f"\n源(s.)  : {src_root}")
    print(f"目标(t.): {dst_root}")
    print(f"扫描线程: {threads}（上限 {MAX_THREADS}）")
    print(f"模式    : {'演练(不落盘)' if args.dry_run else '执行(会复制)'}\n")

    # 1) 先收集路径（walk 本身不算你要的“扫描进度”，所以我把进度放在 stat/哈希阶段）
    dst_paths = list(walk_files(dst_root, follow_symlinks=args.follow_symlinks))
    src_paths = list(walk_files(src_root, follow_symlinks=args.follow_symlinks))

    # 2) 多线程扫描 size（显示 done/total）
    dst_by_size = scan_sizes_multithread(dst_paths, "目标(t.)", threads, args.min_size)

    # 源这边不建 size->list（我们要保留相对路径）
    # 用多线程扫 size，但结果做成 records 列表
    print("源(s.) 扫描进度: 0/{}".format(len(src_paths)) if src_paths else "源(s.) 扫描进度: 0/0")
    src_records = []
    total = len(src_paths)
    done = 0
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {ex.submit(_safe_stat_size, p): p for p in src_paths}
        for fut in as_completed(futs):
            p = futs[fut]
            sz = fut.result()
            if sz is not None and sz >= args.min_size:
                try:
                    rel = p.relative_to(src_root)
                except Exception:
                    # 极少数情况下（奇怪的符号链接/挂载点）会失败，跳过
                    done += 1
                    if done == total or done % 200 == 0:
                        print(f"\r源(s.) 扫描进度: {done}/{total}", end="", flush=True)
                    continue
                dp = dst_root / rel
                src_records.append((p, rel, dp, sz))
            done += 1
            if done == total or done % 200 == 0:
                print(f"\r源(s.) 扫描进度: {done}/{total}", end="", flush=True)
    print("\n")

    # 3) 分类：重名跳过 / 直接复制 / 需要hash去重
    skipped_name = []
    direct_copy = []
    need_hash = []  # 只有 size 在 dst_by_size 才会进来

    for sp, rel, dp, sz in src_records:
        # 重名：目标同相对路径已存在 => 跳过
        try:
            if dp.exists():
                skipped_name.append((sp, rel, dp, sz))
                continue
        except OSError:
            # 目标路径访问异常，当作重名跳过（保守）
            skipped_name.append((sp, rel, dp, sz))
            continue

        if sz in dst_by_size:
            need_hash.append((sp, rel, dp, sz))
        else:
            direct_copy.append((sp, rel, dp, sz))

    print("初步筛选结果：")
    print(f"- 重名跳过（同相对路径目标已存在）: {len(skipped_name)}")
    print(f"- 直接复制（目标无同size文件）      : {len(direct_copy)}")
    print(f"- 需要哈希去重（目标存在同size文件）: {len(need_hash)}\n")

    # 4) 多线程 hash 去重（只对 need_hash 的源文件；目标只对相关 size 的文件）
    #    目标哈希只算“need_hash涉及到的 size”
    sizes_needed = set(sz for (_, _, _, sz) in need_hash)
    dst_hash_by_size = {}  # size -> dict(hash->Path)
    src_hash = {}          # src_path -> hash

    # 构造任务
    hash_tasks = []
    # dst tasks
    for sz in sizes_needed:
        for dp in dst_by_size.get(sz, []):
            hash_tasks.append(("dst", sz, dp))
    # src tasks
    for sp, rel, dp, sz in need_hash:
        hash_tasks.append(("src", sz, sp))

    total_hash = len(hash_tasks)
    done_hash = 0

    if total_hash > 0:
        print(f"哈希去重进度: 0/{total_hash}")
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = {}
            for kind, sz, p in hash_tasks:
                futs[ex.submit(sha256_file, p)] = (kind, sz, p)

            for fut in as_completed(futs):
                kind, sz, p = futs[fut]
                try:
                    h = fut.result()
                except Exception:
                    # hash失败：保守处理
                    h = None

                if h is not None:
                    if kind == "dst":
                        m = dst_hash_by_size.get(sz)
                        if m is None:
                            m = {}
                            dst_hash_by_size[sz] = m
                        # 只需记一个路径用于判重
                        if h not in m:
                            m[h] = p
                    else:
                        src_hash[p] = h

                done_hash += 1
                if done_hash == total_hash or done_hash % 200 == 0:
                    print(f"\r哈希去重进度: {done_hash}/{total_hash}", end="", flush=True)
        print("\n")
    else:
        print("无需进行哈希去重（没有需要比对的同size文件）。\n")

    # 5) 根据 hash 决定是否复制
    skipped_dup = []  # 目标任意位置已存在同内容
    to_copy = []      # 真正要复制的（不重名 & 不重复内容）

    # 直接复制
    to_copy.extend(direct_copy)

    # need_hash：只有源hash不在目标同size hash集合里才复制
    for sp, rel, dp, sz in need_hash:
        sh = src_hash.get(sp)
        if sh is None:
            # 源hash失败：保守做法：不复制（避免“其实重复但我没算出来”导致无意义拷贝）
            skipped_dup.append((sp, rel, dp, sz, None))
            continue
        dh = dst_hash_by_size.get(sz, {})
        if sh in dh:
            skipped_dup.append((sp, rel, dp, sz, dh[sh]))
        else:
            to_copy.append((sp, rel, dp, sz))

    print("最终计划：")
    print(f"- 将复制: {len(to_copy)}")
    print(f"- 重名跳过: {len(skipped_name)}")
    print(f"- 重复内容跳过: {len(skipped_dup)}\n")

    # 6) 单线程复制 + 详细进度（速度/百分比/ETA）
    total_bytes = sum(sz for (_, _, _, sz) in to_copy)
    print(f"总复制数据量: {human_bytes(total_bytes)}")
    if len(to_copy) == 0:
        print("没有需要复制的文件，结束。")
        return

    state = {
        "copied_bytes": 0,
        "start_time": time.monotonic(),
        "last_t": time.monotonic(),
        "last_b": 0,
        "smooth_speed": None,
        "last_print": 0.0,
        "current_rel": "",
        "dry_run": args.dry_run,
        "skipped_name": 0,
    }

    copied_files = 0
    for sp, rel, dp, sz in to_copy:
        state["current_rel"] = str(rel)
        ok = copy_with_progress(sp, dp, total_bytes, state)
        if ok:
            copied_files += 1

    print("\n=== 复制完成 ===")
    print(f"已复制文件数: {copied_files}{'（演练）' if args.dry_run else ''}")
    print(f"重名跳过数:  {len(skipped_name) + state['skipped_name']}")
    print(f"重复跳过数:  {len(skipped_dup)}")
    print(f"写入总量:    {human_bytes(state['copied_bytes'])}{'（演练）' if args.dry_run else ''}")

    # 可选：给你看少量重复跳过样例
    if skipped_dup:
        print("\n重复跳过样例（最多10条）：")
        for i, (sp, rel, dp, sz, matched) in enumerate(skipped_dup[:10], 1):
            if matched is None:
                print(f"  {i}. {rel}  [{human_bytes(sz)}]  -> hash失败，保守跳过")
            else:
                try:
                    mrel = matched.relative_to(dst_root)
                    mshow = f"(目标内: {mrel})"
                except Exception:
                    mshow = f"(目标: {matched})"
                print(f"  {i}. {rel}  [{human_bytes(sz)}]  -> 目标已存在同内容 {mshow}")

if __name__ == "__main__":
    main()