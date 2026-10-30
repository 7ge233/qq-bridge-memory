# 隐私与数据

一句话：**所有记忆都存在你这台机器上，这个服务不向任何外部地址发送数据。**

下面按"存了什么、日志记了什么、什么时候联网、怎么彻底删除"分开写。

## 1. 数据存在哪

默认都在项目目录下，全部被 `.gitignore` 排除，不会跟着仓库上传：

| 路径 | 内容 | 默认保留 |
| --- | --- | --- |
| `data/qq-memory.db` | 记忆本体：消息流水、提炼后的事实、事件时间、向量 | 长期；流水 90 天、软删 30 天 |
| `data/qq-memory.db-wal` / `-shm` | SQLite WAL 模式的两个伴随文件 | 随主库 |
| `data/console-token` | 控制台口令（首次启动生成，不回显） | 长期 |
| `logs/qq-memory-YYYY-MM-DD.log` | 按天滚动 JSONL 日志 | 7 天 |
| `backup/qq-memory-*.db` | `VACUUM INTO` 生成的备份 | 7 份 |
| `models/` | 向量模型文件（约 200MB） | 长期 |

目录可以改到别的盘：`QQ_MEMORY_DB`、`QQ_MEMORY_LOG_DIR`、`QQ_MEMORY_BACKUP_DIR`、
`QQ_MEMORY_MODEL_DIR`。

## 2. 存的是谁的数据

每条记忆都带 `scope`，只有两种：

- `private:<QQ号>`：和某人的私聊
- `group:<群号>`：某个群

召回时按当前会话的 `scope` 过滤，**私聊记忆不会出现在群里，A 群的记忆不会出现在 B 群**。
这条规则可以在 `tests/test_privacy.py` 里核对。

机器人在群里被唤醒时，写入的是"被唤醒消息 + 上下文若干条"，不追全群流水；
群友没有参与的对话不会被记录。

## 3. 日志里有什么

日志用来排查"为什么没召回 / 为什么写失败"，只记元数据，不记原文：

```json
{"ts":"2026-10-30T16:16:34+08:00","level":"INFO","msg":"http","endpoint":"/v1/stats",
 "method":"GET","status":200,"latency_ms":4,"client":"127.0.0.1"}
```

- 记：端点、方法、状态码、耗时、条数、内容长度、**账号指纹**（QQ 号做 SHA-256 后取前 8 位）
- 不记：消息原文、图片、昵称、群名
- 异常日志只记异常类型名（如 `TimeoutError`），不记业务数据

日志按天滚动，默认保留 7 天，超期自动删除。

## 4. 什么时候联网

| 时机 | 目标 | 说明 |
| --- | --- | --- |
| 首次启动 | Hugging Face（或你设的 `HF_ENDPOINT` 镜像） | 下载向量模型；设 `QQ_MEMORY_MODEL_DOWNLOAD=0` 可禁用 |
| 运行期 | 无 | 召回、写入、备份都在本机完成 |
| 手动查看 | 无 | 控制台只监听 `127.0.0.1:8787` |

区分"记忆插件"和"聊天链路"：QQ 消息本来就要经过 SnowLuma → qq-bridge → DSH 发给你选的模型服务，
那是宿主的行为；本项目只在本机多写一份数据库，不额外把内容发出去。

## 5. 谁能访问

- `/v1/*` 与 `/console` 只监听 `127.0.0.1`，外部机器连不上
- `/v1/*` 需要请求头 `X-Memory-Token`；未配置 token 时只接受本机调用（启动日志会警告）
- 控制台需要单独口令，登录失败 5 次按来源锁 10 分钟，写操作带 CSRF 校验并记审计日志
- 控制台只提供软删与停用，物理删除要自己动数据库文件

## 6. 怎么彻底删掉

```powershell
# 1. 停掉记忆服务（关掉那个窗口，或 Ctrl+C）
# 2. 删数据（不可恢复）
Remove-Item data\qq-memory.db, data\qq-memory.db-wal, data\qq-memory.db-shm -Force
Remove-Item logs\*.log -Force
Remove-Item backup\*.db -Force
```

只想让机器人"忘掉最近这段"，用控制台软删即可：软删的记录 30 天内可恢复，之后不再参与召回；
物理行仍在库里，需要彻底清空时按上面的命令删除。

## 7. 备份与恢复

`run_backup.cmd`（或 `python backup.py`）做 `VACUUM INTO` 并跑一次 `integrity_check`，保留最近 7 份。
恢复方式是停服务、把备份文件复制回 `data/qq-memory.db`、再启动。

备份里同样是完整的聊天记忆，**别把 `backup/` 和 `data/` 传到公开仓库或网盘**。

## 8. 关于许可

本项目代码是 MIT。运行时会用到第三方组件，各有自己的许可：

- `fastembed`、`onnxruntime`、`jieba` 等 Python 依赖：见各项目仓库，版本锁定在 `requirements.txt`
- 向量模型 `BAAI/bge-small-zh-v1.5`：以模型仓库的许可声明为准，本项目不重新分发模型文件

如果打算用它处理他人的数据（例如替别人托管机器人），请自行确认所在地区对聊天记录的合规要求。
