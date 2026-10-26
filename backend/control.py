"""
control.py — Machine STOP / RUN decision logic
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Decides when to publish STOP ("1") / RUN ("0") on the MQTT control topic.

Rules:
  ✅ STOP is sent on the FIRST critical reading — no cooldown can block it
  ✅ While still critical and the motor keeps running, STOP is re-sent every
     `resend_s` seconds (covers a lost MQTT message or a device that rebooted)
  ✅ A failed publish is not counted as sent → retried on the next reading
  ✅ No automatic RUN by default — a person resumes the machine (resume())
  ✅ Optional auto-resume only after status has been normal for `resume_hold_s`
     (a motor spinning down after STOP looks "normal" for a few readings)
  ✅ Simulated readings never cause a STOP / RUN
"""

import time

STOP = "1"
RUN  = "0"


class ShutdownController:
    def __init__(self, publish, enabled=True, resend_s=60.0,
                 auto_resume=False, resume_hold_s=120.0, clock=time.time):
        """
        publish(payload, reason) -> bool   True if the message was handed to the broker
        """
        self.publish       = publish
        self.enabled       = enabled
        self.resend_s      = resend_s
        self.auto_resume   = auto_resume
        self.resume_hold_s = resume_hold_s
        self.clock         = clock

        self.last_cmd      = None   # STOP | RUN | None (nothing sent since startup)
        self.last_cmd_ts   = 0.0
        self.last_reason   = None
        self._normal_since = None   # when status became normal after a STOP

    # ── Called for every prediction ───────────────────────────
    def update(self, pred: dict, simulated: bool = False):
        """Returns the payload that was sent (STOP / RUN) or None."""
        if not self.enabled or simulated:
            return None

        now       = self.clock()
        status    = pred.get("status", "normal")
        motor_off = pred.get("motor_off", False)

        if status == "critical" and not motor_off:
            self._normal_since = None
            first_stop = self.last_cmd != STOP
            if first_stop or now - self.last_cmd_ts >= self.resend_s:
                reason = f"AUTO-STOP: fault={pred.get('fault_type')} health={pred.get('health_score')}"
                return self._send(STOP, reason, now)
            return None

        # Not critical — only relevant if we are holding the machine stopped
        if self.last_cmd != STOP or not self.auto_resume:
            return None
        if status != "normal" or motor_off:
            # Motor is off (it obeyed STOP) or still warning → keep waiting
            self._normal_since = None
            return None
        if self._normal_since is None:
            self._normal_since = now
        if now - self._normal_since >= self.resume_hold_s:
            return self._send(RUN, f"AUTO-RUN: normal for {self.resume_hold_s:.0f}s", now)
        return None

    # ── Manual resume (operator) ──────────────────────────────
    def resume(self, reason: str = "MANUAL-RUN"):
        return self._send(RUN, reason, self.clock())

    def _send(self, payload: str, reason: str, now: float):
        if not self.publish(payload, reason):
            return None
        self.last_cmd      = payload
        self.last_cmd_ts   = now
        self.last_reason   = reason
        self._normal_since = None
        return payload

    def state(self) -> dict:
        return {
            "enabled":     self.enabled,
            "auto_resume": self.auto_resume,
            "last_cmd":    {STOP: "STOP", RUN: "RUN"}.get(self.last_cmd),
            "last_cmd_ts": int(self.last_cmd_ts * 1000) if self.last_cmd else None,
            "last_reason": self.last_reason,
            "holding_stop": self.last_cmd == STOP,
        }
