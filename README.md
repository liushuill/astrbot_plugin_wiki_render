# astrbot_plugin_wiki_render

## 重要提示

此插件为作者使用DeepSeek v4 flash制作，由于作者并没有基础的代码编写能力，只有一定的技术栈与类似项目的了解，所以作者对于此项目的主要作用是：
- 提出方案与大致框架规划、确认技术栈
- 搜集相关的规范性/帮助文档
- 搜集参考项目，防止AI由于没有参考出现逻辑思路问题
- 测试、bug反馈
在使用本插件前，请您详细查看 `README.md`，并慎重考虑由于AI带来的可能的不确定因素。尽管作者尽力想了一些可能的bug以及安全性问题，可以在一定程度上避免此类问题，但还是需要您仔细阅读后再使用

## 概览

通用 MediaWiki 查询渲染插件：在聊天中发送 `~wiki <页面名>`，获取 MediaWiki 页面内容并渲染为图片回复。支持绑定 wiki、搜索、最近更改、随机页面、wiki 账号登录、横竖屏截图方向等。

**特性**
- 🌐 通用所有 MediaWiki：只要 Action API（`api.php`）可达即可用（维基媒体系 / Fandom / Miraheze / 自建 / 内网私有 wiki…）
- 🔌 多部署场景连接适配：本地 http、云端 https、私域网（代理 / Basic 认证 / 自签证书）、老旧版本（formatversion 自动降级）
- 🖼️ **原生渲染为默认**：打开真实页面并去掉 wiki 顶部栏截图（还原度高），失败自动回退自建模板
- 📱 横屏/竖屏切换：`~wiki screen`（横屏 PC 观感 1280px / 竖屏手机长截图观感 420px）
- 🔎 模糊识别：页面缺失时三路搜索（text/title/nearmatch）+ 选号确认
- ⏹️ 撤回取消：渲染期间撤回 `~wiki` 命令，机器人检测到后不发送结果
- 🔄 强制刷新：`~wiki 页面 --refresh` 绕过缓存立即重渲染
- 🔑 `~wiki login`：私聊专属的 wiki 账号登录（特殊页面渲染），登录态持久化并注入浏览器
- 📦 零外部渲染服务依赖：直接驱动本地 Playwright + Chromium（不依赖 AstrBot 内置的远程 t2i 服务）
- 📊 渲染日志/报告 + 站点 CSS 预取缓存 + 渲染结果缓存 + **登录/登出/删除审计**
- ⚙️ 插件页面（WebUI 管理面板）：概览 / 绑定管理 / 登录状态

## 命令

| 命令 | 说明 |
| --- | --- |
| `~wiki <页面名>` | 查询并渲染页面为图片（原生渲染去顶栏，支持 `页面名#章节`） |
| `~wiki <页面名> --refresh` | 强制刷新（绕过渲染缓存，用于改完 wiki 立即看新内容，有冷却） |
| `~wiki 前缀:页面名` | 跨 wiki 查询（需先 `~wiki iw add`） |
| `~wiki :前缀:页面名` | 强制在当前 wiki 查询（绕过 interwiki） |
| `~wiki search <关键词>` | 搜索，回复序号选择条目 |
| `~wiki id <页面ID>` | 按页面 ID 查询 |
| `~wiki random` | 随机页面（默认排除内建命名空间与讨论版，配置 `random_namespace_excludes`） |
| `~wiki rc` | 最近更改（OneBot v11 合并转发：每条含页面/编辑者/时间/摘要/页面链接/更改记录地址；其它平台文本列表） |
| `~wiki screen [方向]` | 查看/切换横竖屏：`landscape`（横屏）/ `portrait`（竖屏） |
| `~wiki set <wiki地址>` | 绑定本会话（群/私聊）的 wiki（管理员） |
| `~wiki unset` / `~wiki status` | 解除绑定 / 查看绑定与登录状态（管理员） |
| `~wiki iw add <前缀> <地址>` / `iw remove <前缀>` / `iw list` | interwiki 管理（管理员） |
| `~wiki login <用户> <密码> [#群号]` | 登录 wiki（**仅私聊**；用户名/密码含空格请用下划线 `_` 代替；带 `#群号` 为指定群的 wiki 登录，需为该群群主/管理员） |
| `~wiki logout [#群号]` | 退出登录（仅私聊；带 `#群号` 需为该群群主/管理员） |
| `~wiki help` | 帮助 |

- `~wiki` 前缀大小写不敏感，兼容全角 `～wiki`。
- **`前缀:页面` 的歧义规则**（interwiki 与命名空间都用 `前缀:` 语法）：**会话里显式配置的 interwiki 前缀优先**——`~wiki 曲目:xxx` 若 `曲目` 已通过 `iw add` 配置，则跨 wiki 查询；否则作为当前 wiki 的命名空间查询。想绕过 interwiki、强制查本 wiki 的命名空间，用 `~wiki :曲目:xxx`（前缀加一个冒号）。
- **命令冲突开关**（配置 `command_suffix_mode`，默认关）：开启后所有子命令必须以半角分号 `;` 结尾（如 `~wiki set;`），否则首词一律按页面名处理（可查询名为 set/rc 等的页面）。

