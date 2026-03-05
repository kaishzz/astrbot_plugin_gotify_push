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
| `/gotify_add <umo> <app>` | 给指定 UMO 添加一个监听应用 |
| `/gotify_del <umo> [app]` | 删除指定 UMO 的某个应用；不填 app 时删除该 UMO 全部订阅 |
| `/gotify_list` | 查询所有 UMO 的订阅 |
| `/gotify_list <umo>` | 查询指定 UMO 的订阅应用 |

