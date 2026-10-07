---
name: rrdynb-pan-search
description: 按影视名称搜索网盘资源并返回可直接打开的网盘链接与提取码。聚合两个数据源：PanSou（开源聚合搜索，覆盖 TG 频道 + 站点插件，支持百度/夸克/阿里/迅雷/UC/天翼/115/123/PikPak/磁力等）与人人电影网 rrdynb.com 文章页。当用户给出电影、电视剧、动漫、综艺等名称并要求「网盘链接」「云盘下载」「夸克/百度/迅雷/阿里云盘资源」「在线观看」，或提到 PanSou / pansou / 人人电影网 / rrdynb / rrdynb.com 时使用。也支持从 rrdynb 文章 URL 直接解析网盘链接。
---

# 网盘资源搜索（PanSou + 人人电影网）

按影视名称检索网盘分享链接。默认**同时查询两个数据源并合并去重**，
返回按网盘分类的链接清单与提取码。

## 何时使用

- 用户给出影视名称，要求「网盘链接」「云盘下载」「夸克/百度/迅雷/阿里云盘资源」
- 用户给出 rrdynb.com 的文章链接，要求解析其中的网盘地址
- 用户提到 PanSou / pansou / 人人电影网 / rrdynb

## 数据源

| 数据源 | 覆盖 | 特点 |
|---|---|---|
| **PanSou**（主源） | 100+ Telegram 频道、50+ 站点插件 | 结构化返回、网盘类型最全、有提取码与更新时间 |
| **人人电影网**（补充源） | 站内文章页 | 常含阿里云盘等 PanSou 缺漏的盘，标题信息准确 |

两源**并行**查询，总额外等待时间为 0；同一链接在两个源都命中时合并为一条并标注双来源。

> **重要**：人人电影网的搜索接口被 Cloudflare 托管挑战保护，而本机
> `challenges.cloudflare.com` **不可达**（实测直连与走 Clash 均 HTTP 000），
> 因此换代理也没用，本地任何客户端都过不了挑战。该源的工作方式是：
> 由支持服务端抓取的 WebFetch 打开搜索地址拿候选文章 URL，
> 再用 `--rrdynb-articles` 回填解析。见「标准工作流」第 2 步。

## 核心工具

| 文件 | 作用 |
|---|---|
| `scripts/pansearch.py` | **推荐入口**：双源合并搜索、去重、形态校验、验链、导出 |
| `scripts/pansou.py` | PanSou 客户端：搜索 / 健康检查 / 链接有效性检测 |
| `scripts/rrdy.py` | 人人电影网客户端：搜索 / 解析文章页 / 端到端解析 |
| `scripts/linklib.py` | 链接清洗、去重指纹、形态校验（纯函数，`diagnose` 可直接排障） |
| `scripts/netlib.py` | 出网出口（代理）选择与诊断；三个脚本共用，**不要绕过它直接 urlopen** |
| `proxy.conf` | 出口配置（首个非注释行）；删掉即回到系统环境变量 |
| `scripts/optional/cf_browser_fetch.js` | 真实浏览器抓取桥，**本机实测无效**（`challenges.cloudflare.com` 不可达）；不在主流程、无脚本引用，仅作记录 |
| `tests/run_all.py` | 一键跑全部测试（离线，含 fixture） |
| `references/site-structure.md` | 站点 URL 规律、Cloudflare 行为、网盘链接结构、干扰链接黑名单 |
| `references/pansou-api.md` | PanSou 接口契约、响应结构与实测行为（维护时先看这份） |
| `references/performance.md` | 五层加速机制、耗时预算、超时为何要按源分开 |
| `references/troubleshooting.md` | 现象→原因→处理 速查表、诊断命令、改动注意事项 |

所有命令用绝对路径调用脚本；Python 解释器优先使用受管版本。

## 出网出口（代理）

本机有多条出网路径，**质量差异显著**。同目标、各 6 次采样实测（2026-10-07）：

