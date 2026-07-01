#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import argparse

def hash_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(8*1024*1024):
            h.update(chunk)
    return h.hexdigest()

def scan_files(root):
    files = []
    for r, _, fs in os.walk(root):
        for f in fs:
            p = Path(r)/f
            if p.is_file():
                files.append(p)
    return files

def yn_prompt(msg):
    while True:
        x = input(f"{msg} [Y/N]: ").strip().lower()
        if x == 'y':
            return True
        if x == 'n':
            return False
        print("请输入 Y 或 N")

def main():
    parser = argparse.ArgumentParser(
        description="重复文件检测删除工具（多线程）"
    )
    parser.add_argument("--threads", type=int, default=os.cpu_count()*2,
                        help="哈希线程数（默认=CPU×2）")
    parser.add_argument("--dry", action="store_true",
                        help="演练模式（不真正删除）")
    args = parser.parse_args()

    root = Path(".").resolve()
    print(f"扫描目录: {root}")

    files = scan_files(root)

    print(f"共发现文件: {len(files)}")

    # 1️⃣ 按大小分组
    size_map = defaultdict(list)
    for f in files:
        try:
            size_map[f.stat().st_size].append(f)
        except:
            pass

    # 只保留可能重复的
    candidates = [v for v in size_map.values() if len(v) > 1]

    if not candidates:
        print("没有发现重复候选")
        return

    print("开始哈希比对...")

    hash_map = defaultdict(list)

    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futures = {}
        for group in candidates:
            for f in group:
                futures[ex.submit(hash_file, f)] = f

        for fu in as_completed(futures):
            f = futures[fu]
            try:
                h = fu.result()
                hash_map[h].append(f)
            except:
                pass

    dup = [v for v in hash_map.values() if len(v) > 1]

    if not dup:
        print("没有重复文件")
        return

    print("\n发现重复文件组:\n")

    for group in dup:
        print("----")
        for f in group:
            print(f)

        if yn_prompt("是否删除重复文件？(保留第一个)"):
            keep = group[0]
            for f in group[1:]:
                if args.dry:
                    print(f"[演练] 将删除: {f}")
                else:
                    f.unlink()
                    print(f"已删除: {f}")
        else:
            print("跳过（冗余不足）")

    print("\n完成")

if __name__ == "__main__":
    main()