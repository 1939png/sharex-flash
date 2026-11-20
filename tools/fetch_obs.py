# -*- coding: utf-8 -*-
"""把 OBS Studio 官方 Windows x64 便携版下载到 third_party/obs-portable。

用法：
    python tools/fetch_obs.py            # 下载最新正式版
    python tools/fetch_obs.py --force    # 已存在也重新下载
    python tools/fetch_obs.py --tag 33.0.1

只从 obsproject/obs-studio 的 GitHub Release 取包，不做任何镜像替换。
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

API_LATEST = "https://api.github.com/repos/obsproject/obs-studio/releases/latest"
API_TAG = "https://api.github.com/repos/obsproject/obs-studio/releases/tags/%s"
UA = {"User-Agent": "sharex-flash-fetch-obs"}


def app_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def portable_dir():
    return os.path.join(app_root(), "third_party", "obs-portable")


def obs_exe():
    return os.path.join(portable_dir(), "bin", "64bit", "obs64.exe")


def _get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_asset(release):
    for asset in release.get("assets") or []:
        name = asset.get("name") or ""
        if not name.endswith("-Windows-x64.zip"):
            continue
        if "PDB" in name or "arm64" in name:
            continue
        return asset
    return None


def _download(url, dest):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100.0 / total
                sys.stdout.write("\r  下载中 %5.1f%%  %.1f/%.1f MB" % (
                    pct, done / 1048576.0, total / 1048576.0))
                sys.stdout.flush()
    sys.stdout.write("\n")


def fetch(tag="", force=False):
    if os.path.isfile(obs_exe()) and not force:
        print("已存在：%s（加 --force 可重新下载）" % obs_exe())
        return 0

    release = _get_json(API_TAG % tag if tag else API_LATEST)
    asset = _pick_asset(release)
    if asset is None:
        print("该版本没有找到 Windows x64 便携包：%s" % release.get("tag_name"))
        return 1

    name = asset.get("name")
    size = (asset.get("size") or 0) / 1048576.0
    print("目标版本：OBS Studio %s" % release.get("tag_name"))
    print("资源文件：%s（约 %.0f MB）" % (name, size))

    tmp_zip = os.path.join(tempfile.gettempdir(), "obs-portable-%s.zip" % release.get("tag_name"))
    _download(asset["browser_download_url"], tmp_zip)

    target = portable_dir()
    if os.path.isdir(target):
        shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)
    print("解压到：%s" % target)
    with zipfile.ZipFile(tmp_zip) as zf:
        zf.extractall(target)
    try:
        os.remove(tmp_zip)
    except OSError:
        pass

    if not os.path.isfile(obs_exe()):
        print("解压后未找到 bin\\64bit\\obs64.exe，请手动检查目录结构")
        return 1
    print("完成：%s" % obs_exe())
    print("下一步：直接启动 ShareX Flash，按 Ctrl+Alt+R 开始录屏。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="下载 OBS Studio 便携版")
    parser.add_argument("--tag", default="", help="指定 release tag，例如 33.0.1")
    parser.add_argument("--force", action="store_true", help="即使已存在也重新下载")
    args = parser.parse_args()
    sys.exit(fetch(args.tag, args.force))


if __name__ == "__main__":
    main()
