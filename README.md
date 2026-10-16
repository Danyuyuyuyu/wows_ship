# 这把玩什么船

亚服持有清单优先的随机选船插件。绑定亚服账号后，有持有清单时从清单中选；没有清单时从本地保存的历史池中选取有战斗记录的船，后台每 3 天刷新。未绑定或非亚服绑定记录默认从亚服全库选。持有清单手动维护；历史记录不代表船仍在港口，绑定不验证账号归属。
完整规则见 [设计方案](../../../docs/plugins/wows_ship.md)。

## 命令

群内需要 @机器人；候选序号和“取消”无需再次 @。私聊默认可用。

| 命令 | 行为 |
| --- | --- |
| `这把玩什么船` / `随机船` / `选船` | 已绑定亚服账号时优先持有清单，无清单才用历史池；默认只从 5–10 级船中抽取 |
| `这把玩什么船 全库 日本 10 bb 金币` | 强制全库，按国家、等级、舰种和船只分类过滤 |
| `这把玩什么船 特种` | 筛选特种船；也支持 `研发`/`银币`、`金币`、`超级战舰`（超级战舰为 XI 级） |
| `这把玩什么船 我的 8` | 必须已绑定亚服账号；有清单用持有池，无清单用历史池；异常时提示原因 |
| `wows绑定 <亚服昵称>` | 搜索候选后在 60 秒内回复序号，或“取消” |
| `wows解绑` | 删除绑定和该 QQ 的待选候选 |
| `wows我的账号` | 查看绑定信息 |
| `wows更新船库` | 仅超级用户；强制检查客户端语言并完整拉取比较目录，报告新增、修改、移除、待补船名、图片失败及是否启用新版本 |
| `wows校验船库` | 仅超级用户；完整检查本地图并补缺失/损坏图片；本地目录缺失时才从 API 准备目录 |
| `wows船库状态` | 普通用户可查；显示船数、待补名称、图片失败、上次目录检查时间及可清理快照候选数（不删除） |
| `wows更新历史池` | 更新本人已绑定的亚服历史记录，绕过短期 stats 缓存；每人每分钟限一次，不修改持有清单 |
| `wows持有清单 [操作] [船]` | 本人管理自己已绑定的亚服清单：查看、添加、移除、设置、清空或撤销 |

条件用空白分隔；英文大小写、全角数字及标准罗马数字 I–XI 均支持，等级也可写作 `8级` / `VIII级`。未指定等级时默认限制为 5–10 级；指定“超级战舰”时筛选 XI 级，不受该默认等级范围限制。
分类的含义：「研发」是银币线，「特种」是 `is_special`，「金币」是直售加值船。官方数据里 **10 级起没有 `is_premium` 的船**（这类船统一标 `is_special`），所以 **10 级的「金币」会把特种一并算进来**——否则「10 级金币船」必然空池。
同义词重复合并，同一维度的不同值、未知词和“我的 全库”组合会报错（报错里会列出可用条件）。
其他服暂时只支持全库随机。默认命令和“我的”均为持有清单优先、无清单时使用历史池；空清单、损坏清单或过滤后无船不会降级。历史池为空、最新状态为隐藏、首次同步失败或过滤后无船，都不会自动换全库；显式“全库”始终旁路清单和战绩。

## 配置

沿用 `data/config.yaml`；已为本项目配置亚服应用 ID。含版本化船库更新器的新代码首次部署后需重启机器人；此后通过命令或后台巡检发布数据版本，无需再次重启。启动时后台加载已启用版本，旧目录格式也可兼容读取；缺少有效目录时后台执行完整更新，不阻塞启动。

