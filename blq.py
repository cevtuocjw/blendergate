#!/usr/bin/env python3
"""blq —— 全机 Blender 串行闸门（Cevtuo-Blendergate）。

为什么要这个：多个窗口各自 `blender -b ...` 的时候，同时跑几件、谁先跑
全凭运气，产出文件还可能互相覆盖。`blq` 把并发变成一个可控的数字。

规矩只有一条：**headless 渲染一律 `blq render xxx.py -- args`，不要直接调 Blender 二进制。**
`blq` 默认按整机 CPU 压力动态决定并发（从 1 到上限，默认上限是核数的一半），
其余排队；谁先来谁先跑。也可以用 `BLQ_SLOTS` / `--slots` 固定成某个数。

用法（装好后直接 `blq ...`；在本仓库里等价于 `python3 scripts/blq.py ...`）：
    blq render scripts/g33-shaker-glass.py             # -b --factory-startup -P 已内置
    blq run -- <任意命令行>                             # 自定义命令行也走闸门
    blq status            # 谁在跑、谁在等、等了多久
    blq ps                # 当前所有 Blender 进程，绕过闸门的会被标红
    blq doctor            # 负载 / swap / 并发数体检
    blq open              # 确保状态页在跑，并直接打开浏览器（不用记地址）
    blq serve --stop      # 手动停掉状态页服务
    blq page              # 生成静态快照页（不会自动刷新）
    blq kill-strays --yes # 杀掉绕过闸门的 headless Blender（需显式确认）

状态页：`blq render/run` 会在开跑时自动拉起一个，`blq open` 随时唤醒；
没有浏览器在看时就自动退出（默认空闲 300 秒），不再常驻。

状态目录：$BLQ_STATE，默认 ~/.cache/blender-queue（跨仓库、跨窗口共用一份）。
Blender 路径：$BLENDER，默认自动找 /Applications/Blender.app 等常见位置。

项目主页：https://apps.cevtuogrnd.com/blendergate/
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

__version__ = "1.1.0"


def _find_blender() -> str:
    """BLENDER 环境变量优先；否则找常见安装位置，都没有就交给 PATH。"""
    env = os.environ.get("BLENDER")
    if env:
        return env
    for c in (
        Path("/Applications/Blender.app/Contents/MacOS/Blender"),
        Path.home() / "Applications/Blender.app/Contents/MacOS/Blender",
        Path("/usr/bin/blender"),
        Path("/usr/local/bin/blender"),
        Path("/opt/homebrew/bin/blender"),
    ):
        if c.exists():
            return str(c)
    return shutil.which("blender") or "blender"


BLENDER = _find_blender()
STATE = Path(
    os.environ.get(
        "BLQ_STATE",
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "blender-queue",
    )
)
SLOT_DIR = STATE / "slots"
JOB_DIR = STATE / "jobs"
WAIT_DIR = STATE / "waiting"
LOG_DIR = STATE / "logs"
# 并发：默认动态（按实时 CPU 压力决定），也可以用 BLQ_SLOTS / --slots 固定。
CORES = os.cpu_count() or 1
FIXED_SLOTS = int(os.environ["BLQ_SLOTS"]) if os.environ.get("BLQ_SLOTS") else None
MAX_SLOTS = int(os.environ.get("BLQ_MAX_SLOTS", str(max(2, min(8, CORES // 2)))))
MIN_SLOTS = int(os.environ.get("BLQ_MIN_SLOTS", "1"))
CPU_TARGET = float(os.environ.get("BLQ_CPU_TARGET", "0.85"))
ADMIT_GAP = float(os.environ.get("BLQ_ADMIT_GAP", "3"))
MAX_SWAP_GB = float(os.environ.get("BLQ_MAX_SWAP_GB", "4.5"))
MIN_FREE_MB = int(os.environ.get("BLQ_MIN_FREE_MB", "1024"))
PAGE = STATE / "status.html"
MONITOR = STATE / "monitor.json"
ADMIT_STAMP = STATE / "last-admit"
STATUS_PORT = int(os.environ.get("BLQ_PORT", "8132"))
# 没有浏览器在看时，状态页服务最多再活这么久就自己退出（0 = 永不退出）。
SERVE_IDLE = int(os.environ.get("BLQ_SERVE_IDLE", "300"))
SERVE_PID = STATE / "serve.pid"


def ensure_dirs() -> None:
    for d in (SLOT_DIR, JOB_DIR, WAIT_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def human(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def read_jobs() -> list[dict]:
    ensure_dirs()
    out = []
    for p in JOB_DIR.glob("*.json"):
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        j["_file"] = str(p)
        if not _alive(j.get("pid")):
            # 进程没了但文件还在 = 上次被 kill -9 或崩溃，清掉，别让它永远占着“运行中”。
            p.unlink(missing_ok=True)
            continue
        j["elapsed"] = time.time() - j.get("start", time.time())
        out.append(j)
    out.sort(key=lambda j: j.get("start", 0))
    return out


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def next_seq() -> str:
    # 用「时间戳 + pid」当票据号：同一微秒不可能两个人，字典序就是先来后到。
    return f"{time.time():.6f}-{os.getpid()}"


def waiting_tickets() -> list[dict]:
    ensure_dirs()
    out = []
    for p in WAIT_DIR.glob("*.json"):
        try:
            w = json.loads(p.read_text())
        except Exception:
            p.unlink(missing_ok=True)
            continue
        if not _alive(w.get("pid")):
            p.unlink(missing_ok=True)
            continue
        w["_file"] = str(p)
        out.append(w)
    out.sort(key=lambda w: w.get("seq", ""))
    return out


def find_duplicate(tag: str) -> str | None:
    """同一个脚本已经在跑/已经在排队，就别再来一份。

    2026-10-29 实测：窗口 A 的渲染被我收编后，窗口 A 以“作业挂了”为由又原样起了一个，
    窗口 B 同时也在跑同一个脚本 —— 同一张图被算三遍，还往同一个 GLB 上互相覆盖。
    脚本改过（mtime 晚于作业开始）才算新任务，放行。
    """
    if not tag or not os.path.exists(tag):
        return None
    mtime = os.path.getmtime(tag)
    name = os.path.basename(tag)

    def same(rec: dict) -> bool:
        # 老作业可能没记 tag（收编过来的），退回按脚本名比 —— 同一个脚本只该算一遍
        return rec.get("tag") == tag or (name and rec.get("label") == name)

    for j in read_jobs():
        if same(j) and mtime <= j.get("start", 0):
            return f"已在槽位 {j.get('slot')} 跑 {human(j['elapsed'])}"
    for w in waiting_tickets():
        if same(w) and mtime <= w.get("since", 0):
            return f"已在队里等了 {human(time.time() - w.get('since', time.time()))}"
    return None


def _scan_slots(limit: int) -> tuple[int, list[tuple[int, int]]]:
    """数一下已经占了几个槽位，顺手把空闲槽位的锁 fd 拿在手里。"""
    busy, free = 0, []
    for i in range(1, limit + 1):
        fd = os.open(SLOT_DIR / f"slot-{i}.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free.append((i, fd))
        except OSError:
            os.close(fd)
            busy += 1
    return busy, free


_CPU_CACHE: dict[str, float | None] = {"t": 0.0, "v": None}


def _proc_stat() -> tuple[int, int]:
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def _sample_cpu() -> float | None:
    """当前整机 CPU 占用比例（0~1）。取不到就返回 None。"""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["top", "-l", "1", "-n", "0", "-s", "0"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"CPU usage:.*?([\d.]+)%\s*idle", out)
            return 1.0 - float(m.group(1)) / 100 if m else None
        if sys.platform.startswith("linux"):
            a = _proc_stat()
            time.sleep(0.2)
            b = _proc_stat()
            di, dt = b[0] - a[0], b[1] - a[1]
            return 1.0 - di / dt if dt > 0 else None
    except Exception:
        return None
    return None


def cpu_busy() -> float | None:
    """给手动命令用的一次性采样（进程内缓存 2 秒）。"""
    now = time.time()
    if now - _CPU_CACHE["t"] < 2.0:
        return _CPU_CACHE["v"]
    _CPU_CACHE["t"] = now
    _CPU_CACHE["v"] = _sample_cpu()
    return _CPU_CACHE["v"]


def memory_state() -> tuple[bool, str]:
    """返回 (是否还有余量, 一句人能看懂的说明)。"""
    try:
        if sys.platform == "darwin":
            s = subprocess.run(
                ["sysctl", "-n", "vm.swapusage"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            m = re.search(r"used = ([\d.]+)M", s)
            if m:
                used = float(m.group(1)) / 1024
                return used <= MAX_SWAP_GB, f"swap {used:.1f}G/{MAX_SWAP_GB:.1f}G"
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                info = {
                    line.split(":")[0]: int(line.split()[1])
                    for line in f if ":" in line
                }
            avail = info.get("MemAvailable", 0) / 1024
            return avail >= MIN_FREE_MB, f"可用 {avail:.0f}M"
    except Exception:
        pass
    return True, "内存状态未知"


def _read_monitor() -> dict:
    try:
        return json.loads(MONITOR.read_text())
    except Exception:
        return {}


def _write_monitor(
    busy: int,
    limit: int,
    cpu: float | None,
    cpu_ts: float,
    swap: str,
    allow: bool,
    resample: bool = False,
) -> None:
    try:
        MONITOR.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "busy": busy,
                    "limit": limit,
                    "cpu": cpu,
                    "cpu_ts": cpu_ts,
                    "swap": swap,
                    "allow": allow,
                    "resample": resample,
                }
            )
        )
    except OSError:
        pass


def _last_admit() -> float:
    try:
        return ADMIT_STAMP.stat().st_mtime
    except OSError:
        return 0.0


def _admit(fixed: int | None) -> tuple[int, int] | None:
    """在 admission 锁里决定「现在能不能再进一件」。

    动态模式：第一件永远放行；之后要求整机 CPU 低于目标、swap 还有余量，
    并且距离上一次放行至少 ADMIT_GAP 秒 —— 给刚起的作业一点时间把 CPU 吃上去，
    否则会在采样到之前一口气放进来一堆。
    """
    limit = fixed if fixed is not None else MAX_SLOTS
    guard = os.open(STATE / "admit.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(guard, fcntl.LOCK_EX)
        busy, free = _scan_slots(limit)
        now = time.time()
        mon = _read_monitor() if fixed is None else {}
        cpu: float | None = mon.get("cpu")
        cpu_ts: float = mon.get("cpu_ts", 0.0)
        resample = bool(mon.get("resample"))
        mem_txt = ""
        if fixed is not None:
            allow = busy < fixed
            resample = False
        elif busy < MIN_SLOTS:
            allow = True
        else:
            mem_ok, mem_txt = memory_state()
            gap_ok = now - _last_admit() >= ADMIT_GAP
            if busy < MAX_SLOTS and gap_ok:
                # 所有等待者共享同一个采样，最多每 8 秒跑一次 top；
                # 刚放行过（resample）就强制重新采样，别拿放行前的数字连着放。
                if cpu is None or now - cpu_ts > 8 or resample:
                    cpu = _sample_cpu()
                    cpu_ts = now if cpu is not None else 0.0
                    resample = False
                allow = mem_ok and (cpu is None or cpu < CPU_TARGET)
            else:
                allow = False
        if allow and free:
            slot_no, slot_fd = free.pop(0)
            for _, fd in free:
                os.close(fd)
            if fixed is None:
                ADMIT_STAMP.touch()
                resample = True
            # 刚放行：下一次判断强制重新采样，别拿旧数字连着放
            _write_monitor(busy + 1, limit, cpu, cpu_ts, mem_txt, True, resample)
            # 锁住 fd 必须活到子进程结束，这里直接返回 fd 由调用方持有。
            return slot_fd, slot_no
        _write_monitor(busy, limit, cpu, cpu_ts, mem_txt, allow)
        for _, fd in free:
            os.close(fd)
        return None
    finally:
        os.close(guard)


def acquire_slot(
    label: str, slots: int | None, wait: bool, tag: str = ""
) -> tuple[int, int] | None:
    """拿一个槽位。返回 (fd, 槽位号)；wait=False 且无空位时返回 None。

    先登记票据再抢槽位，抢到前检查「有没有比我更早的票据还在等」——
    否则多个窗口一起 poll，后到的可能一直抢赢先到的（饥饿）。
    slots=None 走动态并发。
    """
    ensure_dirs()
    ticket_seq = next_seq()
    ticket_path = WAIT_DIR / f"{ticket_seq}.json"
    ticket_path.write_text(
        json.dumps(
            {
                "seq": ticket_seq,
                "pid": os.getpid(),
                "label": label,
                "tag": tag,
                "cwd": os.getcwd(),
                "since": time.time(),
            }
        )
    )
    try:
        while True:
            mine = waiting_tickets()
            first = mine[0]["seq"] if mine else ticket_seq
            if first != ticket_seq:
                if not wait:
                    return None
                time.sleep(1.0)
                continue
            got = _admit(slots)
            if got:
                return got
            if not wait:
                return None
            time.sleep(1.0)
    finally:
        ticket_path.unlink(missing_ok=True)


def cmd_run(
    argv: list[str], slots: int | None, label: str | None, tag: str, force: bool = False
) -> int:
    if not argv:
        print("blq run: 后面要跟命令行（用 -- 分隔）", file=sys.stderr)
        return 2
    label = label or _guess_label(argv)
    if not tag:
        py = next((a for a in argv if a.endswith(".py")), "")
        if py and os.path.exists(py):
            tag = os.path.abspath(py)
    if not force:
        dup = find_duplicate(tag)
        if dup:
            print(f"[blq {now()}] 跳过：{label} {dup} —— 不重复跑。改过脚本或要强制重跑加 --force")
            return 0
    # 有作业就顺手把状态页拉起来；没人在看时它会自己休眠，不常驻。
    url = ensure_status_server()
    if url:
        print(f"[blq {now()}] 状态页：{url}  （随时重开：blq open）", flush=True)
    mode = f"并发上限 {slots}" if slots is not None else f"动态并发，上限 {MAX_SLOTS}"
    print(f"[blq {now()}] 排队中：{label}（{mode}）", flush=True)
    t0 = time.time()
    got = acquire_slot(label, slots, wait=True, tag=tag)
    assert got
    fd, slot_no = got
    waited = time.time() - t0
    print(
        f"[blq {now()}] 开始：{label} · 槽位 {slot_no} · 等待 {human(waited)}",
        flush=True,
    )

    job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"
    log_path = LOG_DIR / f"{job_id}.log"
    job_file = JOB_DIR / f"{job_id}.json"
    proc = None
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                # nice：就算闸门被人绕过，我们的作业也不会去跟别人抢满核。
                ["nice", "-n", "10", *argv],
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, "BLQ_JOB_ID": job_id, "BLQ_TAG": tag},
                start_new_session=True,  # 让 ^C 只打到自己，不去连带别人的进程
            )
            job_file.write_text(
                json.dumps(
                    {
                        "id": job_id,
                        "pid": proc.pid,
                        "label": label,
                        "tag": tag,
                        "slot": slot_no,
                        "cwd": os.getcwd(),
                        "cmd": argv,
                        "log": str(log_path),
                        "start": time.time(),
                    }
                )
            )
            rc = proc.wait()
    except KeyboardInterrupt:
        if proc:
            proc.terminate()
        print(f"\n[blq {now()}] 已中断：{label}", file=sys.stderr)
        return 130
    finally:
        job_file.unlink(missing_ok=True)
        os.close(fd)  # 释放槽位

    took = human(time.time() - t0)
    print(f"[blq {now()}] 结束：{label} · exit {rc} · 用时 {took}", flush=True)
    print(f"[blq] 日志：{log_path}", flush=True)
    if rc != 0:
        tail = log_path.read_text(errors="replace").strip().splitlines()[-15:]
        print("\n".join(tail), file=sys.stderr)
    return rc


def _guess_label(argv: list[str]) -> str:
    for i, a in enumerate(argv):
        if a in ("-P", "--python") and i + 1 < len(argv):
            return os.path.basename(argv[i + 1])
        m = re.search(r"scripts/([\w.\-]+\.py)", a)
        if m:
            return m.group(1)
    # 兜底：blender -b --python-expr ... 这类，标签给个能认出来的说法
    for a in argv:
        if a.endswith(".py"):
            return os.path.basename(a)
    return os.path.basename(argv[0]) + (" (expr)" if "--python-expr" in argv else "")


def blender_procs() -> list[dict]:
    out = []
    try:
        ps = subprocess.run(
            ["ps", "-Ao", "pid=,pcpu=,etime=,command="],
            capture_output=True,
            text=True,
        ).stdout
    except Exception:
        return out
    for line in ps.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, pcpu, etime, cmd = parts
        # 必须是「可执行文件本身是 Blender」。光看命令行里有没有这个路径会把
        # 包着 blender 的 zsh / python 外壳也算进去，ps 里就凭空多出一堆“绕过闸门”。
        if not cmd.split()[0].endswith("/Contents/MacOS/Blender"):
            continue
        out.append(
            {
                "pid": int(pid),
                "pcpu": float(pcpu),
                "etime": etime,
                "cmd": cmd,
                "headless": " -b " in f" {cmd} " or cmd.endswith(" -b"),
            }
        )
    return out


def _collect() -> dict:
    jobs = read_jobs()
    waiting = waiting_tickets()
    procs = blender_procs()
    mon = _read_monitor()
    cpu = mon.get("cpu")
    cpu_ts = mon.get("cpu_ts", 0.0)
    data = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "mode": "fixed" if FIXED_SLOTS is not None else "dynamic",
        "slots": FIXED_SLOTS if FIXED_SLOTS is not None else MAX_SLOTS,
        "max_slots": MAX_SLOTS,
        "cpu": round(cpu * 100) if cpu is not None and time.time() - cpu_ts < 30 else None,
        "swap": mon.get("swap") or "",
        "running": [
            {
                "label": j["label"],
                "slot": j.get("slot"),
                "elapsed": human(j["elapsed"]),
                "cwd": j.get("cwd"),
                "pid": j.get("pid"),
                "log": j.get("log"),
            }
            for j in jobs
        ],
        "waiting": [
            {"label": w.get("label"), "waited": human(time.time() - w.get("since", time.time()))}
            for w in waiting
        ],
        "strays": [
            {
                "pid": p["pid"],
                "pcpu": p["pcpu"],
                "etime": p["etime"],
                "cmd": _short(p["cmd"]),
            }
            for p in procs
            if p["headless"] and p["pid"] not in {j.get("pid") for j in jobs}
        ],
        "blender_total": len(procs),
        "headless": sum(1 for p in procs if p["headless"]),
    }
    return data


def cmd_status(as_json: bool = False) -> int:
    data = _collect()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"Blender 闸门 · {data['generated']}")
    print("─" * 62)
    if data["running"]:
        print("运行中：")
        for r in data["running"]:
            print(f"  ● 槽位 {r['slot']}  {r['label']}  {r['elapsed']}")
    else:
        print("运行中：（空）")
    if data["waiting"]:
        print("排队中：")
        for w in data["waiting"]:
            print(f"  · {w['label']}  已等 {w['waited']}")
    if data["strays"]:
        print(f"⚠ 绕过闸门的 headless Blender {len(data['strays'])} 个：")
        for s in data["strays"]:
            print(f"  ! pid {s['pid']:<7} {s['pcpu']:>6.1f}%  {s['etime']:>8}  {s['cmd']}")
        print("  → 这些就是互相抢核的元凶：blq kill-strays --yes")
    live = cpu_busy()
    mode = f"固定并发 {FIXED_SLOTS}" if FIXED_SLOTS is not None else f"动态并发 1–{MAX_SLOTS}"
    cpu_txt = f" · 整机 CPU {live * 100:.0f}%" if live is not None else ""
    print(f"并发：{mode} · 运行 {len(data['running'])} 件{cpu_txt}")
    print(f"Blender 进程总数：{data['blender_total']}")
    print(f"状态页：{_url()}  （打开：blq open）")
    return 0


def _short(cmd: str, n: int = 88) -> str:
    cmd = cmd.replace(BLENDER, "blender")
    cmd = cmd.replace("--factory-startup", "")
    cmd = re.sub(r"\s+", " ", cmd).strip()
    return cmd[:n]


def cmd_ps() -> int:
    jobs = {j["pid"]: j for j in read_jobs()}
    procs = blender_procs()
    if not procs:
        print("没有 Blender 在跑。")
        return 0
    print(f"{'pid':<8}{'cpu':>7} {'elapsed':>9}  {'来源':<8} 命令")
    for p in sorted(procs, key=lambda p: -p["pcpu"]):
        if p["pid"] in jobs:
            src = f"槽位{jobs[p['pid']].get('slot')}"
        elif p["headless"]:
            src = "绕过!"
        else:
            src = "GUI/桥"
        print(f"{p['pid']:<8}{p['pcpu']:>6.1f}% {p['etime']:>9}  {src:<8} {_short(p['cmd'])}")
    return 0


def cmd_doctor() -> int:
    procs = blender_procs()
    headless = [p for p in procs if p["headless"]]
    load1 = os.getloadavg()[0]
    cores = os.cpu_count() or 1
    swap_used_gb = 0.0
    try:
        s = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
        m = re.search(r"used = ([\d.]+)M", s)
        if m:
            swap_used_gb = float(m.group(1)) / 1024
    except Exception:
        pass
    cpu_sum = sum(p["pcpu"] for p in procs) / 100
    limit = FIXED_SLOTS if FIXED_SLOTS is not None else MAX_SLOTS
    mode = (
        f"固定 {FIXED_SLOTS}"
        if FIXED_SLOTS is not None
        else f"动态 1–{MAX_SLOTS}（CPU 目标 {CPU_TARGET * 100:.0f}%）"
    )
    live = cpu_busy()
    verdict = []
    if len(headless) > limit:
        verdict.append(f"headless 并发 {len(headless)} > 闸门上限 {limit} —— 有人绕过 blq")
    if cpu_sum > cores * 1.2:
        verdict.append(f"Blender 合计吃 {cpu_sum:.1f} 核 / 共 {cores} 核 —— 已经超订")
    if swap_used_gb > MAX_SWAP_GB:
        verdict.append(
            f"swap 已用 {swap_used_gb:.1f}G > {MAX_SWAP_GB:.1f}G —— 动态并发不会再扩"
        )
    if load1 > cores * 2:
        verdict.append(f"load {load1:.0f} / {cores} 核 —— 调度器已经排队")
    print(f"Blender 进程 {len(procs)}（headless {len(headless)}）· 合计 {cpu_sum:.1f} 核 / {cores} 核")
    print(f"load1 {load1:.1f} · swap 已用 {swap_used_gb:.1f}G")
    print(f"并发：{mode}" + (f" · 整机 CPU {live * 100:.0f}%" if live is not None else ""))
    if verdict:
        print("\n结论：先把并发收回来 —— 上面这些会让动态闸门停止扩槽。")
        for v in verdict:
            print(f"  ⚠ {v}")
        return 1
    print("\n结论：正常，可以按 blq 排队推进。")
    return 0


def cmd_kill_strays(confirmed: bool) -> int:
    jobs = {j["pid"] for j in read_jobs()}
    strays = [p for p in blender_procs() if p["headless"] and p["pid"] not in jobs]
    if not strays:
        print("没有绕过闸门的 headless Blender。")
        return 0
    for p in strays:
        print(f"  {p['pid']:<8}{p['pcpu']:>6.1f}%  {_short(p['cmd'])}")
    if not confirmed:
        print(f"\n上面 {len(strays)} 个都没走闸门。加 --yes 才会真的杀。")
        return 1
    for p in strays:
        try:
            os.kill(p["pid"], signal.SIGTERM)
        except OSError:
            pass
    time.sleep(2)
    for p in strays:
        if _alive(p["pid"]):
            try:
                os.kill(p["pid"], signal.SIGKILL)
            except OSError:
                pass
    print(f"已停掉 {len(strays)} 个。")
    return 0


_LIVE_JS = r"""
<script>
(function () {
  var board = document.getElementById('board');
  var mode = document.getElementById('mode');
  var btn = document.getElementById('toggle');
  if (!board || !btn) return;
  var paused = false;
  try { paused = localStorage.getItem('blq.paused') === '1'; } catch (e) {}
  var dead = false, timer = null, active = false;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function render(d) {
    board.textContent = '';
    if (d.strays && d.strays.length) {
      var bad = el('div', 'bad', '⚠ 有 ' + d.strays.length +
        ' 个 headless Blender 绕过闸门在抢核 —— 它们就是卡顿的原因。');
      d.strays.forEach(function (s) {
        bad.appendChild(el('div', null, s.cmd + '  ' + Math.round(s.pcpu) + '%'));
      });
      board.appendChild(bad);
    } else {
      board.appendChild(el('div', 'good', '✓ 所有 headless Blender 都走闸门。'));
    }
    var table = el('table');
    var head = el('tr');
    ['槽位', '作业', '已跑', '状态'].forEach(function (h) {
      head.appendChild(el('th', null, h));
    });
    table.appendChild(head);
    var n = 0;
    (d.running || []).forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', null, '槽位 ' + r.slot));
      tr.appendChild(el('td', null, r.label));
      tr.appendChild(el('td', null, r.elapsed));
      tr.appendChild(el('td', 'ok', '运行'));
      table.appendChild(tr); n++;
    });
    (d.waiting || []).forEach(function (w) {
      var tr = el('tr');
      tr.appendChild(el('td', null, '—'));
      tr.appendChild(el('td', null, w.label || '?'));
      tr.appendChild(el('td', null, w.waited));
      tr.appendChild(el('td', 'wait', '排队'));
      table.appendChild(tr); n++;
    });
    if (!n) {
      var tr = el('tr'), td = el('td', 'dim', '队列空闲 —— 现在正是提交的好时候');
      td.colSpan = 4; tr.appendChild(td); table.appendChild(tr);
    }
    board.appendChild(table);
    var stamp = document.getElementById('stamp');
    if (stamp) {
      var modeTxt = d.mode === 'fixed' ? ('固定并发 ' + d.slots) : ('动态并发 1–' + d.max_slots);
      stamp.textContent = d.generated.replace('T', ' ') + ' · ' + modeTxt +
        ' · headless 进程 ' + d.headless + ' 个' +
        (d.cpu != null ? ' · CPU ' + d.cpu + '%' : '');
    }
    active = (d.running || []).length + (d.waiting || []).length > 0;
  }

  function paint() {
    if (paused) {
      btn.textContent = '▶ 恢复自动刷新';
      mode.textContent = '已暂停 · 页面不会自己更新';
    } else if (dead) {
      btn.textContent = '↻ 重试';
      mode.textContent = '状态服务已休眠（没人在看就自动退出）—— 终端运行 blq open 唤醒';
    } else if (document.hidden) {
      btn.textContent = '⏸ 暂停自动刷新';
      mode.textContent = '标签页在后台 · 已停止刷新';
    } else {
      btn.textContent = '⏸ 暂停自动刷新';
      mode.textContent = active ? '队列活动 · 每 3 秒' : '队列空闲 · 每 30 秒';
    }
  }

  function schedule() {
    clearTimeout(timer); timer = null;
    paint();
    if (paused || dead || document.hidden) return;
    timer = setTimeout(tick, active ? 3000 : 30000);
  }

  function tick() {
    clearTimeout(timer); timer = null;
    if (paused || dead || document.hidden) return schedule();
    fetch('/status.json', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    }).then(function (d) {
      dead = false;
      render(d);
      schedule();
    }).catch(function () {
      dead = true;   // 服务空闲退出了，别继续敲一个没人的端口
      schedule();
    });
  }

  btn.addEventListener('click', function () {
    if (dead) { dead = false; tick(); return; }
    paused = !paused;
    try { localStorage.setItem('blq.paused', paused ? '1' : '0'); } catch (e) {}
    if (paused) schedule(); else tick();
  });

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) schedule(); else tick();
  });

  if (paused) schedule(); else tick();
})();
</script>
"""


def _html(d: dict, live: bool = True) -> str:
    rows = "".join(
        f"<tr><td>槽位 {r['slot']}</td><td>{_esc(r['label'])}</td>"
        f"<td>{r['elapsed']}</td><td class=ok>运行</td></tr>"
        for r in d["running"]
    )
    rows += "".join(
        f"<tr><td>—</td><td>{_esc(w['label'] or '?')}</td>"
        f"<td>{w['waited']}</td><td class=wait>排队</td></tr>"
        for w in d["waiting"]
    )
    if not rows:
        rows = "<tr><td colspan=4 class=dim>队列空闲 —— 现在正是提交的好时候</td></tr>"
    strays = d["strays"]
    banner = (
        f"<div class=bad>⚠ 有 {len(strays)} 个 headless Blender 绕过闸门在抢核 —— 它们就是卡顿的原因。"
        + "".join(f"<br><code>{_esc(s['cmd'])}</code> {s['pcpu']:.0f}%" for s in strays)
        + "</div>"
        if strays
        else "<div class=good>✓ 所有 headless Blender 都走闸门。</div>"
    )
    footer = (
        f"<p class=sub>提交方式：<code>blq render scripts/xxx.py</code> ｜"
        f" 命令行查看：<code>blq status</code> ｜"
        f" 地址：<code>{_url()}</code></p>"
        if live
        else "<p class=sub>这是静态快照，不会自动刷新。看活数据：<code>blq open</code></p>"
    )
    controls = (
        "<div class=ctrl><button id=toggle>⏸ 暂停自动刷新</button>"
        "<span id=mode class=sub></span></div>"
        if live
        else ""
    )
    mode_txt = (
        f"固定并发 {FIXED_SLOTS}"
        if FIXED_SLOTS is not None
        else f"动态并发 1–{MAX_SLOTS}"
    )
    cpu_txt = f" · CPU {d['cpu']}%" if d.get("cpu") is not None else ""
    html = f"""<!doctype html><meta charset="utf-8">
