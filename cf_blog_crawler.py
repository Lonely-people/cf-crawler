#!/usr/bin/env python3
"""
Codeforces Blog Crawler
批量爬取 Codeforces Blog 文章并保存为 Markdown 文件。
支持两种模式:
  1. Blog 模式: 直接爬取 blog entry URL，保存完整文章
  2. Problem 模式: 从题目 URL 自动查找 editorial，按题目拆分输出

用法:
    # Blog 模式
    python cf_blog_crawler.py urls.txt
    python cf_blog_crawler.py -u URL1 URL2 ...

    # Problem 模式（自动查找 editorial 并按题目拆分）
    python cf_blog_crawler.py -P problems.txt -o output_dir
"""

import argparse
import datetime
import json
import logging
import os
import re
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag
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
DELAY_BETWEEN_REQUESTS = 7.0  # 请求间隔（秒），避免被封
MAX_RETRIES = 3               # 网络错误最大重试次数
RETRY_BACKOFF = 5.0           # 重试退避基础秒数

# 断点续传 / 日志 / 错误收集
CHECKPOINT_FILE = ".crawler_checkpoint.json"
FAILED_URLS_FILE = "failed_urls.txt"
LOG_FILE = "crawler.log"

# 进度条宽度
PROGRESS_BAR_WIDTH = 40

# 全局中断标志
_interrupted = False


def _signal_handler(signum, frame):
    global _interrupted
    _interrupted = True
    print("\n[WARN] 收到中断信号，正在安全退出...", file=sys.stderr)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_logger: Optional[logging.Logger] = None


def setup_logging(verbose: bool = False):
    """配置日志：同时输出到控制台和文件。"""
    global _logger
    _logger = logging.getLogger("cf-crawler")
    _logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    _logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    )

    # 控制台
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    _logger.addHandler(ch)

    # 文件
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    _logger.addHandler(fh)

    return _logger


def log() -> logging.Logger:
    return _logger or logging.getLogger("cf-crawler")


