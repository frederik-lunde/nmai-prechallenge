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


def decide_bot_action(bot, active, preview, items, drop_off, walls_set, w, h):
    pos = tuple(bot["position"])
    inventory = list(bot["inventory"])
    drop_off_pos = tuple(drop_off)
    inv_space = 3 - len(inventory)

    # What the active order still needs overall
    active_needed = calculate_needed(active)

    # How many active-needed types we already carry
    inv_active = {}
    for t in inventory:
        if t in active_needed:
            inv_active[t] = inv_active.get(t, 0) + 1

    # What still needs to be picked from shelves for active order
    active_still_needed = {}
    for t, count in active_needed.items():
        remaining = count - inv_active.get(t, 0)
        if remaining > 0:
            active_still_needed[t] = remaining

    has_active_items = bool(inv_active)
    total_active_remaining = sum(active_still_needed.values())

    # ── BUILD TARGET ITEMS ───────────────────────────────────────────────────
    # Spare slots = free slots beyond what's needed for active items still to pick
    spare_slots = max(0, inv_space - total_active_remaining)

    # Preview items still needed from shelves (after subtracting what's in inventory)
    preview_needed = calculate_needed(preview) if preview else {}
    preview_still_needed = dict(preview_needed)
    for t in inventory:
        if t in preview_still_needed:
            preview_still_needed[t] -= 1
            if preview_still_needed[t] <= 0:
                del preview_still_needed[t]

    target_items = []
    for item in items:
        t = item["type"]
        if t in active_still_needed:
            target_items.append(item)
        elif spare_slots > 0 and t in preview_still_needed:
            target_items.append(item)

    # ── DELIVER ──────────────────────────────────────────────────────────────
    # Deliver when: carrying active-order items AND (inventory full OR nothing left to pick).
    # Crucially, "nothing left to pick" includes pre-pickable preview items — so the bot
    # fills spare slots with preview items before heading to the drop-off.
    if has_active_items and (inv_space == 0 or not target_items):
        if pos == drop_off_pos:
            return {"bot": bot["id"], "action": "drop_off"}
        path = bfs_to_goal(pos, drop_off_pos, walls_set, w, h)
        return {"bot": bot["id"], "action": path[0] if path else "wait"}

    # ── PICK UP ──────────────────────────────────────────────────────────────
    if target_items:
        path, item = bfs_nearest_item(pos, target_items, walls_set, w, h)

        if item is not None:
            if not path:  # already adjacent to item
                return {"bot": bot["id"], "action": "pick_up", "item_id": item["id"]}
            return {"bot": bot["id"], "action": path[0]}

    # Fallback: deliver if carrying active items (no reachable pick targets)
    if has_active_items:
        if pos == drop_off_pos:
            return {"bot": bot["id"], "action": "drop_off"}
        path = bfs_to_goal(pos, drop_off_pos, walls_set, w, h)
        return {"bot": bot["id"], "action": path[0] if path else "wait"}

    return {"bot": bot["id"], "action": "wait"}


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

    actions = []
    for bot in state["bots"]:
        if active:
            action = decide_bot_action(
                bot, active, preview, state["items"],
                state["drop_off"], walls_set, w, h,
            )
        else:
            action = {"bot": bot["id"], "action": "wait"}
        actions.append(action)

    return actions


async def play():
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

                if r % 10 == 0 or r < 3:
                    orders = state["orders"]
                    active = next((o for o in orders if o["status"] == "active"), None)
                    info = ""
                    if active:
                        needed = calculate_needed(active)
                        delivered = len(active["items_delivered"])
                        total = len(active["items_required"])
                        info = f" | {delivered}/{total} delivered, need {needed}"
                    bot0 = state["bots"][0]
                    print(f"Round {r:3d} | score {state['score']:3d} | pos {bot0['position']} inv {bot0['inventory']}{info}")

                actions = decide_actions(state)

                await ws.send(json.dumps({"actions": actions}))


if __name__ == "__main__":
    if not WS_URL:
        print("Usage: python bot.py <wss://game-dev.ainm.no/ws?token=YOUR_TOKEN>")
        sys.exit(1)
    asyncio.run(play())
