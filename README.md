# astrbot_plugin_gotify_push

将 Gotify 的实时消息转发到 AstrBot 指定 UMO 的插件。

当前版本：`v1.0`

## 功能特性

- 监听 Gotify 流式消息并自动转发到已订阅的 UMO
- 支持按应用名称或应用 Token 添加订阅
- 自动清理已删除应用对应的失效订阅
- 内置重复消息去重和单 UMO 限流
- 订阅数据持久化到 AstrBot 数据目录，并采用原子写入降低损坏风险

## 安装

```bash
pip install -r requirements.txt
```

依赖使用 `gotify[stream]`，会一并安装 Gotify 消息流所需组件。

## 配置说明

| 配置项 | 说明 |
| --- | --- |
| `server` | Gotify 服务地址，必须以 `http://` 或 `https://` 开头 |
| `client_token` | Gotify Client Token，需具备接收消息流权限 |
| `cleanup_interval_seconds` | 自动清理失效订阅周期，默认 `600`，最小 `60` |
| `reconnect_delay_seconds` | 流连接断开后的重连间隔，默认 `15`，最小 `3` |
| `rate_limit_window_seconds` | 单个 UMO 的限流窗口，默认 `60`，最小 `1` |
| `rate_limit_max_messages` | 限流窗口内单个 UMO 最多接收的消息数，默认 `20`，最小 `1` |
| `duplicate_window_seconds` | 相同消息去重窗口，默认 `10`，设为 `0` 可关闭 |
| `max_title_length` | 转发标题最大长度，默认 `200` |
| `max_body_length` | 转发正文最大长度，默认 `2000` |

注意：`client_token` 需要填写 Gotify 的 Client Token，不是 App Token。

## 管理指令

| 指令 | 说明 |
| --- | --- |
| `/gotify_add <umo> <app\|token>` | 给指定 UMO 添加监听应用 |
| `/gotify_del <umo> [app\|token]` | 删除指定 UMO 的单个应用订阅；不带第二个参数时删除该 UMO 全部订阅 |
| `/gotify_list` | 查看全部 UMO 的订阅情况 |
| `/gotify_list <umo>` | 查看指定 UMO 的订阅情况 |
| `/gotify_clear` | 清空全部订阅配置 |

## 存储位置

订阅数据默认保存到：

```text
data/plugin_data/astrbot_plugin_gotify_push/subscriptions.json
```

## 行为说明

- 只有管理员可以执行订阅管理指令
- 按应用名订阅时，会绑定该名称下当前匹配到的全部 Token
- 转发内容包含应用名、标题和正文，超长内容会自动截断
- 命中去重窗口的重复消息会被自动跳过
- 超出限流阈值后，窗口期内的后续消息会被暂时丢弃

## 测试

```bash
py -m unittest discover -s tests
```
