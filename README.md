# cf-crawler

Codeforces Blog 批量爬虫，保存为 Markdown 文件。

## 使用

### 模式一：爬取完整 Blog（Blog 模式）

```bash
# 从文件批量爬取
python cf_blog_crawler.py urls.txt

# 命令行直接传入 URL
python cf_blog_crawler.py -u "https://codeforces.com/blog/entry/153824"

# 指定输出目录和请求间隔
python cf_blog_crawler.py urls.txt -o ./output -d 5.0
```

`urls.txt` 格式（`#` 开头为注释）：

```
https://codeforces.com/blog/entry/153824
https://codeforces.com/blog/entry/153834
```

输出文件名格式：`{entry_id}_{标题}.md`

### 模式二：按题目拆分 Tutorial（Problem 模式）

```bash
python cf_blog_crawler.py -P problems.txt -o ./tutorials
```

`problems.txt` 格式：

```
https://codeforces.com/contest/2230/problem/A
https://codeforces.com/contest/2230/problem/B
https://codeforces.com/problemset/problem/2230/D
```

支持的 URL 格式：
- `https://codeforces.com/contest/{contestId}/problem/{index}`
- `https://codeforces.com/problemset/problem/{contestId}/{index}`

同一 Contest 的多道题目会自动共享一次 Editorial 爬取，每题输出独立的 `{problemCode}.md` 文件。

### 一键运行

```bash
./run.sh                 # 默认读取 problems.txt
./run.sh my_problems.txt # 指定其他文件
```

自动完成：清除旧内容 → 查找 Editorial URL → 按题目拆分爬取。

### 辅助：查找 Editorial URL

```bash
# 从题目文件查找，追加到 urls.txt
python find_editorial_urls.py problems.txt

# 命令行直接传入
python find_editorial_urls.py -p "https://codeforces.com/contest/2230/problem/A"
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-o, --output` | 输出文件 | `urls.txt` |
| `-d, --delay` | 请求间隔（秒） | `5.0` |

### 手动工作流

```bash
# 1. 从题目 URL 查找到 Editorial Blog Entry URL → 输出到 urls.txt
python find_editorial_urls.py problems.txt

# 2. 爬取 urls.txt 中所有 Blog 文章
python cf_blog_crawler.py urls.txt
```
