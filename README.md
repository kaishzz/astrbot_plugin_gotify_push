# astrbot_plugin_gotify_push

将 Gotify 实时消息稳定地转发到 AstrBot 指定 UMO。

当前版本：`v1.4.0`

## 功能概览

- 监听 Gotify 流式消息并自动转发到已订阅的 UMO
- 支持按应用名或应用 token 精确匹配订阅
- 自动清理已删除应用留下的失效 token
- 订阅数据落盘到 AstrBot 数据目录，支持旧版 KV 数据自动迁移
- 内置重复消息去重和单 UMO 限流，降低刷屏和消息洪泛风险
- 持久化写入采用原子替换，降低异常退出导致配置损坏的概率

## 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 使用的是 `gotify[stream]`，这是 Gotify Python 客户端官方提供的流式依赖方式，会自动安装消息流所需组件。

## 配置项

| 配置项 | 说明 |
| --- | --- |
| `server` | Gotify 服务地址，必须以 `http://` 或 `https://` 开头 |
| `client_token` | Gotify Client Token，需要具备接收消息流权限 |
| `cleanup_interval_seconds` | 自动清理失效订阅周期，默认 `600`，最小 `60` |
| `reconnect_delay_seconds` | 流连接断开后的重连间隔，默认 `15`，最小 `3` |
| `rate_limit_window_seconds` | 单个 UMO 限流窗口，默认 `60`，最小 `1` |
| `rate_limit_max_messages` | 限流窗口内单个 UMO 最多接收的消息数，默认 `20`，最小 `1` |
| `duplicate_window_seconds` | 相同消息去重窗口，默认 `10`，设为 `0` 可关闭 |
| `max_title_length` | 转发标题最大长度，默认 `200` |
| `max_body_length` | 转发正文最大长度，默认 `2000` |

## 指令

| 指令 | 说明 |
| --- | --- |
| `/gotify_add <umo> <app\|token>` | 给指定 UMO 添加监听应用 |
| `/gotify_del <umo> [app\|token]` | 删除指定 UMO 的单个应用订阅，不带第二个参数时删除该 UMO 全部订阅 |
| `/gotify_list` | 查看全部 UMO 的订阅情况 |
| `/gotify_list <umo>` | 查看指定 UMO 的订阅情况 |
| `/gotify_clear` | 清空全部订阅配置 |

## 存储位置

订阅数据默认保存到：

```text
data/plugin_data/astrbot_plugin_gotify_push/subscriptions.json
```

如果你从旧版本升级，插件会在首次启动时尝试把旧版 KV 存储迁移到上面的文件路径。

## 转发规则

- 只有管理员可以执行订阅管理指令
- 按应用名订阅时，会绑定该名称下当前匹配到的全部 token
- 转发内容包含应用名、标题和正文，超长内容会自动截断
- 命中去重窗口的重复消息会被跳过
- 单个 UMO 在限流窗口内超过阈值后会暂停接收后续消息，直到窗口自然释放

## 本次重构重点

- 清理了重复和分散的订阅写入逻辑，统一成带回滚的事务式更新
- 增加了运行期缓存清理，避免长时间运行后限流记录和去重指纹持续堆积
- 优化了关闭流程，插件退出时会取消后台任务并尽量释放 Gotify 客户端
- 补齐了持久化失败场景下的用户提示，避免“内存里成功、磁盘上失败”的假成功
- 增加了基础单元测试，覆盖迁移、限流、缓存回收和持久化回滚

## 上线前检查建议

1. 确认 `server` 地址可从 AstrBot 所在环境访问，且带协议头。
2. 确认 `client_token` 对应的是 Gotify Client Token，而不是 App Token。
3. 先手工执行一次 `/gotify_add`、`/gotify_list`、`/gotify_del` 做冒烟测试。
4. 观察插件日志，确认 Gotify 连接建立、消息能正常转发且不会异常重连。
5. 如果消息量很大，按实际业务调整 `rate_limit_*` 和 `duplicate_window_seconds`。

## 测试

```bash
py -m unittest discover -s tests
```