<title>Blender 闸门状态</title>
<style>
body{{font:15px/1.6 -apple-system,PingFang SC,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;
background:#f6f3ee;color:#1d181a}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#8a8078;font-size:13px;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #eee;font-size:14px}}
th{{background:#faf8f5;color:#6b6259;font-weight:600}}
.ok{{color:#2f7d4f}} .wait{{color:#9a6b12}} .dim{{color:#9b948c;text-align:center}}
.bad{{background:#fdecea;color:#a3231a;padding:10px 14px;border-radius:8px;margin:16px 0}}
.good{{background:#eaf6ee;color:#2f7d4f;padding:10px 14px;border-radius:8px;margin:16px 0}}
code{{background:#eee;padding:1px 5px;border-radius:4px;font-size:13px}}
.ctrl{{display:flex;align-items:center;gap:12px;margin:18px 0 0}}
button{{font:13px/1 -apple-system,PingFang SC,sans-serif;padding:7px 12px;border:1px solid #d8d0c6;
border-radius:7px;background:#fff;color:#4a413a;cursor:pointer}}
button:hover{{background:#faf8f5}}
#mode{{margin:0}}</style>
<h1>Blender 闸门</h1>
<div class=sub id=stamp>{d['generated'].replace('T', ' ')} · {mode_txt} · headless 进程 {d['headless']} 个{cpu_txt}</div>
<div id=board>{banner}
<table><tr><th>槽位</th><th>作业</th><th>已跑</th><th>状态</th></tr>{rows}</table></div>
{footer}
{controls}
"""
    return html + (_LIVE_JS if live else "")


def cmd_page() -> int:
    html = _html(_collect(), live=False)
    PAGE.parent.mkdir(parents=True, exist_ok=True)
    PAGE.write_text(html)
    print(PAGE.as_uri())
    return 0


def _url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or STATUS_PORT}/"


def _port_open(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _is_serve_proc(pid: int) -> bool:
    try:
        cmd = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return False
    return "blq.py" in cmd and " serve" in f" {cmd}"


def _serve_pid(port: int | None = None) -> int | None:
    """状态页服务的 pid；先信 pidfile，再用 lsof 兜底（老版本没有 pidfile）。"""
    port = port or STATUS_PORT
    try:
        rec = json.loads(SERVE_PID.read_text())
        pid, pid_port = int(rec["pid"]), int(rec["port"])
    except Exception:
        pid = pid_port = None
    # pidfile 只有一个，必须核对端口 —— 否则会误报别的端口上的服务
    if pid and pid_port == port and _alive(pid) and _is_serve_proc(pid):
        return pid
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return None
    for line in out.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if _is_serve_proc(pid):
            return pid
    return None


def ensure_status_server(port: int | None = None, idle: int | None = None) -> str | None:
    """确保状态页在跑，返回地址。失败就返回 None —— 绝不拖累渲染作业。"""
    port = port or STATUS_PORT
    if os.environ.get("BLQ_NO_SERVE"):
        return None
    if _port_open(port):
        return _url(port) if _serve_pid(port) else None
    ensure_dirs()
    fd = os.open(STATE / "serve.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        # 两个窗口同时开跑时，只让一个去拉起服务
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _port_open(port):
            return _url(port) if _serve_pid(port) else None
        with open(LOG_DIR / "serve.log", "ab") as log:
            subprocess.Popen(
                [
                    sys.executable,
                    os.path.abspath(__file__),
                    "serve",
                    "--port", str(port),
                    "--idle", str(SERVE_IDLE if idle is None else idle),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        for _ in range(30):
            if _port_open(port):
                return _url(port)
            time.sleep(0.1)
        return None
    except Exception:
        return None
    finally:
        os.close(fd)


def cmd_open(port: int, idle: int, browser: bool = True) -> int:
    url = ensure_status_server(port, idle)
    if not url:
        print(
            f"状态页服务没起来（端口 {port} 被别的程序占了？）。日志：{LOG_DIR / 'serve.log'}",
            file=sys.stderr,
        )
        return 1
    print(url)
    if browser:
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", url], check=False)
            elif sys.platform.startswith("linux"):
                subprocess.run(
                    ["xdg-open", url],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif sys.platform.startswith("win"):
                os.startfile(url)  # type: ignore[attr-defined]
        except Exception:
            pass
    return 0


def cmd_serve_stop(port: int) -> int:
    pid = _serve_pid(port)
    if not pid:
        print("状态页服务没在跑。")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(30):
        if not _alive(pid):
            break
        time.sleep(0.1)
    try:
        if json.loads(SERVE_PID.read_text()).get("pid") == pid:
            SERVE_PID.unlink(missing_ok=True)
    except Exception:
        pass
    print(f"状态页服务已停（pid {pid}）。")
    return 0


def cmd_serve(port: int, idle: int) -> int:
    """状态页小服务：每次请求现算，页面才是活的。

    没人看就自己退：超过 idle 秒没有请求直接 shutdown（0 = 永不退）。
    页面在后台标签页时完全停止请求，所以“一直刷新”不会再永远持续。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    existing = _serve_pid(port)
    if existing:
        print(f"状态页服务已在跑（pid {existing}）→ {_url(port)}")
        return 0

    ensure_dirs()
    last = [time.time()]
    guard = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            with guard:
                last[0] = time.time()
            data = _collect()
            if self.path.startswith("/status.json"):
                body = json.dumps(data, ensure_ascii=False).encode()
                ctype = "application/json; charset=utf-8"
            else:
                body = _html(data).encode()
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # 别把访问日志写进终端
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"绑定 127.0.0.1:{port} 失败：{e}", file=sys.stderr)
        return 1
    srv.daemon_threads = True

    def watch() -> None:
        while True:
            time.sleep(5)
            with guard:
                idle_for = time.time() - last[0]
            if idle_for > idle:
                print(
                    f"[blq {now()}] 状态页空闲 {human(idle_for)}，自动退出"
                    "（下次 blq open 再唤醒）",
                    flush=True,
                )
                srv.shutdown()
                return

    if idle > 0:
        threading.Thread(target=watch, daemon=True).start()

    SERVE_PID.write_text(json.dumps({"pid": os.getpid(), "port": port}))
    tip = f"（无人访问 {human(idle)} 后自动退出）" if idle > 0 else "（常驻，--idle 0）"
    print(f"Blender 闸门状态页 → {_url(port)} {tip}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            if json.loads(SERVE_PID.read_text()).get("pid") == os.getpid():
                SERVE_PID.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    ap = argparse.ArgumentParser(prog="blq", description="全机 Blender 串行闸门（Cevtuo-Blendergate）")
    ap.add_argument("--version", action="version", version=f"blq {__version__}")
    ap.add_argument(
        "--slots",
        type=int,
        default=FIXED_SLOTS,
        help=f"固定并发上限；不填则按 CPU 动态（上限 {MAX_SLOTS}）",
    )
    sub = ap.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="在闸门里跑任意命令行")
    p_run.add_argument("argv", nargs=argparse.REMAINDER)
    p_run.add_argument("--label", default=None)
    p_run.add_argument("--tag", default="")
    p_run.add_argument("--force", action="store_true")

    p_render = sub.add_parser("render", help="跑一个 .py 脚本（自动 -b --factory-startup -P）")
    p_render.add_argument("script")
    p_render.add_argument("args", nargs=argparse.REMAINDER)
    p_render.add_argument("--tag", default="")
    p_render.add_argument("--force", action="store_true")

    sub.add_parser("status", help="运行中 / 排队中 / 绕过闸门的进程")
    sub.add_parser("ps", help="所有 Blender 进程")
    sub.add_parser("doctor", help="负载 / swap / 并发体检")
    sub.add_parser("page", help="生成静态状态页 URL")
    p_serve = sub.add_parser("serve", help="起状态页服务（没人访问会自动退出）")
    p_serve.add_argument("--port", type=int, default=STATUS_PORT)
    p_serve.add_argument("--idle", type=int, default=SERVE_IDLE, help="空闲多少秒自动退出（0=常驻）")
    p_serve.add_argument("--stop", action="store_true", help="停掉正在跑的状态页服务")
    p_open = sub.add_parser("open", help="确保状态页在跑，并打开浏览器（不用记地址）")
    p_open.add_argument("--port", type=int, default=STATUS_PORT)
    p_open.add_argument("--idle", type=int, default=SERVE_IDLE)
    p_open.add_argument("--no-browser", action="store_true")
    p_kill = sub.add_parser("kill-strays", help="杀掉绕过闸门的 headless Blender")
    p_kill.add_argument("--yes", action="store_true")
    p_s = sub.add_parser("status-json")
    p_s.add_argument("--json", action="store_true", default=True)

    ns = ap.parse_args()
    if ns.cmd == "run":
        argv = [a for a in ns.argv if a != "--"]
        return cmd_run(argv, ns.slots, ns.label, ns.tag, ns.force)
    if ns.cmd == "render":
        script = ns.script
        if not os.path.isabs(script):
            script = os.path.abspath(script)
        if not os.path.exists(script):
            print(f"找不到脚本：{script}", file=sys.stderr)
            return 2
        extra = [a for a in ns.args if a != "--"]
        argv = [BLENDER, "-b", "--factory-startup", "-P", script]
        if extra:
            argv += ["--", *extra]
        return cmd_run(argv, ns.slots, os.path.basename(script), script, ns.force)
    if ns.cmd == "status":
        return cmd_status()
    if ns.cmd == "status-json":
        return cmd_status(as_json=True)
    if ns.cmd == "ps":
        return cmd_ps()
    if ns.cmd == "doctor":
        return cmd_doctor()
    if ns.cmd == "page":
        return cmd_page()
    if ns.cmd == "serve":
        return cmd_serve_stop(ns.port) if ns.stop else cmd_serve(ns.port, ns.idle)
    if ns.cmd == "open":
        return cmd_open(ns.port, ns.idle, not ns.no_browser)
    if ns.cmd == "kill-strays":
        return cmd_kill_strays(ns.yes)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
