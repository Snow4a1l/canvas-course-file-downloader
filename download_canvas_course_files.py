#!/usr/bin/env python3
"""通过可见浏览器登录，下载任意 Canvas 课程“文件”区域中的全部文件。

双击同目录下的“一键下载Canvas课程文件.bat”即可使用。第一次运行会：
1. 自动安装 Playwright Python 包（若尚未安装）；
2. 打开独立的 Edge 窗口，等待用户完成学校 SSO 登录；
3. 保留登录状态，读取课程文件列表并下载全部可访问文件。

本脚本不会读取日常浏览器资料，不需要 Canvas Access Token，也不会保存密码。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_COURSE_URL = "https://hkust-gz.instructure.com/courses/3885/files"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_RETRIES = 5
DEFAULT_LOGIN_TIMEOUT_SECONDS = 15 * 60


class CanvasError(RuntimeError):
    """可直接展示给用户的错误。"""


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跨域重定向时移除会话信息，避免把 Canvas Cookie 发给对象存储。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new_host = urllib.parse.urlsplit(newurl).netloc.lower()
        if old_host != new_host:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
            redirected.remove_header("Referer")
        return redirected


OPENER = urllib.request.build_opener(SafeRedirectHandler())


def ensure_playwright() -> Any:
    """导入 Playwright；缺失时自动安装。"""
    try:
        return importlib.import_module("playwright.sync_api")
    except ImportError:
        print("首次运行：正在安装浏览器自动化组件 Playwright……")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "playwright"],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CanvasError(
                "Playwright 自动安装失败。请联网后运行：\n"
                f'"{sys.executable}" -m pip install playwright'
            ) from exc
        importlib.invalidate_caches()
        try:
            return importlib.import_module("playwright.sync_api")
        except ImportError as exc:
            raise CanvasError("Playwright 已安装，但当前 Python 无法导入它。") from exc


def parse_course_url(course_url: str) -> tuple[str, str, str]:
    raw = course_url.strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CanvasError(f"课程网址无效：{course_url}")
    match = re.search(r"/courses/(\d+)(?:/|$)", parsed.path)
    if not match:
        raise CanvasError("网址中找不到 /courses/<课程ID>，请粘贴课程页面网址。")
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    course_id = match.group(1)
    files_url = f"{base_url}/courses/{course_id}/files"
    return base_url, course_id, files_url


def prompt_course_url(default: str) -> str:
    print("请输入 Canvas 课程网址（课程主页或 Files 页面均可）。")
    entered = input(f"课程网址 [{default}]：").strip()
    return entered or default


def default_profile_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "CanvasCourseDownloader" / "browser_profile"
    return Path(__file__).resolve().parent / ".canvas_browser_profile"


def launch_browser(playwright: Any, profile_dir: Path) -> Any:
    """优先使用系统 Edge/Chrome；均不可用时安装 Playwright Chromium。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "accept_downloads": False,
        "viewport": {"width": 1280, "height": 850},
        "locale": "zh-CN",
    }
    errors: list[str] = []
    for channel, label in (("msedge", "Microsoft Edge"), ("chrome", "Google Chrome")):
        try:
            print(f"正在打开 {label} 登录窗口……")
            return playwright.chromium.launch_persistent_context(channel=channel, **options)
        except Exception as exc:  # Playwright 的异常类型在运行时导入
            errors.append(f"{label}: {exc}")

    print("未能调用系统 Edge/Chrome，正在安装兼容的 Chromium（仅首次需要）……")
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        return playwright.chromium.launch_persistent_context(**options)
    except Exception as exc:
        errors.append(f"Chromium: {exc}")
        detail = "\n".join(errors[-3:])
        raise CanvasError(
            "无法启动登录浏览器。请关闭之前由本工具打开的浏览器窗口后重试。\n"
            f"详细信息：\n{detail}"
        ) from exc


def request_json_once(context: Any, url: str, *, timeout_seconds: int) -> tuple[int, Any, Any]:
    response = context.request.get(
        url,
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        timeout=timeout_seconds * 1000,
        fail_on_status_code=False,
    )
    try:
        content_type = response.headers.get("content-type", "").lower()
        data = response.json() if "json" in content_type else None
        return response.status, data, response.headers
    finally:
        response.dispose()


