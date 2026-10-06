"""评估逻辑与 HTTP API 的端到端测试（仅标准库）。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from museum.evaluate import evaluate_artifact

BASE = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)


def make_reading(minutes, temp, hum, node_id=1, node_name="节点", node_seq=1):
    return {
        "ts": BASE + timedelta(minutes=minutes),
        "temperature": temp,
        "humidity": hum,
        "node_id": node_id,
        "node_name": node_name,
        "node_seq": node_seq,
    }


ARTIFACT = {"temp_min": 18.0, "temp_max": 24.0, "hum_min": 45.0, "hum_max": 60.0,
            "sample_interval": 600}


class EvaluateTest(unittest.TestCase):
    def test_merge_across_nodes(self):
        """同一指标的连续越界读数跨节点合并为一个事件。"""
        readings = [
            make_reading(0, 20, 50, 1, "库房", 1),
            make_reading(10, 26, 50, 1, "库房", 1),   # 越界开始
            make_reading(20, 27, 50, 2, "运输车", 2),  # 跨节点仍连续
            make_reading(30, 25, 50, 3, "展厅", 3),
            make_reading(40, 21, 50, 3, "展厅", 3),   # 恢复正常
        ]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(len(events), 1)
        self.assertEqual(gaps, [])
        ev = events[0]
        self.assertEqual(ev["metric"], "temperature")
        self.assertEqual(ev["reading_count"], 3)
        self.assertEqual([n["name"] for n in ev["nodes"]], ["库房", "运输车", "展厅"])
        self.assertAlmostEqual(ev["max_deviation"], 3.0)

    def test_gap_interrupts_event(self):
        """相邻读数间隔超过采样间隔：记缺测区间并中断事件。"""
        readings = [
            make_reading(0, 26, 50),    # 越界
            make_reading(10, 27, 50),   # 越界
            make_reading(60, 28, 50),   # 间隔 50 分钟 > 10 分钟 → 缺测，事件中断
            make_reading(70, 21, 50),
        ]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["expected_interval"], 600)
        self.assertEqual(gaps[0]["actual_interval"], 3000)
        temp_events = [e for e in events if e["metric"] == "temperature"]
        self.assertEqual(len(temp_events), 2)
        self.assertEqual(temp_events[0]["reading_count"], 2)
        self.assertEqual(temp_events[1]["reading_count"], 1)

    def test_single_outlier_is_event(self):
        readings = [make_reading(0, 20, 50), make_reading(10, 20, 75)]
        events, _ = evaluate_artifact(readings, ARTIFACT)
        hum = [e for e in events if e["metric"] == "humidity"]
        self.assertEqual(len(hum), 1)
        self.assertEqual(hum[0]["reading_count"], 1)

    def test_all_in_range(self):
        readings = [make_reading(i * 10, 21, 50) for i in range(5)]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(events, [])
        self.assertEqual(gaps, [])


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "test.db")
        cls.server = app.make_server("127.0.0.1", 0, db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_full_flow(self):
        # 创建批次
        st, batch = self.req("POST", "/api/batches",
                             {"name": "2026 春季书画巡展", "description": "测试"})
        self.assertEqual(st, 201)
        bid = batch["id"]

        # 有序节点
        for seq, name in [(1, "库房"), (2, "运输车"), (3, "展厅")]:
            st, _ = self.req("POST", f"/api/batches/{bid}/nodes",
                             {"seq": seq, "name": name})
            self.assertEqual(st, 201)
        # 重复节点序号 → 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/nodes",
                           {"seq": 1, "name": "重复"})
        self.assertEqual(st, 400)
        self.assertIn("已存在", err["error"])

        # 无序上下限 → 拒绝并说明原因
        st, err = self.req("POST", f"/api/batches/{bid}/artifacts",
                           {"name": "坏文物", "temp_min": 25, "temp_max": 18,
                            "hum_min": 45, "hum_max": 60, "sample_interval": 600})
        self.assertEqual(st, 400)
        self.assertIn("无序", json.dumps(err, ensure_ascii=False))

        # 正常文物
        st, art = self.req("POST", f"/api/batches/{bid}/artifacts",
                           {"name": "青花瓷瓶", "temp_min": 18, "temp_max": 24,
                            "hum_min": 45, "hum_max": 60, "sample_interval": 600})
        self.assertEqual(st, 201)
        aid = art["id"]
        _, detail = self.req("GET", f"/api/batches/{bid}")
        n1, n2, n3 = [n["id"] for n in detail["nodes"]]

        # 非数值读数 → 拒绝并说明原因
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [{"artifact_id": aid, "node_id": n1,
                                          "ts": "2026-10-01T08:00:00Z",
                                          "temperature": "abc", "humidity": 50}]})
        self.assertEqual(st, 400)
        self.assertIn("非数值", json.dumps(err, ensure_ascii=False))

        # 第一批读数：温度在库房→运输车→展厅连续越界（跨节点合并），
        # 之后出现超过采样间隔的缺测，再出现一次越界
        readings = [
            {"artifact_id": aid, "node_id": n1, "ts": "2026-10-01T08:00:00Z",
             "temperature": 20, "humidity": 50},
            {"artifact_id": aid, "node_id": n1, "ts": "2026-10-01T08:10:00Z",
             "temperature": 26, "humidity": 50},
            {"artifact_id": aid, "node_id": n2, "ts": "2026-10-01T08:20:00Z",
             "temperature": 27, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T08:30:00Z",
             "temperature": 25, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T08:40:00Z",
             "temperature": 21, "humidity": 50},
            # 缺测：08:40 → 09:40 间隔 60 分钟 > 10 分钟
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T09:40:00Z",
             "temperature": 26, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T09:50:00Z",
             "temperature": 21, "humidity": 50},
        ]
        st, res = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": readings, "note": "首批数据"})
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 1)
        temp_events = [e for e in res["events"] if e["metric"] == "temperature"]
        self.assertEqual(len(temp_events), 2)  # 缺测中断 → 两个事件
        self.assertEqual(temp_events[0]["reading_count"], 3)
        self.assertEqual([n["name"] for n in temp_events[0]["nodes"]],
                         ["库房", "运输车", "展厅"])
        self.assertEqual(len(res["gaps"]), 1)

        # 重复时间戳（相同数值）→ 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [readings[0]]})
        self.assertEqual(st, 400)
        self.assertIn("重复时间戳", json.dumps(err, ensure_ascii=False))

        # 同一提交内重复时间戳 → 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [
                               {"artifact_id": aid, "node_id": n1,
                                "ts": "2026-10-01T10:00:00Z",
                                "temperature": 20, "humidity": 50},
                               {"artifact_id": aid, "node_id": n1,
                                "ts": "2026-10-01T10:00:00Z",
                                "temperature": 22, "humidity": 55}]})
        self.assertEqual(st, 400)
        self.assertIn("重复时间戳", json.dumps(err, ensure_ascii=False))

        # 关闭事件：缺少处置说明/证据编号 → 拒绝
        ev_id = temp_events[0]["id"]
        st, err = self.req("POST", f"/api/events/{ev_id}/close", {"note": "已处置"})
        self.assertEqual(st, 400)
        self.assertIn("证据编号", err["error"])
        # 填写完整 → 关闭成功
        st, closed = self.req("POST", f"/api/events/{ev_id}/close",
                              {"note": "已调整车厢温控并复查", "evidence_no": "EV-2026-001"})
        self.assertEqual(st, 200)
        self.assertEqual(closed["status"], "closed")

        # 更正读数（同一时间戳、不同数值）→ 生成新版本
        st, res2 = self.req("POST", f"/api/batches/{bid}/readings",
                            {"readings": [{"artifact_id": aid, "node_id": n2,
                                           "ts": "2026-10-01T08:20:00Z",
                                           "temperature": 22, "humidity": 50}],
                             "note": "更正运输车读数"})
        self.assertEqual(st, 201)
        self.assertEqual(res2["version"], 2)
        self.assertEqual(res2["corrected"], 1)

        # 新版事件不继承关闭状态
        v2_events = res2["events"]
        self.assertTrue(all(e["status"] == "open" for e in v2_events))

        # 旧版读数与关闭结论保持可查
        st, v1 = self.req("GET", f"/api/batches/{bid}/versions/1")
        self.assertEqual(st, 200)
        old_event = [e for e in v1["events"] if e["id"] == ev_id][0]
        self.assertEqual(old_event["status"], "closed")
        self.assertEqual(old_event["close_evidence"], "EV-2026-001")
        old_reading = [r for r in v1["readings"]
                       if r["ts"].startswith("2026-10-01T08:20")][0]
        self.assertEqual(old_reading["temperature"], 27)  # 旧值保留

        # 版本差异
        st, diff = self.req("GET", f"/api/batches/{bid}/diff?from=1&to=2")
        self.assertEqual(st, 200)
        self.assertEqual(len(diff["readings"]["corrected"]), 1)
        self.assertGreaterEqual(len(diff["events"]["closure_not_inherited"]), 1)

        # 历史版本事件不可再关闭
        st, err = self.req("POST", f"/api/events/{ev_id}/close",
                           {"note": "x", "evidence_no": "y"})
        self.assertEqual(st, 400)

        # 批次风险摘要
        st, summary = self.req("GET", f"/api/batches/{bid}/summary")
        self.assertEqual(st, 200)
        self.assertEqual(summary["current_version"], 2)
        self.assertEqual(summary["gaps"], 1)
        self.assertGreaterEqual(summary["open_events"], 1)

        # 页面可访问
        for path in ("/", f"/batches/{bid}", f"/batches/{bid}/versions/1",
                     f"/batches/{bid}/diff?from=1&to=2"):
            url = f"http://127.0.0.1:{self.port}{path}"
            with urllib.request.urlopen(url) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers["Content-Type"])


if __name__ == "__main__":
    unittest.main()
