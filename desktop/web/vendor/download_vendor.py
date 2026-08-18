#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载思维导图的本地依赖：仅需 markmap-autoloader 单文件。

markmap-autoloader 是官方自包含 bundle（内置 d3 + markmap-lib + markmap-view），
因此只下载这 1 个文件即可实现思维导图的离线渲染。

使用方式（任选一种）：
    # 项目根目录
    cd E:\\编程项目开发\\录音卡
    python record\\web\\vendor\\download_vendor.py

    # 在 record 目录
    cd E:\\编程项目开发\\录音卡\\record
    python web\\vendor\\download_vendor.py
"""
import os
import sys
import urllib.request
import ssl

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

VENDOR_DIR = os.path.dirname(os.path.abspath(__file__))

# 只需要下载 markmap-autoloader（自包含：d3 + Transformer + Markmap）
FILES = {
    "markmap-autoloader.js": [
        {
            "pkg_ver": [
                ("markmap-autoloader", "0.18.12"),
                ("markmap-autoloader", "0.17.6"),
                ("markmap-autoloader", "0.16.3"),
            ],
            # 不同版本发布的主文件名不一致，都尝试
            "subpaths": [
                "/dist/autoloader.js",
                "/dist/index.umd.js",
                "/dist/index.js",
            ],
        },
    ],
}

CDN_BASES = [
    # 国内镜像优先（更稳定）
    "https://registry.npmmirror.com/{pkg}/{ver}/files",
    "https://cdn.jsdmirror.com/npm/{pkg}@{ver}",
    # 海外 CDN
    "https://cdn.jsdelivr.net/npm/{pkg}@{ver}",
    "https://unpkg.com/{pkg}@{ver}",
    "https://testingcf.jsdelivr.net/npm/{pkg}@{ver}",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _fetch(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=35, context=_CTX) as resp:
            data = resp.read()
        return data if len(data) > 100 else None
    except Exception:
        return None


def download_one(fname: str, specs: list) -> bool:
    dst = os.path.join(VENDOR_DIR, fname)
    if os.path.exists(dst) and os.path.getsize(dst) > 5000:
        print(f"[SKIP] {fname} 已存在 ({os.path.getsize(dst)} bytes)")
        return True
    for spec in specs:
        for pkg, ver in spec["pkg_ver"]:
            for sub in spec["subpaths"]:
                for cdn in CDN_BASES:
                    base = cdn.format(pkg=pkg, ver=ver)
                    url = base + sub
                    print(f"[TRY] {pkg}@{ver}  {url}")
                    data = _fetch(url)
                    if not data:
                        print(f"      [x] 失败")
                        continue
                    with open(dst, "wb") as f:
                        f.write(data)
                    print(f"      [OK] {len(data)} bytes -> {dst}")
                    return True
    print(f"[ERROR] 无法下载 {fname}，请检查网络或手动下载。")
    return False


def main():
    ok = 0
    total = len(FILES)
    for fname, specs in FILES.items():
        if download_one(fname, specs):
            ok += 1
    print(f"\n完成：{ok}/{total} 个 vendor 文件就绪。")
    if ok < total:
        print(
            "提示：若全部 CDN 失败，可手动复制以下文件到 vendor/ 目录并重命名为 markmap-autoloader.js：\n"
            "   - https://cdn.jsdelivr.net/npm/markmap-autoloader@0.18.12/dist/autoloader.js"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
