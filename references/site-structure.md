# rrdynb.com 站点结构与提取规则

## 站点概况

- 域名：`https://www.rrdynb.com`（镜像 `www.rrdyw.net` 常不可用）
- 建站系统：**DedeCMS V57_UTF8_SP2**
- 内容：电影 / 电视剧 / 老电影（zongyi）/ 动漫（dongman）资源页，每篇文章页正文里给出网盘链接

## URL 规律

| 用途 | 形式 | 备注 |
|---|---|---|
| 首页 | `/index.html` | 可直接抓取 |
| 站内搜索 | `/plus/search.php?q=<关键词>&pagesize=11` | **被 Cloudflare 托管挑战保护** |
| 高级搜索表单 | `/plus/heightsearch.php` | 页面可抓，但表单 action 仍是 `search.php` |
| 分类列表 | `/plus/list.php?tid=<栏目ID>&PageNo=<页码>` | 可直接抓取，每页 10 条 |
| 静态栏目页 | `/movie/list_<tid>_<page>.html` | 可直接抓取 |
| 文章详情 | `/movie/YYYY/MMDD/<aid>.html` | 可直接抓取 |
| 文章详情（别名） | `/plus/view.php?aid=<aid>` | 可直接抓取 |
| 站点地图 | `/sitemap.xml` | 2022 年生成、仅 554 条，**过时**，勿作索引 |
| RSS | `/plus/rss.php` | 返回内容极少，无实用价值 |

栏目 ID 参考（由高级搜索页 `<select name="typeid">` 得到）：
`1` 电影、`2` 电视剧、`3` 老电影、`4` 动漫、`5` 其他信息。

## Cloudflare 保护（关键坑）

- 站点启用了 Cloudflare **托管挑战（managed challenge / Turnstile）**。
- **只有 `/plus/search.php` 被规则命中**；首页、分类列表、`/plus/view.php`、文章详情页均可直接 HTTP 抓取。
- 挑战脚本来自 `https://challenges.cloudflare.com/turnstile/v0/g/<id>/api.js`。
  - 若该域名不可达（部分网络环境如此），则**任何**浏览器或 HTTP 客户端都无法完成挑战，
    表现为：先返回 403 + 「请稍候…」干扰页，随后长时间挂起直至超时。
  - 此时唯一可行办法是用服务端抓取能力（WorkBuddy 的 **WebFetch**）打开搜索地址。
- 判定干扰页时**不要**用 `challenge-platform` 字符串 —— Cloudflare 会把该脚本注入
  到**所有**正常页面里，用它判定会导致全部误报。可靠特征见 `scripts/rrdy.py`
  中的 `looks_like_challenge()`：`window._cf_chl_opt`、`cf_chl_rt_tk`、
  `Enable JavaScript and cookies to continue`、`正在进行安全验证`，以及标题为
  `Just a moment` / `请稍候` 且页面总长明显偏小。

### 本机实测（2026-10-07）：挑战域名不可达，本地无解

| 目标 | 直连 | 走 Clash `127.0.0.1:7897` |
|---|---|---|
| `https://challenges.cloudflare.com/turnstile/v0/api.js` | HTTP 000，0.46s | HTTP 000，0.48s |
| `/plus/search.php?q=...` | 403 | 403 |
| 文章详情页 | 2/6 成功 | 6/6 成功，1.56s |

结论：

1. `challenges.cloudflare.com` 在本机**两条路径都不通**，因此
   `scripts/optional/cf_browser_fetch.js`（真实浏览器桥，已移入 `optional/`，不在主流程）在本机**无法通过挑战**，
   属网络环境限制而非脚本缺陷；`playwright-core` 也未安装。
2. **换代理不能绕过 CF** —— 搜索接口在任何出口下都返回 403。
3. 该站**文章页不受影响**，所以可行路径是「服务端抓取拿候选文章 URL」+
   「本地解析文章页」两段式，对应 `pansearch.py --rrdynb-articles`。

