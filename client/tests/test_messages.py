import unittest

from lychee_basic_client.messages import heartbeat_action, move_action


class MessageTests(unittest.TestCase):
    def test_heartbeat_action_uses_empty_actions(self) -> None:
        self.assertEqual(
            {
                "msg_name": "action",
                "msg_data": {
                    "matchId": "match-1",
                    "round": 7,
                    "playerId": 1006,
                    "actions": [],
                },
            },
            heartbeat_action("match-1", 7, 1006),
        )

    def test_move_action_uses_target_node_id(self) -> None:
        self.assertEqual(
            {
                "msg_name": "action",
                "msg_data": {
                    "matchId": "match-1",
                    "round": 7,
                    "playerId": 1006,
                    "actions": [
                        {
                            "action": "MOVE",
                            "targetNodeId": "S10",
                        }
                    ],
                },
            },
            move_action("match-1", 7, 1006, "S10"),
        )


if __name__ == "__main__":
    unittest.main()
