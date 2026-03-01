import asyncio
import json
import sys
from collections import deque

import websockets


WS_URL = sys.argv[1] if len(sys.argv) > 1 else None

# Shelf cells are impassable but not included in grid.walls.
# We accumulate all ever-seen item positions so empty shelves stay blocked in BFS.
known_shelves: set[tuple[int, int]] = set()


def bfs_to_goal(start, goal, walls_set, width, height):
    """BFS from start to goal (both walkable). Returns list of move actions or None."""
    if start == goal:
        return []
    sx, sy = start
    gx, gy = goal
    queue = deque([(sx, sy, [])])
    visited = {(sx, sy)}
    while queue:
        x, y, path = queue.popleft()
        for dx, dy, action in [
            (0, -1, "move_up"), (0, 1, "move_down"),
            (-1, 0, "move_left"), (1, 0, "move_right"),
        ]:
            nx, ny = x + dx, y + dy
            if (nx, ny) in visited or (nx, ny) in walls_set:
                continue
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            new_path = path + [action]
            if (nx, ny) == (gx, gy):
                return new_path
            visited.add((nx, ny))
            queue.append((nx, ny, new_path))
    return None


def bfs_nearest_item(start, target_items, walls_set, width, height):
    """
    BFS from start to nearest walkable cell adjacent to any target item (on shelf).
    Returns (path, item) — empty path means already adjacent.
    Returns (None, None) if no item is reachable.
    """
    if not target_items:
        return None, None

    # Map shelf position → item (first wins if duplicates)
    item_at = {}
    for item in target_items:
        pos = tuple(item["position"])
        if pos not in item_at:
            item_at[pos] = item

    sx, sy = start

    # Already adjacent to a target item?
    for dx, dy in [(0, -1), (0, 1), (-1, 0), (1, 0)]:
        nb = (sx + dx, sy + dy)
        if nb in item_at:
            return [], item_at[nb]

    queue = deque([(sx, sy, [])])
    visited = {(sx, sy)}
    while queue:
        x, y, path = queue.popleft()
        for dx, dy, action in [
            (0, -1, "move_up"), (0, 1, "move_down"),
            (-1, 0, "move_left"), (1, 0, "move_right"),
        ]:
            nx, ny = x + dx, y + dy
            if (nx, ny) in visited or (nx, ny) in walls_set:
                continue
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            new_path = path + [action]
            # Is this walkable cell adjacent to a target shelf?
            for ddx, ddy in [(0, -1), (0, 1), (-1, 0), (1, 0)]:
                nb = (nx + ddx, ny + ddy)
                if nb in item_at:
                    return new_path, item_at[nb]
            visited.add((nx, ny))
            queue.append((nx, ny, new_path))

    return None, None


def calculate_needed(order):
    """Returns {item_type: count} still needed (required minus delivered)."""
    needed = {}
    for t in order["items_required"]:
        needed[t] = needed.get(t, 0) + 1
    for t in order["items_delivered"]:
        if t in needed:
            needed[t] -= 1
            if needed[t] == 0:
                del needed[t]
    return needed


def predict_position(bot, action):
    """Given a bot and its decided action string, return predicted next-round position."""
    x, y = bot["position"]
    moves = {"move_up": (0, -1), "move_down": (0, 1), "move_left": (-1, 0), "move_right": (1, 0)}
    if action in moves:
        dx, dy = moves[action]
        return (x + dx, y + dy)
    return (x, y)