## 安装

1. 将本目录（或打包 zip）放入 AstrBot 插件目录并启用，或在 AstrBot 中通过 URL/上传安装。
2. 安装依赖并准备 Chromium：
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
   > 若你的 AstrBot 环境已安装 playwright + chromium（本项目容器已具备），可跳过。浏览器不可用时插件自动降级为纯文本回复。
3. 在插件配置中设置默认 wiki（可选），或让用户在会话中执行 `~wiki set <wiki地址>`。
   > 注意：默认值为 `zh.wikipedia.org`，若你的网络无法访问（部分地区受限），请改为可达的 wiki，或直接使用 `~wiki set` 绑定。
   > 非常不建议您将本配置留空。作者在测试时有考虑到无默认地址时进行登录的情况，这只会出现一个的报错，不会有崩溃性错误。

## 权限模型（群管理级别）

- 群聊中的管理命令（`set`/`unset`/`status`/`iw`）与给群设置登录/退出（`~wiki login/logout ... #群号`）：要求**执行者是该群的群主或管理员，且机器人也在该群**（通过平台 API 查询成员角色，aiocqhttp 平台）。不是该群管理、不在该群、机器人不在该群，均会被拒绝。
- bot 全局管理员（WebUI 配置）在群聊中**不豁免**群管理检查（严格群管理）；私聊中的管理命令仍要求 bot 管理员。
- 配置 `group_admin_manage`（默认开）可关闭群管理鉴权，回退为仅 bot 管理员。
- 登录本身仍**仅限私聊**（防密码泄露）；不带 `#群号` 的私聊登录/登出仅影响私聊自己的会话，任何私聊用户可用。
- **审计**：登录/登出（命令与插件页面删除）均记录审计日志（时间、操作者、目标会话、动作、结果），插件页面「概览」展示最近操作；插件页面删除登录/绑定是 bot 管理员专属入口，不受群管理鉴权限制。
  > 不过您不必担心管理员从 '''插件页面''' 中查找wiki账户，审计并不统计登录账号的信息

## 登录（~wiki login）

- **仅限私聊**使用（群聊中密码会泄露；插件会在群聊中拒绝该命令）。
- 语法：`~wiki login <用户名> <密码>`（登录私聊自己绑定的 wiki），或 `~wiki login <用户名> <密码> #群号`（给指定群绑定的 wiki 登录）。
- 登录态按会话持久化（`data/plugin_data/astrbot_plugin_wiki_render/login/`，0600 权限），渲染与 API 请求自动携带；`~wiki logout` 退出。
- 💡 **强烈建议使用 BotPassword**（wiki 后台「机器人密码」生成的受限密码）登录，降低密码泄露风险。
- 部分 wiki 禁用了 API 登录或需要验证码，登录失败时会给出明确提示。

## 渲染模式与截图方向

- 配置 `render_mode`：
  - `native`（默认）：打开真实页面 URL，注入 CSS 隐藏站点头部/导航后对内容容器截图，观感与网页版一致；失败自动回退自建模板。
  - `self`：使用内置的自建 MediaWiki 风格模板渲染（更快、更轻，适合慢站点）。
- 截图方向 `~wiki screen landscape|portrait`：横屏 1280px PC UA（观感类似 PC 界面），竖屏 420px 手机 UA（观感类似手机长截图）；会话级记忆，全局默认在配置 `screen_default`。
- 渲染结果按 `(wiki, 页面, 方向, 登录态)` 缓存，重复查询秒回；每次渲染写入 `render_report.jsonl`，可在插件页面「概览」查看统计。

## 插件页面（WebUI）

- 需要 AstrBot 支持「插件页面」的版本（`context.register_web_api` 可用）；不支持的旧版本会静默跳过，不影响插件本体功能。
- 在 WebUI 插件详情页打开 **settings** 页面：概览（浏览器/渲染模式/报告统计/审计）、绑定管理（各会话 wiki 绑定、解除）、登录状态（各会话登录、登出）、**设置**（数组类配置：随机命名空间排除、绑定白名单——AstrBot 配置界面无法友好编辑长 JSON，改在插件页面维护）。

## 多部署场景连接配置

`~wiki set` 自动探测并解析 API 端点（支持首页 / 页面 URL / 裸域名 / 直接 api.php），自动处理 https→http 回退、自签证书回退。

