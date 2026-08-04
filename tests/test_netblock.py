"""The network monitor's off switch.

Suspending an application is the only per-application way to stop its
traffic without Apple's network-extension entitlement, so the guards around
it matter: it must never freeze macOS itself, never act on a name that was
not on the page, and always be reversible.
"""
import json
import unittest
from unittest import mock

from procwatch import procs, server


class _Handler(server.Handler):
    """A handler with the socket work stubbed out."""

    def __init__(self, body):
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = mock.Mock()
        self.rfile.read.return_value = body.encode()
        self.sent = []

    def _send(self, code, payload, kind):
        self.sent.append((code, payload, kind))
        return None

    def answer(self):
        return json.loads(self.sent[-1][1]), self.sent[-1][0]


def _traffic(apps):
    return {"apps": apps, "age": 1, "total_in": 0, "total_out": 0,
            "here": None, "lookup": False}


class TestOffSwitch(unittest.TestCase):
    def setUp(self):
        self.arc = {"app": "Arc", "is_system": False, "pids": [101, 102],
                    "conns": [], "suspended": False}
        self.kernel = {"app": "mDNSResponder", "is_system": True,
                       "pids": [55], "conns": [], "suspended": False}

    def _call(self, body, apps=None, signal_result=None):
        handler = _Handler(json.dumps(body))
        with mock.patch.object(server.live, "network_traffic",
                               return_value=_traffic(apps or [self.arc])), \
                mock.patch.object(server.procs, "signal_group") as group:
            group.return_value = signal_result if signal_result is not None \
                else [{"ok": True, "pid": p, "signal": "STOP"}
                      for p in (apps or [self.arc])[0]["pids"]]
            handler._netblock()
            self.called = group.call_args
        return handler.answer()

    def test_turning_an_application_off_suspends_every_process_it_has(self):
        answer, code = self._call({"app": "Arc", "stop": True})
        self.assertEqual(code, 200)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["count"], 2)
        self.assertEqual(self.called[0], ([101, 102], "STOP"))

    def test_turning_it_back_on_resumes_it(self):
        answer, code = self._call({"app": "Arc", "stop": False})
        self.assertEqual(code, 200)
        self.assertEqual(self.called[0], ([101, 102], "CONT"))
        self.assertFalse(answer["stopped"])

    def test_macos_itself_is_never_frozen(self):
        answer, code = self._call({"app": "mDNSResponder", "stop": True},
                                  apps=[self.kernel])
        self.assertEqual(code, 400)
        self.assertIn("part of macOS", answer["error"])

    def test_but_a_system_process_may_still_be_resumed(self):
        # The refusal is about causing harm, not about symmetry: something
        # already stopped must always be startable again.
        answer, code = self._call({"app": "mDNSResponder", "stop": False},
                                  apps=[self.kernel])
        self.assertEqual(code, 200)
        self.assertEqual(self.called[0], ([55], "CONT"))

    def test_an_application_not_on_the_page_is_refused(self):
        answer, code = self._call({"app": "Nothing", "stop": True})
        self.assertEqual(code, 404)
        self.assertIn("holds no connection", answer["error"])

    def test_a_request_naming_nothing_is_refused(self):
        handler = _Handler(json.dumps({"stop": True}))
        handler._netblock()
        answer, code = handler.answer()
        self.assertEqual(code, 400)

    def test_a_refusal_from_the_kernel_is_reported(self):
        answer, code = self._call(
            {"app": "Arc", "stop": True},
            signal_result=[{"ok": False, "error": "pid 101 belongs to "
                                                  "another user"}])
        self.assertEqual(code, 400)
        self.assertFalse(answer["ok"])
        self.assertIn("another user", answer["error"])

    def test_procwatch_never_suspends_itself(self):
        """The bug this guard exists for, reproduced.

        Applications are grouped by the bundle their executable belongs to,
        so a Procwatch running on some other bundle's Python is grouped with
        everything else on that Python -- including the server answering the
        request. Turning that group off suspended the server mid-reply, and
        nothing was left running that could turn it back on.
        """
        import os
        group = {"app": "Xcode-beta", "is_system": False,
                 "pids": [os.getpid(), 4242], "conns": [], "suspended": False}
        answer, code = self._call({"app": "Xcode-beta", "stop": True},
                                  apps=[group])
        self.assertEqual(code, 400)
        self.assertIn("Procwatch itself", answer["error"])

    def test_a_group_holding_procwatch_can_still_be_resumed(self):
        import os
        group = {"app": "Xcode-beta", "is_system": False,
                 "pids": [os.getpid(), 4242], "conns": [], "suspended": True}
        answer, code = self._call({"app": "Xcode-beta", "stop": False},
                                  apps=[group])
        self.assertEqual(code, 200)

    def test_the_pids_come_from_the_machine_not_the_request(self):
        # A crafted body must not be able to aim SIGSTOP at something that
        # was never on the page.
        self._call({"app": "Arc", "stop": True, "pids": [1, 2, 3]})
        self.assertEqual(self.called[0], ([101, 102], "STOP"))


class TestSignals(unittest.TestCase):
    def test_stop_and_cont_are_allowed_now(self):
        self.assertIn("STOP", procs.ALLOWED_SIGNALS)
        self.assertIn("CONT", procs.ALLOWED_SIGNALS)

    def test_nothing_else_crept_in(self):
        self.assertEqual(sorted(procs.ALLOWED_SIGNALS),
                         ["CONT", "KILL", "STOP", "TERM"])

    def test_an_unknown_signal_is_still_refused(self):
        answer = procs.signal_pid(99999, "HUP")
        self.assertFalse(answer["ok"])
        self.assertIn("signal must be", answer["error"])

    def test_launchd_is_still_untouchable(self):
        self.assertFalse(procs.signal_pid(1, "STOP")["ok"])


class TestGuardedRoute(unittest.TestCase):
    def test_the_off_switch_is_behind_the_csrf_check(self):
        import inspect
        source = inspect.getsource(server.Handler.do_POST)
        before = source[:source.index("_reject_cross_origin")]
        self.assertIn("/api/netblock", before)


if __name__ == "__main__":
    unittest.main()
