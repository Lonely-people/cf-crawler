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
def fetch_page(url: str) -> Optional[str]:
    """获取页面 HTML，失败返回 None。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"[ERROR] 获取页面失败: {url} —— {e}", file=sys.stderr)
        return None


def parse_blog(html: str, url: str) -> Optional[dict]:
    """从 HTML 中解析 blog 元数据和正文，返回 dict 或 None。"""
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

    for i, url in enumerate(urls, 1):
        url = url.strip()
        if not url or url.startswith("#"):
            continue

        print(f"[{i}/{len(urls)}] 正在爬取: {url}")

        html = fetch_page(url)
        if not html:
            results.append({"url": url, "success": False, "error": "fetch failed"})
            continue

        blog = parse_blog(html, url)
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
