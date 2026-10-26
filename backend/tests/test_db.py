import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import Store

NOW = int(time.time() * 1000)


def reading(ts, temp=27.0, mac="E4:65:B8:2C:73:48"):
    return {"timestamp": ts, "mac": mac, "seq": 1, "aRMSx": 0.05, "aRMSy": 0.1, "aRMSz": 0.05,
            "vRMSx": 0.5, "vRMSy": 1.1, "vRMSz": 0.5, "temperature": temp, "aucausticRMS": 49.0}


def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


def test_history_range_thins_evenly_and_keeps_latest(tmp_path):
    s = store(tmp_path)
    for i in range(1000):
        s.add_reading(reading(NOW - (1000 - i) * 1000, temp=20 + i / 100))
    rows = s.history_range(limit=100)
    assert len(rows) <= 100
    assert rows[-1]["timestamp"] == NOW - 1000          # newest reading always included
    assert rows[0]["timestamp"] < NOW - 900_000         # covers the whole span, not just the tail
    assert [r["timestamp"] for r in rows] == sorted(r["timestamp"] for r in rows)
    assert rows[0]["mac"] == "E4:65:B8:2C:73:48"


def test_history_range_since(tmp_path):
    s = store(tmp_path)
    for i in range(10):
        s.add_reading(reading(NOW - i * 60_000))
    assert len(s.history_range(since_ms=NOW - 150_000, limit=1000)) == 3


def test_history_page_filters_and_paginates(tmp_path):
    s = store(tmp_path)
    for i in range(45):
        s.add_reading(reading(NOW - i * 1000, temp=30.5 if i % 9 == 0 else 27.0))
    rows, total = s.history_page(page=3, limit=20)
    assert total == 45 and len(rows) == 5
    rows, total = s.history_page(q="30.5", limit=20)
    assert total == 5
    rows, total = s.history_page(start_ms=NOW - 9_500, end_ms=NOW + 1, limit=50)
    assert total == 10
    assert rows[0]["timestamp"] > rows[-1]["timestamp"]  # newest first


def test_alert_dedup_and_ack(tmp_path):
    s = store(tmp_path)
    a = s.add_alert({"type": "temperature", "severity": "critical", "message": "hot", "timestamp": NOW})
    assert a["id"] and not a["acknowledged"]
    assert s.add_alert({"type": "temperature", "severity": "critical", "message": "hot", "timestamp": NOW + 5000}) is None
    assert s.add_alert({"type": "temperature", "severity": "critical", "message": "hot", "timestamp": NOW + 61_000})
    acked = s.ack_alert(a["id"], "Vansh")
    assert acked["acknowledged"] and acked["ack_by"] == "Vansh"
    assert s.ack_alert(a["id"], "Someone else") is None        # already acknowledged
    assert [x["id"] for x in s.alerts(unacked_only=True)] != [a["id"]]
    assert len(s.alerts()) == 2


def test_commands_audit(tmp_path):
    s = store(tmp_path)
    s.add_command("1", "AUTO-STOP", "wired/control")
    s.add_command("0", "MANUAL-RUN by Vansh", "wired/control")
    assert [c["command"] for c in s.commands()] == ["RUN", "STOP"]


def test_csv_import_skips_bad_rows_and_runs_once(tmp_path):
    csv_path = tmp_path / "sensor_data.csv"
    csv_path.write_text(
        "timestamp,seq,key,aRMSx,aRMSy,aRMSz,vRMSx,vRMSy,vRMSz,temperature,aucausticRMS,extra,mac\n"
        f"{NOW},1,RMS,0.05,0.1,0.05,0.5,1.1,0.5,27.1,49,52,E4:65:B8:2C:73:48\n"
        f"\0\0\0{NOW + 1},2,RMS,0.05,0.1,0.05,0.5,1.1,0.5,27.2,49,,E4:65:B8:2C:73:48\n"   # NUL bytes, empty extra
        f"{NOW + 2},3,RMS,0.05,0.1,0.05,0.5,1.1,0.5,27.3,49,52,SIM:00:00:00:00:01\n"      # simulator row
        ",,RMS,garbage\n"
    )
    s = store(tmp_path)
    assert s.import_csv(str(csv_path)) == (2, 2)
    assert s.import_csv(str(csv_path)) == (0, 0)       # second start: nothing re-imported
    assert len(s.history_range()) == 2


def test_prune(tmp_path):
    s = store(tmp_path)
    s.add_reading(reading(NOW - 400 * 86_400_000))
    s.add_reading(reading(NOW))
    assert s.prune(365) == 1
    assert len(s.history_range()) == 1