```yaml
wows_ship:
  ENABLE: true
  WOWS_APP_ID: "你的应用 ID"
  SERVER: asia
  LANGUAGE: zh-cn
  CLIENT_LANGUAGE: auto
  CLIENT_TRANSLATIONS_PATH: ""
  CATALOG_TTL_HOURS: 24
  UPDATE_TIMEOUT_SECONDS: 1800
  IMAGE_VERIFY_DAYS: 7
  SNAPSHOT_RETENTION_DAYS: 7
  SNAPSHOT_KEEP_COUNT: 3
  UNKNOWN_SHIP_REFRESH_MINUTES: 60
  UNKNOWN_SHIP_WAIT_SECONDS: 5
  MAINTENANCE_INTERVAL_MINUTES: 60
  WATCHDOG_INTERVAL_SECONDS: 60
  HEALTH_FAILURE_THRESHOLD: 3
  ALERT_REPEAT_HOURS: 24
  PENDING_NAMES_ALERT_HOURS: 24
  BACKUP_INTERVAL_HOURS: 24
  BACKUP_RETENTION_DAYS: 30
  BACKUP_KEEP_COUNT: 3
  DISK_MIN_FREE_MB: 512
  IMAGE_RETRY_MINUTES: 10
  API_CIRCUIT_BASE_SECONDS: 60
  API_CIRCUIT_MAX_HOURS: 6
  API_CONFIG_RETRY_MINUTES: 60
  NOTIFY_SUPERUSERS: true
  HISTORY_REFRESH_DAYS: 3
  HISTORY_CHECK_INTERVAL_MINUTES: 60
  HISTORY_RETRY_MINUTES: 60
  ALLOWED_GROUPS: []
  ALLOW_PRIVATE: true
  USER_COOLDOWN_SECONDS: 5
  MAX_RENDER_CONCURRENCY: 2
  MAX_RENDER_QUEUE: 5
```

APP_ID 为空、配置非法或 ENABLE=false 时插件不响应；不修改用户保存的 ENABLE。
当前 API 客户端直连，不继承环境代理，WG 白名单需包含这条连接的 IPv4 出口。
运行中配置值变化会重建服务并关闭旧任务；候选会话不会跨配置重建保留。
其他超时、TTL 和重试配置见方案第 7 节及 `config.py`。

机器人运行期间，独立巡检默认每小时检查目录、舰图、语言资源、备份和磁盘空间：目录超过 24 小时会完整比较 API；舰图每 7 天完整校验，失败 URL 至少冷却 10 分钟，巡检会继续补齐失败图片。每小时巡检也会调用 `wows_replay` 客户端语言桥接；只有 `wows_replay` 已加载且 `ENABLE`、`UPDATE_CHECK_ENABLE`、`AUTO_INSTALL_DATA` 均开启时才检查上游，间隔沿用 `UPDATE_CHECK_INTERVAL_HOURS`（默认 24 小时）。桥接优先复用完整的本地语言文件对；缺少最新文件时才按需从第三方 `wows-replay-data` 更新目标 locale 与英文 `.mo`，不会下载整套约 400 MB 渲染数据。独立缓存位于 `data/wows_replay/translations/<locale>/<build>/`，并带 SHA-256 清单；同步失败时保留旧名称资源并进入 6 小时失败冷却。未加载或关闭 replay 插件、或 `UPDATE_CHECK_ENABLE` 关闭时不执行上游检查；`AUTO_INSTALL_DATA` 关闭时只复用本地已有语言文件，不查询上游也不下载新的 `.mo` 文件。显式设置 `CLIENT_TRANSLATIONS_PATH` 会跳过桥接。以上情况下，本地 `.mo` 和名称 JSON 指纹仍每小时检查，资源变化后自动发布新的名称 bundle。待补名称持续 24 小时会产生严重告警。

运行状态保存在 `data/wows_ship/health.json`。严重故障和恢复通知优先发给 `BotConfig.get_superuser("qq")` 中的 QQ；未配置时回退到 NoneBot `driver.config.superusers`。严重故障立即提醒，普通错误连续 3 次后提醒，同一持续问题的重复通知间隔为 24 小时。每位 QQ 会尝试多个在线 bot；部分投递失败时后续重试只补发未送达者，没有在线 bot 时留待下一轮巡检。60 秒 watchdog 会重建意外退出的历史扫描或维护任务；服务关闭后不会复活任务。同一数据目录由操作系统锁限制为单个服务实例。

低于 `DISK_MIN_FREE_MB`（默认 512 MB）时暂停船库更新和计划备份，并拒绝新增图片磁盘缓存；已有目录、名称、玩家数据和图片不会因此删除。

## 数据和降级

公共资料以版本化 bundle 发布：`data/wows_ship/metadata.json` 的 `active[<LANGUAGE>]` 指向当前 `data_version`；完整资料位于 `data/wows_ship/snapshots/<LANGUAGE>/<data_version>/`。API 目录、客户端名称表、`ships.json` 和 `download.json` 共享同一版本号。目录分页或 bundle 写入失败时保留原 active；缺少客户端名的条目记入 `pending_ship_ids`，舰图失败记入检查结果，两者都不会单独阻止完整目录发布。旧根目录 `catalog/`、`names/`、`ships.json` 和 `download_manifest.json` 继续作为兼容副本；未使用 active 元数据的旧安装仍能读取原路径。

