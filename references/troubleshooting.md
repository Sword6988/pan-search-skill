# 排障

按现象查表。**先看数据再改代码**：出口问题用 `netlib.py probe`，
解析问题用 `linklib.py diagnose "<链接>"`。

## 现象 → 原因 → 处理

| 现象 | 原因 | 处理 |
|---|---|---|
| `pansearch` 退出码 2 | 两源都不可用 | 看输出里的逐源说明：CF 拦截走 WebFetch 兜底 + `--rrdynb-articles` 回填；PanSou 不可用则换实例 |
| 请求无端变慢 / 超时 / 偶发 400 | 出口线路质量差 | `netlib.py probe` 实测；把快的写进 `proxy.conf` 或用 `--proxy` |
| 提示「端口不可达，已跳过」 | 配置的代理没启动 | 属正常的自动顺延；启动代理或改用 `--proxy direct` |
| 提示「不支持 socks5 代理」 | urllib 的 ProxyHandler 只认 http/https | 改用客户端提供的 HTTP 混合端口（Clash 常见 7897）；不要填 `socks5://` |
| rrdynb 提示「搜索接口被 Cloudflare 拦截」 | 本机无法完成 JS 挑战（**换出口无效**） | 走 WebFetch 兜底，用 `--rrdynb-articles` 回填 |
| PanSou 提示「服务不可达」 | 实例临时故障 / 代理 502 | 重跑；或 `--api` 指向其它实例 |
| PanSou 提示「实例限流或过载」 | 公开实例 403/429 限流 | 稍后重试；高频查询请自建实例 |
| PanSou 提示「实例需要 Token」 | 实例启用了认证 | 设置 `PANSOU_API_TOKEN` 或加 `--token` |
| `--verify` 提示「实例未提供 /api/check/links」 | 公开演示实例未编译该路由（实测 404） | 属正常降级，结果照常输出；要验链需自建实例 |
| `health` 返回的插件/频道数每次都不同 | 公开实例是多副本负载均衡 | 属正常现象，结果以当次为准 |
| 首次搜索耗时 12~45s | 服务端并发检索上百个源 | 正常现象；命中缓存后为秒回 |
| 抓取返回 `HTTP 0` | 站点/实例瞬时不可达 | 脚本自动回退过期缓存；无缓存时重跑 |
| 结果疑似陈旧 | 命中本地缓存 | 加 `--refresh` 强制重取 |
| 换了新片名却秒回兜底地址 | 命中 rrdy 端点不可用标记（默认 6h） | 属正常加速；确需探测加 `--force-search` |
| 命中过期的错误结果 | 缓存被污染 | `--clear-cache` 后重试 |
| 某条链接点开是死链 | 上游把分享 ID 截断了 | 默认已剔除并提示数量；`linklib.py diagnose` 可查看原因 |
| 结果里同一条链接出现多次 | 上游用了不同别名主机/子路径 | 已按 `linklib` 指纹归并；若仍有漏网，把域名补进 `HOST_GROUPS` |
| 搜索有结果但都不相关 | 关键词过宽 | 换官方原名或「片名+年份」；或用 `--include/--exclude` 收窄 |
| 某篇文章返回 0 条链接 | 该页确实没有网盘链接，或文章已下架 | 换下一个候选；增大 `--top` / `--rrdynb-articles` 多给几篇 |

## 诊断工具速查

```bash
# 出口：本次会选哪条 / 各候选出口实测质量
<python> <skill>/scripts/netlib.py show
<python> <skill>/scripts/netlib.py probe --url https://www.rrdynb.com/ --repeat 3

# 链接：清洗 + 指纹 + 形态校验的完整流水线视图
<python> <skill>/scripts/linklib.py "https://123912.com/s/pbeojv-bLFbh提取码:SSTA"

# 实例状态 / 批量验链
<python> <skill>/scripts/pansou.py health
<python> <skill>/scripts/pansou.py check "<链接1>" "<链接2>"

# 单源排查
<python> <skill>/scripts/rrdy.py search "<名称>"
<python> <skill>/scripts/pansou.py resolve "<名称>" --json
```

## Cloudflare 的现实约束（实测，2026-10-07）

- `challenges.cloudflare.com` 在本机**直连与走 Clash 均不可达**
  （HTTP 000，0.46s 即失败），因此**任何本地客户端都过不了托管挑战**，
  包括 `optional/cf_browser_fetch.js` 浏览器桥。
- 该站的**文章页不受影响**（1.56s，6/6 成功），只有 `/plus/search.php` 返回 403。
- 结论：rrdynb 的搜索只能靠**服务端抓取**（如 WorkBuddy 的 WebFetch），
  本地脚本负责「拿到文章 URL 之后的解析」。

## 改动脚本时的注意事项

- **不要绕过 `netlib.open_url` 直接 `urlopen`**：会丢失出口优选，退化成读取环境变量。
- **不要把 PanSou 的 4xx 改成一律短路**：实测三条出网路径都会偶发 HTTP 400
  （响应体是 HTML 而非 JSON），属服务端间歇故障，必须重试且不写负缓存。
- **不要用 `challenge-platform` 判定 Cloudflare 干扰页**：该脚本被注入到
  所有正常页面，用它判定会全部误报。只用 `looks_like_challenge` 里的特征串。
- **不要对链接 path 做整段 `lower()`**：分享 ID 大小写敏感，会把两个资源误合并。
- **不要在 `canon_host` 里保留端口**：`pan.quark.cn:443` 会变成「未识别主机」，
  既让同一条链接去不掉重，又让 `validate()` 走放行兜底（等于不校验）。
- **新增/改动 CLI 参数后，确认它真的被下游读取**：`--sort` 曾经只在 parser 里
  声明、没有任何地方使用，而 SKILL.md 却推荐了它 —— 属静默失效，比参数不存在更糟。
- **聚类（`group_items`）不要跨网盘合并，也不要把 variants 的提取码补到代表条目**：
  variants 是另一条分享，它的码对代表条目无效，补上去会让用户拿错码。
- **改动前先跑 `tests/run_all.py`**，改动后再跑一次；`tests/fixtures/` 里的
  HTML 覆盖了推广位剔除、提取码粘连、ID 截断这几类真实坑。
