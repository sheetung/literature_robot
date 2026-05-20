# 文献下载机器人

Literature Robot 会根据论文标题先检索开放 PDF；如果没有找到可信 PDF，则使用 WebUI 配置的科研通 Cookie 发布求助，并在后台定时监控、自动接受上传文件并下载 PDF。

## WebUI 配置

- `enabled`：是否启用插件。
- `ablesci_cookie`：科研通 Cookie。从已登录 `www.ablesci.com` 的浏览器请求头中复制完整 Cookie。发布求助和监控下载时必填。
- `auto_publish`：找不到开放 PDF 时是否自动发布科研通求助。
- `default_points`：默认悬赏点数，默认 `30`。
- `proxy`：公开文献查询使用的 HTTP 代理（如 `http://127.0.0.1:7890`）。科研通请求不走代理。
- `allowed_user_ids` / `allowed_group_ids`：可选用户/群白名单，多个 ID 可用逗号、空格、分号或换行分隔。

## 命令

- `!lit <论文标题>`：先查开放 PDF；找不到则发布科研通求助并后台监控。
- `!lit open <论文标题>`：只尝试开放 PDF 下载，不发布求助。
- `!lit monitor <详情页URL>`：监控已有科研通求助详情页。
- `!lit once <详情页URL>`：立即检查一次详情页并尝试下载附件。
- `!lit status`：查看后台监控任务状态。
- `!lit reset`：清空所有任务记录和本地缓存。
- `!lit help`：显示帮助。

默认下载目录为 `data/literature_robot/downloads`。

## 本地缓存

插件会自动缓存最近下载的 10 篇论文 PDF。每次查询标题时会先扫描本地缓存和下载目录，命中则直接发送文件，不再发起网络请求。

## 后台监控

当 `!lit <标题>` 发布科研通求助后，插件会在后台定时检查详情页，自动接受上传的文件并下载 PDF。如果求助在科研通被手动关闭（显示"已关闭"），监控会自动停止并通过机器人发送通知。

## 安全提示

科研通 Cookie 可能用于消耗账号积分并访问你的求助页面。建议在群聊或多人环境中配置 `allowed_user_ids` 或 `allowed_group_ids`。

## 问题反馈及功能开发

[![QQ群](https://img.shields.io/badge/QQ群-965312424-green)](https://qm.qq.com/cgi-bin/qm/qr?k=en97YqjfYaLpebd9Nn8gbSvxVrGdIXy2&jump_from=webapi&authKey=41BmkEjbGeJ81jJNdv7Bf5EDlmW8EHZeH7/nktkXYdLGpZ3ISOS7Ur4MKWXC7xIx)