巡检会验证 active 元数据及快照中的目录、名称和导出文件；active 无效时回退到最近的完整快照。损坏的 `metadata.json` 会保存在 `quarantine/` 后重建索引；若没有可用快照则进入降级状态并等待 API 重建。玩家 JSON 备份只用于保全和校验，不会自动恢复旧绑定或清单，以免复活已解绑或撤销的状态。每小时巡检计算绑定、玩家记录和当前快照 JSON（不包含图片）的 SHA-256 指纹；内容变化且达到 24 小时间隔时创建完整性可复核的备份。成功创建备份后，会保留最近 `BACKUP_KEEP_COUNT` 份，并回收超过 `BACKUP_RETENTION_DAYS` 的已校验旧备份；磁盘空间低于阈值时不清理旧备份、历史、玩家文件或图片。

更新器拉取全部 API 分页并比较船只增删改。目录和客户端名称的规范内容 hash 未变化时复用当前版本，只更新 `checked_at` 与检查结果；`catalog_fetched_at` 保持该版本原始抓取时间，目录 TTL 按 `checked_at` 计算。版本有变化时原子替换 active 指针；运行中的选择固定使用自己的 `data_version`，新选择热切换到新版。旧快照不会自动删除：状态命令只报告超过 7 天、且超出最近 3 份和在途操作安全时间的候选；不会删除快照、玩家数据或共享图片。图片仍按 URL 哈希跨版本共用。

`LANGUAGE` 控制 API 原始舰名和目录语言，默认 `zh-cn`；可设为 `zh-tw` 或 `en`。绑定和清单写入仍按单进程锁串行；统计和昵称为有界内存缓存，重复请求合并；历史舰船池按账号持久化到本地文件。新的公共快照、导出和 manifest 使用 UTF-8、2 空格缩进的 JSON。

### 舰名与语言

`CLIENT_LANGUAGE` 独立控制卡片和文本展示名。默认 `auto` 把 `LANGUAGE=zh-cn` 映射到亚服简体客户端 `zh_sg`，把 `zh-tw` 映射到 `zh_tw`；也可显式指定 `zh_sg`、`zh_tw`、`en` 或 `ja`。API 目录的 `name` 保留接口原值，展示层按 `ship_id_str` 对应客户端 `global.mo` 名称。

`data/wows_ship/names/<locale>.json` 是本地准备/兼容名称表；`resources/names/` 是随插件提供的内置回退表。更新时优先使用 `wows_replay` 提供的最新完整客户端翻译；也可用 `CLIENT_TRANSLATIONS_PATH` 指定翻译根目录（应含对应 locale 与 `en/LC_MESSAGES/global.mo`），此配置会停用上游翻译桥接。若有明确 `ship_code` 的船没有客户端名称，目录和原始 API 名仍会保存，该船标为待补且不参与随机；展示不会用 API 鱼名顶替客户端名，补齐翻译后巡检或手动更新即可使名称齐备的船参与选择。

有 active 版本后，运行时从对应不可变 bundle 加载名称，并按当前选择的 `data_version` 渲染；直接改 `data/names` 或 `resources/names` 不会改动已启用版本。更新器会在新 bundle 中同时发布目录和名称，切换后新的选船自动采用新名字，正在渲染的选择继续使用原版本。没有 active 元数据的旧安装仍按 `data/names` 优先、`resources/names` 回退的兼容顺序读取。

也可从已有客户端翻译离线生成准备表（命令本身无需联网，也不在运行时依赖 replay 模块）：

```bash
python tools/import_wows_ship_names.py --translations wows_replay_data/15.9.0_13357625/translations --game-build 15.9.0_13357625 --languages zh_sg zh_tw en ja --bundle
```

`--bundle` 同时写入 data 名称表和插件回退表；生成后还需通过 `wows更新船库` 或下方预下载命令发布新 bundle，才会供新选择使用。未指定 `CLIENT_TRANSLATIONS_PATH` 时，运行期由 `wows_replay` 桥接检查上游语言资源；来源是第三方 `landaire/wows-replay-data`，不是 WG 官方 API 或游戏客户端下载。桥接未启用或暂不可用时继续使用现有名称表。亚服简体名称来自 `zh_sg`，例如 `PJSB208` 对应弓张、`PJSB720` 对应安艺、`PJSB510` 对应敷岛、`PJSB018` 对应大和。

