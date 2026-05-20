#!/usr/bin/env bash
#
# cf-crawler 自动化脚本
# 1. 清除 tutorials/ 和 urls.txt 旧内容
# 2. 按题目拆分 Tutorial → tutorials/
#
# 用法: ./run.sh [problems_file]

set -euo pipefail

PROBLEMS_FILE="${1:-problems.txt}"
TUTORIALS_DIR="./tutorials"
URLS_FILE="./urls.txt"

echo "=== cf-crawler 自动化 ==="

# ---- 清理 ----
echo "[1/2] 清理旧内容..."
rm -rf "$TUTORIALS_DIR"/*
echo "# Codeforces Blog URLs - 每行一个 URL，以 # 开头的行会被忽略" > "$URLS_FILE"
echo "  ✓ 已清除"

# ---- 按题目拆分爬取 ----
echo "[2/2] 按题目拆分爬取 Tutorial..."
python cf_blog_crawler.py -P "$PROBLEMS_FILE" -o "$TUTORIALS_DIR"

echo "=== 完成 ==="
