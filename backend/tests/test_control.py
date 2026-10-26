import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from control import ShutdownController, STOP, RUN


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(**kw):
    sent  = []
    clock = FakeClock()
    ok    = {"value": True}

    def publish(payload, reason):
        if ok["value"]:
            sent.append(payload)
        return ok["value"]

    ctl = ShutdownController(publish, clock=clock, **kw)
    return ctl, sent, clock, ok


CRIT   = {"status": "critical", "motor_off": False, "fault_type": "bearing fault", "health_score": 30}
NORMAL = {"status": "normal",   "motor_off": False}
OFF    = {"status": "normal",   "motor_off": True}


def test_first_critical_sends_stop():
    ctl, sent, _, _ = make()
    assert ctl.update(CRIT) == STOP
    assert sent == [STOP]


def test_stop_not_spammed_but_resent_after_interval():
    ctl, sent, clock, _ = make(resend_s=60)
    ctl.update(CRIT)
    clock.t += 10
    assert ctl.update(CRIT) is None
    clock.t += 55
    assert ctl.update(CRIT) == STOP
    assert sent == [STOP, STOP]


def test_critical_again_shortly_after_resume_still_stops():
    # Old code: STOP → RUN → critical within 5 min cooldown = no STOP at all
    ctl, sent, clock, _ = make()
    ctl.update(CRIT)
    clock.t += 30
    ctl.resume()
    clock.t += 30
    assert ctl.update(CRIT) == STOP
    assert sent == [STOP, RUN, STOP]


def test_no_auto_run_by_default():
    ctl, sent, clock, _ = make()
    ctl.update(CRIT)
    for _ in range(100):
        clock.t += 10
        ctl.update(NORMAL)
    assert sent == [STOP]


def test_auto_resume_waits_for_hold_time():
    ctl, sent, clock, _ = make(auto_resume=True, resume_hold_s=120)
    ctl.update(CRIT)
    clock.t += 2
    assert ctl.update(NORMAL) is None       # spin-down flicker must not restart
    clock.t += 60
    assert ctl.update(NORMAL) is None
    clock.t += 61
    assert ctl.update(NORMAL) == RUN
    assert sent == [STOP, RUN]


def test_auto_resume_timer_resets_when_motor_off():
    ctl, sent, clock, _ = make(auto_resume=True, resume_hold_s=120)
    ctl.update(CRIT)
    clock.t += 100
    ctl.update(NORMAL)
    clock.t += 100
    ctl.update(OFF)
    clock.t += 100
    assert ctl.update(NORMAL) is None
    assert sent == [STOP]


def test_failed_publish_is_retried():
    ctl, sent, clock, ok = make()
    ok["value"] = False
    assert ctl.update(CRIT) is None
    ok["value"] = True
    clock.t += 2
    assert ctl.update(CRIT) == STOP


def test_simulated_and_disabled_never_send():
    ctl, sent, _, _ = make()
    assert ctl.update(CRIT, simulated=True) is None
    ctl2, sent2, _, _ = make(enabled=False)
    assert ctl2.update(CRIT) is None
    assert sent == [] and sent2 == []


def test_motor_off_critical_does_not_stop():
    ctl, sent, _, _ = make()
    assert ctl.update({"status": "critical", "motor_off": True}) is None