def wait_for_login(
    context: Any,
    page: Any,
    *,
    base_url: str,
    course_files_url: str,
    login_timeout_seconds: int,
) -> dict[str, Any]:
    try:
        page.goto(course_files_url, wait_until="domcontentloaded", timeout=120_000)
    except Exception:
        # SSO 页面可能持续重定向；窗口已打开即可继续等待。
        pass
    print("\n请在打开的浏览器窗口中完成学校登录。")
    print("脚本会自动识别登录成功，无需复制 Cookie，也无需按回车。")
    deadline = time.monotonic() + login_timeout_seconds
    next_notice = 0.0
    self_url = f"{base_url}/api/v1/users/self"
    while time.monotonic() < deadline:
        try:
            status, payload, _ = request_json_once(context, self_url, timeout_seconds=15)
            if status == 200 and isinstance(payload, dict) and payload.get("id"):
                print(f"已登录：{payload.get('name') or payload.get('short_name') or 'Canvas 用户'}")
                try:
                    if page.url != course_files_url:
                        page.goto(course_files_url, wait_until="domcontentloaded", timeout=120_000)
                except Exception:
                    pass
                return payload
        except Exception:
            pass
        now = time.monotonic()
        if now >= next_notice:
            remaining = max(0, int(deadline - now))
            print(f"等待登录中……剩余约 {remaining // 60} 分钟")
            next_notice = now + 15
        time.sleep(2)
    raise CanvasError("等待登录超时。请重新运行脚本，并在浏览器中完成学校 SSO 登录。")


def retry_delay(headers: Any, attempt: int) -> float:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw:
        try:
            return min(max(float(raw), 0.0), 60.0)
        except ValueError:
            pass
    return min(2**attempt, 30)


def browser_api_get(
    context: Any,
    url: str,
    *,
    timeout: int,
    retries: int,
) -> tuple[Any, Any]:
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            status, payload, headers = request_json_once(context, url, timeout_seconds=timeout)
            if status == 200:
                if payload is None:
                    raise CanvasError(f"Canvas 没有返回 JSON，登录状态可能已失效：{url}")
                return payload, headers
            if status in {429, 500, 502, 503, 504} and attempt < retries:
                delay = retry_delay(headers, attempt)
                print(f"  Canvas 暂时不可用（HTTP {status}），{delay:g} 秒后重试……")
                time.sleep(delay)
                continue
            if status == 401:
                raise CanvasError("登录状态已失效（HTTP 401），请重新运行并登录。")
            if status == 403:
                raise CanvasError("没有访问这个课程或其文件的权限（HTTP 403）。")
            if status == 404:
                raise CanvasError("课程不存在或当前账号看不到该课程（HTTP 404）。")
            raise CanvasError(f"Canvas 返回 HTTP {status}：{url}")
        except CanvasError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            delay = retry_delay(None, attempt)
            print(f"  请求异常，{delay:g} 秒后重试：{exc}")
            time.sleep(delay)
    raise CanvasError(f"读取 Canvas 数据失败：{last_error}")


