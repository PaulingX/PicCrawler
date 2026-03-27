## 图片在线浏览下载系统

### 1、功能
#### 1.1 在线浏览功能
- 多个 tab，每个规则一个 tab，可以分页查看图片主题。
- 支持规则内搜索（如 WNACG / 漫香阁 / Hitomi），并按规则分类聚合分页。
- 进入主题后，下拉查看所有图片，20 张图片加载一次，超过 3/4 时自动加载下一批。
- 每个规则 tab 可设置本地下载目录，支持勾选多个主题后台下载。
- 页面提供“在线规则开关”，可关闭/开启某条规则的在线浏览 tab（关闭后 tab 隐藏，可随时再开启）。
- 在线主题在查看器中完整浏览后，下载可直接复用已缓存的图片链接，不重复爬取。

#### 1.2 本地书架
- 按规则目录与自定义目录形成多个 tab。
- 每个 tab 可分页查看主题，进入主题后下拉加载全部图片（20 张一批）。
- 支持刷新：删除 SQLite 中该书架数据并重新扫描文件系统入库。
- 扫描支持多目录，最大目录深度 2。
- 文件夹封面逻辑：优先当前目录第一张图；若无图，取第一个子目录中的第一张图。

### 2、技术
- Python 3.12
- Flask + SQLite
- `0.0.0.0` 监听
- 支持 PyInstaller 打包为 exe

### 3、内置规则
- `https://www.4khd.com/`
- `https://asmhentai.com/language/chinese/`
- `https://youwu.im/`（支持 `?page=` 翻页，支持关键词搜索）
- `https://www.wnacg.com/`（分类聚合：`cate-1/9/10`，支持搜索）
- `https://漫香阁.com/`（自动尝试可用镜像域名，分类聚合，支持搜索）
- `https://hitomi.la/index-chinese.html`（支持标签查询与关键词搜索）

### 4、开发启动
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

浏览器打开：`http://127.0.0.1:5000`

局域网访问（同一网络设备）：
- `http://<本机IP>:5000`，例如：`http://192.168.1.33:5000`
- 默认监听：`0.0.0.0:5000`
- 可选环境变量：
  - `PICCRAWLER_HOST`（默认 `0.0.0.0`）
  - `PICCRAWLER_PORT`（默认 `5000`）
- 若局域网仍无法访问，请放行 Windows 防火墙入站端口 `5000`。

如部分站点在当前网络无法访问（如 ASMHentai / 漫香阁 / Hitomi），可在启动前设置代理：
```powershell
$env:PICCRAWLER_PROXY="socks5h://127.0.0.1:10808"
python main.py
```

WNACG 若触发 Cloudflare，可选配置：
```powershell
# 1) 安装 cloudscraper（已在 requirements.txt 中）
pip install -r requirements.txt

# 2) 可选：注入浏览器中的 cf_clearance 等 Cookie
$env:PICCRAWLER_WNACG_COOKIE="cf_clearance=...; __cf_bm=...;"
python main.py
```

### 5、打包 exe
```powershell
# 默认使用 python
powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1

# 或指定解释器
powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1 -Python "C:\Python312\python.exe"
```

产物：`dist/PicCrawler.exe`

说明：
- exe 运行后，`data/piccrawler.db` 与 `downloads/` 会落在 exe 同目录。
- 首次运行会自动初始化数据库与默认规则。

### 6、规则扩展方式
1. 新增爬虫文件，例如 `app/services/crawler_xxx.py`，实现：
   - `list_topics(page_no, query="")`
   - `topic_images(detail_url)`
2. 在 `app/services/rule_registry.py` 的 `_CRAWLER_MAP` 注册：
   - `"crawler_xxx": CrawlerXXX`
3. 在数据库 `rules` 表中新增一条规则记录（`rule_id/name/base_url/crawler`）。

### 7、主要目录
- `main.py`: 启动入口
- `app/routes.py`: Flask API 与页面路由
- `app/database.py`: SQLite 初始化与访问
- `app/services/crawler_4khd.py`: 4khd 抓取规则
- `app/services/crawler_asmhentai.py`: ASMHentai 中文规则
- `app/services/crawler_youwu.py`: 尤物丧志规则（分页 + 搜索）
- `app/services/crawler_wnacg.py`: WNACG 规则（分类 + 搜索）
- `app/services/crawler_manxiangge.py`: 漫香阁规则（自动域名 + 分类 + 搜索）
- `app/services/crawler_hitomi.py`: Hitomi 中文规则（标签 + 搜索）
- `app/services/download_worker.py`: 后台下载队列
- `app/services/library_scanner.py`: 本地书架扫描入库
- `app/templates/index.html`: 前端页面
- `app/static/app.js`: 前端交互
- `app/static/style.css`: 前端样式
- `scripts/build_exe.ps1`: 一键打包脚本