## 站点响应不稳定

同一 URL 会出现 `200` 与连接超时（`000`）交替的情况，实测需要 1–4 次重试。
`scripts/rrdy.py` 已内置退避重试；调用时若偶发失败，直接重跑即可，不要判定为脚本缺陷。

## 文章页网盘链接结构

正文（剧情简介所在的内容区）内以固定格式罗列，例如：

```html
<span ...>资源：</span><a href="https://pan.quark.cn/s/7528ca80276f" target="_blank">
  <span style="color: rgb(0,0,255);">夸克网盘</span></a><br>
...<span ...>资源：</span><a href="https://pan.xunlei.com/s/XXXX?pwd=b7vd#">
  <span>迅雷云盘</span></a>
...<span ...>资源：</span><a href="https://pan.baidu.com/s/XXXX?pwd=kpjg">
  <span>百度网盘</span></a> <span>提取码：</span>kpjg
```

特征：
- 锚文本即网盘名称（夸克网盘 / 迅雷云盘 / 百度网盘 / 阿里云盘 …）
- 提取码有两种承载方式：URL 查询串 `?pwd=xxxx`，或紧随其后的正文文本
  「提取码：xxxx」「密码: xxxx」
- 同一篇文章可能给出多个网盘，互为备份

### 必须剔除的干扰链接

文章页底部有一个 `display:none` 的隐藏提示框，里面的链接**与当前影片无关**，
是站点通用推广位，每个页面都一样：

| 锚文本 | 链接 | 处理 |
|---|---|---|
| 百度网盘离线下载 | `https://pan.baidu.com/download` | 剔除 |
| 使用早期版本迅雷5.8下载 | `https://pan.baidu.com/s/1nvT6eE1` | 剔除 |
| 使用utorrent下载 | `https://pan.baidu.com/s/1kVQSszd` | 剔除 |

剔除规则已写入 `rrdy.py` 的 `is_decoy()`：按 href 片段（`pan.baidu.com/download`、
`/download/`）与锚文本关键词过滤。锚文本关键词已**收窄**为
「离线下载 / 迅雷5.8 / utorrent / BitComet / 客户端下载 / 电脑客户端 /
手机客户端 / 软件下载」——早期直接匹配裸词「客户端」会误杀
「UC网盘客户端」这类有效锚文本。

## 搜索结果页结构

DedeCMS 搜索页把命中词用 `<font color="red">` 高亮，且结果都是指向
`/movie|/dianshiju|/zongyi|/dongman/YYYY/MMDD/<aid>.html` 的链接。
因此解析器采用**通用链接抽取 + 高亮标记加权**策略，不依赖具体模板类名，
在搜索页与分类列表页上都能工作。

## 已支持的网盘域名

百度网盘 `pan.baidu.com` / `yun.baidu.com`、夸克 `pan.quark.cn`、
迅雷云盘 `pan.xunlei.com`、阿里云盘 `aliyundrive.com` / `alipan.com`、
115 `115.com` / `115cdn.com` / `anxia.com`、
123 网盘 `123pan.com` / `123684.com` / `123865.com` / `123912.com` / `123pan.cn`、
蓝奏云 `lanzou*`、天翼云盘 `cloud.189.cn`、
移动云盘 `caiyun.139.com` / `yun.139.com`、联通云盘 `pan.wo.cn`、腾讯微云 `weiyun.com`、
UC 网盘 `uc.cn`、城通网盘 `ctfile.com`、PikPak、Google Drive、OneDrive、MediaFire。

新增域名要改**两个**地方：

1. `rrdy.py` 的 `PAN_PROVIDERS`（中文展示名 → 用于 rrdynb 侧识别）
2. `linklib.py` 的 `HOST_GROUPS`（规范主机 → 用于跨源去重与形态校验）

只改前者会导致同一资源的别名域名被当成两个链接重复展示。