| 场景 | 说明 | 需要配置的项 |
| --- | --- | --- |
| 本地 / LAN 自建 | `http://192.168.x.x/w/api.php` 或 `http://localhost:x` | 无（自动探测） |
| 云端公网 | Wikipedia / Fandom / 萌百 等 | 建议配置合理 UA |
| 私域网 | 需代理 / Basic 认证 / 自签证书 | `proxy`、`auth_user`、`auth_pass`、`verify_tls` |
| 老旧 MediaWiki（<1.25） | 不支持 formatversion=2 | 无（自动降级） |
| 非标准 scriptpath | 如 `/mediawiki/api.php` | 无（候选探测）或直接填 api.php 地址 |

## 配置项（WebUI 插件设置，已分组）

| 分组 | 配置 | 默认 | 说明 |
| --- | --- | --- | --- |
| 基础设置 | `default_wiki_api` | `https://zh.wikipedia.org/w/api.php` | 默认 wiki |
| | `user_agent` | `astrbot_plugin_wiki_render/0.1 (...)` | API 请求 UA |
| | `request_timeout` | 15 | API 请求超时（秒） |
| 连接与登录 | `verify_tls` | true | TLS 证书校验（私域自签站点关闭） |
| | `proxy` / `auth_user` / `auth_pass` | "" | 代理与 Basic 认证（私域 wiki） |
| 查询与模糊识别 | `max_query_pages` | 5 | 批量查询上限 |
| | `fuzzy_search_limit` | 3 | 缺失页候选列表上限 |
| | `auto_fuzzy_jump` | false | 唯一候选时自动跳转出图（**默认关闭**） |
| 渲染设置 | `render_mode` | `native` | 原生渲染 / 自建模板 |
| | `landscape_width` / `portrait_width` | 1280 / 420 | 横竖屏视口宽度 |
| | `render_width` | 860 | 自建模板内容宽度 |
| | `device_scale_factor` | 2 | 截图清晰度 |
| | `render_timeout` / `max_render_height` / `max_html_size` | 30 / 15000 / 2 | 渲染超时与截断上限 |
| | `prefetch_on_set` | true | 绑定后预取站点 CSS |
| | `resource_wait_ms` | 5000 | 截图前等待图片加载上限（ms） |
| | `refresh_cooldown` | 10 | `--refresh` 冷却（秒） |
| | `content_padding` | 16 | 原生渲染截图内容容器边距（px），避免文字贴边 |
| | `cache_max_files` / `cache_max_age` | 50 / 3600 | 缓存清理策略 |
| 截图方向 | `screen_default` | `landscape` | 全局默认方向 |
| 高级 | `command_suffix_mode` | false | 命令分号结尾开关 |
| | `group_admin_manage` | true | 群管理级别鉴权（群聊管理命令与 `#群号` 操作要求该群群主/管理员） |

## 数组类配置（插件页面维护）

以下两个数组配置在 AstrBot 配置界面无法友好编辑长 JSON，请在插件页面「设置」中维护（每行一项）：

- **随机页面排除命名空间**（`random_namespace_excludes`）：格式 `ID:<数字>`，如 `ID:6`（File）、`ID:3000`（自定义）。留空 = 使用内建默认（排除 MediaWiki 内建 1-15：内容命名空间 2-14 偶数项 + 全部讨论版奇数项，0 主空间保留）；填写 `[]` = 不排除任何命名空间。MediaWiki 内建命名空间 ID：0 主、1 讨论、2 用户、3 用户讨论、4 项目、5 项目讨论、6 文件、7 文件讨论、8 MediaWiki、9 MediaWiki 讨论、10 模板、11 模板讨论、12 帮助、13 帮助讨论、14 分类、15 分类讨论。
- **绑定 wiki 白名单**（`allowed_wiki_apis`）：每行一个 wiki API 地址；留空 = 不限制。填了之后只有白名单内的 wiki 能被 `~wiki set`/`iw add` 绑定。

## 开发与测试

```bash
python -m pytest tests/ -v
```

测试覆盖：端点解析、连接画像、链接重写、命令解析、编排流程、模糊无结果、原生回退、screen/后缀开关/login 私聊限制、渲染缓存等。
> 您可以在本仓库的docs/路径中找到作者在开发时的一些历程和错误信息

## 已知限制

- 原生渲染的「去顶栏」选择器覆盖常见皮肤（Vector 新旧 / MonoBook / Timeless），极特殊皮肤可能残留导航元素。
- 登录需 wiki 开启 API 登录（clientlogin）；带验证码的 wiki 登录会失败并提示。
- 长页面按高度截断；自建模板与站点原 CSS 有差异（原生模式已缓解）。
- 消歧义页/搜索选择依赖 `session_waiter`，超时（45s）后自动取消。

## 参考项目

- [akari-bot（小可机器人）wiki 模块](https://github.com/9Bakabaka/akari-bot/tree/master/modules/wiki) —— 功能与交互设计参考（命令集、缺失页候选确认、跨 wiki 前缀等）
- [astrbot_plugin_web_analyzer](https://github.com/Sakura520222/astrbot_plugin_web_analyzer) —— Playwright 渲染与插件页面（pages / register_web_api）方案参考

## License

MIT