### 持有清单

清单按 QQ 和亚服账号隔离，保存在 `data/wows_ship/players/<QQ>/asia/<account_id>.json`；
只存身份和舰船 ID，不复制公用目录中的舰名或图片。清单文件缺失或 `owned` 缺失 / 为 `null` 表示尚未维护，默认回退历史池；
`ship_ids: []` 表示已确认清单为空，不回退历史。清单仅手动维护，本版不接 WG 官方登录或港口同步，也不会从历史战绩生成清单；`owned.updated_at` 是 Unix 秒时间戳。改写已有绑定或清单文件前，会在同目录保留 `.bak` 备份。

在服务器项目根目录运行维护工具。它读取当前 QQ 已绑定的亚服账号，并按 `data/config.yaml` 中的 `LANGUAGE`，用对应 active 快照校验舰船 ID；没有 active 元数据时使用该语言的兼容目录。需先准备本地目录。默认拒绝未知 ID；要明确保存尚未进入目录的新船 ID，可在 `--ship-ids` 命令后加 `--allow-unknown`。这类 ID 会保存在持有清单中，但待目录和客户端名称齐备后才参与随机；后续选船会限频触发资料同步。工具不需要应用 ID、网络或启动机器人。设置非空清单时，在 `--ship-ids` 后以空格列出舰船 ID；
不带 ID 表示确认清单为空；`--forget` 撤销已有清单、保留 JSON 文件并恢复历史池；若原本没有清单，则保持未维护状态。`--data-path` 可指定数据目录。

示例 ID `3539941072`（安艺）和 `4076811984`（弓张）来自本地目录；运行前请换成该账号实际持有的舰船 ID。
之后也可用空 `--ship-ids` 确认清单为空，或用 `--forget` 撤销清单并恢复历史池。

也可在机器人中由本人管理当前绑定的亚服清单。船名必须是唯一的准确客户端名称，也可直接给舰船 ID；机器人添加/设置时会拒绝当前船库中不存在的 ID（防止输错），尚未进入船库的新船请改用上方维护工具的 `--allow-unknown`：

```text
wows持有清单 查看
wows持有清单 添加 安艺
wows持有清单 移除 3539941072
wows持有清单 设置 安艺
wows持有清单 清空
wows持有清单 撤销
```

`清空` 会保存已确认的空清单，个人池不回退历史；`撤销` 会撤掉手动清单，默认命令改用历史池。抽到或写入历史使用记录都不会自动修改持有清单。

```bash
export QQ=替换为已绑定亚服账号的QQ
.venv/bin/python tools/set_wows_ship_inventory.py --qq "$QQ" --ship-ids 3539941072 4076811984
.venv/bin/python tools/set_wows_ship_inventory.py --qq "$QQ" --ship-ids
.venv/bin/python tools/set_wows_ship_inventory.py --qq "$QQ" --forget
```

### 历史使用舰船

历史池保存在 `data/wows_ship/players/<QQ>/asia/<account_id>.history.json`，与持有清单的 `<account_id>.json` 分开写入。文件记录 `status`、`ship_ids`、`updated_at`、`last_attempt_at`、`next_retry_at` 和 `last_error`；`ship_ids` 保存全部 `battles>0` 的舰船 ID，当前目录中没有的 ID 也保留，供目录更新后重新匹配。历史记录始终是历史数据，不会写入持有清单。

每次历史池选船都读取本地文件；快照未到期时不请求 WG API。超过 `HISTORY_REFRESH_DAYS`（默认 3 天）后，先用已有 `available` 记录继续选船并在结果中显示该历史日期，同时后台更新。最新 `hidden` 或 `empty` 状态是有效结果，会阻止个人选船，也不会沿用更早的 `available` 记录。更新失败时保留最近一次成功状态并按 `HISTORY_RETRY_MINUTES`（默认 60 分钟）退避；首次更新失败且没有成功记录时提示错误，不改用全库。