| 出口 | 人人电影网文章页 | PanSou /health | PanSou 搜索 |
|---|---|---|---|
| 本机 Clash `127.0.0.1:7897` | **6/6 成功，1.56s** | 1.77s | **13.1s** |
| 直连（不走代理） | 2/6 成功，2.11s | 4.58s | 0/2 失败 |
| 环境变量代理 `127.0.0.1:59323` | 3/6 成功，4.03s | 1.89s | 52.9s |

因此脚本**不沿用 `urllib` 默认读取的环境变量**，而是显式解析一次出口。
优先级（前者优先）：`--proxy` > `PANSEARCH_PROXY` > `proxy.conf` > 系统环境变量 > 直连。
除「命令行显式指定」外，候选项先做 0.6s 端口可达性探测，**不可达自动顺延**。

```bash
<python> <skill>/scripts/netlib.py show                      # 本次会选哪条出口（不发请求）
<python> <skill>/scripts/netlib.py probe --url <url> --repeat 3   # 实测各出口质量
<python> <skill>/scripts/pansearch.py "片名" --proxy 127.0.0.1:7897
<python> <skill>/scripts/pansearch.py "片名" --proxy direct
```

`--timing` 会一并打印本次实际使用的出口与跳过的候选项。**换出口前先跑
`netlib.py probe` 用数据说话**，不要凭猜测。注意：换出口**不能**绕过 Cloudflare。

## 标准工作流

### 第 1 步：合并搜索（首选）

```bash
<python> <skill>/scripts/pansearch.py "<影视名称>"
```

脚本并行查询两个数据源，按网盘类型分桶输出，每条含链接、提取码、标题、更新日期、
来源。默认每种网盘最多显示 6 条（`--per-type`），总数上限 30（`--limit`）。

**判断退出码：**

- `0` —— 有结果，整理后回复用户（见第 4 步）
- `2` —— **所有数据源都不可用**。若提示里出现「人人电影网：搜索接口被 Cloudflare
  拦截」，则该源改走第 2 步；若 PanSou 也不可用，检查实例配置
- `3` —— 数据源可用但**无匹配结果**。告知用户未找到，建议换更短的名称或官方原名

### 第 2 步（rrdynb 被 CF 拦截时）：WebFetch 取候选 + 回填

用 **WebFetch** 打开搜索地址：

```
WebFetch(url="https://www.rrdynb.com/plus/search.php?q=<urlencoded 名称>&pagesize=11",
         prompt="列出页面上所有影视资源结果：每条给出完整标题和文章链接
                 （形如 /movie/YYYY/MMDD/<aid>.html，需补全为 https://www.rrdynb.com 开头）。
                 若为 Cloudflare 挑战页，只回复 CLOUDFLARE CHALLENGE。")
```

拿到候选文章 URL 后**一条命令回填**（脚本会解析并合并进统一输出）：

```bash
<python> <skill>/scripts/pansearch.py "<影视名称>" \
    --rrdynb-articles "https://www.rrdynb.com/movie/2020/0216/8515.html,https://www.rrdynb.com/movie/2020/0408/9843.html"
```

只想要该源的结果时，也可以直接用 `rrdy.py links "<url1>" "<url2>"`。

### 第 3 步：验证命中

搜索常返回同名或近似资源（例如「王者天下」同时对应**真人电影系列**与**日本动漫各季」；
「死亡笔记」有动画 37 集 / 日版真人 4 部 / 2015 日剧 / 2017 美版 / 2016 大陆同名片）。
结合输出的标题、以及 rrdynb 文章的 `article.alias` / `keywords` 确认命中的是哪一部；
标题与用户诉求不符时，宁可说明「未能确认」也不要含糊带过。

按需进一步收窄：

```bash
<python> <skill>/scripts/pansearch.py "唐朝诡事录" --include 全集,合集 --exclude 预告,花絮
<python> <skill>/scripts/pansearch.py "王者天下" --cloud-types quark,aliyun,baidu
<python> <skill>/scripts/pansearch.py "王者天下" --src plugin        # 只搜站点插件
<python> <skill>/scripts/pansearch.py "王者天下" --sort date         # 按日期排序
<python> <skill>/scripts/pansearch.py "王者天下" --min-score 80      # 丢掉弱相关结果
```

