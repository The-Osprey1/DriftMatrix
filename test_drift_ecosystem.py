"""Unit tests for the Drift Hardware Ecosystem & Event Bus."""

import time
import unittest
from unittest.mock import MagicMock, patch

from drift_link_core import DriftEvent, DriftEventBus, DriftEventType
from drift_ecosystem import DriftEcosystem, DeviceStatus


class TestDriftEventBus(unittest.TestCase):
    def test_event_immutability(self):
        event = DriftEvent(
            event_type=DriftEventType.MACRO_TRIGGERED,
            source="DriftPad",
            data={"macro_index": 1, "action": "switch_app", "target": "nowplaying"},
        )
        self.assertEqual(event.event_type, DriftEventType.MACRO_TRIGGERED)
        self.assertEqual(event.source, "DriftPad")
        self.assertEqual(event.data["target"], "nowplaying")
        with self.assertRaises(Exception):
            event.source = "Modified"  # frozen dataclass

    def test_synchronous_pub_sub(self):
        bus = DriftEventBus()
        received = []

        def on_macro(evt: DriftEvent):
            received.append(evt)

        bus.subscribe(DriftEventType.MACRO_TRIGGERED, on_macro)
        event = DriftEvent(
            event_type=DriftEventType.MACRO_TRIGGERED,
            source="DriftPad",
            data={"action": "switch_app", "target": "moon"},
        )
        bus.publish_sync(event)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].data["target"], "moon")

        # Unsubscribe
        bus.unsubscribe(DriftEventType.MACRO_TRIGGERED, on_macro)
        bus.publish_sync(event)
        self.assertEqual(len(received), 1)

    def test_global_subscriber(self):
        bus = DriftEventBus()
        all_events = []

        bus.subscribe(None, lambda e: all_events.append(e))

        bus.publish_sync(DriftEvent(DriftEventType.MACRO_TRIGGERED, "Pad"))
        bus.publish_sync(DriftEvent(DriftEventType.ALERT_BROADCAST, "Watch"))

        self.assertEqual(len(all_events), 2)

    def test_asynchronous_worker_dispatch(self):
        bus = DriftEventBus()
        received = []

        bus.subscribe(DriftEventType.SCENE_CHANGED, lambda e: received.append(e))
        bus.start()

        bus.publish(
            DriftEvent(
                DriftEventType.SCENE_CHANGED,
                source="DriftMatrix",
                data={"scene": "ocean"},
            )
        )

        # Allow worker thread to process
        time.sleep(0.1)
        bus.stop()

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].data["scene"], "ocean")

    def test_handler_error_isolation(self):
        bus = DriftEventBus()
        delivered = []

        def bad_handler(e):
            raise RuntimeError("Handler failure")

        def good_handler(e):
            delivered.append(e)

        bus.subscribe(DriftEventType.ALERT_BROADCAST, bad_handler)
        bus.subscribe(DriftEventType.ALERT_BROADCAST, good_handler)

        # Should not raise exception
        bus.publish_sync(DriftEvent(DriftEventType.ALERT_BROADCAST, "Test"))
        self.assertEqual(len(delivered), 1)


class TestDriftEcosystem(unittest.TestCase):
    @patch("requests.Session.get")
    def test_probe_matrix_online(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "version": "1.1.2",
            "currentApp": "moon",
            "fps": 42,
            "wifiRssi": -65,
            "freeHeapBytes": 45000,
            "matrixPower": True,
            "brightness": 25,
        }
        mock_get.return_value = mock_resp

        eco = DriftEcosystem()
        status = eco.probe_matrix()

        self.assertTrue(status.connected)
        self.assertEqual(status.name, "DriftMatrix")
        self.assertEqual(status.details["active_app"], "moon")
        self.assertEqual(status.details["fps"], 42)

    @patch("requests.Session.put")
    def test_switch_matrix_app(self, mock_put):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        mock_put.return_value = mock_resp

        eco = DriftEcosystem()
        ok = eco.switch_matrix_app("nowplaying")

        self.assertTrue(ok)
        mock_put.assert_called_once()
        args, kwargs = mock_put.call_args
        self.assertIn("apps/active", args[0])
        self.assertEqual(kwargs["json"]["name"], "nowplaying")

    @patch("requests.Session.put")
    def test_macro_event_trigger_switches_app(self, mock_put):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_put.return_value = mock_resp

        eco = DriftEcosystem()
        eco.bus.publish_sync(
            DriftEvent(
                event_type=DriftEventType.MACRO_TRIGGERED,
                source="DriftPad",
                data={"macro_index": 1, "action": "switch_app", "target": "nowplaying"},
            )
        )

        mock_put.assert_called_once()
        _, kwargs = mock_put.call_args
        self.assertEqual(kwargs["json"]["name"], "nowplaying")


if __name__ == "__main__":
    unittest.main()
