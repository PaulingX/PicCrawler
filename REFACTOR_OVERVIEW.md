# PicCrawler 架构升级与下载并发优化 — 交付说明

> 按需求「浏览/下载优化 + 架构升级 + 规则问题可改 + 验证正确后才真正修改」完成。
> 全部改动以「先建测试锚定行为 → 再重构 → 跑测试回归」的方式落地。

## 1. 架构升级：单体路由拆分为蓝图

原 `app/routes.py`（1281 行，system/rules/online/download/library 全混在一起）
拆分为按域蓝图：

| 蓝图 | 文件 | 路由前缀 |
|------|------|----------|
| system | `app/api/system.py` | `/api/system/*` |
| rules | `app/api/rules.py` | `/api/rules/*` |
| online | `app/api/online.py` | `/api/online/*` |
| download | `app/api/download.py` | `/api/download`, `/api/download/jobs` |
| library | `app/api/library.py` | `/api/shelves*`, `/api/library/*` |
| pages | `app/pages.py` | `/` |

- `app/routes.py` 退化为「蓝图注册聚合 + 向后兼容再导出」，保证旧脚本
  （`tests/baseline_snapshot.py`）仍可直接 `import app.routes` 拿到原函数。
- 图片 URL 归一化/代理/回退逻辑统一抽到 `app/services/image_proxy.py`；
  路由层 DB 辅助抽到 `app/api/helpers.py`，避免重复实现。
- **验证**：`test_routes_registry.py` 断言全部 21 条路由的方法与路径与原版一致，
  且无重复注册。

## 2. 下载优化：单线程 → 线程池并发

- `DownloadWorker` 由「单消费者线程 + queue」改为 `ThreadPoolExecutor`
  （默认 4 线程，可通过环境变量 `PICCRAWLER_DOWNLOAD_WORKERS` /
  `CONCURRENCY` 调整）。
- 多个主题下载任务可并行执行；单主题内部仍顺序落盘、独立目录，互不干扰。
- 进程退出时通过 `atexit` 释放线程池。

## 3. SQLite 并发支撑

- `app/database.py` 的 `_connect` 开启 **WAL** 日志模式、`busy_timeout=30000`、
  `synchronous=NORMAL`，使多线程读写安全且减少锁等待。

## 4. 规则能力单一来源

- 原 `_CRAWLER_CAPABILITIES` 是与 `_CRAWLER_MAP` 平行的维护字典，易失同步。
- 现由每个 crawler 类的 `supports_search` / `categories` 类属性**派生**，
  删除手工平行表。`test_rule_registry.py` 锁定该单一来源约束。

## 5. create_app 可注入（测试隔离）

- `create_app(db_path=None, download_root=None)` 支持覆盖路径；
  不再依赖导入期常量快照（此前会让测试误用真实 `data/piccrawler.db`）。

## 6. 验证结果

| 套件 | 结果 |
|------|------|
| `tests/baseline_snapshot.py`（43 项改前行为快照） | 43/43 PASS |
| `tests/test_*.py`（6 个模块，全程离线 mock） | 41/41 PASS |

测试在 `.venv_dev/`（Python 3.13）运行。因 3.13 暂无 Pillow wheel，
仅安装 flask/requests/beautifulsoup4/cloudscraper/pytest；Pillow 仅在 Hitomi
AVIF→WebP 转换时惰性导入，不影响导入与测试。

运行方式：
```bash
.venv_dev/Scripts/python.exe -m pytest -q
.venv_dev/Scripts/python.exe tests/baseline_snapshot.py
```

## 7. 打包说明

`PicCrawler.spec` 的 `hiddenimports` 已显式列入全部新蓝图模块，
重新打包（`scripts/build_exe.ps1`）不会漏掉新模块。打包需 Pillow，
请在 Python 3.12 环境执行（3.13 无对应 wheel）。

## 8. 已知限制 / 后续可选项

- 测试中存在 `datetime.utcnow()` 弃用告警（来自原代码多处时间处理），
  不影响功能；如需消除可后续统一替换为 `datetime.now(UTC)`。
- 当前并发为「主题级」；若需「图级」并发，可在 `_run_task` 内对
  `image_urls` 再做一层并发（需评估对单目录落盘与远端限流的影响）。

---

# 第二轮交付：浏览/下载功能增强（2026-09-11）

## 1. 下载：图片级并发 + 失败重试 + 任务取消

- `DownloadWorker._run_task` 在主题级线程池内，对单主题的图片再用
  `PICCRAWLER_IMAGE_WORKERS`（默认 3）并发拉取；文件按 `{序号:04d}` 命名，
  与完成顺序无关，进度统一经 `state_lock` 汇总后写库，终态回写校准计数。
- 首轮失败的图片在收尾时统一重试一轮（远端偶发 5xx/超时常见），提高完成率。
- 新增任务取消：`DownloadWorker.cancel(job_id)`（排队中直接出队；
  运行中设置事件，在图片间停止），新状态 `canceled`。
  对应端点 `POST /api/download/jobs/<job_id>/cancel`，前端任务卡片带「取消」按钮。