> 前四项走 PanSou；`--sort` 与 `--min-score` 是**本地**行为，对两源合并后的结果生效。

排序与日期语义：

- `--sort relevance`（默认）按相关性 → 网盘优先级 → 更新日期；`--sort date` 改为
  日期优先，新的在前、**无日期的沉底**。
- PanSou 的日期是资源被分享的时间；人人电影网的日期取自文章 URL 里的**帖子发布日**
  （`/movie/2020/0216/8515.html` → 2020-02-16），两者语义略有差别，用日期排序只是近似。
- `--min-score N` 取 0~100，按相关性得分过滤（被剔除的条目会如实列出原因与得分）。

### 第 4 步：整理输出

按网盘分组呈现，链接与提取码明确列出，保证可直接点击/复制：

```
《流浪地球（加长版）》(2019)
  阿里云盘   https://www.alipan.com/s/inozxF6c518
  夸克网盘   https://pan.quark.cn/s/05ecb2c47fc9
  迅雷云盘   https://pan.xunlei.com/s/VOMus_IlDOpGJuP-_iw67tTnA1?pwd=zd94
  百度网盘   https://pan.baidu.com/s/1gcwCn-kYry7PBKPnRBvYiQ?pwd=phq2
```

要求：
- 提取码已含在 URL 里（`?pwd=`）时不必重复写；分离给出时单独标注「提取码：xxxx」
- 按版本分组（真人版 / 动漫 / 各季），标明哪一部对应哪条链接
- 只呈现脚本实际提取到的内容，不要编造或拼接链接
- 结果全部失效或为空时如实说明，不要臆造
- PanSou 是**多副本负载均衡**的共享实例，两次调用结果可能有差异；不要把单次结果当作全集
- 结果较多时优先导出而不是手抄（既省 token 又避免抄错）：

```bash
<python> <skill>/scripts/pansearch.py "<名称>" -o "<输出目录>/<名称>-网盘.md"
<python> <skill>/scripts/pansearch.py "<名称>" --json -o out.json
<python> <skill>/scripts/pansearch.py "<名称>" --verify 8 --drop-dead   # 验链并剔除失效项
```

## 链接质量的自动化保障

结果出库前会自动做四件事，都是纯本地计算、不额外耗时：

1. **清洗**：剥离粘在 URL 末尾的提取码（`.../s/xxx提取码:SSTA`），使其可点击；
   同时把提取码单独列出来。
2. **去重**：按「规范主机 + 分享 ID」指纹归并——`aliyundrive.com` 与 `alipan.com`、
   `115cdn.com` 与 `115.com`、`123912.com` 与 `123684.com` 视为同一网盘；
   `/s/AAA` 与 `/s/AAA/folder/BBB` 视为同一分享；带 `:443` 端口的写法也会归一。
   分享 **ID 大小写敏感**，不会误合并。
3. **形态校验**：剔除上游把分享 ID 截断产生的死链（如迅雷 ID 正常 26 字符、
   截断成 17 字符）。默认剔除并提示数量，`--keep-invalid` 可保留；
   单条链接详情用 `linklib.py diagnose "<url>"` 查看。
4. **同资源聚类**：同一网盘内标题相同的多个分享折叠成一条，其余链接以 `└` 列在
   该条下方——**不丢链接**，只是不再各占一行。年份/季数保留在标题里参与区分
   （`死亡笔记(2006)` 与 `死亡笔记(2016)` 不会被并成一条），跨网盘也不折叠。
   用 `--no-group` 关闭。

## PanSou 使用与配置

### 实例地址

默认使用项目作者提供的公开演示实例 `https://so.252035.xyz`（无需认证）。
可指向自建或其它实例：

```bash
export PANSOU_API_BASE="https://your-instance.example.com"
<python> <skill>/scripts/pansou.py health --api https://your-instance.example.com
```

多个实例用英文逗号分隔即启用**故障转移**：

```bash
export PANSOU_API_BASE="https://a.example.com,https://b.example.com"
```

启用认证的实例需提供 JWT Token：

