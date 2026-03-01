"""Structured per-game logging for the Grocery Bot.

Accumulates data in memory during the game, writes a single JSON file at game end.
"""

import json
import os
from datetime import datetime


DIFFICULTY_MAP = {
    (12, 10): "easy",
    (16, 12): "medium",
    (22, 14): "hard",
    (28, 18): "expert",
}


def detect_difficulty(w, h):
    return DIFFICULTY_MAP.get((w, h), f"{w}x{h}")


class GameLogger:
    def __init__(self):
        self.game = {}
        self.rounds = []
        self.events = []
        self.summary = {}

        # Accumulators
        self._timestamp = datetime.now()
        self._difficulty = "unknown"
        self._prev_inventories = {}  # bot_id -> list of item types
        self._prev_active_required = None  # sorted items_required of active order
        self._order_counter = 0  # internal sequential order index
        self._order_start_round = 0  # round when current order became active
        self._order_details = []  # completed order records
        self._bot_stats = {}  # bot_id -> counters
        self._action_counts = {}  # action_name -> count
        self._role_counts = {}  # role_name -> count (bot-rounds)
        self._score_timeline = []  # (round, score) every 10 rounds
        self._preview_picks = 0
        self._preview_useful = 0

    def init_game(self, state):
        grid = state["grid"]
        w, h = grid["width"], grid["height"]
        self._difficulty = detect_difficulty(w, h)
        self._timestamp = datetime.now()

        self.game = {
            "timestamp": self._timestamp.isoformat(),
            "difficulty": self._difficulty,
            "grid_width": w,
            "grid_height": h,
            "num_bots": len(state["bots"]),
            "drop_off": state["drop_off"],
            "total_orders": len(state["orders"]),
            "max_rounds": 300,
        }

        # Initialize per-bot stats
        for bot in state["bots"]:
            bid = bot["id"]
            self._prev_inventories[bid] = list(bot["inventory"])
            self._bot_stats[bid] = {
                "pickups": 0,
                "deliveries": 0,
                "rounds_moving": 0,
                "rounds_waiting": 0,
                "rounds_picking": 0,
                "rounds_delivering": 0,
                "bfs_fallbacks": 0,
            }

        # Track first order by its required items signature
        active = next((o for o in state["orders"] if o["status"] == "active"), None)
        if active:
            self._prev_active_required = sorted(active["items_required"])
            self._order_counter = 0
            self._order_start_round = 0

    def log_round(self, state, assignments, actions, round_events):
        r = state["round"]
        grid = state["grid"]
        drop_off_pos = tuple(state["drop_off"])

        # Build action lookup
        action_map = {}
        for a in actions:
            action_map[a["bot"]] = a["action"]

        # Build assignment lookup for roles
        role_map = {}
        for bid, asn in assignments.items():
            role_map[bid] = asn.get("role", "wait")

        # Compact round data
        bots_data = []
        for bot in state["bots"]:
            bid = bot["id"]
            bots_data.append({
                "id": bid,
                "pos": bot["position"],
                "inv": len(bot["inventory"]),
                "role": role_map.get(bid, "wait"),
                "action": action_map.get(bid, "wait"),
            })

        orders = state["orders"]
        active = next((o for o in orders if o["status"] == "active"), None)
        preview = next((o for o in orders if o["status"] == "preview"), None)

        round_data = {
            "r": r,
            "score": state["score"],
            "items_on_shelves": len(state["items"]),
            "bots": bots_data,
        }
        if active:
            needed = {}
            for t in active["items_required"]:
                needed[t] = needed.get(t, 0) + 1
            for t in active["items_delivered"]:
                if t in needed:
                    needed[t] -= 1
                    if needed[t] == 0:
                        del needed[t]
            round_data["active_order"] = {
                "idx": self._order_counter,
                "required": len(active["items_required"]),
                "delivered": len(active["items_delivered"]),
                "needed": needed,
            }
        if preview:
            round_data["preview_order"] = {
                "idx": self._order_counter + 1,
                "required": len(preview["items_required"]),
            }

        self.rounds.append(round_data)

        # Detect events by diffing inventories
        for bot in state["bots"]:
            bid = bot["id"]
            curr_inv = list(bot["inventory"])
            prev_inv = self._prev_inventories.get(bid, [])
            bot_pos = tuple(bot["position"])
            action = action_map.get(bid, "wait")

            # Pickup detection: inventory grew
            if len(curr_inv) > len(prev_inv):
                # Find new items
                prev_copy = list(prev_inv)
                new_items = []
                for item_type in curr_inv:
                    if item_type in prev_copy:
                        prev_copy.remove(item_type)
                    else:
                        new_items.append(item_type)
                # Check if this was a preview pre-pick
                is_preview = False
                if active:
                    active_needed_types = set()
                    need_count = {}
                    for t in active["items_required"]:
                        need_count[t] = need_count.get(t, 0) + 1
                    for t in active["items_delivered"]:
                        if t in need_count:
                            need_count[t] -= 1
                            if need_count[t] == 0:
                                del need_count[t]
                    active_needed_types = set(need_count.keys())
                    if new_items and all(t not in active_needed_types for t in new_items):
                        is_preview = True
                        self._preview_picks += len(new_items)

                for item_type in new_items:
                    self.events.append({
                        "type": "pickup",
                        "round": r,
                        "bot": bid,
                        "item_type": item_type,
                        "position": bot_pos,
                        "preview_prepick": is_preview,
                    })
                    self._bot_stats[bid]["pickups"] += 1

            # Delivery detection: inventory shrank while on drop-off
            if len(curr_inv) < len(prev_inv) and bot_pos == drop_off_pos:
                curr_copy = list(curr_inv)
                delivered_items = []
                for item_type in prev_inv:
                    if item_type in curr_copy:
                        curr_copy.remove(item_type)
                    else:
                        delivered_items.append(item_type)
                if delivered_items:
                    self.events.append({
                        "type": "delivery",
                        "round": r,
                        "bot": bid,
                        "items": delivered_items,
                        "score_after": state["score"],
                    })
                    self._bot_stats[bid]["deliveries"] += 1

            self._prev_inventories[bid] = curr_inv

        # Order completion detection: compare active order's required items signature
        if active:
            curr_required = sorted(active["items_required"])
            if self._prev_active_required is not None and curr_required != self._prev_active_required:
                # Active order changed — previous one completed
                prev_idx = self._order_counter
                start_r = self._order_start_round
                self._order_details.append({
                    "index": prev_idx,
                    "start_round": start_r,
                    "end_round": r,
                    "rounds_taken": r - start_r,
                })
                self.events.append({
                    "type": "order_complete",
                    "round": r,
                    "order_index": prev_idx,
                    "rounds_taken": r - start_r,
                    "score_after": state["score"],
                })
                self._order_counter += 1
                self._order_start_round = r
            self._prev_active_required = curr_required

        # Accumulate per-bot action stats
        for bot in state["bots"]:
            bid = bot["id"]
            action = action_map.get(bid, "wait")
            role = role_map.get(bid, "wait")

            # Action counts
            self._action_counts[action] = self._action_counts.get(action, 0) + 1

            # Role counts
            self._role_counts[role] = self._role_counts.get(role, 0) + 1

            # Per-bot categorized rounds
            if action.startswith("move_"):
                self._bot_stats[bid]["rounds_moving"] += 1
            elif action == "wait":
                self._bot_stats[bid]["rounds_waiting"] += 1
            elif action == "pick_up":
                self._bot_stats[bid]["rounds_picking"] += 1
            elif action == "drop_off":
                self._bot_stats[bid]["rounds_delivering"] += 1

        # BFS fallback events from decide_bot_action
        for ev in round_events:
            if ev["type"] == "bfs_fallback":
                self.events.append(ev)
                self._bot_stats[ev["bot"]]["bfs_fallbacks"] += 1

        # Score timeline every 10 rounds
        if r % 10 == 0:
            self._score_timeline.append({"round": r, "score": state["score"]})

    def finalize(self, game_over_state):
        # Check if the last active order was still in progress at game end
        # (order_complete event only fires on transition, so the final active order
        # may not have been recorded)

        self._compute_summary(game_over_state)
        self._write_log()

    def _compute_summary(self, game_over_state):
        final_score = game_over_state["score"]
        rounds_used = game_over_state["rounds_used"]
        items_delivered = game_over_state["items_delivered"]
        orders_completed = game_over_state["orders_completed"]

        # Preview usefulness: count preview picks where the item was later delivered
        # (approximation: if any preview-picked type appears in a delivery, count it)
        preview_types_picked = []
        for ev in self.events:
            if ev["type"] == "pickup" and ev.get("preview_prepick"):
                preview_types_picked.append(ev["item_type"])
        delivered_types = []
        for ev in self.events:
            if ev["type"] == "delivery":
                delivered_types.extend(ev["items"])
        # Count how many preview picks were eventually delivered
        delivered_copy = list(delivered_types)
        useful = 0
        for t in preview_types_picked:
            if t in delivered_copy:
                delivered_copy.remove(t)
                useful += 1
        self._preview_useful = useful

        avg_rounds_per_order = (
            sum(o["rounds_taken"] for o in self._order_details) / len(self._order_details)
            if self._order_details else 0
        )

        self.summary = {
            "final_score": final_score,
            "rounds_used": rounds_used,
            "items_delivered": items_delivered,
            "orders_completed": orders_completed,
            "per_order": self._order_details,
            "per_bot": self._bot_stats,
            "action_distribution": self._action_counts,
            "role_distribution": self._role_counts,
            "score_timeline": self._score_timeline,
            "preview_prepicks": self._preview_picks,
            "preview_useful": self._preview_useful,
            "avg_rounds_per_order": round(avg_rounds_per_order, 1),
        }

    def _write_log(self):
        os.makedirs("logs", exist_ok=True)
        ts = self._timestamp.strftime("%Y%m%d_%H%M%S")
        filename = f"logs/{ts}_{self._difficulty}.json"
        data = {
            "game": self.game,
            "rounds": self.rounds,
            "events": self.events,
            "summary": self.summary,
        }
        with open(filename, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        print(f"Log written to {filename}")