def assign_items_to_bots(bots, active_needed, preview_needed, items, walls_set, w, h):
    """
    Greedy nearest-first item assignment using Manhattan distance.
    Returns {bot_id: {"role": str, "target_items": [item], "deliver_priority": None}}.
    """
    assignments = {}
    for bot in bots:
        assignments[bot["id"]] = {"role": "wait", "target_items": [], "deliver_priority": None}

    # Build per-bot info
    bot_info = {}
    for bot in bots:
        inv = list(bot["inventory"])
        inv_space = 3 - len(inv)
        # Count how many active-needed types this bot already carries
        inv_active = {}
        for t in inv:
            if t in active_needed:
                inv_active[t] = inv_active.get(t, 0) + 1
        bot_info[bot["id"]] = {
            "pos": tuple(bot["position"]),
            "inv_space": inv_space,
            "inv_active": inv_active,
            "inv": inv,
        }

    # Global active items still needed from shelves (subtract ALL bots' carried active items)
    active_still_needed = dict(active_needed)
    for bid, info in bot_info.items():
        for t, count in info["inv_active"].items():
            if t in active_still_needed:
                active_still_needed[t] = max(0, active_still_needed[t] - count)
                if active_still_needed[t] == 0:
                    del active_still_needed[t]

    # Filter shelf items that match active needs
    active_shelf_items = []
    active_type_counts = dict(active_still_needed)
    for item in items:
        t = item["type"]
        if t in active_type_counts and active_type_counts[t] > 0:
            active_shelf_items.append(item)
            active_type_counts[t] -= 1

    reserved_item_ids = set()

    # Build (manhattan_dist, bot_id, item) triples for active items
    triples = []
    for item in active_shelf_items:
        ix, iy = item["position"]
        for bot in bots:
            bid = bot["id"]
            if bot_info[bid]["inv_space"] <= 0:
                continue
            bx, by = bot_info[bid]["pos"]
            dist = abs(bx - ix) + abs(by - iy)
            triples.append((dist, bid, item))

    triples.sort(key=lambda x: x[0])

    # Track how many slots each bot has used for assignments
    bot_assigned_count = {bot["id"]: 0 for bot in bots}
    remaining_need = dict(active_still_needed)

    for dist, bid, item in triples:
        iid = item["id"]
        t = item["type"]
        if iid in reserved_item_ids:
            continue
        if t not in remaining_need or remaining_need[t] <= 0:
            continue
        if bot_info[bid]["inv_space"] - bot_assigned_count[bid] <= 0:
            continue
        # Assign
        assignments[bid]["target_items"].append(item)
        assignments[bid]["role"] = "pick"
        reserved_item_ids.add(iid)
        bot_assigned_count[bid] += 1
        remaining_need[t] -= 1
        if remaining_need[t] == 0:
            del remaining_need[t]

    # Mark bots carrying active-order items but with no pick targets as "deliver"
    for bot in bots:
        bid = bot["id"]
        assignments[bid]["has_active_inv"] = bool(bot_info[bid]["inv_active"])
        if bot_info[bid]["inv_active"] and not assignments[bid]["target_items"]:
            assignments[bid]["role"] = "deliver"

    # Fill spare capacity with preview items
    if preview_needed:
        # Subtract ALL bots' carried preview items
        preview_still_needed = dict(preview_needed)
        for bid, info in bot_info.items():
            for t in info["inv"]:
                if t in preview_still_needed:
                    preview_still_needed[t] -= 1
                    if preview_still_needed[t] <= 0:
                        del preview_still_needed[t]

        preview_shelf_items = []
        preview_type_counts = dict(preview_still_needed)
        for item in items:
            iid = item["id"]
            if iid in reserved_item_ids:
                continue
            t = item["type"]
            if t in preview_type_counts and preview_type_counts[t] > 0:
                preview_shelf_items.append(item)
                preview_type_counts[t] -= 1

        preview_triples = []
        for item in preview_shelf_items:
            ix, iy = item["position"]
            for bot in bots:
                bid = bot["id"]
                spare = bot_info[bid]["inv_space"] - bot_assigned_count[bid]
                if spare <= 0:
                    continue
                bx, by = bot_info[bid]["pos"]
                dist = abs(bx - ix) + abs(by - iy)
                preview_triples.append((dist, bid, item))

        preview_triples.sort(key=lambda x: x[0])
        preview_remaining = dict(preview_still_needed)

        for dist, bid, item in preview_triples:
            iid = item["id"]
            t = item["type"]
            if iid in reserved_item_ids:
                continue
            if t not in preview_remaining or preview_remaining[t] <= 0:
                continue
            spare = bot_info[bid]["inv_space"] - bot_assigned_count[bid]
            if spare <= 0:
                continue
            assignments[bid]["target_items"].append(item)
            if assignments[bid]["role"] == "wait":
                assignments[bid]["role"] = "pick"
            reserved_item_ids.add(iid)
            bot_assigned_count[bid] += 1
            preview_remaining[t] -= 1
            if preview_remaining[t] == 0:
                del preview_remaining[t]

    return assignments