def next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?([^";]+)"?', part)
        if match and match.group(2).strip() == "next":
            return match.group(1)
    return None


def api_get_all_pages(
    context: Any,
    url: str,
    *,
    timeout: int,
    retries: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_url: str | None = url
    while page_url:
        payload, headers = browser_api_get(context, page_url, timeout=timeout, retries=retries)
        if not isinstance(payload, list):
            raise CanvasError(f"分页接口返回了意外的数据格式：{page_url}")
        items.extend(payload)
        page_url = next_link(headers.get("link"))
    return items


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str, *, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    cleaned = cleaned or fallback
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > 180:
        suffix = Path(cleaned).suffix
        stem = cleaned[: max(1, 160 - len(suffix))]
        digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:10]
        cleaned = f"{stem}__{digest}{suffix}"
    return cleaned


def build_folder_paths(folders: Iterable[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    by_id = {str(folder["id"]): folder for folder in folders if "id" in folder}
    cache: dict[str, tuple[str, ...]] = {}

    def resolve(folder_id: str) -> tuple[str, ...]:
        if folder_id in cache:
            return cache[folder_id]
        parts: list[str] = []
        seen: set[str] = set()
        current_id: str | None = folder_id
        while current_id and current_id in by_id:
            if current_id in seen:
                raise CanvasError(f"检测到循环文件夹关系：folder_id={folder_id}")
            seen.add(current_id)
            folder = by_id[current_id]
            parent = folder.get("parent_folder_id")
            if parent is not None:  # 根文件夹本身不写入本地路径
                parts.append(safe_name(str(folder.get("name") or current_id), fallback=current_id))
            current_id = str(parent) if parent is not None else None
        result = tuple(reversed(parts))
        cache[folder_id] = result
        return result

    for fid in by_id:
        resolve(fid)
    return cache


def with_file_id(name: str, file_id: str) -> str:
    path = Path(name)
    if path.suffix:
        return f"{path.stem}__id_{file_id}{path.suffix}"
    return f"{name}__id_{file_id}"


def assign_relative_paths(
    files: list[dict[str, Any]], folder_paths: dict[str, tuple[str, ...]]
) -> dict[str, Path]:
    provisional: dict[str, Path] = {}
    groups: dict[str, list[str]] = {}
    for item in files:
        file_id = str(item.get("id", "unknown"))
        folder_id = str(item.get("folder_id", ""))
        folder_parts = folder_paths.get(folder_id, (f"unknown_folder_{folder_id or 'none'}",))
        raw_name = str(item.get("display_name") or item.get("filename") or file_id)
        filename = safe_name(raw_name, fallback=f"file_{file_id}")
        rel_path = Path(*folder_parts, filename)
        provisional[file_id] = rel_path
        groups.setdefault(str(rel_path).casefold(), []).append(file_id)
    assigned = dict(provisional)
    for file_ids in groups.values():
        if len(file_ids) > 1:
            for file_id in file_ids:
                rel_path = provisional[file_id]
                assigned[file_id] = rel_path.with_name(with_file_id(rel_path.name, file_id))
    return assigned


def same_origin(url: str, base_url: str) -> bool:
    return urllib.parse.urlsplit(url).netloc.lower() == urllib.parse.urlsplit(base_url).netloc.lower()


def canvas_cookie_header(context: Any, base_url: str) -> str:
    cookies = context.cookies([base_url])
    return "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value") is not None
    )


def open_with_retry(request: urllib.request.Request, *, timeout: int, retries: int):
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return OPENER.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                raise
            delay = retry_delay(exc.headers, attempt)
            print(f"  下载暂时失败（HTTP {exc.code}），{delay:g} 秒后重试……")
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            delay = retry_delay(None, attempt)
            print(f"  网络异常，{delay:g} 秒后重试：{exc}")
            time.sleep(delay)
    raise CanvasError(f"下载请求失败：{last_error}")


def friendly_download_error(exc: urllib.error.HTTPError) -> CanvasError:
    if exc.code in {401, 403}:
        return CanvasError(f"下载被拒绝（HTTP {exc.code}）：登录可能已过期，或文件尚未解锁。")
    if exc.code == 404:
        return CanvasError("文件不存在（HTTP 404），可能已被教师移动或删除。")
    return CanvasError(f"下载返回 HTTP {exc.code}: {exc.reason}")


def download_one(
    item: dict[str, Any],
    destination: Path,
    *,
    context: Any,
    base_url: str,
    course_files_url: str,
    timeout: int,
    retries: int,
    overwrite: bool,
) -> str:
    expected_size = item.get("size")
    if (
        not overwrite
        and destination.is_file()
        and isinstance(expected_size, int)
        and destination.stat().st_size == expected_size
    ):
        return "skipped"
    url = item.get("url")
    if not isinstance(url, str) or not url:
        raise CanvasError("文件记录中缺少下载 URL。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(destination.name + ".part")
    headers = {
        "User-Agent": "Mozilla/5.0 CanvasCourseDownloader/2.0",
        "Referer": course_files_url,
    }
    if same_origin(url, base_url):
        cookie_header = canvas_cookie_header(context, base_url)
        if cookie_header:
            headers["Cookie"] = cookie_header
    request = urllib.request.Request(url, headers=headers)
    try:
        with open_with_retry(request, timeout=timeout, retries=retries) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            expected_type = str(item.get("content-type") or "").lower()
            if "text/html" in content_type and expected_type and "text/html" not in expected_type:
                raise CanvasError("下载地址返回了登录网页，请重新运行并登录。")
            with part_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise friendly_download_error(exc) from exc
    actual_size = part_path.stat().st_size
    if isinstance(expected_size, int) and actual_size != expected_size:
        raise CanvasError(f"文件大小不符：应为 {expected_size} 字节，实际为 {actual_size} 字节")
    os.replace(part_path, destination)
    return "downloaded"


def save_manifest(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(path.name + ".part")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="打开浏览器登录并下载任意 Canvas 课程中的全部可访问文件。"
    )
    parser.add_argument("--course-url", default=None, help="课程主页或 Files 页面网址；省略时交互输入")
    parser.add_argument("--output", type=Path, default=None, help="本地保存目录")
    parser.add_argument("--profile-dir", type=Path, default=None, help="浏览器登录状态保存目录")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT_SECONDS,
        help="等待浏览器登录的秒数",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已完整下载的文件")
    parser.add_argument("--dry-run", action="store_true", help="只生成文件清单，不下载")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    context = None
    try:
        args = parse_args(argv)
        course_url = args.course_url or prompt_course_url(DEFAULT_COURSE_URL)
        base_url, course_id, course_files_url = parse_course_url(course_url)
        if args.timeout <= 0 or args.retries < 0 or args.login_timeout <= 0:
            raise CanvasError("超时必须大于 0，重试次数不能小于 0。")
        script_dir = Path(__file__).resolve().parent
        profile_dir = (args.profile_dir or default_profile_dir()).expanduser().resolve()

        playwright_api = ensure_playwright()
        with playwright_api.sync_playwright() as playwright:
            context = launch_browser(playwright, profile_dir)
            page = context.pages[0] if context.pages else context.new_page()
            user = wait_for_login(
                context,
                page,
                base_url=base_url,
                course_files_url=course_files_url,
                login_timeout_seconds=args.login_timeout,
            )
            course, _ = browser_api_get(
                context,
                f"{base_url}/api/v1/courses/{course_id}",
                timeout=args.timeout,
                retries=args.retries,
            )
            if not isinstance(course, dict):
                raise CanvasError("Canvas 返回了意外的课程信息格式。")
            raw_course_code = str(
                course.get("course_code") or course.get("name") or f"course_{course_id}"
            )
            course_folder_name = safe_name(raw_course_code, fallback=f"course_{course_id}")
            output_root = (
                args.output or script_dir / "downloads" / course_folder_name
            ).expanduser().resolve()
            output_root.mkdir(parents=True, exist_ok=True)
            print(f"课程：{course.get('name') or raw_course_code}")
            print(f"本地课程文件夹：{course_folder_name}")

            print(f"读取课程 {course_id} 的文件夹……")
            folders = api_get_all_pages(
                context,
                f"{base_url}/api/v1/courses/{course_id}/folders?per_page=100",
                timeout=args.timeout,
                retries=args.retries,
            )
            folder_paths = build_folder_paths(folders)
            print(f"读取课程 {course_id} 的文件列表……")
            files = api_get_all_pages(
                context,
                f"{base_url}/api/v1/courses/{course_id}/files?per_page=100",
                timeout=args.timeout,
                retries=args.retries,
            )
            relative_paths = assign_relative_paths(files, folder_paths)
            print(f"共找到 {len(files)} 个当前账号可访问的文件。")

            results: list[dict[str, Any]] = []
            downloaded = skipped = failed = 0
            for index, item in enumerate(files, start=1):
                file_id = str(item.get("id", "unknown"))
                rel_path = relative_paths[file_id]
                destination = output_root / rel_path
                size = item.get("size")
                size_text = f"，{size / 1024 / 1024:.1f} MiB" if isinstance(size, int) else ""
                print(f"[{index}/{len(files)}] {rel_path}{size_text}")
                record = {
                    "id": item.get("id"),
                    "canvas_display_name": item.get("display_name"),
                    "relative_path": str(rel_path),
                    "size": size,
                    "updated_at": item.get("updated_at"),
                    "status": "planned" if args.dry_run else None,
                    "error": None,
                }
                if args.dry_run:
                    results.append(record)
                    continue
                try:
                    status = download_one(
                        item,
                        destination,
                        context=context,
                        base_url=base_url,
                        course_files_url=course_files_url,
                        timeout=args.timeout,
                        retries=args.retries,
                        overwrite=args.overwrite,
                    )
                    record["status"] = status
                    if status == "downloaded":
                        downloaded += 1
                    else:
                        skipped += 1
                except (CanvasError, OSError, urllib.error.URLError) as exc:
                    failed += 1
                    record["status"] = "failed"
                    record["error"] = str(exc)
                    print(f"  失败：{exc}", file=sys.stderr)
                results.append(record)

            manifest = {
                "course_url": course_files_url,
                "course_id": course_id,
                "course_code": course.get("course_code"),
                "course_name": course.get("name"),
                "canvas_user": {
                    "id": user.get("id"),
                    "name": user.get("name") or user.get("short_name"),
                },
                "output_directory": str(output_root),
                "dry_run": args.dry_run,
                "summary": {
                    "total": len(files),
                    "downloaded": downloaded,
                    "skipped": skipped,
                    "failed": failed,
                },
                "files": results,
            }
            manifest_path = output_root / "canvas_download_manifest.json"
            save_manifest(manifest_path, manifest)
            if args.dry_run:
                print(f"预览完成。清单：{manifest_path}")
            else:
                print(
                    f"\n完成：下载 {downloaded}，跳过 {skipped}，失败 {failed}。\n"
                    f"保存目录：{output_root}\n清单：{manifest_path}"
                )
            context.close()
            context = None
            return 2 if failed else 0
    except CanvasError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中止。已完成的文件会保留，下次运行将自动跳过。", file=sys.stderr)
        return 130
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
