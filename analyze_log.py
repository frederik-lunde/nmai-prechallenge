"""Analyze a grocery bot game log and print actionable insights.

Usage:
    python analyze_log.py              # analyzes most recent log
    python analyze_log.py logs/FILE    # analyzes specific log
"""

import json
import sys
from pathlib import Path


def load_log(path=None):
    if path:
        return json.loads(Path(path).read_text())
    logs = sorted(Path("logs").glob("*.json"))
    if not logs:
        print("No logs found in logs/")
        sys.exit(1)
    print(f"Reading {logs[-1]}\n")
    return json.loads(logs[-1].read_text())


def fmt_pct(n, total):
    return f"{n:>4d} ({100*n/total:4.1f}%)" if total else f"{n:>4d}"


def analyze(data):
    game = data["game"]
    summary = data["summary"]
    events = data["events"]
    rounds = data["rounds"]

    total_rounds = summary["rounds_used"]
    num_bots = game["num_bots"]
    bot_rounds = total_rounds * num_bots

    # ── Header ────────────────────────────────────────────────────────────
    print(f"{'='*60}")
    print(f" {game['difficulty'].upper()}  {game['grid_width']}x{game['grid_height']}  "
          f"{num_bots} bot{'s' if num_bots > 1 else ''}  "
          f"{total_rounds} rounds")
    print(f"{'='*60}")
    print(f" Score: {summary['final_score']}  "
          f"({summary['items_delivered']} items + "
          f"{summary['orders_completed']} orders x5)")
    print(f" Avg rounds/order: {summary['avg_rounds_per_order']}")
    print()

    # ── Order timing ──────────────────────────────────────────────────────
    per_order = summary.get("per_order", [])
    if per_order:
        print("Orders:")
        for o in per_order:
            print(f"  #{o['index']:>2d}  rounds {o['start_round']:>3d}-{o['end_round']:>3d}  "
                  f"({o['rounds_taken']:>3d} rounds)")
        # Find slowest
        slowest = max(per_order, key=lambda o: o["rounds_taken"])
        fastest = min(per_order, key=lambda o: o["rounds_taken"])
        print(f"  Fastest: #{fastest['index']} ({fastest['rounds_taken']}r)  "
              f"Slowest: #{slowest['index']} ({slowest['rounds_taken']}r)")
        print()

    # ── Throughput curve ──────────────────────────────────────────────────
    timeline = summary.get("score_timeline", [])
    if len(timeline) >= 2:
        print("Score timeline (delta per 10 rounds):")
        segments = []
        for i in range(1, len(timeline)):
            prev = timeline[i - 1]
            curr = timeline[i]
            delta = curr["score"] - prev["score"]
            segments.append((curr["round"], delta))
        # Show as sparkline-style bar
        max_delta = max(d for _, d in segments) if segments else 1
        for rnd, delta in segments:
            bar_len = int(20 * delta / max_delta) if max_delta else 0
            bar = "#" * bar_len
            print(f"  r{rnd:>3d}: +{delta:>2d}  {bar}")

        # Identify stalls (0 score change for 20+ rounds)
        stall_start = None
        stalls = []
        for i, (rnd, delta) in enumerate(segments):
            if delta == 0:
                if stall_start is None:
                    stall_start = rnd - 10
            else:
                if stall_start is not None:
                    stalls.append((stall_start, rnd - 10))
                    stall_start = None
        if stall_start is not None:
            stalls.append((stall_start, segments[-1][0]))
        if stalls:
            total_stall = sum(e - s for s, e in stalls)
            print(f"  Stalls (0 score): {len(stalls)} periods, {total_stall} rounds total")
            for s, e in stalls:
                print(f"    r{s}-{e} ({e - s} rounds)")
        print()

    # ── Bot efficiency ────────────────────────────────────────────────────
    print("Bot efficiency:")
    per_bot = summary.get("per_bot", {})
    for bid_str, stats in sorted(per_bot.items(), key=lambda x: int(x[0])):
        bid = int(bid_str)
        moving = stats["rounds_moving"]
        waiting = stats["rounds_waiting"]
        picking = stats["rounds_picking"]
        delivering = stats["rounds_delivering"]
        total = moving + waiting + picking + delivering
        idle_pct = 100 * waiting / total if total else 0
        print(f"  Bot {bid}: move={moving} wait={waiting} pick={picking} deliver={delivering}"
              f"  idle={idle_pct:.0f}%"
              f"  pickups={stats['pickups']} deliveries={stats['deliveries']}"
              f"  bfs_fallbacks={stats['bfs_fallbacks']}")
    print()

    # ── Action distribution ───────────────────────────────────────────────
    actions = summary.get("action_distribution", {})
    print(f"Actions ({bot_rounds} bot-rounds):")
    for action in ["move_up", "move_down", "move_left", "move_right", "pick_up", "drop_off", "wait"]:
        count = actions.get(action, 0)
        print(f"  {action:<11s} {fmt_pct(count, bot_rounds)}")
    print()

    # ── Role distribution ─────────────────────────────────────────────────
    roles = summary.get("role_distribution", {})
    print(f"Roles ({bot_rounds} bot-rounds):")
    for role in ["pick", "deliver", "wait"]:
        count = roles.get(role, 0)
        print(f"  {role:<8s} {fmt_pct(count, bot_rounds)}")
    print()

    # ── Preview pre-picks ─────────────────────────────────────────────────
    pp = summary.get("preview_prepicks", 0)
    pu = summary.get("preview_useful", 0)
    if pp:
        print(f"Preview pre-picks: {pp} picked, {pu} useful ({100*pu/pp:.0f}% hit rate)")
    else:
        print("Preview pre-picks: 0")
    print()

    # ── BFS fallbacks ─────────────────────────────────────────────────────
    fallbacks = [e for e in events if e["type"] == "bfs_fallback"]
    if fallbacks:
        from collections import Counter
        ctx_counts = Counter(e["context"] for e in fallbacks)
        print(f"BFS fallbacks: {len(fallbacks)} total")
        for ctx, count in ctx_counts.most_common():
            print(f"  {ctx}: {count}")
        print()

    # ── Key bottlenecks ───────────────────────────────────────────────────
    print("--- Actionable insights ---")
    issues = []

    # High idle time
    for bid_str, stats in per_bot.items():
        total = stats["rounds_moving"] + stats["rounds_waiting"] + stats["rounds_picking"] + stats["rounds_delivering"]
        if total and stats["rounds_waiting"] / total > 0.15:
            issues.append(f"Bot {bid_str} idle {100*stats['rounds_waiting']/total:.0f}% of the time — "
                          "consider better task assignment or collision avoidance")

    # Stalls
    if stalls:
        issues.append(f"{total_stall} rounds with zero score progress — "
                      "look at what bots were doing during stall periods")

    # Slow orders
    if per_order:
        avg = summary["avg_rounds_per_order"]
        slow = [o for o in per_order if o["rounds_taken"] > avg * 1.5]
        if slow:
            idxs = ", ".join(f"#{o['index']}" for o in slow)
            issues.append(f"Orders {idxs} took >1.5x average — check item locations or pathing")

    # Many BFS fallbacks
    if len(fallbacks) > 10:
        issues.append(f"{len(fallbacks)} BFS fallbacks — bots frequently blocked by each other")

    # Low preview hit rate
    if pp > 2 and pu / pp < 0.5:
        issues.append(f"Preview pre-pick hit rate only {100*pu/pp:.0f}% — "
                      "pre-picked items often not needed by next order")

    # No preview picks at all
    if pp == 0 and num_bots >= 3:
        issues.append("No preview pre-picks despite multiple bots — idle bots could pre-pick")

    if issues:
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
    else:
        print("  No major issues detected.")
    print()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    data = load_log(path)
    analyze(data)