机器人启动后立即检查已绑定的亚服账号，之后按 `HISTORY_CHECK_INTERVAL_MINUTES`（默认 60 分钟）扫描到期记录；扫描也包含已有持有清单的账号，不需要玩家发命令。机器人离线时不会更新，重启后会再次检查并补做已到期更新。历史刷新、每小时扫描或选船发现目录未知/待补的 ID 时，都会合并触发船库资料同步，最短间隔由 `UNKNOWN_SHIP_REFRESH_MINUTES`（默认 60 分钟）控制，记录在 `checks/asia/<LANGUAGE>.unknown.json`。同一船库版本下，同步成功后仍补不上的 ID 最多重试 3 次（API 故障不计次），之后停止触发，直到船库发布新版本或服务重启，避免下架船或错误 ID 导致每小时整库重抓。如果没有其它可用候选，最多等待 `UNKNOWN_SHIP_WAIT_SECONDS`（默认 5 秒），仍未就绪就提示待补；新船资料就绪后按原 ID 自动匹配，历史仍只收录 WG 实际记录 `battles>0` 的船。

### 舰图与版本更新

优先使用官方高清舰图，旧目录或缺少高清图时使用轮廓图。图片按官方 URL 哈希保存在 `data/wows_ship/images/<SHA-256-官方URL>.png`，跨船库版本复用；常规更新只补新增或缺失/损坏图片，每隔 `IMAGE_VERIFY_DAYS`（默认 7 天）完整复查一次。下载失败会记录在 `checks/asia/<LANGUAGE>.json`；同一失败 URL 冷却 10 分钟后才重试，独立巡检每小时再次尝试补齐。船仍可参与随机，渲染按现有降级提供无图卡片或文本。

卡片由 `templates/ship_card.html` 渲染（`card.py` 提供数据映射）：宽 840px 深色圆角卡片，顶部显示“为 @群名片（无群名片时用 QQ 昵称）抽取”和类别徽标，主标题为客户端舰名、副标题为客户端英文名，下方依次是等级、舰种和国家标签，中部为舰图区，底部为“抽取范围 · 来源”。类别按 `tier >= 11` 记为超级战舰，其次 `is_special` 为特种船、`is_premium` 为金币船，其余为研发船。舰种、国籍（含泛亚、泛美的素材别名）和类别图标取自随插件内置的 data URI 素材 `card_assets.py`；缺少对应素材时自动退化为纯文字标签。渲染仍会屏蔽全部远程请求，因此舰图必须是本地缓存转成的 data URI。舰图区固定高 370px，图片按 760px 宽等比缩放并用渐变遮罩裁去远端；轮廓图使用反白轮廓样式；没有可用舰图时不显示图片区。舰名超长会折行，卡片整体变高。

素材原始文件保存在 `zhenxun/plugins/wows_ship/resources/card_assets/`，由 `tools/gen_wows_ship_card_assets.py` 生成内置模块：

```bash
.venv/bin/python tools/gen_wows_ship_card_assets.py
```

替换素材后重新生成 `card_assets.py` 并重启机器人即可生效；`ship_*.png` 只是设计稿样例，不会写入模块。

群内 @机器人或私聊发送 `wows更新船库` 可强制检查客户端语言并触发完整 API 目录比较；仅 SUPERUSER 可执行。更新期间仍可使用当前版本，完成后报告新增、修改、移除、待补名称、图片失败以及是否发布新版本。可用 `wows校验船库` 手动全量核验图片；存在本地目录时不拉 API。`wows船库状态` 显示上次检查、未补资料和可清理快照候选数；候选只列出，不执行删除。

也可在服务器项目根目录运行预下载工具；它使用同一个更新器，不启动机器人：

```bash
.venv/bin/python tools/download_wows_ship_data.py
```

工具默认读 `data/config.yaml`，环境变量 `WOWS_APP_ID` 可覆盖应用 ID；`--config` 和 `--data-path` 可指定配置与数据目录。默认复用本地目录做差异检查，确有变化才事务发布新版本；`--refresh-catalog` 强制重新抓取并比较完整 API 目录；`--verify-images` 强制完整检查并修复本地图片；`--workers` 设置图片并发数（默认 4，范围 1–8）。更新超时默认 1800 秒。除使用 `--verify-images` 做完整检查外，常规运行复用已有有效图片，只补新增或缺失/损坏图片。

