# 微信公众号发布服务（微信云托管）

把「每天生成的公众号稿件」变成自动发布：调用本服务一次，它就完成
上传正文图 → 上传封面 → 建草稿 → 提交发布。

## 一、为什么必须部署在云托管里

微信官方接口正常调用需要 `access_token` + 服务器 IP 白名单。本机出口 IP 会漂移
（实测同一台机器两个接口分别返回 `223.220.137.252` 和 `216.106.186.133`），
白名单必然失效。

微信云托管的「开放接口服务」在容器内旁加载一个代理，中转 `api.weixin.qq.com`
的请求并自动处理 token 与鉴权，因此：
- 代码里**不带** `access_token`；
- 公众号后台**不需要**启用 AppSecret、**不需要**配置 IP 白名单；
- 但必须在控制台「云调用 → 微信令牌权限」登记要调的接口路径，否则报 `85107`。

## 二、账号与环境现状（2026-11-17 核对）

| 项 | 值 |
|---|---|
| 公众号 | 国德招采数智服务平台 |
| 类型 | 服务号（已认证） |
| AppID | `wx03749235fcfffe80` |
| 云托管环境 | `prod` |
| 现有服务 | `django-w9tp`（2022 年官方模板，0 实例自动暂停） |
| 开放接口服务 | 已开启 |
| 微信令牌权限 | 已登记 6 条：`/cgi-bin/media/uploadimg`、`/cgi-bin/material/add_material`、`/cgi-bin/draft/add`、`/cgi-bin/freepublish/submit`、`/cgi-bin/freepublish/get`、`/cgi-bin/message/mass/sendall` |

## 三、部署步骤

1. **GitHub 授权**：云托管控制台 → 服务 `django-w9tp` → 部署发布 → 「选择方式」里
   点「代码权限未授权或授权过期。点击授权」，完成 GitHub 授权。
   注意：控制台只支持绑定 **公开仓库**；私有仓库只能用 CLI（本机没有 Docker，走不通）。
2. **把本目录推到 GitHub 公开仓库**（main 分支，根目录即本目录）。
3. **绑定仓库并发布**：部署发布 → 选择代码 → 代码仓库选该仓库 → 分支 `main`
   → 端口填 **8080**（必须与容器监听端口一致）→ 发布。
4. **设置环境变量**：服务设置 → 环境变量 → `AUTH_TOKEN=<一串你自己定的随机口令>`。
   不设这个变量，服务会拒绝所有请求。
5. **开启公网访问**：服务详情页「公网域名访问」开启，拿到访问域名。
6. **最小实例数设 0**：无人调用时缩容到 0，不产生常驻费用；调用时冷启动数秒。

## 四、调用方式

```bash
curl -X POST "https://<云托管公网域名>/publish" \
  -H "X-Auth-Token: <AUTH_TOKEN>" \
  -F 'meta={"title":"标题","digest":"摘要","author":"国德招标","auto_publish":true,"content_html":"<p>正文</p><p><img src=\"{{img0}}\"></p>"};type=application/json' \
  -F "cover=@cover.jpg" \
  -F "images=@pic1.jpg"
```

`meta` 字段说明：

| 字段 | 说明 |
|---|---|
| `title` | 必填，≤64 字 |
| `content_html` | 必填，正文 HTML；配图占位 `{{img0}}`、`{{img1}}` 按上传顺序替换 |
| `digest` | 摘要，≤120 字 |
| `author` | 作者名，≤16 字 |
| `auto_publish` | `true` 提交发布；`false` 只停在草稿箱 |
| `content_source_url` | 原文链接，可空 |
| `need_open_comment` | 1 开留言 / 0 关 |
| `only_fans_can_comment` | 仅粉丝可留言 |

返回体里 `steps` 记录每一步结果；失败时带 `failed_step` 与微信原始 `detail`。

## 五、排错对照

| 返回 | 含义 | 处理 |
|---|---|---|
| `85107 URL 不在白名单内` | 接口路径没登记 | 控制台「微信令牌权限」补上该路径 |
| `48001 API unauthorized` | 主体没这个接口权限 | 公众号类型/认证问题，不是代码问题 |
| `40164 invalid ip` | 走了非云调用链路 | 检查是否在容器内调用、是否带了 access_token |
| `40001 invalid credential` | 用了失效 token | 去掉 URL 里的 `access_token` 参数 |
| `45009` 类频率限制 | 触发平台次数限制 | 降低频率；群发每天 1 次是平台规则 |

## 六、尚未验证、需实测的点

1. `/cgi-bin/freepublish/submit`、`/cgi-bin/message/mass/sendall` 是否在云调用
   **支持清单**内 —— 官方文档注明「部分接口未支持云调用」，且控制台添加时不会报错，
   只有真实调用才会返回无权限。
2. 云托管常驻/冷启动的实际计费金额，以控制台账单为准。
3. 「发布」（freepublish）与「群发」（mass/sendall）的可调用次数是否互相独立，
   以本账号实测返回码为准；不要依据经验值假设。
