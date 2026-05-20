#!/usr/bin/env python3
"""
Codeforces Problem → Editorial URL 查找器

根据 Codeforces 题目 URL，找到其对应的 Tutorial/Editorial 所在的 Blog Entry URL，
并追加输出到 urls.txt 文件中。

用法:
    # 从文件读取题目 URL
    python find_editorial_urls.py problems.txt

    # 直接传入题目 URL
    python find_editorial_urls.py -p "https://codeforces.com/contest/2230/problem/A"

    # 指定输出文件
    python find_editorial_urls.py problems.txt -o urls.txt

    # 指定请求间隔
    python find_editorial_urls.py problems.txt -d 3.0

支持的题目 URL 格式:
    - https://codeforces.com/contest/{contestId}/problem/{index}
    - https://codeforces.com/problemset/problem/{contestId}/{index}
"""

import argparse
import os
import re
import sys
import time
from typing import Optional, Set
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


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
DEFAULT_DELAY = 10.0   # 默认请求间隔（秒），避免被封 IP

# 已知的 editorial 链接锚文本/标题关键词（大小写不敏感）
EDITORIAL_KEYWORDS = [
    "editorial",
    "tutorial",
    "题解",
]


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
def extract_contest_id(problem_url: str) -> Optional[str]:
    """从题目 URL 提取 Contest ID。

    支持格式:
        /contest/{contestId}/problem/{index}
        /problemset/problem/{contestId}/{index}
    """
    patterns = [
        r"/contest/(\d+)/problem/",
        r"/problemset/problem/(\d+)/",
    ]
    for pat in patterns:
        m = re.search(pat, problem_url)
        if m:
            return m.group(1)
    return None


def find_editorial_url(
    session: requests.Session,
    contest_id: str,
    problem_url: str,
) -> Optional[str]:
    """根据 contest ID 查找对应的 Editorial Blog Entry URL。

    策略:
      1. 访问 contest 页面 /contest/{contestId}
      2. 在侧边栏查找文字为 "Tutorial" 或 title 含 "Editorial" 的 /blog/entry/ 链接
      3. 如果 contest 页面找不到，回退到访问 problem 页面查找
    """
    # 策略 1：contest 页面
    contest_url = f"https://codeforces.com/contest/{contest_id}"
    editorial = _search_editorial_on_page(session, contest_url)
    if editorial:
        return editorial

    # 策略 2：problem 页面
    editorial = _search_editorial_on_page(session, problem_url)
    return editorial


def _search_editorial_on_page(
    session: requests.Session, url: str
) -> Optional[str]:
    """在给定页面中搜索 Editorial 的 Blog Entry 链接。"""
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [WARN] 无法访问页面 {url}: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # 查找所有指向 /blog/entry/ 的链接
    for a_tag in soup.find_all("a", href=re.compile(r"/blog/entry/\d+")):
        link_text = a_tag.get_text(strip=True).lower()
        title_attr = (a_tag.get("title") or "").lower()

        # 判断是否为 editorial 链接
        for keyword in EDITORIAL_KEYWORDS:
            if keyword in link_text or keyword in title_attr:
                href = a_tag["href"]
                if href.startswith("/"):
                    href = "https://codeforces.com" + href
                return href

    return None


def read_existing_urls(output_file: str) -> Set[str]:
    """读取输出文件中已有的 URL，返回去重集合。"""
    existing = set()
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    existing.add(line)
    return existing


def read_problem_urls_from_file(filepath: str) -> list[str]:
    """从文件读取题目 URL 列表。"""
    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def process_problems(
    problem_urls: list[str],
    output_file: str,
    delay: float,
) -> dict:
    """处理题目 URL 列表，查找 editorial 并写入输出文件。

    返回统计信息 dict。
    """
    existing_urls = read_existing_urls(output_file)
    session = requests.Session()
    session.headers.update(HEADERS)

    found_editorials: Set[str] = set()
    new_count = 0
    skip_count = 0
    fail_count = 0

    total = len(problem_urls)
    for i, problem_url in enumerate(problem_urls, 1):
        problem_url = problem_url.strip()
        if not problem_url or problem_url.startswith("#"):
            continue

        print(f"[{i}/{total}] 处理题目: {problem_url}")

        # 提取 contest ID
        contest_id = extract_contest_id(problem_url)
        if not contest_id:
            print(f"  ✗ 无法从 URL 提取 Contest ID，跳过")
            fail_count += 1
            continue

        # 查找 editorial URL
        editorial_url = find_editorial_url(session, contest_id, problem_url)
        if not editorial_url:
            print(f"  ✗ 未找到 Editorial（Contest {contest_id}），跳过")
            fail_count += 1
            continue

        # 去重
        if editorial_url in existing_urls or editorial_url in found_editorials:
            print(f"  → {editorial_url} （已存在，跳过）")
            skip_count += 1
        else:
            print(f"  ✓ 找到 Editorial: {editorial_url}")
            found_editorials.add(editorial_url)
            new_count += 1

        # 请求间隔
        if i < total:
            time.sleep(delay)

    # 追加新找到的 editorial URL 到输出文件
    if found_editorials:
        with open(output_file, "a", encoding="utf-8") as f:
            for url in sorted(found_editorials):
                f.write(url + "\n")
        print(f"\n已追加 {len(found_editorials)} 个新 URL 到 {output_file}")

    return {
        "total": total,
        "new": new_count,
        "skipped": skip_count,
        "failed": fail_count,
    }


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Codeforces Problem → Editorial URL 查找器",
    )
    parser.add_argument(
        "problems_file",
        nargs="?",
        help="包含题目 URL 列表的文本文件（每行一个 URL）",
    )
    parser.add_argument(
        "-p", "--problems",
        nargs="+",
        help="直接传入一个或多个题目 URL",
    )
    parser.add_argument(
        "-o", "--output",
        default="urls.txt",
        help="输出的 Editorial URL 文件 (默认: urls.txt)",
    )
    parser.add_argument(
        "-d", "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"请求间隔秒数 (默认: {DEFAULT_DELAY})",
    )

    args = parser.parse_args()

    # 收集题目 URL 列表
    problem_urls = []
    if args.problems:
        problem_urls.extend(args.problems)
    if args.problems_file:
        problem_urls.extend(read_problem_urls_from_file(args.problems_file))

    if not problem_urls:
        parser.print_help()
        print(
            "\n[ERROR] 请提供至少一个题目 URL（通过文件或 -p 参数）",
            file=sys.stderr,
        )
        sys.exit(1)

    # 去重题目 URL
    seen = set()
    unique_problems = []
    for u in problem_urls:
        if u not in seen:
            seen.add(u)
            unique_problems.append(u)

    print(f"共 {len(unique_problems)} 个题目 URL 待处理，输出文件: {args.output}")
    print(f"请求间隔: {args.delay}s")
    print("-" * 50)

    result = process_problems(unique_problems, args.output, args.delay)

    print("-" * 50)
    print(
        f"完成！新增: {result['new']}, "
        f"已存在跳过: {result['skipped']}, "
        f"失败: {result['failed']}"
    )


if __name__ == "__main__":
    main()
