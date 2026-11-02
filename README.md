# Cevtuo-Blendergate

**One gate for every headless Blender job on your machine.**
一台机器上所有 headless Blender 作业的串行闸门：默认同时只跑 2 件，其余排队，先来先跑。

多个窗口、多个 agent、多个脚本同时 `blender -b`，每个进程都会开满线程去抢同一批核。
实测 9 个作业并行时 load 冲到 300、swap 吃满、单件从 30 秒掉到 10 分钟，
而且会互相覆盖同名 GLB。**并发不赚钱，只会让每一件都变慢。**

`blq` 用一个跨进程的文件锁 + 排队票据，把「全机同时跑几个 Blender」变成一件确定的事。

- 单文件、零第三方依赖（Python 3.9+ 标准库）
- 状态全局共享：`~/.cache/blender-queue`，跨仓库、跨窗口排同一队
- 同一个脚本已在跑或已在排队 → 直接跳过，不会算第二遍
- 绕过闸门的 headless Blender 会被点名，一条命令停掉
- 状态页按需启动，没人看就自动休眠（不常驻、不后台空转）

支持 macOS / Linux。Windows 尚未支持（依赖 `fcntl` 文件锁）。

---

## 安装

一条命令（装到 `~/.local/bin/blq`）：

```bash
curl -fsSL https://raw.githubusercontent.com/cevtuocjw/blendergate/main/install.sh | sh
```

国内网络可以用镜像：

```bash
curl -fsSL https://ghproxy.net/https://raw.githubusercontent.com/cevtuocjw/blendergate/main/install.sh | sh
```

也可以手动下载：

```bash
curl -fsSL -o blq https://github.com/cevtuocjw/blendergate/releases/latest/download/blq.py
chmod +x blq
mv blq ~/.local/bin/blq        # 或任何在 PATH 里的目录
blq --version
```

安装脚本会用 `BLQ_BIN` 覆盖安装目录：

```bash
BLQ_BIN="$HOME/bin" sh install.sh
```

前置条件：

| | |
|---|---|
| Python | 3.9 及以上（macOS 自带 / Homebrew 均可） |
| Blender | 装在 `/Applications/Blender.app`、`~/Applications/Blender.app` 或 PATH 里；也可用 `BLENDER=/path/to/Blender` 指定 |

---

## 用法

```bash
blq render scripts/xxx.py -- --res 700     # 走闸门；-b --factory-startup -P 自动带上
blq run -- <任意命令行>                     # 非 .py 的命令也走同一个闸门
blq status                                 # 谁在跑、谁在等、谁绕过了闸门
blq ps                                     # 所有 Blender 进程及来源
blq doctor                                 # 负载 / swap / 超订体检
blq open                                   # 起状态页并打开浏览器
blq serve --stop                           # 停掉状态页
blq kill-strays --yes                      # 杀掉绕过闸门的 headless Blender
```

`blq render` 会在开跑前检查去重：同一个脚本已经在跑或排队就跳过，
脚本改过（mtime 晚于作业开始）才算新任务；确实要重跑加 `--force`。

### 状态页

`blq render` / `blq run` 会自动拉起状态页，地址固定是：

```
http://127.0.0.1:8132/
```

页面会自己控制节奏：

- 队列有作业：每 3 秒静默更新（不整页刷新）
- 队列空闲：降到每 30 秒
- 标签页切到后台：完全停止请求
- 没有请求超过 5 分钟：服务自动退出；下次 `blq open` 或任意作业再唤醒
- 页面底部有「暂停自动刷新」开关

不想让它自动起：`BLQ_NO_SERVE=1 blq render ...`。

---

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `BLQ_SLOTS` | `2` | 全机并发上限 |
| `BLQ_STATE` | `~/.cache/blender-queue` | 队列状态目录 |
| `BLENDER` | 自动探测 | Blender 可执行文件 |
| `BLQ_PORT` | `8132` | 状态页端口 |
| `BLQ_SERVE_IDLE` | `300` | 状态页空闲多少秒自动退出（`0` = 常驻） |
| `BLQ_NO_SERVE` | 未设置 | 设为任意值则不自动拉起状态页 |

---

## 原理

1. 每个作业先拿一张排队票据（时间戳 + pid，字典序即先来后到）。
2. 抢 `~/.cache/blender-queue/slots/slot-N.lock` 的 `flock`；拿到锁的 fd 一直活到子进程结束。
3. 只有队首票据能抢槽位，避免多个窗口一起轮询时后到的饿死先到的。
4. 作业信息写进 `jobs/*.json`，进程没了就清掉，不会永远显示「运行中」。
5. `blq ps` 把命令行第一个 token 是 Blender 可执行文件的进程挑出来，和作业表比对，剩下的就是绕过闸门的。

不需要守护进程：排队、去重、状态都靠文件系统，所有窗口共享同一份。

---

## 卸载

```bash
rm ~/.local/bin/blq
rm -rf ~/.cache/blender-queue    # 可选：清掉队列与历史日志
```

---

## License

MIT © 2026 cevtuo
