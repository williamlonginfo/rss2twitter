# rss2twitter

一个基于 Python 的自动化项目：定时抓取指定 RSS Feed 并自动发布到 Twitter，支持图片上传与重复发布防护。

## 功能

- 定时从 RSS 源抓取最新条目
- 自动生成 Twitter 发布内容
- 支持 RSS 中包含图片的自动下载与上传
- 基于条目 ID 记录已发布内容，避免重复发布
- 使用 GitHub Actions 执行定时任务，并将已发布状态持续保存到仓库

## 文件结构

- `rss2twitter.py`：主发布脚本
- `requirements.txt`：Python 依赖
- `.github/workflows/rss2twitter.yml`：GitHub Actions 定时执行流程
- `published_ids.json`：已发布条目 ID 存储文件

## 环境变量配置

以下变量必须在 GitHub 仓库 Secrets 中配置，不要把它们写入程序或仓库文件：

- `RSS_FEED_URLS`：RSS 地址列表，可使用换行、逗号或分号分隔
- `TWITTER_API_KEY`
- `TWITTER_API_SECRET_KEY`
- `TWITTER_ACCESS_TOKEN`
- `TWITTER_ACCESS_TOKEN_SECRET`

> 例如，`RSS_FEED_URLS` 可以是：
> `https://example.com/feed.xml\nhttps://another.com/rss`

## 本地运行

1. 创建虚拟环境并激活：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. 安装依赖：

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

3. 设置环境变量并运行：

```powershell
$env:RSS_FEED_URLS = "https://example.com/rss"
$env:TWITTER_API_KEY = "你的API Key"
$env:TWITTER_API_SECRET_KEY = "你的API Secret"
$env:TWITTER_ACCESS_TOKEN = "你的Access Token"
$env:TWITTER_ACCESS_TOKEN_SECRET = "你的Access Token Secret"
python rss2twitter.py
```

## GitHub Actions 自动化

工作流文件：`.github/workflows/rss2twitter.yml`

- `schedule`：默认每 6 小时执行一次，可按需修改
- `workflow_dispatch`：支持手动触发
- 使用 `actions/checkout@v4` 拉取仓库，并启用 `persist-credentials: true` 以便推送状态文件
- 运行 `python rss2twitter.py` 并将 `published_ids.json` 的更新提交回仓库

## 发布流程说明

1. GitHub Actions 读取 `RSS_FEED_URLS` 中配置的 RSS 地址
2. 脚本抓取 RSS 条目，按时间排序并过滤已发布条目
3. 下载 RSS 条目中的图片并上传到 Twitter
4. 发布推文内容，并把已发布的条目 ID 写入 `published_ids.json`
5. Workflow 将状态文件提交回仓库，下一次运行可继续避免重复发布

## 注意事项

- 请勿将 Twitter 密钥和 RSS 地址写入代码库，使用 GitHub Secrets 管理
- `published_ids.json` 应当随仓库版本控制，以便 Actions 持久保存已发布记录
- 若想增加抓取频率，可调整 `.github/workflows/rss2twitter.yml` 内的 cron 设置

## 许可证

该项目可根据需要扩展和自定义。