# ---------------------------------------------------------------------------
# 网络重试
# ---------------------------------------------------------------------------
def retry_on_network_error(func, max_retries=MAX_RETRIES, backoff=RETRY_BACKOFF):
    """装饰器：网络错误时自动重试，指数退避。"""
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                return func(*args, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt < max_retries:
                    wait = backoff * (2 ** (attempt - 1))
                    log().warning(
                        "网络错误 (尝试 %d/%d): %s，%ds 后重试...",
                        attempt, max_retries, e, wait,
                    )
                    time.sleep(wait)
                else:
                    log().error("网络错误，已达最大重试次数: %s", e)
            except requests.HTTPError as e:
                last_exc = e
                status = e.response.status_code if hasattr(e, 'response') else '?'
                if attempt < max_retries and status in (429, 502, 503, 504):
                    wait = backoff * (2 ** (attempt - 1))
                    log().warning(
                        "HTTP %s (尝试 %d/%d)，%ds 后重试...",
                        status, attempt, max_retries, wait,
                    )
                    time.sleep(wait)
                else:
                    log().error("HTTP 错误: %s", e)
                    raise
            except requests.RequestException as e:
                last_exc = e
                log().error("请求异常: %s", e)
                raise
        raise last_exc
    return wrapper


# ---------------------------------------------------------------------------
# 断点续传
# ---------------------------------------------------------------------------
def load_checkpoint() -> Optional[dict]:
    """加载上次运行的断点信息。"""
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_checkpoint(state: dict):
    """保存断点信息。"""
    state["updated_at"] = datetime.datetime.now().isoformat()
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clear_checkpoint():
    """清除断点文件。"""
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


def append_failed_url(url: str, reason: str = ""):
    """追加失败 URL 到 failed_urls.txt。"""
    with open(FAILED_URLS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{url}")
        if reason:
            f.write(f"  # {reason}")
        f.write("\n")


# ---------------------------------------------------------------------------
# 进度条
# ---------------------------------------------------------------------------
def render_progress_bar(current: int, total: int, start_time: float, label: str = "") -> str:
    """渲染进度条字符串。"""
    if total <= 0:
        return ""
    ratio = current / total
    filled = int(PROGRESS_BAR_WIDTH * ratio)
    bar = "█" * filled + "░" * (PROGRESS_BAR_WIDTH - filled)

    elapsed = time.time() - start_time
    if current > 0:
        eta = (elapsed / current) * (total - current)
        eta_str = str(datetime.timedelta(seconds=int(eta)))
    else:
        eta_str = "--:--"

    elapsed_str = str(datetime.timedelta(seconds=int(elapsed)))
    percent = f"{ratio * 100:.1f}%"

    return f"  {label}[{bar}] {current}/{total} ({percent}) | ⏱ {elapsed_str} | ETA {eta_str}"


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
def fetch_page(url: str, session: requests.Session = None) -> Optional[str]:
    """获取页面 HTML，失败返回 None。支持自动重试。"""
    fetcher = session if session else requests

    @retry_on_network_error
    def _do_fetch():
        resp = fetcher.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text

    try:
        return _do_fetch()
    except requests.RequestException as e:
        log().error("获取页面失败: %s —— %s", url, e)
        return None


def fetch_tutorial_content(
    session: requests.Session, problem_code: str, csrf_token: str = None
) -> Optional[str]:
    """通过 AJAX API 获取题解 Tutorial 的真实内容。"""
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

    @retry_on_network_error
    def _do_fetch():
        resp = session.post(
            api_url,
            data={"problemCode": problem_code},
            headers=ajax_headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    try:
        resp = _do_fetch()
        result = resp.json()
        if result.get("success") == "true":
            return result.get("html", "")
        else:
            log().warning("Tutorial API 返回失败: %s", problem_code)
            return None
    except (requests.RequestException, json.JSONDecodeError) as e:
        log().warning("获取 Tutorial 失败 (%s): %s", problem_code, e)
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
        log().error("未找到正文内容: %s", url)
        return None

    # 正文是 content 下的第一个 ttypography div
    body_div = content_div.select_one("div.ttypography")
    if not body_div:
        log().error("未找到博客正文: %s", url)
        return None

    # ---- 异步加载的 Tutorial 占位符替换 ----
    tutorial_placeholders = body_div.select("div.problemTutorial[problemcode]")
    if tutorial_placeholders and session:
        # 从页面提取 CSRF Token
        csrf_meta = soup.find("meta", attrs={"name": "X-Csrf-Token"})
        csrf_token = csrf_meta.get("content", "") if csrf_meta else ""

        log().info("检测到 %d 个 Tutorial 占位符，正在获取真实内容...", len(tutorial_placeholders))
        for placeholder in tutorial_placeholders:
            problem_code = placeholder.get("problemcode", "")
            if not problem_code:
                continue
            real_html = fetch_tutorial_content(session, problem_code, csrf_token)
            if real_html:
                real_fragment = BeautifulSoup(real_html, "html.parser")
                placeholder.clear()
                placeholder.append(real_fragment)
                log().info("  ✓ Tutorial %s 获取成功", problem_code)
            else:
                placeholder.string = f"*Tutorial for {problem_code} is not available.*"
                log().warning("  ✗ Tutorial %s 获取失败，保留占位", problem_code)
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
        "_body_div": body_div,  # 内部使用：已修改的 BeautifulSoup 节点
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


# ---------------------------------------------------------------------------
# Problem → Editorial 查找 & 拆分
# ---------------------------------------------------------------------------
EDITORIAL_KEYWORDS = ["editorial", "tutorial", "题解"]


def extract_contest_id(problem_url: str) -> Optional[str]:
    """从题目 URL 提取 Contest ID。"""
    patterns = [
        r"/contest/(\d+)/problem/",
        r"/problemset/problem/(\d+)/",
    ]
    for pat in patterns:
        m = re.search(pat, problem_url)
        if m:
            return m.group(1)
    return None


def extract_problem_code(problem_url: str) -> Optional[str]:
    """从题目 URL 提取完整的 problem code（如 "2230A"）。

    支持格式:
        /contest/{contestId}/problem/{index}  → "{contestId}{index}"
        /problemset/problem/{contestId}/{index} → "{contestId}{index}"
    """
    patterns = [
        r"/contest/(\d+)/problem/(\w+)",
        r"/problemset/problem/(\d+)/(\w+)",
    ]
    for pat in patterns:
        m = re.search(pat, problem_url)
        if m:
            return m.group(1) + m.group(2)
    return None


def find_editorial_url(
    session: requests.Session,
    contest_id: str,
    problem_url: str,
) -> Optional[str]:
    """根据 contest ID 查找对应的 Editorial Blog Entry URL。

    优先级：文字 > 视频；覆盖题目多 > 覆盖题目少。
    """
    # 策略 1：contest 页面
    contest_url = f"https://codeforces.com/contest/{contest_id}"
    candidates = _find_all_editorial_links(session, contest_url)
    # 策略 2：problem 页面回退
    if not candidates:
        candidates = _find_all_editorial_links(session, problem_url)

    if not candidates:
        return None

    # 评分并排序：优先文字 Editorial，其次选覆盖题目最多的
    scored = []
    for url in candidates:
        is_video, problem_count = _evaluate_editorial(url, session)
        if not is_video:
            scored.append((problem_count, url))

    if scored:
        scored.sort(reverse=True)  # problem_count 降序
        best_count, best_url = scored[0]
        log().info("  选择 Editorial: %s（覆盖 %d 道题）", best_url, best_count)
        return best_url

    log().warning("所有 Editorial 均为视频题解，跳过 Contest %s", contest_id)
    return None


def _evaluate_editorial(blog_url: str, session: requests.Session) -> tuple:
    """评估 Editorial 质量。

    返回 (is_video: bool, problem_count: int)。
    - is_video: 标题含 "Video Editorial" 或大量 YouTube 嵌入
    - problem_count: 正文中不同 problem code 的数量
    """
    try:
        @retry_on_network_error
        def _fetch():
            resp = session.get(blog_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text

        html = _fetch()
    except requests.RequestException:
        return (False, 0)

    soup = BeautifulSoup(html, "html.parser")
    is_video = False

    # 检查标题
    title_tag = soup.select_one("div.title a p")
    if title_tag:
        title = title_tag.get_text(strip=True).lower()
        if "video editorial" in title:
            is_video = True

    # 检查正文中的 YouTube 嵌入数量
    body = soup.select_one("div.content div.ttypography")
    iframes = 0
    youtube_links = 0
    if body:
        iframes = len(body.find_all("iframe"))
        youtube_links = len(body.find_all("a", href=re.compile(r"youtube\.com|youtu\.be")))
        if iframes >= 3 or youtube_links >= 5:
            is_video = True

    # 统计正文中不同 problem code 的数量
    problem_codes = set()
    if body:
        for a_tag in body.find_all("a", href=re.compile(r"/contest/\d+/problem/\w+")):
            m = re.search(r"/contest/(\d+)/problem/(\w+)", a_tag.get("href", ""))
            if m:
                problem_codes.add(m.group(1) + m.group(2))

    return (is_video, len(problem_codes))


def _find_all_editorial_links(
    session: requests.Session, url: str
) -> list[str]:
    """在给定页面中搜索所有 Editorial Blog Entry 链接，返回 URL 列表。"""
    @retry_on_network_error
    def _do_fetch():
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text

    try:
        html = _do_fetch()
    except requests.RequestException as e:
        log().warning("无法访问页面 %s: %s", url, e)
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []
    for a_tag in soup.find_all("a", href=re.compile(r"/blog/entry/\d+")):
        link_text = a_tag.get_text(strip=True).lower()
        title_attr = (a_tag.get("title") or "").lower()
        for keyword in EDITORIAL_KEYWORDS:
            if keyword in link_text or keyword in title_attr:
                href = a_tag["href"]
                if href.startswith("/"):
                    href = "https://codeforces.com" + href
                if href not in results:
                    results.append(href)
                break
    return results


def split_body_by_problem(body_div) -> dict:
    """将 editorial 正文按题目拆分为多个 section。

    返回 {problem_code: html_fragment_str} 的字典。
    problem_code 形如 "2230A", "2230B"。
    """
    sections = {}
    current_code = None
    current_elements = []
    buf = []

    for child in body_div.children:
        # 跳过非 Tag 节点（如 NavigableString）
        if not isinstance(child, Tag):
            buf.append(child)
            continue

        # 检测题目标题：<p> 或 <h2>/<h3> 等块级元素内包含 /contest/{id}/problem/{code} 链接
        problem_link = child.find(
            "a", href=re.compile(r"/contest/\d+/problem/\w+")
        )
        if child.name in ("p", "h1", "h2", "h3", "h4", "h5", "h6") and problem_link:
            # 保存上一个 section
            if current_code:
                for b in buf:
                    current_elements.append(b)
                buf = []
                sections[current_code] = "".join(
                    str(el) for el in current_elements
                )
            # 开始新 section
            href = problem_link.get("href", "")
            m = re.search(r"/contest/(\d+)/problem/(\w+)", href)
            current_code = m.group(1) + m.group(2) if m else None
            current_elements = []
            if buf:
                current_elements.extend(buf)
                buf = []

        if current_code is not None:
            current_elements.append(child)
        elif buf:
            # 尚未遇到任何题目标题的内容，保留在 buf 中
            pass

    # 最后一个 section
    if current_code and current_elements:
        sections[current_code] = "".join(
            str(el) for el in current_elements
        )

    return sections


def build_problem_markdown(
    problem_url: str,
    problem_code: str,
    section_html: str,
    blog_meta: dict,
) -> str:
    """为单个题目生成 Markdown 文件内容。"""
    # 将 section HTML 中的相对链接补全
    soup = BeautifulSoup(section_html, "html.parser")
    for a_tag in soup.find_all("a"):
        href = a_tag.get("href", "")
        if href.startswith("/"):
            a_tag["href"] = "https://codeforces.com" + href
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("//"):
            img["src"] = "https:" + src
        elif src.startswith("/"):
            img["src"] = "https://codeforces.com" + src

    section_md = md(str(soup), heading_style="ATX", bullets="-")
    section_md = re.sub(r"\n{3,}", "\n\n", section_md)

    lines = [
        f"# {problem_code} — Tutorial",
        "",
        f"**题目链接**: [{problem_url}]({problem_url})  ",
        f"**来源 Editorial**: [{blog_meta['url']}]({blog_meta['url']})  ",
        f"**作者**: {blog_meta['author']}  ",
        f"**日期**: {blog_meta['date']}  ",
        "",
        "---",
        "",
        section_md,
    ]
    return "\n".join(lines)


def crawl_problems(
    problem_urls: list[str],
    output_dir: str,
    resume: bool = False,
) -> list[dict]:
    """按题目爬取：查找 editorial → 拆分 → 按题目输出 Markdown。

    支持断点续传：中断后下次运行可从中断处继续。
    支持进度条、错误收集、实时保存。
    """
    global _interrupted
    os.makedirs(output_dir, exist_ok=True)
    results = []
    failed_urls = []

    session = requests.Session()
    session.headers.update(HEADERS)
    overall_start = time.time()

    # ---- 断点续传：加载上次进度 ----
    checkpoint = load_checkpoint() if resume else None
    already_found: dict[str, dict] = {}
    already_crawled: set = set()
    phase = 1  # 1=查找editorial, 2=爬取拆分

    if checkpoint:
        already_found = {k: v for k, v in checkpoint.get("found_editorials", {}).items()}
        already_crawled = set(checkpoint.get("crawled_problems", []))
        phase = checkpoint.get("phase", 1)
        log().info("断点续传：已恢复 %d 个 editorial 映射，%d 个已爬取题目",
                    len(already_found), len(already_crawled))
        if phase >= 2:
            log().info("上次已进入阶段 2，跳过 Editorial 查找")

    # ---- 第一阶段：查找 Editorial URL ----
    editorial_to_problems: dict[str, list[str]] = {}  # editorial_url → [problem_url, ...]
    problem_info: dict[str, dict] = {}

    # 恢复已有的映射
    for purl, info in already_found.items():
        eurl = info.get("editorial_url", "")
        if eurl:
            editorial_to_problems.setdefault(eurl, []).append(purl)
            problem_info[purl] = info

    if phase == 1:
        log().info("=" * 50)
        log().info("第一步：查找各题目对应的 Editorial...")
        log().info("-" * 50)

        total = len(problem_urls)
        step_start = time.time()

        for i, problem_url in enumerate(problem_urls, 1):
            if _interrupted:
                log().warning("收到中断信号，保存进度后退出...")
                break

            problem_url = problem_url.strip()
            if not problem_url or problem_url.startswith("#"):
                continue

            # 跳过已找到的
            if problem_url in already_found:
                log().info("[%d/%d] ⏭ %s（已有缓存）", i, total,
                           already_found[problem_url].get("problem_code", ""))
                continue

            contest_id = extract_contest_id(problem_url)
            problem_code = extract_problem_code(problem_url)
            if not contest_id or not problem_code:
                log().warning("[%d/%d] ✗ 无法解析: %s", i, total, problem_url)
                failed_urls.append((problem_url, "parse url failed"))
                results.append({"url": problem_url, "success": False, "error": "parse url failed"})
                continue

            editorial_url = find_editorial_url(session, contest_id, problem_url)
            if not editorial_url:
                log().warning("[%d/%d] ✗ 未找到 Editorial: %s", i, total, problem_url)
                failed_urls.append((problem_url, "editorial not found"))
                results.append({"url": problem_url, "success": False, "error": "editorial not found"})
            else:
                log().info("[%d/%d] ✓ %s → %s", i, total, problem_code, editorial_url)
                info = {
                    "contest_id": contest_id,
                    "problem_code": problem_code,
                    "editorial_url": editorial_url,
                }
                problem_info[problem_url] = info
                already_found[problem_url] = info
                editorial_to_problems.setdefault(editorial_url, []).append(problem_url)

                # ★ 实时保存 checkpoint
                save_checkpoint({
                    "phase": 1,
                    "found_editorials": already_found,
                    "crawled_problems": list(already_crawled),
                })

            # 进度条
            bar = render_progress_bar(i, total, step_start, "查找 Editorial: ")
            sys.stdout.write(f"\r{bar}")
            sys.stdout.flush()

            if i < total and not _interrupted:
                time.sleep(DELAY_BETWEEN_REQUESTS)

        print()  # 换行
        log().info("第一阶段完成，找到 %d 个 Editorial", len(editorial_to_problems))
        phase = 2

    if not problem_info:
        log().error("没有找到任何有效的 Editorial URL")
        _save_failed_urls(failed_urls)
        return results

    # ---- 第二阶段：爬取 editorial 并按题目拆分输出 ----
    log().info("=" * 50)
    log().info("第二步：爬取 Editorial 并按题目拆分输出...")
    log().info("-" * 50)

    editorial_count = len(editorial_to_problems)
    step_start = time.time()
    processed_count = 0
    total_problems = sum(len(v) for v in editorial_to_problems.values())

    for ei, (editorial_url, probs) in enumerate(editorial_to_problems.items(), 1):
        if _interrupted:
            log().warning("收到中断信号，保存进度后退出...")
            break

        # 检查该 editorial 的所有题目是否都已爬取
        remaining = [p for p in probs if p not in already_crawled]
        if not remaining:
            log().info("[Editorial %d/%d] ⏭ %s（全部已爬取，跳过）",
                       ei, editorial_count, editorial_url)
            processed_count += len(probs)
            bar = render_progress_bar(processed_count, total_problems, step_start, "爬取 Tutorial: ")
            sys.stdout.write(f"\r{bar}")
            sys.stdout.flush()
            continue

        log().info("[Editorial %d/%d] 正在爬取: %s", ei, editorial_count, editorial_url)

        html = fetch_page(editorial_url, session=session)
        if not html:
            for pu in remaining:
                log().warning("  ✗ Editorial 获取失败: %s", pu)
                failed_urls.append((pu, "fetch editorial failed"))
                results.append({"url": pu, "success": False, "error": "fetch editorial failed"})
            processed_count += len(probs)
            continue

        blog = parse_blog(html, editorial_url, session=session)
        if not blog:
            for pu in remaining:
                log().warning("  ✗ Editorial 解析失败: %s", pu)
                failed_urls.append((pu, "parse editorial failed"))
                results.append({"url": pu, "success": False, "error": "parse editorial failed"})
            processed_count += len(probs)
            continue

        body_div2 = blog.get("_body_div")
        if not body_div2:
            for pu in remaining:
                failed_urls.append((pu, "body not found"))
                results.append({"url": pu, "success": False, "error": "body not found"})
            processed_count += len(probs)
            continue

        sections = split_body_by_problem(body_div2)
        log().info("  拆分出 %d 个题目 section", len(sections))
        _use_full_body = not sections  # 无法拆分时回退到完整 Editorial

        for problem_url in probs:
            if problem_url in already_crawled:
                continue

            info = problem_info.get(problem_url, {})
            problem_code = info.get("problem_code", extract_problem_code(problem_url) or "unknown")

            if problem_code not in sections:
                if _use_full_body:
                    # 非标准 editorial 格式或无法拆分：回退为保存完整 editorial 内容
                    log().warning("  ⚠ Editorial 无法按题目拆分，保存完整内容作为 %s 的 Tutorial", problem_code)
                    safe_code = sanitize_filename(problem_code)
                    filename = f"{safe_code}.md"
                    filepath = os.path.join(output_dir, filename)

                    # 使用 blog 中的完整 body_md
                    md_content = build_problem_markdown(
                        problem_url, problem_code,
                        str(body_div2), blog
                    )
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(md_content)

                    log().info("  ✓ %s → %s（非标准格式，完整 Editorial）", problem_code, filepath)
                    results.append({
                        "url": problem_url,
                        "success": True,
                        "filepath": filepath,
                        "problem_code": problem_code,
                    })
                    already_crawled.add(problem_url)
                    processed_count += 1
                else:
                    log().warning("  ✗ 未在 Editorial 中找到 %s 的 section", problem_code)
                    failed_urls.append((problem_url, "section not found"))
                    results.append({"url": problem_url, "success": False, "error": "section not found"})
                    already_crawled.add(problem_url)
                    processed_count += 1
                # ★ 实时保存 checkpoint
                save_checkpoint({
                    "phase": 2,
                    "found_editorials": already_found,
                    "crawled_problems": list(already_crawled),
                })
                bar = render_progress_bar(processed_count, total_problems, step_start, "爬取 Tutorial: ")
                sys.stdout.write(f"\r{bar}")
                sys.stdout.flush()
                continue

            section_html = sections[problem_code]
            md_content = build_problem_markdown(
                problem_url, problem_code, section_html, blog
            )

            safe_code = sanitize_filename(problem_code)
            filename = f"{safe_code}.md"
            filepath = os.path.join(output_dir, filename)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md_content)

            log().info("  ✓ %s → %s", problem_code, filepath)
            results.append({
                "url": problem_url,
                "success": True,
                "filepath": filepath,
                "problem_code": problem_code,
            })
            already_crawled.add(problem_url)
            processed_count += 1

            # ★ 实时保存 checkpoint
            save_checkpoint({
                "phase": 2,
                "found_editorials": already_found,
                "crawled_problems": list(already_crawled),
            })

            bar = render_progress_bar(processed_count, total_problems, step_start, "爬取 Tutorial: ")
            sys.stdout.write(f"\r{bar}")
            sys.stdout.flush()

            if _interrupted:
                break

        if ei < editorial_count and not _interrupted:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    print()  # 换行

    # ---- 清理 ----
    if not _interrupted:
        clear_checkpoint()
        log().info("全部完成，已清除断点文件")

    _save_failed_urls(failed_urls)

    elapsed = time.time() - overall_start
    log().info("总耗时: %s", str(datetime.timedelta(seconds=int(elapsed))))

    return results


def _save_failed_urls(failed: list):
    """保存失败的 URL 列表。"""
    if not failed:
        # 清空旧文件
        if os.path.exists(FAILED_URLS_FILE):
            os.remove(FAILED_URLS_FILE)
        return
    seen = set()
    with open(FAILED_URLS_FILE, "w", encoding="utf-8") as f:
        f.write("# 爬取失败的 URL\n")
        for url, reason in failed:
            if url not in seen:
                seen.add(url)
                f.write(f"{url}  # {reason}\n")
    log().warning("%d 个 URL 爬取失败，已记录到 %s", len(seen), FAILED_URLS_FILE)


def sanitize_filename(name: str) -> str:
    """将标题转为安全的文件名。"""
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = name.strip().strip(".")
    return name[:120]  # 限制长度


def crawl_urls(urls: list[str], output_dir: str) -> list[dict]:
    """批量爬取 URL 列表，保存 Markdown，返回结果列表。"""
    global _interrupted
    os.makedirs(output_dir, exist_ok=True)
    results = []
    failed_urls = []

    session = requests.Session()
    session.headers.update(HEADERS)

    total = len(urls)
    start_time = time.time()

    for i, url in enumerate(urls, 1):
        if _interrupted:
            log().warning("收到中断信号，停止爬取")
            break

        url = url.strip()
        if not url or url.startswith("#"):
            continue

        log().info("[%d/%d] 正在爬取: %s", i, total, url)

        html = fetch_page(url, session=session)
        if not html:
            log().error("[%d/%d] ✗ 获取页面失败: %s", i, total, url)
            failed_urls.append((url, "fetch failed"))
            results.append({"url": url, "success": False, "error": "fetch failed"})
            bar = render_progress_bar(i, total, start_time, "Blog 爬取: ")
            sys.stdout.write(f"\r{bar}")
            sys.stdout.flush()
            continue

        blog = parse_blog(html, url, session=session)
        if not blog:
            log().error("[%d/%d] ✗ 解析失败: %s", i, total, url)
            failed_urls.append((url, "parse failed"))
            results.append({"url": url, "success": False, "error": "parse failed"})
            bar = render_progress_bar(i, total, start_time, "Blog 爬取: ")
            sys.stdout.write(f"\r{bar}")
            sys.stdout.flush()
            continue

        safe_title = sanitize_filename(blog["title"])
        filename = f"{blog['entry_id']}_{safe_title}.md"
        filepath = os.path.join(output_dir, filename)

        md_content = build_markdown(blog)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)

        log().info("  ✓ 已保存: %s", filepath)
        results.append({"url": url, "success": True, "filepath": filepath, **blog})

        bar = render_progress_bar(i, total, start_time, "Blog 爬取: ")
        sys.stdout.write(f"\r{bar}")
        sys.stdout.flush()

        if i < total and not _interrupted:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    print()  # 换行
    _save_failed_urls(failed_urls)
    elapsed = time.time() - start_time
    log().info("总耗时: %s", str(datetime.timedelta(seconds=int(elapsed))))

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
        help="包含 Blog URL 列表的文本文件（每行一个 URL）",
    )
    parser.add_argument(
        "-u", "--urls",
        nargs="+",
        help="直接传入一个或多个 Blog URL",
    )
    parser.add_argument(
        "-P", "--problems-file",
        help="题目 URL 列表文件（每行一个题目 URL），自动查找 Editorial 并按题目拆分输出",
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
    parser.add_argument(
        "--resume",
        action="store_true",
        help="从上次中断处继续（断点续传）",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="详细日志输出（DEBUG 级别）",
    )

    args = parser.parse_args()

    # 初始化日志
    setup_logging(verbose=args.verbose)
    DELAY_BETWEEN_REQUESTS = args.delay

    # ---- Problem 模式 ----
    if args.problems_file:
        with open(args.problems_file, "r", encoding="utf-8") as f:
            problem_urls = [
                line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")
            ]

        if not problem_urls:
            log().error("文件 %s 中没有有效的题目 URL", args.problems_file)
            sys.exit(1)

        log().info("Problem 模式：共 %d 个题目 URL 待处理", len(problem_urls))
        log().info("输出目录: %s", args.output_dir)
        log().info("-" * 50)

        results = crawl_problems(problem_urls, args.output_dir, resume=args.resume)

        success = sum(1 for r in results if r["success"])
        fail = len(results) - success
        log().info("-" * 50)
        log().info("完成！成功: %d, 失败: %d", success, fail)
        return

    # ---- Blog 模式 ----
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
        log().error("请提供至少一个 URL（通过文件或 -u 参数）")
        sys.exit(1)

    log().info("共 %d 个 URL 待爬取，输出目录: %s", len(urls), args.output_dir)
    log().info("-" * 50)

    results = crawl_urls(urls, args.output_dir)

    success = sum(1 for r in results if r["success"])
    fail = len(results) - success
    log().info("-" * 50)
    log().info("完成！成功: %d, 失败: %d", success, fail)


if __name__ == "__main__":
    main()
