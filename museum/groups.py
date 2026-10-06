"""跨文物共因风险组：对同一评估版本内的风险事件做连通分量分组。

规则：
- 仅**同一指标**（温度/湿度）的事件之间可能连边；
- 两个事件的**起止时间闭区间相交**（端点相接也算相交），
  且涉及**至少一个相同运输节点**时连边；
- 取连通分量，**覆盖至少两件文物**的分量成为一个共因风险组；
- 传递相连的成员全部纳入同一组——不要求每对成员都有共同节点，
  因此全组共有节点（所有成员节点集合的交集）可能为空。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone


def _parse(ts):
    """解析时间戳为 UTC datetime（兼容 datetime 对象与 ISO 字符串）。"""
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _overlaps(a_start, a_end, b_start, b_end):
    """闭区间 [a_start, a_end] 与 [b_start, b_end] 是否相交。"""
    return a_start <= b_end and b_start <= a_end


def build_risk_groups(events):
    """对同一评估版本的事件列表计算共因风险组。

    events: 字典列表，键含 id、artifact_id、metric、start_ts、end_ts、
            nodes（[{"id": .., "name": ..}, ...]）。
    返回组列表（按开始时间、指标排序），每组含：
    metric、start_ts、end_ts（datetime）、event_ids、artifact_ids（集合）、
    common_node_ids（全组共有节点 id 集合，可能为空）。
    """
    by_metric = defaultdict(list)
    for e in events:
        by_metric[e["metric"]].append(e)

    groups = []
    for metric, evs in by_metric.items():
        n = len(evs)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        spans = [(_parse(e["start_ts"]), _parse(e["end_ts"])) for e in evs]
        node_sets = [{nd["id"] for nd in e["nodes"]} for e in evs]
        for i in range(n):
            si, ei = spans[i]
            for j in range(i + 1, n):
                sj, ej = spans[j]
                if _overlaps(si, ei, sj, ej) and node_sets[i] & node_sets[j]:
                    union(i, j)

        components = defaultdict(list)
        for i in range(n):
            components[find(i)].append(i)

        for members in components.values():
            artifact_ids = {evs[i]["artifact_id"] for i in members}
            if len(artifact_ids) < 2:
                continue  # 仅覆盖单件文物的分量不成组
            common = set.intersection(*(node_sets[i] for i in members))
            groups.append({
                "metric": metric,
                "start_ts": min(spans[i][0] for i in members),
                "end_ts": max(spans[i][1] for i in members),
                "event_ids": [evs[i]["id"] for i in members],
                "artifact_ids": artifact_ids,
                "common_node_ids": common,
            })

    groups.sort(key=lambda g: (g["start_ts"], g["metric"]))
    return groups
