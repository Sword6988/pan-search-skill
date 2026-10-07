# PanSou 接口与实例参考

上游项目：<https://github.com/fish2018/pansou>（Go，MIT，作者公开演示站 <https://so.252035.xyz>）
本文记录 `scripts/pansou.py` 依赖的接口契约，便于后续维护与故障判断。

## 接口一览

| 接口 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 搜索 | GET / POST | `/api/search` | 主接口，按关键词聚合搜索 |
| 健康检查 | GET | `/api/health` | 实例状态、插件与频道清单，无需认证 |
| 链接检测 | POST | `/api/check/links` | 检测网盘分享链接当前是否有效。**公开演示实例未编译该路由**（实测 404，见下文） |
| 登录 | POST | `/api/auth/login` | 启用认证的实例用，换取 JWT |

启用认证时（`AUTH_ENABLED=true`）除 `/api/health` 外都需带
`Authorization: Bearer <jwt>`。

## /api/search 参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `kw` | string | **必填**，关键词 |
| `res` | string | `merge`(默认，仅 merged_by_type) / `results` / `all` |
| `src` | string | `all`(默认) / `tg`(仅 TG 频道) / `plugin`(仅插件) |
| `cloud_types` | string[] | 限定网盘：`baidu` `aliyun` `quark` `guangya` `tianyi` `uc` `mobile` `115` `pikpak` `xunlei` `123` `magnet` `ed2k` |
| `plugins` | string[] | 限定插件，缺省搜全部 |
| `channels` | string[] | 限定 TG 频道，缺省用服务端默认配置 |
| `conc` | number | 并发数，缺省为「频道数 + 插件数 + 10」 |
| `refresh` | bool | 服务端强制刷新，绕过服务端缓存 |
| `filter` | object | `{"include":[...],"exclude":[...]}`，对标题做 OR 过滤 |
| `ext` | object | 传给插件的自定义参数 |

### 响应结构（两种形态都要兼容）

部署版本间存在差异，`pansou.py: unwrap()` 同时处理：

```json
// 新版：外层信封
{"code": 0, "message": "success", "data": {"total": 34, "merged_by_type": {...}}}

// 旧版：直接返回
{"total": 34, "results": [...], "merged_by_type": {...}}
```

`merged_by_type` 是「网盘类型 → 链接数组」，元素字段：

| 字段 | 说明 |
|---|---|
| `url` | 网盘链接。天翼云盘等会把访问码写进 URL，形如 `https://cloud.189.cn/t/xxx（访问码：3djx）` |
| `password` | 提取码 |
| `note` | 资源标题/说明 |
| `datetime` | 更新时间，未知时为 `0001-01-01T00:00:00Z` |
| `source` | 来源标识：`tg:频道名` 或 `plugin:插件名` |
| `images` | TG 消息配图（可选） |

## 实测行为（重要）

以下均为在**本机 + 公开实例**上的实测结论，脚本默认值据此设定：

1. **延迟波动极大**。命中服务端缓存约 0.4s；未命中时实测 10.2s、12.1s、17.5s、
   31.3s、43.6s，偶发超过 100s。服务端要并发检索上百个频道与插件，慢是常态。
   → 故 `DEFAULT_TIMEOUT = 55`，`SEARCH_BUDGET = 100`，`DEFAULT_DEADLINE = 120`。
2. **`src=plugin` 不更快、还可能更少**。实测「流浪地球」用 `src=plugin` 12.1s 但
   `total=0`；`cloud_types` 过滤发生在服务端收集之后，也不减少耗时。不要用它当「快速模式」。
3. **公开实例是多副本负载均衡**。连续三次 `/api/health` 分别返回
   71 / 86 / 87 个插件、6 / 56 / 108 / 147 个频道，`channels_count` 每次都不同。
   → 结果非确定性属正常，不要把单次结果当全集。
4. **会限流**。实测出现过 HTTP 403（响应体 17 字节、非 JSON）。
   → `pansou.py` 只在 4xx **响应体是 JSON** 时才认定为 API 的确定性错误（含 401/403
   认证问题）；非 JSON 的 403/429 归为 `rate_limited`，提示稍后重试。
5. **本机网络需经代理时**，代理偶发返回 `502 Bad Gateway`（隧道失败）或
   `400 The plain HTTP request was sent to HTTPS port`（HTML 错误页）。
   两者都是**瞬时**故障，必须重试——这也是「4xx 不无脑短路」这一设计的原因。
6. **公开演示实例没有链接检测接口**。实测 `POST /api/check/links`、
   `/api/links/check`、`/api/check` 三条路径**全部 404**（响应体 `404 page not found`，
   0.86s）。因此 `pansearch.py --verify` 在默认实例上会返回 `unsupported` 并**优雅降级**
   （照常输出结果，只在提示里说明未做验链）。要真正验链必须自建实例。
7. **结果里会出现粘着提取码的 URL**。实测遇到过
   `https://123912.com/s/pbeojv-bLFbh提取码:SSTA`——提取码既没进 `password` 字段也没括号，
   直接拼在 URL 末尾。`linklib.clean_url()` 负责剥离（带括号/不带括号/`?` 紧接三种写法），
   否则这类链接既不可点击、又会因 path 不同而逃过去重。
8. **结果里会出现被截断的分享 ID**。实测迅雷出现过 17 字符的
   `https://pan.xunlei.com/s/VP2QdPpoCbscUHKAM`（正常为 26 字符），点开是死链。
   `linklib.validate()` 会剔除并提示数量。

## 自建实例

```bash
# 前后端集成版（含 Web UI）
docker run -d --name pansou -p 8888:80 ghcr.io/fish2018/pansou:latest

# 仅后端 API
docker run -d --name pansou-api -p 8888:8888 ghcr.io/fish2018/pansou-api:latest
```

自建后把客户端指向它即可：

```bash
export PANSOU_API_BASE="http://127.0.0.1:8888"
# 多个实例用英文逗号分隔即启用故障转移
export PANSOU_API_BASE="https://a.example.com,https://b.example.com"
```

具体镜像名、环境变量与认证配置以项目 README 为准（本项目文档不复制上游配置细节，
避免与上游版本漂移脱节）。
