# Log Analysis & Strategy Learnings (2026-03-01)

## Score History

| Run | Difficulty | Score | Orders | Items | Avg rds/order | Notes |
|-----|-----------|-------|--------|-------|---------------|-------|
| 16:50 | Medium | 116 | 13 | 51 | 21.9 | Baseline (serial delivery) |
| 20:13 | Medium | **35** | 4 | 15 | 22.5 | Pipeline delivery, NO collision fix = deadlock |
| 20:15 | Hard | **2** | 0 | 2 | 0 | Same — total deadlock at round 43 |
| 20:30 | Medium | **133** | 15 | 58 | 19.4 | Pipeline + collision fix |
| 20:32 | Hard | 106 | 12 | 46 | 24.5 | Pipeline + collision fix |
| 20:34 | Expert | 72 | 7 | 37 | 34.0 | Pipeline + collision fix |
| 20:36 | Easy | 92 | 11 | 37 | 25.7 | Unchanged (1 bot, no collisions) |

## What Worked

### Pipeline delivery + collision fix (Medium: 116 → 133, +15%)
- All deliver bots path to drop-off simultaneously instead of serial queueing
- Average rounds per order dropped from 21.9 → 19.4
- Wait rounds dropped dramatically: Bot 0 (27% → 17%), Bot 1 (39% → 22%), Bot 2 (34% → 13%)
- More pickups per bot: ~18 → ~21 each
- Preview prepicks stayed at 35 useful — already optimal

### Collision fix prevented game-ending deadlock
The crashed medium run shows the exact deadlock mechanism:
- Round 103+: Bot 0 at (1,9) tries `move_down` to (1,10). Bot 1 at (1,10) tries `move_up` to (1,9).
- Both moves fail on the server (swap = collision). Both bots remain in place.
- `predict_position` incorrectly assumes both moved, so next round repeats identically.
- **197 rounds wasted** in permanent deadlock.

The fix: after predicting a position, check if an undecided bot is still there. If so, downgrade to `wait` and predict the bot stays put. This costs 1 round but prevents infinite loops.

## Current Problems

### 1. Massive idle waste on Hard/Expert

**Hard (5 bots):** Only 3 bots are productive. Bot 2 was 65% waiting, Bot 3 was 85% waiting.
- Bot 3 stuck at (17,11) for 200 consecutive rounds doing nothing
- Bot 2 stuck at (16,10) for 153 rounds

**Expert (10 bots):** Only 3-4 bots do real work. The rest are >90% idle.
- Bot 3: stuck at (21,15) for 292/300 rounds
- Bot 4: stuck at (20,14) for 289/300 rounds
- Bot 9: stuck at (19,15) for 283/300 rounds
- Bot 8: stuck at (17,15) for 213/300 rounds

**Root cause:** Bots spawn at the far corner (hard: (20,12), expert: (26,16)) while drop-off is at the opposite corner (hard: (1,12), expert: (1,16)). With only 3-5 items per order, Manhattan-based assignment gives all work to the 2-3 bots that reach the shelves first. The rest never get assigned and sit idle in `wait` role.

The newly implemented idle pre-positioning should help — bots will walk toward shelves instead of waiting at spawn. But the fundamental issue is that there are more bots than work.

### 2. Slow first order

Every difficulty shows 0 score for the first 27-60 rounds:
- Easy: 27 rounds
- Medium: 38 rounds
- Hard: 47 rounds
- Expert: 60 rounds

This is partly grid size (longer paths) but also the "batch pick" strategy: we collect ALL items before delivering. For a 4-item order where items are spread across the grid, the bot walks to each one before heading to drop-off. Delivering partial loads sooner might improve throughput.

### 3. BFS fallback frequency

Medium: 9 fallbacks total (all from Bot 2). Hard: 14 fallbacks (all pick context). Expert: 42 fallbacks (18 pick + 24 deliver). Each fallback means a bot's collision-aware BFS completely failed and it used the non-collision-aware path — which may collide anyway.

### 4. Delivery bottleneck at drop-off (1 cell)

All bots must deliver through a single drop-off cell. On medium with 3 deliver bots, the collision-aware BFS naturally queues them. But on expert with 10 bots, the approach corridor becomes a traffic jam. The drop-off is always at corner position (1, h-2), meaning there's only ~2 adjacent walkable cells for queueing.

## Ideas for Higher Scores

### A. Partial delivery (deliver early, pick more)
Instead of "collect all items then deliver," deliver when inventory is full (3 items) or when remaining items are far away. This could reduce rounds per order, especially for large orders (5-6 items on expert). Trade-off: more trips to drop-off but fewer rounds waiting.

### B. Smarter bot-to-work distribution
Current: Manhattan greedy assigns nearest bot. All 5 bots fight over the same 3-4 items.
Better: Assign each bot to a different shelf zone. Or limit active bots to min(num_bots, items_needed) and have excess bots pre-pick preview items.

### C. Two-phase assignment: active pickers + preview pickers
On hard/expert, assign 2-3 bots to the active order and the rest to preview items. When the active order completes, the preview pickers already have the next order's items in inventory → instant delivery.

### D. Path caching / pre-computation
BFS runs every round for every bot. For hard/expert with 5-10 bots, that's 10-20+ BFS calls per round. Pre-computing distances once per round and reusing could reduce computation, though Python GIL isn't an issue with async.

### E. Traffic-aware pathfinding
Instead of BFS fallback (try without bot-walls if collision-aware fails), use A* with penalties for cells near other bots. This would create naturally spaced paths without hard blocking.

### F. Order look-ahead beyond preview
If we can see the full order queue, pre-position bots near items for orders 2-3 ahead.

## Key Metrics to Watch

| Metric | Easy | Medium | Hard | Expert | Target |
|--------|------|--------|------|--------|--------|
| Score | 92 | 133 | 106 | 72 | Higher |
| Orders completed | 11 | 15 | 12 | 7 | More |
| Avg rounds/order | 25.7 | 19.4 | 24.5 | 34.0 | Lower |
| Bot idle % (worst) | 0% | 13% | 85% | 98% | <30% |
| BFS fallbacks | 0 | 9 | 14 | 42 | <5 |
| Preview prepick useful | 2 | 35 | 25 | 28 | Higher |
| First order rounds | 27 | 38 | 47 | 60 | <20 |
