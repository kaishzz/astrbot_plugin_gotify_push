# astrbot_plugin_gotify_push

### 介绍

- 监听 Gotify 消息并推送

---

| 参数         | 解释                                         |
| ------------ | -------------------------------------------- |
| Server       | 请输入服务器地址                             |
| Client Token | Gotify Client Token                          |

---

### 指令

| 指令 | 说明 |
| --- | --- |
| `/gotify_add <umo> <app\|token>` | 给指定 UMO 添加监听应用（仅支持应用名或应用 token；名称为精确匹配，按名称时会添加所有同名应用 token） |
| `/gotify_del <umo> [app\|token]` | 删除指定 UMO 的某个应用；不填参数时删除该 UMO 全部订阅（名称为精确匹配，按名称时会删除所有同名应用 token） |
| `/gotify_list` | 查询所有 UMO 的订阅（基于 token 显示当前应用名 + token，并自动清理已删除应用） |
| `/gotify_list <umo>` | 查询指定 UMO 的订阅（基于 token 显示当前应用名 + token，并自动清理已删除应用） |
| `/gotify_clear` | 清除全部 UMO 订阅配置 |

- 插件会每 10 分钟自动刷新应用并清理已删除应用的失效 token 订阅。

