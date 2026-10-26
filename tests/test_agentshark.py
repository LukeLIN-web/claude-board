"""Tests for the AgentShark badge: `agentshark failures --json` grouped onto
cards by session id, and a board without agentshark staying as it was.

The report is fixtures/agentshark_failures.json, the shape `agentshark failures
--json` prints. The one test of the subprocess path runs a stub `agentshark`
script that prints that file, so no test needs the real tool.
"""
import json
import stat
import unittest
from pathlib import Path
from unittest import mock

from core import agentshark
from tests.helpers import scratch_dir

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "agentshark_failures.json"
CC = "11111111-1111-4111-8111-111111111111"
CX = "01a16a23-f0d8-78e0-a3b4-b7d25ee36b59"


def _report() -> dict:
    return json.loads(FIXTURE.read_text())


class BySessionTests(unittest.TestCase):

    def test_groups_by_root_session_with_subagents_on_the_root(self):
        got = agentshark.by_session(_report())
        self.assertEqual(set(got), {CC, CX})
        cc = got[CC]
        self.assertEqual(cc["count"], 3)
        self.assertEqual(cc["sections"], {"blocked": 2, "unrecovered": 1})
        self.assertTrue(cc["live"])  # the subagent's episode is still open
        self.assertEqual([e["subagent"] for e in cc["episodes"]], [None, "a89d6ab058d461fd9", None])
        self.assertEqual(cc["command"], f"agentshark why --session cc:{CC}")

    def test_episodes_newest_first_in_seconds(self):
        eps = agentshark.by_session(_report())[CC]["episodes"]
        self.assertEqual([e["ts"] for e in eps], sorted((e["ts"] for e in eps), reverse=True))
        self.assertEqual(eps[0]["ts"], 1792964999)
        self.assertEqual(eps[0]["signature"], "Permission denied by sandbox")

    def test_codex_session(self):
        cx = agentshark.by_session(_report())[CX]
        self.assertEqual((cx["count"], cx["sections"], cx["live"]), (1, {"unrecovered": 1}, False))
        self.assertEqual(cx["command"], f"agentshark why --session cx:{CX}")

    def test_lists_at_most_shown_but_counts_all(self):
        r = _report()
        r["episodes"] = [dict(r["episodes"][0], ts=r["episodes"][0]["ts"] + i) for i in range(agentshark.SHOWN + 3)]
        cc = agentshark.by_session(r)[CC]
        self.assertEqual((cc["count"], len(cc["episodes"])), (agentshark.SHOWN + 3, agentshark.SHOWN))

    def test_empty_report(self):
        self.assertEqual(agentshark.by_session({"episodes": []}), {})


class AttachTests(unittest.TestCase):

    def test_only_cards_with_episodes_get_the_field(self):
        windows = [{"session_id": CC}, {"session_id": CX}, {"session_id": "other"}, {}]
        with mock.patch.object(agentshark, "_latest", agentshark.by_session(_report())):
            agentshark.attach(windows)
        self.assertEqual([("agentshark" in w) for w in windows], [True, True, False, False])
        self.assertEqual(windows[0]["agentshark"]["count"], 3)

    def test_without_agentshark_no_sampler_and_no_field(self):
        with mock.patch.object(agentshark.shutil, "which", return_value=None), \
             mock.patch.object(agentshark.threading, "Thread") as thread, \
             mock.patch.object(agentshark, "_thread", None):
            agentshark.start()
        thread.assert_not_called()
        windows = [{"session_id": CC}]
        agentshark.attach(windows)  # nothing sampled: _latest is still empty
        self.assertEqual(windows, [{"session_id": CC}])


class SampleTests(unittest.TestCase):

    def test_indexes_then_reads_failures(self):
        d = scratch_dir()
        log = d / "calls"
        exe = d / "agentshark"
        exe.write_text(f'#!/bin/sh\necho "$@" >> {log}\n'
                       f'[ "$1" = failures ] && cat {FIXTURE}\nexit 0\n')
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        got = agentshark.sample(str(exe))
        self.assertEqual(set(got), {CC, CX})
        self.assertEqual(log.read_text().splitlines(), ["index", f"failures --since {agentshark.WINDOW} --json"])

    def test_a_failed_index_still_reads(self):
        d = scratch_dir()
        exe = d / "agentshark"
        exe.write_text(f'#!/bin/sh\n[ "$1" = index ] && exit 1\ncat {FIXTURE}\n')
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        self.assertIn(CX, agentshark.sample(str(exe)))


if __name__ == "__main__":
    unittest.main()
