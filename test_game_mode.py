import unittest

import game_mode as gm
import visual_system as vs
from test_new_app_support import FakeClock, SpyPanel

GB = 1024 ** 3


def proc(exe, rss=2 * GB, pid=10, created=0.0):
    return gm.ProcessInfo(pid, exe, created, rss)


class DetectTests(unittest.TestCase):
    def test_game_in_a_library_is_found_and_named_by_folder(self):
        game = gm.detect_game([
            proc(r"C:\Windows\explorer.exe"),
            proc(r"D:\SteamLibrary\steamapps\common\Baldurs Gate 3\bin\bg3_dx11.exe", pid=42),
        ])
        self.assertEqual((game.name, game.pid), ("BALDURS GATE 3", 42))

    def test_launchers_and_small_helpers_are_not_games(self):
        self.assertIsNone(gm.detect_game([
            proc(r"C:\Program Files (x86)\Steam\steam.exe"),
            proc(r"C:\Program Files\Epic Games\Launcher\EpicGamesLauncher.exe"),
            proc(r"D:\SteamLibrary\steamapps\common\Game\CrashHandler.exe", rss=40 * 1024 ** 2),
        ]))

    def test_extra_games_count_wherever_they_live(self):
        game = gm.detect_game([proc(r"C:\Games\osu!\osu!.exe", rss=100)], extra_games=["osu!.exe"])
        self.assertEqual(game.name, "OSU!")

    def test_biggest_match_is_the_game(self):
        game = gm.detect_game([
            proc(r"C:\XboxGames\Forza\Content\server.exe", rss=400 * 1024 ** 2, pid=1),
            proc(r"C:\XboxGames\Forza\Content\forza.exe", rss=6 * GB, pid=2),
        ])
        self.assertEqual(game.pid, 2)


class LaneTests(unittest.TestCase):
    def test_one_meter_on_row_7_measures_the_temperature(self):
        cool = gm.render_lane(gm.GpuReading(40, 100), 60)
        hot = gm.render_lane(gm.GpuReading(90, 100), 60)
        lit = vs.meter_fill
        self.assertLess(lit(cool), lit(hot))
        # Row 0 is no longer a second meter.
        self.assertTrue(all(cool[0][x] == (0, 0, 0) for x in range(9, 32)))
        self.assertEqual(vs.read_meter(hot)[1], gm.CORAL)

    def test_missing_gpu_still_renders(self):
        gm.build_payload(None, 5)

    def test_temperature_bands(self):
        self.assertEqual([gm.temperature_level(t) for t in (60, 75, 82, 90)],
                         ["good", "fair", "poor", "bad"])


class ControllerTests(unittest.TestCase):
    def test_present_while_gaming_then_removed(self):
        panel, clock = SpyPanel(), FakeClock()
        running = [[proc(r"D:\steamapps\common\Hades\Hades.exe", created=clock.t - 600)]]
        c = gm.GameModeController(panel, processes=lambda: running[0],
                                  gpu=lambda: gm.GpuReading(65, 80), clock=clock)
        self.assertEqual(c.tick().name, "HADES")
        running[0] = []
        clock.advance(5)
        self.assertIsNone(c.tick())
        self.assertEqual(len(panel.pushes), 1)
        self.assertEqual(panel.deletes, ["game0"])


if __name__ == "__main__":
    unittest.main()
