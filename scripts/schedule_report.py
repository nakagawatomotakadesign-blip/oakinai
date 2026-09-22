"""daily-screen の schedule 起動遅れを集計する（手元で実行する運用ツール。Actions では使わない）。

使い方:
    python scripts/schedule_report.py                    # 直近7日
    python scripts/schedule_report.py --since 2026-09-23

workflow の cron（予定時刻）と gh run list の schedule 起動を突き合わせ、
cron / 予定 / 実際の起動 / 遅れ / 取得内容 を Markdown の表で出す。
どの cron が発火したかは run-name（"daily-screen <cron>"）から読む。
run-name 導入前の run は直前の予定枠に割り当て「(推定)」と表示する。
"""
import argparse
import json
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/screen.yml"
CRON_RE = re.compile(r'cron:\s*"(\d+) (\d+) \* \* (\d)-(\d)"')


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def load_crons() -> list[tuple[str, int, int, range]]:
    crons = []
    for m in CRON_RE.finditer(WORKFLOW.read_text(encoding="utf-8")):
        mi, h, d0, d1 = map(int, m.groups())
        crons.append((f"{mi} {h} * * {d0}-{d1}", h, mi, range(d0, d1 + 1)))
    return crons


def slots(crons, since: date, now: datetime) -> list[tuple[str, datetime]]:
    """since 以降 now までの予定枠。cron の曜日は 0=日曜。"""
    out = []
    d = since
    while d <= now.date():
        dow = (d.weekday() + 1) % 7
        for expr, h, mi, days in crons:
            t = datetime(d.year, d.month, d.day, h, mi, tzinfo=timezone.utc)
            if dow in days and t <= now:
                out.append((expr, t))
        d += timedelta(days=1)
    return sorted(out, key=lambda x: x[1])


def cached_days() -> set[str]:
    """リモートの日足キャッシュに実在する日付。ログだけでは休場日（データなし）と取得を区別できないため。"""
    subprocess.run(["git", "-C", str(WORKFLOW.parent.parent.parent), "fetch", "-q", "origin"], check=False)
    names = subprocess.run(["git", "-C", str(WORKFLOW.parent.parent.parent), "ls-tree", "--name-only",
                            "origin/main", "cache/daily/"], capture_output=True, text=True).stdout
    return {Path(n).stem for n in names.split()}


def fetched(run_id: int, have: set[str]) -> str:
    """ログの [polygon] 行から、その run で何が起きたかを要約する。"""
    log = gh("run", "view", str(run_id), "--log")
    got, skipped = [], []
    for line in log.splitlines():
        if m := re.search(r"\[polygon\] (\d{4}-\d{2}-\d{2}) は未配信", line):
            skipped.append(m.group(1))
        elif m := re.search(r"\[polygon\] (\d{4}-\d{2}-\d{2}) \(\d+/\d+\)", line):
            got.append(m.group(1))
    requested = [d for d in got if d not in skipped]
    got = [d for d in requested if d in have]
    closed = [d for d in requested if d not in have]
    parts = []
    if got:
        parts.append("取得 " + ", ".join(d[5:] for d in got))
    if closed:
        parts.append("休場/データなし " + ", ".join(d[5:] for d in closed))
    if skipped:
        parts.append("未配信スキップ " + ", ".join(d[5:] for d in skipped))
    return " / ".join(parts) or "新規なし"


def fmt_delay(td: timedelta) -> str:
    m = int(td.total_seconds() // 60)
    return f"約{m // 60}時間{m % 60}分" if m >= 60 else f"約{m}分"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=date.fromisoformat,
                    default=datetime.now(timezone.utc).date() - timedelta(days=7))
    ap.add_argument("--no-log", action="store_true", help="取得内容の列を省く（ログ取得をしない）")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    crons = load_crons()
    plan = slots(crons, args.since, now)
    runs = json.loads(gh("run", "list", "--workflow", "daily-screen", "--event", "schedule",
                         "--limit", "200", "--json", "databaseId,createdAt,displayTitle,conclusion"))

    have = set() if args.no_log else cached_days()
    # run → 予定枠。run 名の cron と一致し、起動時刻以前で最も新しい枠に割り当てる
    matched: dict[tuple[str, datetime], tuple[dict, bool]] = {}
    for r in sorted(runs, key=lambda r: r["createdAt"]):
        started = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        expr = r["displayTitle"].removeprefix("daily-screen").strip()
        cands = [s for s in plan if s[1] <= started and s not in matched
                 and (not expr or s[0] == expr)]
        if cands:
            matched[cands[-1]] = (r, not expr)

    print("| cron | 予定（UTC） | 実際の起動 | 遅れ | 結果 |")
    print("|---|---|---|---|---|")
    for s in plan:
        expr, t = s
        if s not in matched:
            print(f"| `{expr}` | {t:%m-%d %H:%M} | 起動なし（{now:%m-%d %H:%M} 時点） | — | — |")
            continue
        r, guessed = matched[s]
        started = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        result = r["conclusion"] or "実行中"
        if not args.no_log and r["conclusion"]:
            result += f" / {fetched(r['databaseId'], have)}"
        mark = " (推定)" if guessed else ""
        print(f"| `{expr}`{mark} | {t:%m-%d %H:%M} | {started:%m-%d %H:%M} "
              f"| {fmt_delay(started - t)} | {result} |")


if __name__ == "__main__":
    main()