def schedule_dropoffs(assignments, bots, drop_off_pos, walls_set, w, h):
    """
    Among bots with role=deliver, assign deliver_priority based on BFS distance to drop-off.
    Only priority-0 bot actively paths to drop-off; others wait or pre-pick.
    """
    deliver_bots = []
    for bot in bots:
        bid = bot["id"]
        if assignments[bid]["role"] == "deliver":
            pos = tuple(bot["position"])
            path = bfs_to_goal(pos, drop_off_pos, walls_set, w, h)
            dist = len(path) if path is not None else 9999
            deliver_bots.append((dist, bid))

    deliver_bots.sort()
    for priority, (dist, bid) in enumerate(deliver_bots):
        assignments[bid]["deliver_priority"] = priority


def decide_bot_action(bot, assignment, drop_off_pos, walls_set, w, h,
                      bot_positions, decided_moves):
    """Per-bot decision using centralized assignment and collision-aware BFS."""
    bid = bot["id"]
    pos = tuple(bot["position"])
    role = assignment["role"]
    target_items = assignment["target_items"]
    deliver_priority = assignment["deliver_priority"]

    # Build collision-aware walls.
    # Game resolves actions in bot ID order: already-processed bots (in decided_moves)
    # are at their NEW positions; not-yet-processed bots are at their CURRENT positions.
    other_bots = set()
    for other_bid, other_pos in bot_positions.items():
        if other_bid != bid:
            if other_bid in decided_moves:
                other_bots.add(decided_moves[other_bid])
            else:
                other_bots.add(other_pos)
    # Don't block the drop-off cell itself
    other_bots.discard(drop_off_pos)
    walls_with_bots = walls_set | other_bots

    # ── DELIVER ──────────────────────────────────────────────────────────────
    if role == "deliver":
        if pos == drop_off_pos:
            return {"bot": bid, "action": "drop_off"}
        # Only priority-0 bot actively paths to drop-off
        if deliver_priority == 0:
            path = bfs_to_goal(pos, drop_off_pos, walls_with_bots, w, h)
            if path is None:
                path = bfs_to_goal(pos, drop_off_pos, walls_set, w, h)
            return {"bot": bid, "action": path[0] if path else "wait"}
        else:
            # Lower priority: pick preview items if assigned, otherwise wait
            if target_items:
                path, item = bfs_nearest_item(pos, target_items, walls_with_bots, w, h)
                if path is None:
                    path, item = bfs_nearest_item(pos, target_items, walls_set, w, h)
                if item is not None:
                    if not path:
                        return {"bot": bid, "action": "pick_up", "item_id": item["id"]}
                    return {"bot": bid, "action": path[0]}
            # Don't block drop-off while waiting for delivery turn
            if pos == drop_off_pos:
                return _step_off(bid, pos, walls_with_bots, w, h)
            return {"bot": bid, "action": "wait"}

    # ── PICK UP ──────────────────────────────────────────────────────────────
    if role == "pick" and target_items:
        path, item = bfs_nearest_item(pos, target_items, walls_with_bots, w, h)
        if path is None:
            path, item = bfs_nearest_item(pos, target_items, walls_set, w, h)
        if item is not None:
            if not path:
                return {"bot": bid, "action": "pick_up", "item_id": item["id"]}
            return {"bot": bid, "action": path[0]}

    # ── FALLBACK: deliver if carrying active items but pick targets unreachable ─
    if assignment.get("has_active_inv"):
        if pos == drop_off_pos:
            return {"bot": bid, "action": "drop_off"}
        path = bfs_to_goal(pos, drop_off_pos, walls_with_bots, w, h)
        if path is None:
            path = bfs_to_goal(pos, drop_off_pos, walls_set, w, h)
        if path:
            return {"bot": bid, "action": path[0]}

    # ── IDLE ─────────────────────────────────────────────────────────────────
    # Don't block the drop-off; step to an adjacent walkable cell
    if pos == drop_off_pos:
        return _step_off(bid, pos, walls_with_bots, w, h)
    return {"bot": bid, "action": "wait"}