```bash
export PANSOU_API_TOKEN="eyJhbGc..."
<python> <skill>/scripts/pansou.py resolve "<名称>" --token "$PANSOU_API_TOKEN"
```

> 共享实例资源有限，脚本已内置缓存与退避重试。需要大量查询或要用
> `--verify` 验链（公开实例未提供该路由），建议按官方 README 用 Docker 自建：
> `github.com/fish2018/pansou`。

### 单源命令

```bash
<python> <skill>/scripts/pansou.py resolve "<名称>" --limit 12 --per-type 4
<python> <skill>/scripts/pansou.py health
<python> <skill>/scripts/pansou.py check "<链接1>" "<链接2>"
<python> <skill>/scripts/rrdy.py links "<文章URL>"
<python> <skill>/scripts/rrdy.py links 34378          # 也接受 aid
```

`--cloud-types` 支持：`baidu`、`aliyun`、`quark`、`guangya`、`tianyi`、`uc`、
`mobile`、`115`、`pikpak`、`xunlei`、`123`、`magnet`、`ed2k`。

### 统一入口的其它选项

```bash
<python> <skill>/scripts/pansearch.py "<名称>" --only pansou|rrdynb
<python> <skill>/scripts/pansearch.py "死亡笔记" "我不是大师"      # 一次查多个名称
<python> <skill>/scripts/pansearch.py "<名称>" --json
<python> <skill>/scripts/pansearch.py "<名称>" --format md -o out.md
<python> <skill>/scripts/pansearch.py "<名称>" --min-score 80 --no-group
<python> <skill>/scripts/pansearch.py "<名称>" --refresh --timing
<python> <skill>/scripts/pansearch.py --clear-cache
```

多名称时逐个查询并依次输出，每个名称各占一份 `--deadline` 预算，总耗时约为
N × 单次；`--json` 会输出 `{"keywords": [...], "results": [...]}` 结构，
文本/Markdown 则用分隔线拼接后写入同一个 `-o` 文件。

`--proxy` / `--timeout` / `--retries` 三个脚本都支持，可写在子命令前或后。
超时按源分开设更稳（见 `references/performance.md`）：

```bash
<python> <skill>/scripts/pansearch.py "<名称>" --pansou-timeout 55 --rrdy-timeout 12
```

## 性能与排障

日常重复查询通常在 1 秒内返回；冷启动时 PanSou 本身要 12~45 秒（服务端检索
上百个源），属正常现象。

- 加速原理、耗时预算表、超时参数为何按源拆分 → `references/performance.md`
- 现象→原因→处理 速查表、诊断命令、改动注意事项 → `references/troubleshooting.md`

## 注意事项

- **出口选择影响速度与成功率**：遇到莫名其妙的超时或 400，先跑
  `netlib.py probe` 确认是不是出口问题，再怀疑脚本。
- **不要用 `challenge-platform` 判定 Cloudflare 干扰页**：该脚本被注入到所有正常
  页面，用它判定会全部误报。
- **PanSou 的 HTTP 400 属服务端间歇故障**：三条出网路径都会偶发 400
  （响应体是 HTML 而非 JSON），脚本把它当瞬时错误重试且不写负缓存，
  **不要改成「4xx 一律短路」**。
- **务必剔除干扰链接**：rrdynb 文章页含 `display:none` 的通用推广位，指向
  `pan.baidu.com/download`、`/s/1nvT6eE1` 等**与影片无关**的链接。
  `rrdy.py` 已按 href 与锚文本双重规则过滤，不要绕过脚本手工提取。
- **两个站点都只聚合分享链接**，资源版权归原权利人。回复时只呈现链接本身，
  不要输出影片正文内容，也不要对资源合法性作背书。
- 不要把公开实例当作无限额度使用；批量查询前先想清楚是否需要，并依赖缓存。
- 改动脚本后跑 `tests/run_all.py`（当前 249 项全绿）。**改 CLI 参数时务必确认
  它真的被下游读取**——`--sort` 曾经只在 parser 里声明、没有任何地方使用，
  文档却推荐了它，属静默失效；`tests/test_pansearch.py` 里有专门锁这条的用例。
