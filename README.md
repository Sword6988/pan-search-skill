# pan-search-skill

按影视名称检索网盘分享链接的工具。聚合 **PanSou** 与 **人人电影网** 两个数据源，
并行查询、合并去重后按网盘分类输出可直接点击的链接与提取码。

可作为一个 [WorkBuddy](https://www.workbuddy.cn) 技能使用，也可当作独立 CLI 运行。

> 仓库名是 `pan-search-skill`，但技能内部名称为 `rrdynb-pan-search`
> （见 `SKILL.md` 的 front-matter）。作为技能安装时，目录名请保持 `rrdynb-pan-search`。

---

## 特性

- **双源并行合并** —— PanSou（100+ Telegram 频道、50+ 站点插件）与人人电影网站内文章页
  同时查询，两源都命中的链接合并为一条并标注双来源，额外等待时间为 0。
- **链接卫生自动化** —— 清洗粘连的提取码、按指纹去重、剔除被截断的死链、同资源聚类折叠，
  全部是纯本地计算，不额外耗时。
- **出网出口优选** —— 显式选择代理出口（直连 / Clash / 环境变量），带可达性探测与自动顺延。
- **多层缓存与耗时预算** —— 按数据源分别设置超时、缓存 TTL、重试预算；网络失败时回退到过期缓存。
- **零第三方依赖** —— 纯 Python 标准库，无需 `pip install`。
- **249 项离线测试** —— 含 HTML fixture，覆盖提取码粘连、ID 截断、推广位剔除等真实坑。

## 数据源

| 数据源 | 覆盖 | 特点 |
|---|---|---|
| **PanSou**（主源） | 100+ Telegram 频道、50+ 站点插件 | 结构化返回，网盘类型最全，含提取码与更新时间 |
| **人人电影网**（补充源） | rrdynb.com 站内文章页 | 常含阿里云盘等 PanSou 缺漏的网盘，标题信息准确 |

支持的网盘类型：`baidu`、`aliyun`、`quark`、`guangya`、`tianyi`、`uc`、`mobile`、
`115`、`pikpak`、`xunlei`、`123`、`magnet`、`ed2k`。

## 环境要求

- **Python 3.8+**（开发与测试在 3.13 上完成）
- 无需任何第三方包
- 可选：一个本地代理（如 Clash），用于改善出网质量

## 安装

### 作为 WorkBuddy 技能

```bash
git clone https://github.com/Sword6988/pan-search-skill.git \
  ~/.workbuddy/skills/rrdynb-pan-search
```

Windows 下即 `C:\Users\<你>\.workbuddy\skills\rrdynb-pan-search`。

### 作为独立 CLI

```bash
git clone https://github.com/Sword6988/pan-search-skill.git
cd pan-search-skill
python scripts/pansearch.py "流浪地球"
```

下文用 `<skill>` 代表技能目录，`<python>` 代表你的 Python 解释器。

## 快速开始

```bash
# 双源合并搜索（推荐入口）
<python> <skill>/scripts/pansearch.py "流浪地球"

# 导出 Markdown 清单（结果多时优先用这个，避免手抄出错）
<python> <skill>/scripts/pansearch.py "流浪地球" --format md -o 流浪地球-网盘.md

# 机器可读
<python> <skill>/scripts/pansearch.py "流浪地球" --json

# 一次查多个名称
<python> <skill>/scripts/pansearch.py "死亡笔记" "我不是大师"
```

**退出码**：

| 码 | 含义 |
|---|---|
| `0` | 有结果 |
| `2` | 所有数据源都不可用 |
| `3` | 数据源可用但无匹配结果 |

### 单源命令

```bash
<python> <skill>/scripts/pansou.py resolve "流浪地球" --limit 12 --per-type 4
<python> <skill>/scripts/pansou.py health
<python> <skill>/scripts/pansou.py check "<链接1>" "<链接2>"     # 验链（需自建实例）
<python> <skill>/scripts/rrdy.py links "<rrdynb 文章URL>"        # 解析文章页
<python> <skill>/scripts/rrdy.py links 34378                    # 也接受 aid
```

### 从 rrdynb 文章链接解析

```bash
<python> <skill>/scripts/pansearch.py "片名" \
  --rrdynb-articles "https://www.rrdynb.com/movie/2020/0216/8515.html"
```

## 常用参数

```bash
--only pansou|rrdynb            # 只用单个数据源
--limit N / --per-type N        # 总数上限 / 每种网盘上限
--sort relevance|date           # 关联度优先（默认）或日期优先
--min-score N                   # 按相关性得分过滤（0~100）
--include 全集,合集             # 标题须包含（OR，走 PanSou）
--exclude 预告,花絮             # 标题须排除（OR，走 PanSou）
--cloud-types quark,aliyun      # 限定网盘类型
--src all|tg|plugin             # 限定数据通道
--plugins a,b,c                 # 限定插件
--no-group                      # 关闭同资源聚类
--keep-invalid                  # 保留形态异常的链接（默认剔除）
--verify N --drop-dead          # 校验前 N 条链接并剔除失效项
--pansou-timeout 55 --rrdy-timeout 12   # 超时按源分开设
--refresh --timing              # 绕过缓存 / 打印耗时与出口
--clear-cache                   # 清空本地缓存
```

排序与日期语义：

- `--sort relevance`（默认）按 相关性 → 网盘优先级 → 更新日期 排序；`--sort date`
  改为日期优先，新的在前、无日期的沉底。
- PanSou 的日期是资源被分享的时间；人人电影网的日期取自文章 URL 里的**帖子发布日**
  （`/movie/2020/0216/8515.html` → 2020-02-16），两者语义略有差别，按日期排序只是近似。

## 链接质量保障

结果出库前会自动做四件事：

1. **清洗** —— 剥离粘在 URL 末尾的提取码（`.../s/xxx提取码:SSTA`）使其可点击，
   并把提取码单独列出。
2. **去重** —— 按「规范主机 + 分享 ID」指纹归并。`aliyundrive.com` 与 `alipan.com`、
   `115cdn.com` 与 `115.com`、`123912.com` 与 `123684.com` 视为同一网盘；
   `/s/AAA` 与 `/s/AAA/folder/BBB` 视为同一分享；带 `:443` 端口的写法也会归一。
   分享 **ID 大小写敏感**，不会把两个不同资源误合并。
3. **形态校验** —— 剔除上游把分享 ID 截断产生的死链（例如迅雷 ID 正常 26 字符、
   被截断成 17 字符）。默认剔除并提示数量，`--keep-invalid` 可保留。
   单条链接详情：`<python> <skill>/scripts/linklib.py diagnose "<url>"`。
4. **同资源聚类** —— 同一网盘内标题相同的多个分享折叠为一条，其余链接列在该条下方，
   **不丢链接**。年份/季数保留在标题里参与区分，跨网盘不折叠。`--no-group` 可关闭。

## 出网出口（代理）

脚本不沿用 `urllib` 默认读取的环境变量，而是显式解析一次出口。优先级（前者优先）：

```
--proxy  >  PANSEARCH_PROXY  >  proxy.conf  >  系统环境变量  >  直连
```

除「命令行显式指定」外，候选项会先做 0.6 秒端口可达性探测，**不可达自动顺延**，
因此本机代理没启动时不会卡住。

```bash
<python> <skill>/scripts/netlib.py show                          # 本次会选哪条出口（不发请求）
<python> <skill>/scripts/netlib.py probe --url <url> --repeat 3  # 实测各出口质量
```

`proxy.conf` 只读首个非注释行，默认值为 `http://127.0.0.1:7897`（Clash 混合端口）。
写成 `direct` 则强制直连；删除该文件即回到系统环境变量。

> **换出口不能绕过 Cloudflare。** 人人电影网的搜索接口受 CF 托管挑战保护，
> 走任何出口都一样，需改用 WebFetch 服务端抓取（见下文「已知限制」）。

## 测试

```bash
<python> tests/run_all.py
```

当前 **249 项全部通过**（unittest 100 + legacy 149），全部离线运行、不发起网络请求，
HTML fixture 位于 `tests/fixtures/`。

> 改动脚本后请务必跑一次。**改 CLI 参数时尤其要确认它真的被下游读取** ——
> 本项目的 `--sort` 曾经只在 parser 里声明、没有任何地方使用，而文档却推荐了它，
> 属静默失效；`tests/test_pansearch.py` 里有专门锁这条的用例。

## 目录结构

```
SKILL.md                        技能定义与工作流（WorkBuddy 读这个文件）
proxy.conf                      出网出口配置
scripts/
  pansearch.py                  推荐入口：双源合并、去重、校验、验链、导出
  pansou.py                     PanSou 客户端：搜索 / 健康检查 / 链接检测
  rrdy.py                       人人电影网客户端：搜索 / 解析文章页 / 端到端解析
  linklib.py                    链接清洗、去重指纹、形态校验（纯函数）
  netlib.py                     出网出口选择与诊断
  optional/cf_browser_fetch.js  真实浏览器抓取桥（见「已知限制」，默认不用）
references/
  pansou-api.md                 PanSou 接口契约与实测行为
  site-structure.md             站点 URL 规律、CF 行为、网盘链接结构
  performance.md                加速机制、耗时预算、超时为何按源拆分
  troubleshooting.md            现象 → 原因 → 处理 速查表
tests/                          离线测试与 fixture
```

## 已知限制

- **人人电影网的搜索接口被 Cloudflare 拦截，本地无解。** 该站点依赖
  `challenges.cloudflare.com` 完成挑战，而在部分网络环境下该域名**直连与走代理均不可达**
  （实测 HTTP 000）。因此本技能的工作方式是：先用支持服务端抓取的 WebFetch 打开搜索地址
  拿到候选文章 URL，再用 `--rrdynb-articles` 回填解析。`scripts/optional/cf_browser_fetch.js`
  是为这条路径准备的浏览器桥，在上面的网络环境下无法工作，故未接入主流程。
- **PanSou 公开实例不稳定，且结果是多副本负载均衡的。** 同一关键词两次查询可能得到
  不同结果集，偶发 HTTP 400 或 SSL 握手超时（属服务端间歇故障，非脚本缺陷）。
  不要把单次结果当作全集。高频使用建议自建实例，并指向它：
  ```bash
  export PANSOU_API_BASE="https://your-instance.example.com"
  ```
  自建还能启用 `--verify` 验链（公开实例未提供 `/api/check/links` 路由）。
  多个实例用英文逗号分隔即启用故障转移。
- **`--verify` 依赖服务端接口**，实例不支持时会优雅降级并明确提示，不会静默失败。
- **同名作品极多**，检索结果需人工确认是哪一部（例如「死亡笔记」同时对应动画 37 集、
  日版真人 4 部、2015 日剧、2017 美版与 2016 年大陆同名片）。

## 免责声明

本项目**只聚合公开的网盘分享链接**，不存储、不传输、不破解任何受版权保护的内容。
所有资源版权归原权利人所有。请仅将本工具用于个人检索用途，并遵守当地法律法规与
各网盘服务条款。使用者需自行承担使用风险。

## 致谢

- [PanSou](https://github.com/fish2018/pansou) —— 开源网盘聚合搜索，本项目的主数据源
- [人人电影网](https://www.rrdynb.com) —— 补充数据源

## 许可证

[MIT](LICENSE)