def _step_off(bid, pos, walls_with_bots, w, h):
    """Move to an adjacent walkable cell to clear the drop-off."""
    for dx, dy, act in [(0, -1, "move_up"), (0, 1, "move_down"),
                        (-1, 0, "move_left"), (1, 0, "move_right")]:
        nx, ny = pos[0] + dx, pos[1] + dy
        if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in walls_with_bots:
            return {"bot": bid, "action": act}
    return {"bot": bid, "action": "wait"}


def decide_actions(state):
    global known_shelves
    grid = state["grid"]
    w, h = grid["width"], grid["height"]

    # Update known shelves from current item positions (shelves stay impassable even when empty)
    for item in state["items"]:
        known_shelves.add(tuple(item["position"]))

    walls_set = {tuple(wall) for wall in grid["walls"]} | known_shelves

    orders = state["orders"]
    active = next((o for o in orders if o["status"] == "active"), None)
    preview = next((o for o in orders if o["status"] == "preview"), None)

    if not active:
        return [{"bot": bot["id"], "action": "wait"} for bot in state["bots"]]

    drop_off_pos = tuple(state["drop_off"])
    bots = state["bots"]
    items = state["items"]

    # Compute what orders need
    active_needed = calculate_needed(active)
    preview_needed = calculate_needed(preview) if preview else {}

    # Centralized item assignment
    assignments = assign_items_to_bots(bots, active_needed, preview_needed, items, walls_set, w, h)

    # Schedule drop-off priorities among delivering bots
    schedule_dropoffs(assignments, bots, drop_off_pos, walls_set, w, h)

    # Build bot positions dict
    bot_positions = {bot["id"]: tuple(bot["position"]) for bot in bots}

    # Decide actions in bot ID order, tracking decided moves for collision avoidance
    decided_moves = {}
    actions = []
    for bot in sorted(bots, key=lambda b: b["id"]):
        action = decide_bot_action(
            bot, assignments[bot["id"]], drop_off_pos, walls_set, w, h,
            bot_positions, decided_moves,
        )
        actions.append(action)
        # Track where this bot will be next round
        decided_moves[bot["id"]] = predict_position(bot, action["action"])

    return actions


async def play():
    global known_shelves
    print(f"Connecting to {WS_URL}")
    async with websockets.connect(WS_URL) as ws:
        print("Connected!\n")
        async for raw in ws:
            state = json.loads(raw)

            if state["type"] == "game_over":
                print("\n=== GAME OVER ===")
                print(f"Score:            {state['score']}")
                print(f"Rounds used:      {state['rounds_used']}")
                print(f"Items delivered:  {state['items_delivered']}")
                print(f"Orders completed: {state['orders_completed']}")
                break

            if state["type"] == "game_state":
                r = state["round"]

                # Reset shelves on first round to prevent stale data between games
                if r == 0:
                    known_shelves = set()

                if r % 10 == 0 or r < 3:
                    orders = state["orders"]
                    active = next((o for o in orders if o["status"] == "active"), None)
                    info = ""
                    if active:
                        needed = calculate_needed(active)
                        delivered = len(active["items_delivered"])
                        total = len(active["items_required"])
                        info = f" | {delivered}/{total} delivered, need {needed}"
                    bots = state["bots"]
                    n_bots = len(bots)
                    bot_summary = " ".join(
                        f"B{b['id']}@{b['position']}({len(b['inventory'])})"
                        for b in bots[:4]
                    )
                    if n_bots > 4:
                        bot_summary += f" +{n_bots - 4} more"
                    print(f"Round {r:3d} | score {state['score']:3d} | {bot_summary}{info}")

                actions = decide_actions(state)

                await ws.send(json.dumps({"actions": actions}))


if __name__ == "__main__":
    if not WS_URL:
        print("Usage: python bot.py <wss://game-dev.ainm.no/ws?token=YOUR_TOKEN>")
        sys.exit(1)
    asyncio.run(play())
