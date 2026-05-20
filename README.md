# Literature Robot

[中文](./readme/README_zh_Hans.md)

Literature Robot resolves a paper title, tries to download an open PDF first, and falls back to an AbleSci request when no reliable open PDF is found. Accepted AbleSci uploads are monitored in the background and downloaded automatically.

## WebUI Configuration

- `enabled`: Turns the plugin on or off.
- `ablesci_cookie`: Raw Cookie header copied from a logged-in `www.ablesci.com` browser session. Required for publishing AbleSci requests and monitoring request downloads.
- `auto_publish`: When enabled, `!lit <title>` publishes an AbleSci request if no trusted open PDF is downloaded.
- `default_points`: AbleSci bounty points, default `30`.
- `proxy`: HTTP proxy for public literature sources (e.g. `http://127.0.0.1:7890`). AbleSci requests do NOT use the proxy.
- `allowed_user_ids` / `allowed_group_ids`: Optional allowlists. Separate multiple IDs with commas, spaces, semicolons, or new lines.

## Commands

- `!lit <paper title>`: Search open sources first. If no trusted PDF is found, publish an AbleSci request and start background monitoring.
- `!lit open <paper title>`: Only try open-PDF download.
- `!lit request <paper title>`: Explicitly run the full workflow.
- `!lit monitor <detail_url>`: Monitor an existing AbleSci request page.
- `!lit once <detail_url>`: Check one AbleSci request page immediately and try downloading attachments.
- `!lit status`: Show background job status.
- `!lit help`: Show help.

Downloaded files are stored under `data/literature_robot/downloads` by default.

## Local Cache

The plugin caches the last 10 downloaded papers. Before making any network request, it checks the local cache and the download directory. On a cache hit, the file is sent directly without any network lookup.

## Background Monitoring

When `!lit request` or `!lit <title>` publishes an AbleSci request, the plugin periodically checks the request page in the background, accepts uploaded files, and downloads the PDF. If the request is manually closed on AbleSci (showing "已关闭" / "closed"), monitoring stops automatically and a notification is sent.

## Security Notes

The AbleSci Cookie can spend account points and access your request pages. Configure user/group allowlists when this plugin is available in shared chats.

## Questions and Feedback

[![QQ Group](https://img.shields.io/badge/QQ群-965312424-green)](https://qm.qq.com/cgi-bin/qm/qr?k=en97YqjfYaLpebd9Nn8gbSvxVrGdIXy2&jump_from=webapi&authKey=41BmkEjbGeJ81jJNdv7Bf5EDlmW8EHZeH7/nktkXYdLGpZ3ISOS7Ur4MKWXC7xIx)
