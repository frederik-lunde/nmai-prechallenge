# NM i AI — Grocery Bot



## Run

```bash
source .venv/bin/activate
python bot.py "wss://game-dev.ainm.no/ws?token=YOUR_TOKEN"
```


Get token from dev.ainm.no/challenge → Play → copy WebSocket URL.
10 s cooldown between games. Game is deterministic per day.

## Architecture

Two files: `bot.py` (main bot) and `game_logger.py` (structured logging). Python 3.14, websockets==16.0.

Key functions:
- `play()` — WebSocket loop; receives `game_state`/`game_over`, sends `{"actions": [...]}`; resets `known_shelves` on round 0
- `decide_actions(state)` — orchestrator; builds wall set, finds orders, calls `assign_items_to_bots()` and `schedule_dropoffs()`, loops bots in ID order with collision tracking
- `assign_items_to_bots(bots, active_needed, preview_needed, items, walls_set, w, h)` — greedy Manhattan-distance item assignment; returns `{bot_id: {"role", "target_items", "deliver_priority"}}`
- `schedule_dropoffs(assignments, bots, drop_off_pos, walls_set, w, h)` — assigns delivery priority by BFS distance to drop-off; only priority-0 bot actively delivers
- `decide_bot_action(bot, assignment, drop_off_pos, walls_set, w, h, bot_positions, decided_moves)` — per-bot decision using assignment + collision-aware BFS with fallback
- `predict_position(bot, action)` — returns predicted next-round position for collision tracking
- `bfs_to_goal(start, goal, walls, w, h)` — BFS shortest path between two walkable cells
- `bfs_nearest_item(start, items, walls, w, h)` — BFS to nearest walkable cell adjacent to a target shelf item
- `calculate_needed(order)` — returns `{type: count}` still needed (required minus delivered)

## Game Mechanics

### All difficulties
- Coordinate origin (0,0) = top-left; `move_up` = y-1, `move_down` = y+1
- **Collision**: bots block each other (no two on same tile). Actions resolve in bot ID order (lower first). Spawn tile exempt.
- Items sit ON wall cells (shelves); pick up from an adjacent floor cell
- Shelves are impassable even when empty — `known_shelves` set tracks them
- `drop_off` is walkable; bot must stand ON it to deliver
- Inventory capacity: 3 items per bot; invalid actions silently become `wait`
- `drop_off` only consumes items matching active order; non-matching items **stay in inventory**
- Scoring: `items_delivered * 1 + orders_completed * 5`
- On order completion, remaining inventory items are re-checked against the new active order (auto-delivery)

### Difficulty scaling
| Difficulty | Grid    | Bots | Aisles | Item Types | Rounds |
|------------|---------|------|--------|------------|--------|
| Easy       | 12x10   | 1    | 2      | 4          | 300    |
| Medium     | 16x12   | 3    | 3      | 8          | 300    |
| Hard       | 22x14   | 5    | 4      | 12         | 300    |
| Expert     | 28x18   | 10   | 5      | 16         | 300    |

## Strategy

### Core
1. **BFS pathfinding** — not greedy Manhattan; handles walls correctly
2. **Batch pick** — collect all remaining active-order items before delivering
3. **Preview pre-pick** — fill spare inventory slots with preview-order items

### Multi-bot coordination (Medium/Hard/Expert)
1. **Centralized item assignment** — `assign_items_to_bots()` uses Manhattan distance to greedily assign items to nearest bots, preventing duplicate targeting
2. **Global inventory tracking** — all bots' carried items are subtracted from order needs before assigning shelf picks
3. **Pipeline delivery** — all deliver bots path to drop-off simultaneously; collision-aware BFS queues them naturally instead of serial wait
4. **Collision avoidance** — other bots treated as temporary BFS walls; higher-ID bots also avoid lower-ID bots' decided next positions
5. **BFS fallback** — if collision-aware BFS fails (trapped), retry with original walls to avoid permanent deadlock
6. **Re-assignment every round** — cheap with Manhattan; naturally handles picked items and order transitions

The strategy can be changed if better strategies are found/tested. This file must then be updated.

## Logging

Structured per-game logs in `logs/{YYYYMMDD_HHMMSS}_{difficulty}.json`. Zero I/O during rounds — all data accumulated in memory, single write at game end.

**JSON sections:**
- `game` — metadata: timestamp, difficulty (auto-detected from grid size), grid dims, num_bots, drop_off, total_orders
- `rounds` — compact per-round array (300 entries): score, bot positions/inv/role/action, active/preview order status
- `events` — sparse: `pickup`, `delivery`, `order_complete`, `bfs_fallback`
- `summary` — final score, per-order timing, per-bot stats, action/role distribution, score timeline, preview pre-pick effectiveness

**Key files:**
- `game_logger.py` — `GameLogger` class + `detect_difficulty()`. Event detection via inventory diffs between rounds.
- `bot.py` changes: `decide_bot_action()` returns `(action, events)`, `decide_actions()` returns `(actions, assignments, round_events)`, `play()` wires up logger.

## Challenge Docs

MCP server `grocery-bot` provides full docs — use `list_docs` and `search_docs` tools.


When iterating on the bot, run game_logger.py on the latest run of the bot and iterate using the data from the logs as well as other ideas.