每个版本写入 `data/wows_ship/snapshots/<LANGUAGE>/<32位data_version>/`，包含 `catalog.json`、`names/<locale>.json`、`ships.json` 与 `download.json`，并由 `metadata.json` 原子启用。供人查看的最新导出为 `exports/ships.json` 和 `manifests/download.json`；根目录 `ships.json`、`download_manifest.json` 及旧 `catalog/`、`names/` 路径保留为兼容副本。新运行时按 active bundle 读取目录和名称，`ships.json` 是字段与图片路径的导出，不是运行时目录入口。`download.json` 按官方 URL 记录图片本地路径、成功状态、字节数；`images/` 图片文件在各版本间共用。

## 验证与调试

在服务器的项目根目录运行完整测试（不联网、不向群发消息）：

```bash
.venv/bin/python -m unittest discover -s tests/plugins/wows_ship -p 'test_*.py'
```

本机 Windows 与服务器共享源码，使用独立调试环境；不要用 Windows 环境替换服务器的 `.venv`。
纯领域逻辑可以直接运行 `python -m unittest discover -s tests/plugins/wows_ship -p 'test_domain.py'`。

真实 API/截图探测工具使用临时目录，不加载机器人、不写实际绑定。通过环境变量 WOWS_APP_ID
提供应用 ID；公开目录探测无需昵称，个人探测仅查询明确授权的公开账号：

```bash
.venv/bin/python tools/wows_ship_probe.py --filters '全库 日本 10 bb' --output data/wows_ship/debug/preview.png
.venv/bin/python tools/wows_ship_probe.py --nickname <已授权的亚服昵称> --output data/wows_ship/debug/personal.png
```

2026-10-11 已验证：完整目录、官方舰图下载、个人池、版本热切换、真实 Chromium 截图、中文布局和
NoneBot 插件加载。102 项自动化测试、完整 Ruff check 和 32 个 Python 文件的 3.10 AST 检查通过。该阶段的真实群消息尚未验收；协议失败使用模拟 ActionFailed 验证。新版部署及服务重启结果见下方最新记录。

2026-10-11 客户端语言资源真实文件物化 smoke：原始 MO 无 charset 声明，结构性 UTF-8 校验兼容通过；`zh_sg` 文件 5,557,150 B、`en` 文件 5,941,831 B，SHA-256 manifest 生成后 `translations_ready=True`。随后服务器服务已重启加载桥接；本轮运行复用已有最新语言资源，没有下载新版本 MO。

2026-10-11 历史池持久化验证：102 项测试全部通过，覆盖本地重启读取、三天到期后台刷新、失败退避、隐藏/空记录覆盖、并发与解绑隔离、定时任务第二轮更新。使用模拟战绩及真实缓存舰图完成 Chromium 截图，未查询真实玩家或发送 QQ 消息。新代码需重启一次机器人；历史后台更新仅在机器人运行期间执行。

2026-10-11 差异更新与新船流程验证：完整 113 项测试通过，33 个 Python 文件通过 3.10 AST 检查，Ruff check/格式检查通过。真实公开亚服目录复查为 1017 艘、2034 图，新增/修改/移除均为 0，图片新增下载为 0，继续使用原版本（published=false）。模拟新船缺名时不进入随机池，补齐名称后按本地历史 ID 自动匹配；持有清单未创建或修改。Chromium 舰卡已检查，预览为 `data/wows_ship/debug/new-ship-pending-preview.png`。自动删除旧快照未执行，只报告候选；未重启机器人、未查询真实玩家或发送 QQ 消息。

2026-10-11 无人值守维护阶段代码验证：服务器全量插件测试 131 项通过，Ruff 全插件检查通过；当时尚未重启线上 worker。

2026-10-11 新版部署核验：新增 12 项配置已写入 `data/config.yaml`；服务器服务已成功重启加载新版，`ActiveState=active`、`SubState=running`。`health.json` 中 `history`、`watchdog`、`maintenance` 三个任务心跳正常，`active_issues=[]`；当前船库 1017 艘、待补名称 0、图片失败 0。语言桥接状态为 ready，upstream_build/local_build 均为 15.9.0_13357625，`data/wows_replay/translations/zh_sg/sync.json` 已写入。本轮复用已有最新语言目录，没有下载新版本 MO。首次生成的备份含 6 个文件且 SHA-256 校验通过；重启后因数据未变，备份状态为 skipped。ship 插件 141 项、replay 插件 81 项测试全部通过（共 222 项）；两个插件及其 tests/tools 的 Ruff 检查通过，40 个相关文件格式检查通过，57 个 Python 文件通过 AST 检查。未进行真实 QQ 群消息触发验收。