- `shutdown` 改为 `cancel_futures=True`，退出时不遗留排队任务。

## 2. 浏览：代理缓存 + 连接池 + 前端限流

- `image-proxy` 成功与 302 直连回退响应均加 `Cache-Control: public, max-age=86400`，
  翻页/重开查看器时浏览器直接命中缓存，不再重复走回退链。
- `image_proxy` 改用模块级共享 `requests.Session`（HTTPAdapter 连接池 8/32），
  一页几十张代理图片复用 TCP/TLS 连接，显著降低延迟。
- 前端 `/api/online/topic-count` 请求改为 4 并发的小队列（原先一页 20 张卡片同时打满远端）。

## 3. 规则修复：Cloudflare 拦截页误判（4khd 修复实测）

- `looks_like_challenge` 此前把页面内 Cloudflare 埋点脚本
  （`/cdn-cgi/challenge-platform/...`，所有 CF 站点都会注入）当成拦截页，
  导致 4khd 镜像站返回 12 个主题的正常列表页被丢弃、解析恒为空。
  现改为「命中特征且页面 ≤ 4KB（几乎无正文）」才判定拦截，并新增回归测试。
- `Crawler4KHD` / `CrawlerYouwu` 改用 `make_session`（cloudscraper），
  与 asmhentai / hitomi / xchina 一致；4khd 实测恢复在线浏览。
- **修复 `CrawlerHitomi` 缺少 `make_session` 导入**（运行时报
  `name 'make_session' is not defined` → 在线接口 502）；
  新增回归测试「实例化 _CRAWLER_MAP 全部爬虫」，此类装配错误测试阶段即暴露。

## 4. 架构清理

- 新增 `app/services/utils.py::utcnow / utcnow_str`（timezone-aware 实现、
  输出 naive ISO 与历史数据同基准），替换 database / download_worker /
  helpers / library_scanner 四处重复的 `_utcnow`，全部弃用告警清零。

## 5. 验证

| 套件 | 结果 |
|------|------|
| `tests/test_*.py`（7 个模块，全程离线 mock） | 56/56 PASS，0 warnings |
| 冒烟：hitomi-chinese 在线列表（25 条）→ 主题图片（19 张） | PASS（修复后） |
| `tests/baseline_snapshot.py` | 43/43 PASS |
| 冒烟：页面/静态资源/rules/jobs/取消 404 | 全部 200/404 符合预期 |
| 冒烟：4khd 在线列表（12 条）→ 主题图片（63 张，分页正常）→ image-proxy 200 + Cache-Control | PASS |
| 冒烟：下载提交（63 张）→ 并发进度（0→3→8→10→13）→ 取消 → `canceled`(14/63) | PASS |


---

# 第三轮交付：hitomi 图片全量 404 修复（2026-09-12）

## 现象
浏览 hitomi 主题时图片加载失败，image-proxy 全部候选抓取失败后 302 直连回退：
`GET /api/online/image-proxy?url=https://w1.gold-usergeneratedcontent.net/.../bb1e....webp` → 302。

## 根因（两层叠加）
1. **扩展名**：hitomi 图片可用性由 per-file `haswebp`/`hasavif` 标志决定。
   大量画廊（如 4183442）只有 AVIF（`hasavif:1`、无 `haswebp`），但
   `_pick_image_variant` 里 `if ext == "webp"` 按文件名（`03.webp`）放行，
   生成的 `.webp` URL 在 CDN 上不存在（纯 nginx 404，实测直连/代理一致）。
2. **子域分片**：图片按 `gg.m(hash)` 分片到 w1/w2、a1/a2，任一解析偏差
   （gg.js case 列表轮换、缓存过期回退）即落到错误主机，同样 404。

修复过程用 Node 原样执行线上 common.js + gg.js 生成标准 URL，
并用 DoH + `--resolve` 逐一验证主机/IP/扩展名组合，确认
`a2.../{hash}.avif → 200`、`w2.../{hash}.webp → 200`。

## 修复
- `_pick_image_variant` 只信任 `haswebp`/`hasavif` 标志；两标志皆无时
  仅允许 jpg/jpeg/png/gif（原始位图），webp/avif 名不再放行。
- 新增共享模块 `app/services/hitomi_urls.py`（扩展名互换、子域互换、
  `images/` 原图路径、gg 分段等价值），同时接入：
  - `image_proxy._candidate_fetch_urls`：浏览 404 自愈（此前仅下载侧有回退）；
  - `download_worker._candidate_download_urls`：删除重复实现。
- `requirements.txt` 缺 PySocks，`PICCRAWLER_PROXY=socks5h://` 实际不可用，补充依赖。

## 验证
| 项 | 结果 |
|----|------|
| `tests/test_*.py`（新增 test_hitomi_urls.py） | 60/60 PASS |
| 实测：hitomi 列表 → 图片分页 → image-proxy | 200 image/avif + Cache-Control |
| 实测：用户报障的原始 webp URL 走代理 | 200（回退链 w2/webp 自愈） |
