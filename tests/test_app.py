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
from museum.groups import build_risk_groups

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


def make_event(eid, artifact_id, metric, start_min, end_min, node_ids):
    return {
        "id": eid,
        "artifact_id": artifact_id,
        "metric": metric,
        "start_ts": (BASE + timedelta(minutes=start_min)).isoformat(),
        "end_ts": (BASE + timedelta(minutes=end_min)).isoformat(),
        "nodes": [{"id": n, "name": f"节点{n}"} for n in node_ids],
    }


class RiskGroupTest(unittest.TestCase):
    def test_edge_requires_overlap_and_shared_node(self):
        """连边需同时满足：时间闭区间相交 + 至少一个相同节点。"""
        events = [
            make_event(1, 1, "temperature", 0, 10, [1, 2]),
            make_event(2, 2, "temperature", 5, 15, [3]),   # 时间相交但无共同节点
            make_event(3, 3, "temperature", 20, 30, [2]),  # 有共同节点但时间不相交
        ]
        self.assertEqual(build_risk_groups(events), [])

    def test_closed_interval_boundary_counts_as_overlap(self):
        """闭区间端点相接视为相交。"""
        events = [
            make_event(1, 1, "temperature", 0, 10, [1]),
            make_event(2, 2, "temperature", 10, 20, [1]),
        ]
        groups = build_risk_groups(events)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]["event_ids"]), [1, 2])

    def test_transitive_members_grouped_with_empty_common_nodes(self):
        """传递相连的成员纳入同组；全组共有节点可为空。"""
        events = [
            make_event(1, 1, "temperature", 0, 10, [1, 2]),
            make_event(2, 2, "temperature", 5, 15, [2, 3]),
            make_event(3, 3, "temperature", 10, 20, [3, 4]),  # 与事件 1 不直接相连
        ]
        groups = build_risk_groups(events)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(sorted(g["event_ids"]), [1, 2, 3])
        self.assertEqual(g["artifact_ids"], {1, 2, 3})
        self.assertEqual(g["common_node_ids"], set())  # 并非每对成员都有共同节点
        self.assertEqual(g["start_ts"], BASE)
        self.assertEqual(g["end_ts"], BASE + timedelta(minutes=20))

    def test_single_artifact_component_not_grouped(self):
        """分量只覆盖一件文物时不成组。"""
        events = [
            make_event(1, 1, "temperature", 0, 10, [1]),
            make_event(2, 1, "temperature", 5, 15, [1]),
        ]
        self.assertEqual(build_risk_groups(events), [])

    def test_metrics_are_separate(self):
        """不同指标的事件即使同时段同节点也不连边。"""
        events = [
            make_event(1, 1, "temperature", 0, 10, [1]),
            make_event(2, 2, "humidity", 0, 10, [1]),
        ]
        self.assertEqual(build_risk_groups(events), [])


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

    def test_risk_groups_flow(self):
        # 批次 + 三个有序节点 + 三件文物
        st, batch = self.req("POST", "/api/batches", {"name": "共因风险组测试"})
        self.assertEqual(st, 201)
        bid = batch["id"]
        for seq, name in [(1, "库房"), (2, "运输车"), (3, "展厅")]:
            st, _ = self.req("POST", f"/api/batches/{bid}/nodes",
                             {"seq": seq, "name": name})
            self.assertEqual(st, 201)
        aids = {}
        for name in ("青花瓷瓶", "青铜鼎", "玉璧"):
            st, art = self.req("POST", f"/api/batches/{bid}/artifacts",
                               {"name": name, "temp_min": 18, "temp_max": 24,
                                "hum_min": 45, "hum_max": 60, "sample_interval": 600})
            self.assertEqual(st, 201)
            aids[name] = art["id"]
        _, detail = self.req("GET", f"/api/batches/{bid}")
        n1, n2, n3 = [n["id"] for n in detail["nodes"]]
        a1, a2, a3 = aids["青花瓷瓶"], aids["青铜鼎"], aids["玉璧"]

        def rd(aid, nid, ts, t=27):
            return {"artifact_id": aid, "node_id": nid, "ts": ts,
                    "temperature": t, "humidity": 50}

        # v1：文物1 事件 [08:10,08:20]@节点{库房,运输车}；
        #     文物2 事件 [08:15,08:25]@节点{运输车,展厅} → 时间相交 + 共用运输车
        st, res = self.req("POST", f"/api/batches/{bid}/readings", {"readings": [
            rd(a1, n1, "2026-10-01T08:00:00Z", 20),
            rd(a1, n1, "2026-10-01T08:10:00Z", 26),
            rd(a1, n2, "2026-10-01T08:20:00Z", 27),
            rd(a1, n2, "2026-10-01T08:30:00Z", 21),
            rd(a2, n2, "2026-10-01T08:05:00Z", 20),
            rd(a2, n2, "2026-10-01T08:15:00Z", 28),
            rd(a2, n3, "2026-10-01T08:25:00Z", 26),
            rd(a2, n3, "2026-10-01T08:35:00Z", 21),
        ], "note": "首批数据"})
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 1)
        self.assertEqual(len(res["risk_groups"]), 1)  # 提交读数即返回新版分组

        # 当前版风险组：一组两事件，全组共有节点为运输车
        st, rg = self.req("GET", f"/api/batches/{bid}/risk-groups")
        self.assertEqual(st, 200)
        self.assertEqual(rg["version"], 1)
        self.assertEqual(len(rg["groups"]), 1)
        g = rg["groups"][0]
        self.assertEqual(g["metric"], "temperature")
        self.assertEqual(g["metric_label"], "温度")
        self.assertEqual((g["open_events"], g["closed_events"]), (2, 0))
        self.assertEqual(len(g["events"]), 2)
        self.assertEqual({a["name"] for a in g["artifacts"]}, {"青花瓷瓶", "青铜鼎"})
        self.assertEqual([n["name"] for n in g["common_nodes"]], ["运输车"])
        self.assertTrue(g["start_ts"].startswith("2026-10-01T08:10"))
        self.assertTrue(g["end_ts"].startswith("2026-10-01T08:25"))

        # 关闭事件：只更新所属版本的状态计数，成员不变
        ev1 = [e for e in g["events"] if e["artifact_id"] == a1][0]
        st, _ = self.req("POST", f"/api/events/{ev1['id']}/close",
                         {"note": "已调整温控", "evidence_no": "EV-1"})
        self.assertEqual(st, 200)
        st, rg = self.req("GET", f"/api/batches/{bid}/risk-groups")
        g = rg["groups"][0]
        self.assertEqual((g["open_events"], g["closed_events"]), (1, 1))
        self.assertEqual(len(g["events"]), 2)

        # v2：补录文物3 读数 → 重算分组。文物3 事件 [08:22,08:22]@节点{展厅}
        # 与文物2 事件相连、与文物1 事件不直接相连（传递相连入组，全组无共有节点）
        st, res2 = self.req("POST", f"/api/batches/{bid}/readings", {"readings": [
            rd(a3, n3, "2026-10-01T08:12:00Z", 20),
            rd(a3, n3, "2026-10-01T08:22:00Z", 29),
            rd(a3, n3, "2026-10-01T08:32:00Z", 21),
        ], "note": "补录玉璧"})
        self.assertEqual(st, 201)
        self.assertEqual(res2["version"], 2)
        st, rg2 = self.req("GET", f"/api/batches/{bid}/risk-groups")
        self.assertEqual(rg2["version"], 2)
        self.assertEqual(len(rg2["groups"]), 1)
        g2 = rg2["groups"][0]
        self.assertEqual(len(g2["events"]), 3)          # 传递相连成员纳入
        self.assertEqual(len(g2["artifacts"]), 3)
        self.assertEqual(g2["common_nodes"], [])         # 全组无共有节点
        self.assertEqual((g2["open_events"], g2["closed_events"]), (3, 0))  # 不继承关闭

        # 旧版分组保持可查，状态计数停留在旧版（v1 已关闭一件）
        st, rg1 = self.req("GET", f"/api/batches/{bid}/risk-groups?version=1")
        self.assertEqual(st, 200)
        self.assertEqual(rg1["version"], 1)
        self.assertEqual(len(rg1["groups"]), 1)
        g1 = rg1["groups"][0]
        self.assertEqual(len(g1["events"]), 2)
        self.assertEqual((g1["open_events"], g1["closed_events"]), (1, 1))

        # 版本快照内含该版本风险组
        st, v1 = self.req("GET", f"/api/batches/{bid}/versions/1")
        self.assertEqual(st, 200)
        self.assertEqual(len(v1["risk_groups"]), 1)
        self.assertEqual(len(v1["risk_groups"][0]["events"]), 2)

        # 批次 / 版本不存在 → 中文错误
        st, err = self.req("GET", "/api/batches/9999/risk-groups")
        self.assertEqual(st, 404)
        self.assertIn("不存在", err["error"])
        st, err = self.req("GET", f"/api/batches/{bid}/risk-groups?version=99")
        self.assertEqual(st, 404)
        self.assertIn("不存在", err["error"])

        # 批次详情页展示当前版风险组
        url = f"http://127.0.0.1:{self.port}/batches/{bid}"
        with urllib.request.urlopen(url) as resp:
            html = resp.read().decode()
        self.assertIn("跨文物共因风险组", html)
        self.assertIn("玉璧", html)


if __name__ == "__main__":
    unittest.main()
