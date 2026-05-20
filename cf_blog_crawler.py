#!/usr/bin/env python3
"""
Codeforces Blog Crawler
批量爬取 Codeforces Blog 文章并保存为 Markdown 文件。

用法:
    python cf_blog_crawler.py urls.txt          # 从文件读取 URL 列表
    python cf_blog_crawler.py -u URL1 URL2 ...  # 直接传入 URL
    python cf_blog_crawler.py -f urls.txt -o output_dir  # 指定输出目录
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

REQUEST_TIMEOUT = 90  # 秒
DELAY_BETWEEN_REQUESTS = 5.0  # 请求间隔（秒），避免被封


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
def fetch_page(url: str, session: requests.Session = None) -> Optional[str]:
    """获取页面 HTML，失败返回 None。"""
    fetcher = session if session else requests
    try:
        resp = fetcher.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"[ERROR] 获取页面失败: {url} —— {e}", file=sys.stderr)
        return None


def fetch_tutorial_content(
    session: requests.Session, problem_code: str, csrf_token: str = None
) -> Optional[str]:
    """通过 AJAX API 获取题解 Tutorial 的真实内容。

    Codeforces 的 editorial 页面中，Tutorial 折叠块通过 JS 异步加载，
    POST 到 /data/problemTutorial 获取实际内容。
    需要携带从博客页面提取的 CSRF Token。
    """
    api_url = "https://codeforces.com/data/problemTutorial"
    ajax_headers = {
        **HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://codeforces.com",
    }
    if csrf_token:
        ajax_headers["X-Csrf-Token"] = csrf_token
    try:
        resp = session.post(
            api_url,
            data={"problemCode": problem_code},
            headers=ajax_headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("success") == "true":
            return result.get("html", "")
        else:
            print(
                f"  [WARN] Tutorial API 返回失败: {problem_code}",
                file=sys.stderr,
            )
            return None
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(
            f"  [WARN] 获取 Tutorial 失败 ({problem_code}): {e}",
            file=sys.stderr,
        )
        return None


def parse_blog(
    html: str, url: str, session: requests.Session = None
) -> Optional[dict]:
    """从 HTML 中解析 blog 元数据和正文，返回 dict 或 None。

    如果提供了 session 且页面包含 problemTutorial 占位符，
    会通过 AJAX API 获取真实的 Tutorial 内容进行替换。
    """
    soup = BeautifulSoup(html, "html.parser")

    # ---- 标题 ----
    title_tag = soup.select_one("div.title a p")
    if title_tag:
        title = title_tag.get_text(strip=True)
    else:
        # 回退：使用 <title> 标签
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True).removesuffix(" - Codeforces") if title_tag else "Unknown"

    # ---- 作者 & 日期 ----
    info_div = soup.select_one("div.info")
    author = "Unknown"
    date_str = "Unknown"

    if info_div:
        # 作者： "By ..." 后的第一个 <a> 标签
        author_link = info_div.select_one("a.rated-user, a[href*='/profile/']")
        if author_link:
            author = author_link.get_text(strip=True)

        # 日期： <span class="format-humantime">
        date_span = info_div.select_one("span.format-humantime")
        if date_span:
            date_str = date_span.get_text(strip=True)
            # 也尝试从 title 属性取精确时间
            if date_span.has_attr("title"):
                date_str = date_span["title"]

    # ---- 正文 ----
    content_div = soup.select_one("div.content")
    if not content_div:
        print(f"[ERROR] 未找到正文内容: {url}", file=sys.stderr)
        return None

    # 正文是 content 下的第一个 ttypography div
    body_div = content_div.select_one("div.ttypography")
    if not body_div:
        print(f"[ERROR] 未找到博客正文: {url}", file=sys.stderr)
        return None

    # ---- 异步加载的 Tutorial 占位符替换 ----
    tutorial_placeholders = body_div.select("div.problemTutorial[problemcode]")
    if tutorial_placeholders and session:
        # 从页面提取 CSRF Token
        csrf_meta = soup.find("meta", attrs={"name": "X-Csrf-Token"})
        csrf_token = csrf_meta.get("content", "") if csrf_meta else ""

        print(f"  检测到 {len(tutorial_placeholders)} 个 Tutorial 占位符，正在获取真实内容...")
        for placeholder in tutorial_placeholders:
            problem_code = placeholder.get("problemcode", "")
            if not problem_code:
                continue
            real_html = fetch_tutorial_content(session, problem_code, csrf_token)
            if real_html:
                # 将 API 返回的 HTML 解析后替换占位符
                real_fragment = BeautifulSoup(real_html, "html.parser")
                placeholder.clear()
                placeholder.append(real_fragment)
                print(f"    ✓ Tutorial {problem_code} 获取成功")
            else:
                # 保留占位文本，但加上标记
                placeholder.string = f"*Tutorial for {problem_code} is not available.*"
                print(f"    ✗ Tutorial {problem_code} 获取失败，保留占位")
            # 请求间隔（对 API 也适用）
            time.sleep(0.5)

    # 移除 MathJax 脚本（保留 LaTeX 源码即可）
    for script in body_div.find_all("script"):
        script.decompose()

    # 将相对路径的图片/链接补全为绝对路径
    for img in body_div.find_all("img"):
        src = img.get("src", "")
        if src.startswith("//"):
            img["src"] = "https:" + src
        elif src.startswith("/"):
            img["src"] = "https://codeforces.com" + src

    for a_tag in body_div.find_all("a"):
        href = a_tag.get("href", "")
        if href.startswith("/"):
            a_tag["href"] = "https://codeforces.com" + href

    body_html = str(body_div)

    # HTML → Markdown
    body_md = md(body_html, heading_style="ATX", bullets="-")

    # 清理多余空行
    body_md = re.sub(r"\n{3,}", "\n\n", body_md)

    # 提取 entry ID 用于文件名
    entry_id = _extract_entry_id(url)

    return {
        "entry_id": entry_id,
        "title": title,
        "author": author,
        "date": date_str,
        "url": url,
        "body_md": body_md,
    }


def _extract_entry_id(url: str) -> str:
    """从 URL 提取 entry ID，如 .../entry/153824 → 153824"""
    m = re.search(r"/entry/(\d+)", url)
    return m.group(1) if m else "unknown"


def build_markdown(blog: dict) -> str:
    """将 blog 数据组装为 Markdown 文件内容。"""
    lines = [
        f"# {blog['title']}",
        "",
        f"**作者**: {blog['author']}  ",
        f"**日期**: {blog['date']}  ",
        f"**原文链接**: [{blog['url']}]({blog['url']})  ",
        f"**Entry ID**: {blog['entry_id']}  ",
        "",
        "---",
        "",
        blog["body_md"],
    ]
    return "\n".join(lines)


def sanitize_filename(name: str) -> str:
    """将标题转为安全的文件名。"""
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = name.strip().strip(".")
    return name[:120]  # 限制长度


def crawl_urls(urls: list[str], output_dir: str) -> list[dict]:
    """批量爬取 URL 列表，保存 Markdown，返回结果列表。"""
    os.makedirs(output_dir, exist_ok=True)
    results = []

    # 使用 Session 保持 cookie，避免 Cloudflare 拦截对 /data/problemTutorial 的请求
    session = requests.Session()
    session.headers.update(HEADERS)

    for i, url in enumerate(urls, 1):
        url = url.strip()
        if not url or url.startswith("#"):
            continue

        print(f"[{i}/{len(urls)}] 正在爬取: {url}")

        html = fetch_page(url, session=session)
        if not html:
            results.append({"url": url, "success": False, "error": "fetch failed"})
            continue

        blog = parse_blog(html, url, session=session)
        if not blog:
            results.append({"url": url, "success": False, "error": "parse failed"})
            continue

        # 生成文件名: {entry_id}_{title}.md
        safe_title = sanitize_filename(blog["title"])
        filename = f"{blog['entry_id']}_{safe_title}.md"
        filepath = os.path.join(output_dir, filename)

        md_content = build_markdown(blog)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)

        print(f"  ✓ 已保存: {filepath}")
        results.append({"url": url, "success": True, "filepath": filepath, **blog})

        # 请求间隔
        if i < len(urls):
            time.sleep(DELAY_BETWEEN_REQUESTS)

    return results


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main():
    global DELAY_BETWEEN_REQUESTS

    _default_delay = DELAY_BETWEEN_REQUESTS

    parser = argparse.ArgumentParser(
        description="Codeforces Blog Crawler — 批量爬取 CF Blog 并保存为 Markdown",
    )
    parser.add_argument(
        "urls_file",
        nargs="?",
        help="包含 URL 列表的文本文件（每行一个 URL）",
    )
    parser.add_argument(
        "-u", "--urls",
        nargs="+",
        help="直接传入一个或多个 URL",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="./cf_blogs_output",
        help="输出目录 (默认: ./cf_blogs_output)",
    )
    parser.add_argument(
        "-d", "--delay",
        type=float,
        default=_default_delay,
        help=f"请求间隔秒数 (默认: {_default_delay})",
    )

    args = parser.parse_args()

    DELAY_BETWEEN_REQUESTS = args.delay

    # 收集 URL 列表
    urls = []
    if args.urls:
        urls.extend(args.urls)
    if args.urls_file:
        with open(args.urls_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)

    if not urls:
        parser.print_help()
        print("\n[ERROR] 请提供至少一个 URL（通过文件或 -u 参数）", file=sys.stderr)
        sys.exit(1)

    print(f"共 {len(urls)} 个 URL 待爬取，输出目录: {args.output_dir}")
    print("-" * 50)

    results = crawl_urls(urls, args.output_dir)

    # 汇总
    success = sum(1 for r in results if r["success"])
    fail = len(results) - success
    print("-" * 50)
    print(f"完成！成功: {success}, 失败: {fail}")


if __name__ == "__main__":
    main()
