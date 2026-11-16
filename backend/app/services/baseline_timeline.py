"""基线健康时间线：把基线定义、版本与运行分区数据关联成可追溯的时间线。

设计要点：

* **版本归属**：运行结果可以指定基线版本；没有指定时按到达时间取最新生效版本。
  版本只覆盖自己声明过的字段，其余字段沿着承接链回落到原有基线定义。
* **幂等节点**：每个关键节点都有稳定的节点键，重复计算只会命中已有节点，不会重复写入。
* **历史不可变**：节点只追加不修改；后续运行结果只能追加新节点，不能改写旧节点。
* **同事务落库**：节点、基线当前状态与运行实例结论投影在一次事务里写入，失败整体回滚。
* **只记录已发生的事实**：未来的预警/破线只是预测，写在基线状态里，不提前生成历史节点。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from app.services.timeutil import fmt_dt, parse_dt, parse_int, parse_jobs
from app.store import store

BASELINE_MODULE = "baseline"
NODE_MODULE = "_baseline_timeline_node"
STATE_MODULE = "_baseline_timeline_state"
INGEST_MODULE = "_baseline_timeline_ingest"
RUN_MODULE = "_run_partition"
VERSION_MODULE = "_baseline_version"
RUNTIME_MODULE = "runtime"

REQUIRED_RUN_FIELDS = ["运行编号", "作业名称"]
INHERITED_FIELDS = ["承诺完成时间", "预警余量分钟", "破线级别", "关联作业列表", "工作日标记", "节假日偏移"]
NODE_PRIORITY = {
    "基线版本": 0,
    "运行水位": 1,
    "分区缺失": 2,
    "分区完整": 3,
    "预计完成": 4,
    "预警触发": 5,
    "破线判定": 6,
    "结论确认": 7,
}
RISK_ORDER = {"已破线": 5, "高": 4, "中": 3, "低": 2, "待观测": 1}
COMPLETE_STATES = {"成功", "完成", "已完成"}


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _first(*values: object) -> str:
    for value in values:
        text = str(value if value is not None else "").strip()
        if text:
            return text
    return ""


class BaselineTimelineService:
    def __init__(self) -> None:
        self._ready = False

    # ------------------------------------------------------------------ 读取辅助
    @staticmethod
    def _baseline_rows() -> list[dict[str, Any]]:
        return store.rows(BASELINE_MODULE)

    @staticmethod
    def _version_rows(baseline_no: str) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(VERSION_MODULE) if row.get("基线编号") == baseline_no]
        return sorted(rows, key=lambda row: parse_int(row.get("版本号"), 1) or 1)

    @staticmethod
    def _run_rows() -> list[dict[str, Any]]:
        return store.rows(RUN_MODULE)

    @staticmethod
    def _node_rows() -> list[dict[str, Any]]:
        return store.rows(NODE_MODULE)

    @staticmethod
    def _state_rows() -> list[dict[str, Any]]:
        return store.rows(STATE_MODULE)

    # ------------------------------------------------------------------ 版本归属
    def _effective_version(self, baseline: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
        """沿承接链回放版本覆盖字段，未声明的字段承接上一版或基线定义。"""
        baseline_no = str(baseline.get("基线编号", ""))
        versions = {int(row.get("版本号", 1) or 1): row for row in self._version_rows(baseline_no)}
        chain: list[dict[str, Any]] = []
        cursor = int(version.get("版本号", 1) or 1)
        visited: set[int] = set()
        while cursor and cursor not in visited:
            visited.add(cursor)
            row = versions.get(cursor)
            if row is None:
                break
            chain.append(row)
            parent = parse_int(row.get("承接版本"))
            cursor = parent if parent is not None else 0
        chain.reverse()

        fields = {field: baseline.get(field) for field in INHERITED_FIELDS}
        inherited_from: dict[str, str] = {field: "基线定义" for field in INHERITED_FIELDS}
        for row in chain:
            for field in INHERITED_FIELDS:
                value = row.get(field)
                if value is not None and str(value).strip() != "":
                    fields[field] = value
                    inherited_from[field] = f"v{row.get('版本号')}"
        return {
            "基线编号": baseline_no,
            "基线名称": baseline.get("基线名称"),
            "版本号": int(version.get("版本号", 1) or 1),
            "生效时间": fmt_dt(version.get("生效时间")),
            "承接版本": parse_int(version.get("承接版本")),
            "承诺完成时间": fmt_dt(fields.get("承诺完成时间")),
            "预警余量分钟": parse_int(fields.get("预警余量分钟"), 0) or 0,
            "破线级别": _first(fields.get("破线级别"), "P3"),
            "关联作业列表": parse_jobs(fields.get("关联作业列表")),
            "工作日标记": fields.get("工作日标记"),
            "节假日偏移": fields.get("节假日偏移"),
            "字段承接": inherited_from,
            "变更说明": version.get("变更说明") or "",
        }

    def _resolve_version(
        self,
        baseline: dict[str, Any],
        arrival: datetime | None,
        claimed: int | None,
    ) -> tuple[dict[str, Any] | None, str]:
        """决定一条运行结果归属哪一版基线，并给出可追溯的选取依据。"""
        versions = self._version_rows(str(baseline.get("基线编号", "")))
        if not versions:
            synthetic = {
                "版本号": 1,
                "生效时间": "",
                "承接版本": None,
                "基线编号": baseline.get("基线编号"),
            }
            return self._effective_version(baseline, synthetic), "基线尚无版本记录，按基线定义当前字段归属"

        at = arrival or datetime.max
        if claimed is not None:
            for row in versions:
                if (parse_int(row.get("版本号"), 1) or 1) != claimed:
                    continue
                started = parse_dt(row.get("生效时间"))
                if started is None or started <= at:
                    return self._effective_version(baseline, row), "运行结果指定基线版本"
                return (
                    self._effective_version(baseline, row),
                    f"运行结果指定版本 v{claimed}，但该版本在到达时尚未生效，仍按指定归属",
                )
            return None, f"指定版本 v{claimed} 不存在，运行结果不纳入时间线"

        effective = [
            row for row in versions
            if (parse_dt(row.get("生效时间")) or datetime.min) <= at
        ]
        if effective:
            latest = max(effective, key=lambda row: parse_int(row.get("版本号"), 1) or 1)
            return (
                self._effective_version(baseline, latest),
                "未指定版本，按运行结果到达时间取最新生效版本",
            )
        earliest = versions[0]
        return (
            self._effective_version(baseline, earliest),
            "到达时无生效版本，按最早版本归属并承接基线定义",
        )

    def _current_version(self, baseline: dict[str, Any], at: datetime | None) -> dict[str, Any] | None:
        version, _ = self._resolve_version(baseline, at, None)
        return version

    # ------------------------------------------------------------------ 运行结果解析
    @staticmethod
    def _missing_partitions(run: dict[str, Any]) -> tuple[list[str], int]:
        raw = run.get("缺失分区")
        if isinstance(raw, (list, tuple, set)):
            items = [str(item).strip() for item in raw if str(item).strip()]
        else:
            text = str(raw or "")
            for separator in ("；", ";", "，", "、", "|"):
                text = text.replace(separator, ",")
            items = [item.strip() for item in text.split(",") if item.strip()]
        expected = parse_int(run.get("期望分区数"), 0) or 0
        produced = parse_int(run.get("已产出分区数"), 0) or 0
        missing_count = max(expected - produced, 0) if expected else 0
        return items, max(len(items), missing_count)

    @staticmethod
    def _water_level(run: dict[str, Any]) -> int:
        explicit = parse_int(run.get("运行水位"))
        if explicit is not None:
            return max(0, min(100, explicit))
        expected = parse_int(run.get("期望分区数"), 0) or 0
        produced = parse_int(run.get("已产出分区数"), 0) or 0
        if expected <= 0:
            return 100 if BaselineTimelineService._is_complete(run) else 0
        return max(0, min(100, round(produced * 100 / expected)))

    @staticmethod
    def _is_complete(run: dict[str, Any]) -> bool:
        if parse_dt(run.get("完成时间")):
            return True
        if str(run.get("执行状态") or "").strip() in COMPLETE_STATES:
            return True
        expected = parse_int(run.get("期望分区数"), 0) or 0
        produced = parse_int(run.get("已产出分区数"), 0) or 0
        return expected > 0 and produced >= expected

    def _run_facts(self, run: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
        arrival = parse_dt(run.get("到达时间")) or parse_dt(run.get("开始时间"))
        completion = parse_dt(run.get("完成时间"))
        complete = self._is_complete(run)
        eta = completion or parse_dt(run.get("预计完成时间"))
        commit = parse_dt(version.get("承诺完成时间"))
        margin = int(version.get("预警余量分钟") or 0)
        warning = commit - timedelta(minutes=margin) if commit else None
        missing_list, missing_count = self._missing_partitions(run)
        return {
            "arrival": arrival,
            "completion": completion,
            "complete": complete,
            "eta": eta,
            "commit": commit,
            "margin": margin,
            "warning": warning,
            "water": self._water_level(run),
            "missing_list": missing_list,
            "missing_count": missing_count,
        }

    @staticmethod
    def _run_risk(facts: dict[str, Any], eval_at: datetime) -> str:
        commit, warning = facts["commit"], facts["warning"]
        if facts["complete"]:
            if commit and facts["completion"] and facts["completion"] > commit:
                return "高"
            return "低" if facts["missing_count"] == 0 else "中"
        if (commit and facts["eta"] and facts["eta"] > commit) or (commit and eval_at > commit):
            return "高"
        if (
            (warning and facts["eta"] and facts["eta"] > warning)
            or (warning and eval_at >= warning)
            or facts["missing_count"] > 0
        ):
            return "中"
        return "低"

    @staticmethod
    def _run_order(run: dict[str, Any]) -> tuple[datetime, datetime, int]:
        """同一作业的多条结果里，较晚到达的那条才算最新。

        到达时间是主序，数据日期只是同批到达时的次序，避免把迟到的旧数据日期结果
        误当成最新水位。
        """
        arrival = parse_dt(run.get("到达时间")) or parse_dt(run.get("开始时间")) or datetime.min
        data_date = parse_dt(run.get("数据日期")) or datetime.min
        return (arrival, data_date, int(run.get("id", 0) or 0))

    def _normalize_run(self, values: dict[str, Any]) -> dict[str, Any]:
        missing = [field for field in REQUIRED_RUN_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            raise ValueError("、".join(missing))
        arrival = fmt_dt(values.get("到达时间") or values.get("开始时间") or datetime.now())
        expected = parse_int(values.get("期望分区数"), 0) or 0
        produced = parse_int(values.get("已产出分区数"), 0) or 0
        missing_list, _ = self._missing_partitions(values)
        return {
            "运行编号": str(values.get("运行编号")).strip(),
            "作业名称": str(values.get("作业名称")).strip(),
            "数据日期": fmt_dt(values.get("数据日期")),
            "分区批次": str(values.get("分区批次") or "").strip(),
            "期望分区数": expected,
            "已产出分区数": produced,
            "缺失分区": ",".join(missing_list),
            "运行水位": parse_int(values.get("运行水位"), round(produced * 100 / expected) if expected else None),
            "开始时间": fmt_dt(values.get("开始时间") or arrival),
            "到达时间": arrival,
            "完成时间": fmt_dt(values.get("完成时间")),
            "预计完成时间": fmt_dt(values.get("预计完成时间")),
            "执行状态": str(values.get("执行状态") or "运行中").strip(),
            "基线版本": parse_int(values.get("基线版本")),
            "适用基线": ",".join(parse_jobs(values.get("适用基线"))),
            "来源": str(values.get("来源") or "接口提交").strip(),
        }

    @staticmethod
    def _fingerprint(run: dict[str, Any]) -> str:
        stable = {key: value for key, value in run.items() if key not in {"到达时间", "开始时间", "来源"}}
        blob = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------------ 节点写入
    def _append_node(
        self,
        nodes: list[dict[str, Any]],
        *,
        tx_id: str,
        baseline_no: str,
        version_no: int | None,
        node_type: str,
        node_time: datetime | None,
        title: str,
        detail: str,
        risk: str,
        run_id: str = "",
        job: str = "",
        version_basis: str = "",
        water: int | None = None,
        missing: int | None = None,
        eta: str = "",
        warning: str = "",
        signature: str = "",
        key_time: bool = True,
    ) -> bool:
        stamp = fmt_dt(node_time) if node_time else ""
        key = "|".join([
            baseline_no,
            f"v{version_no}" if version_no else "v-",
            node_type,
            run_id or "-",
            stamp if key_time else "-",
            signature or "-",
        ])
        if any(str(row.get("节点键")) == key for row in nodes):
            return False
        nodes.append({
            "id": _next_id(nodes),
            "节点键": key,
            "基线编号": baseline_no,
            "版本号": version_no,
            "节点类型": node_type,
            "节点时间": stamp,
            "标题": title,
            "详情": detail,
            "风险级别": risk,
            "运行编号": run_id,
            "作业名称": job,
            "归属依据": version_basis,
            "运行水位": water,
            "缺失分区数": missing,
            "预计完成时间": eta,
            "预警时间": warning,
            "交易号": tx_id,
            "是否可变": False,
        })
        return True

    # ------------------------------------------------------------------ 计算主流程
    def recompute(
        self,
        *,
        baseline_nos: list[str] | None = None,
        trigger: str = "手动重算",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        eval_at = (now or datetime.now()).replace(second=0, microsecond=0)
        with store.transaction() as tx_id:
            result = self._recompute_inner(
                tx_id, baseline_nos=baseline_nos, trigger=trigger, eval_at=eval_at
            )
            result["transaction"] = tx_id
            self._ready = True
            return result

    def _recompute_inner(
        self,
        tx_id: str,
        *,
        baseline_nos: list[str] | None,
        trigger: str,
        eval_at: datetime,
    ) -> dict[str, Any]:
        baselines = self._baseline_rows()
        if baseline_nos:
            wanted = set(baseline_nos)
            baselines = [row for row in baselines if str(row.get("基线编号")) in wanted]
        runs = self._run_rows()
        nodes = self._node_rows()
        states = self._state_rows()
        added = 0
        summaries: list[dict[str, Any]] = []

        for baseline in baselines:
            node_count, state = self._compute_baseline(tx_id, baseline, runs, nodes, eval_at)
            added += node_count
            self._upsert_state(states, state, tx_id)
            baseline.update({
                "当前版本": state.get("当前版本"),
                "健康状态": state.get("健康状态"),
                "破线风险": state.get("破线风险"),
                "运行水位": state.get("运行水位"),
                "缺失分区数": state.get("缺失分区数"),
                "预计完成时间": state.get("预计完成时间"),
                "预警时间": state.get("预警时间"),
                "基线结论": state.get("基线结论"),
                "最近时间线交易号": tx_id,
                "时间线更新时间": state.get("更新时间"),
                "abnormal": state.get("健康状态") in {"已破线", "预警中"},
                "pending": state.get("健康状态") in {"已破线", "预警中", "待观测"},
            })
            summaries.append(state)

        self._sync_runtime_projection(tx_id)
        rows = store.rows(INGEST_MODULE)
        rows.append({
            "id": _next_id(rows),
            "交易号": tx_id,
            "触发方式": trigger,
            "新增节点数": added,
            "涉及基线": "、".join(str(item.get("基线编号")) for item in summaries if item.get("基线编号")),
            "记录时间": fmt_dt(eval_at),
        })
        return {"ok": True, "added_nodes": added, "baselines": summaries, "trigger": trigger}

    def _compute_baseline(
        self,
        tx_id: str,
        baseline: dict[str, Any],
        runs: list[dict[str, Any]],
        nodes: list[dict[str, Any]],
        eval_at: datetime,
    ) -> tuple[int, dict[str, Any]]:
        baseline_no = str(baseline.get("基线编号", ""))
        versions = self._version_rows(baseline_no)
        applicable: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]] = []

        for run in runs:
            arrival = parse_dt(run.get("到达时间")) or parse_dt(run.get("开始时间"))
            if arrival and arrival > eval_at:
                continue
            claimed = parse_int(run.get("基线版本"))
            version, basis = self._resolve_version(baseline, arrival, claimed)
            if version is None:
                continue
            jobs = version.get("关联作业列表") or []
            declared = parse_jobs(run.get("适用基线"))
            if str(run.get("作业名称")) not in jobs:
                continue
            if declared and baseline_no not in declared:
                continue
            facts = self._run_facts(run, version)
            applicable.append((run, version, facts, basis))

        current = self._current_version(baseline, eval_at) or self._current_version(baseline, None)
        if current is None:
            fallback = {
                "版本号": 1,
                "生效时间": "",
                "承接版本": None,
                "基线编号": baseline_no,
                "基线名称": baseline.get("基线名称"),
                "承诺完成时间": baseline.get("承诺完成时间"),
                "预警余量分钟": baseline.get("预警余量分钟"),
                "破线级别": baseline.get("破线级别"),
                "关联作业列表": baseline.get("关联作业列表"),
                "工作日标记": baseline.get("工作日标记"),
                "节假日偏移": baseline.get("节假日偏移"),
            }
            current = self._effective_version(baseline, fallback)

        added = 0
        # 版本定义节点：让时间线能解释每条运行结果为什么按这个版本判定。
        definition_versions = {int(current.get("版本号", 1) or 1)}
        definition_versions.update(int(version.get("版本号", 1) or 1) for _, version, _, _ in applicable)
        for version in versions:
            number = int(version.get("版本号", 1) or 1)
            if number not in definition_versions:
                continue
            effective = self._effective_version(baseline, version)
            commit = effective.get("承诺完成时间") or ""
            warning = ""
            if parse_dt(commit):
                warning = fmt_dt(parse_dt(commit) - timedelta(minutes=int(effective.get("预警余量分钟") or 0)))
            added += self._append_node(
                nodes,
                tx_id=tx_id,
                baseline_no=baseline_no,
                version_no=number,
                node_type="基线版本",
                node_time=parse_dt(effective.get("生效时间")),
                title=f"基线版本 v{number} 生效",
                detail=(
                    f"承诺完成时间 {commit}，预警余量 {effective.get('预警余量分钟')} 分钟，"
                    f"预警时间 {warning or '—'}；关联作业 "
                    f"{'、'.join(effective.get('关联作业列表') or []) or '承接基线定义'}"
                ),
                risk="低",
                version_basis="版本定义",
                warning=warning,
                signature="definition",
            )

        for run, version, facts, basis in applicable:
            job = str(run.get("作业名称", ""))
            run_id = str(run.get("运行编号", ""))
            version_no = int(version.get("版本号", 1) or 1)
            commit_text = version.get("承诺完成时间") or ""
            warning_dt = facts["warning"]
            warning_text = fmt_dt(warning_dt)
            eta_text = fmt_dt(facts["eta"])
            risk = self._run_risk(facts, eval_at)
            missing_label = f"缺失 {facts['missing_count']} 个分区"
            if facts["missing_list"]:
                missing_label += f"（{'、'.join(facts['missing_list'][:6])}）"

            added += self._append_node(
                nodes,
                tx_id=tx_id,
                baseline_no=baseline_no,
                version_no=version_no,
                node_type="运行水位",
                node_time=facts["arrival"],
                title=f"{job} 运行结果到达",
                detail=(
                    f"数据日期 {run.get('数据日期') or '—'}，水位 {facts['water']}%"
                    f"（{run.get('已产出分区数')}/{run.get('期望分区数')} 个分区），{missing_label}；"
                    f"归属 {baseline_no} v{version_no}，预警时间 {warning_text or '—'}（{basis}）"
                ),
                risk=risk,
                run_id=run_id,
                job=job,
                version_basis=basis,
                water=facts["water"],
                missing=facts["missing_count"],
                eta=eta_text,
                warning=warning_text,
                signature=f"{facts['water']}-{facts['missing_count']}-{eta_text}-{run.get('执行状态')}",
            )

            if facts["missing_count"] > 0:
                added += self._append_node(
                    nodes,
                    tx_id=tx_id,
                    baseline_no=baseline_no,
                    version_no=version_no,
                    node_type="分区缺失",
                    node_time=facts["arrival"],
                    title=f"{job} 分区不完整",
                    detail=(
                        f"期望 {run.get('期望分区数')} 个分区，已产出 {run.get('已产出分区数')} 个，"
                        f"{missing_label}"
                    ),
                    risk="中" if risk == "低" else risk,
                    run_id=run_id,
                    job=job,
                    version_basis=basis,
                    water=facts["water"],
                    missing=facts["missing_count"],
                    eta=eta_text,
                    warning=warning_text,
                    signature=f"{facts['missing_count']}-{','.join(facts['missing_list'][:20])}",
                )

            if facts["complete"]:
                on_time = not facts["commit"] or not facts["completion"] or facts["completion"] <= facts["commit"]
                added += self._append_node(
                    nodes,
                    tx_id=tx_id,
                    baseline_no=baseline_no,
                    version_no=version_no,
                    node_type="分区完整",
                    node_time=facts["completion"] or facts["arrival"],
                    title=f"{job} 分区完整",
                    detail=(
                        f"已完成 {run.get('已产出分区数')}/{run.get('期望分区数')} 个分区，"
                        f"完成时间 {fmt_dt(facts['completion']) or '—'}"
                    ),
                    risk="低" if on_time else "高",
                    run_id=run_id,
                    job=job,
                    version_basis=basis,
                    water=facts["water"],
                    missing=0,
                    eta=fmt_dt(facts["completion"]),
                    warning=warning_text,
                    signature="complete",
                )

            if facts["eta"] and not facts["complete"]:
                added += self._append_node(
                    nodes,
                    tx_id=tx_id,
                    baseline_no=baseline_no,
                    version_no=version_no,
                    node_type="预计完成",
                    node_time=facts["arrival"],
                    title=f"{job} 预计完成时间",
                    detail=(
                        f"按当前水位 {facts['water']}% 预计 {eta_text} 完成，"
                        f"承诺完成时间 {commit_text}，预警时间 {warning_text or '—'}"
                    ),
                    risk=risk,
                    run_id=run_id,
                    job=job,
                    version_basis=basis,
                    water=facts["water"],
                    missing=facts["missing_count"],
                    eta=eta_text,
                    warning=warning_text,
                    signature=f"eta-{eta_text}",
                )

            # 预警触发只记录已经发生的时刻；未来预警只是预测，留在状态里。
            warning_hit_at: datetime | None = None
            if facts["complete"] and warning_dt and facts["completion"] and facts["completion"] > warning_dt:
                warning_hit_at = facts["completion"]
            elif not facts["complete"] and warning_dt and eval_at >= warning_dt:
                warning_hit_at = warning_dt
            if warning_hit_at:
                added += self._append_node(
                    nodes,
                    tx_id=tx_id,
                    baseline_no=baseline_no,
                    version_no=version_no,
                    node_type="预警触发",
                    node_time=warning_hit_at,
                    title=f"{job} 触发破线预警",
                    detail=(
                        f"预警时间 {warning_text}（承诺 {commit_text} 减余量 {facts['margin']} 分钟），"
                        f"归属 v{version_no}（{basis}）"
                    ),
                    risk="中" if risk != "高" else "高",
                    run_id=run_id,
                    job=job,
                    version_basis=basis,
                    water=facts["water"],
                    missing=facts["missing_count"],
                    eta=eta_text,
                    warning=warning_text,
                    signature="warning",
                )

            # 破线判定只记录已经成立的事实，预测性突破放在基线状态的风险里。
            breach_at: datetime | None = None
            breach_title = ""
            if facts["complete"] and facts["commit"] and facts["completion"]:
                if facts["completion"] > facts["commit"]:
                    breach_at = facts["completion"]
                    breach_title = f"{job} 晚于承诺完成时间完成"
            elif facts["commit"] and eval_at > facts["commit"]:
                breach_at = facts["commit"]
                breach_title = f"{job} 已超过承诺完成时间仍未完成"
            if breach_at:
                added += self._append_node(
                    nodes,
                    tx_id=tx_id,
                    baseline_no=baseline_no,
                    version_no=version_no,
                    node_type="破线判定",
                    node_time=breach_at,
                    title=breach_title,
                    detail=(
                        f"承诺完成时间 {commit_text}，实际完成 {fmt_dt(facts['completion']) or '—'}，"
                        f"预计完成 {eta_text or '—'}，水位 {facts['water']}%，{missing_label}"
                    ),
                    risk="高",
                    run_id=run_id,
                    job=job,
                    version_basis=basis,
                    water=facts["water"],
                    missing=facts["missing_count"],
                    eta=eta_text,
                    warning=warning_text,
                    signature="breach",
                )

        state = self._build_state(baseline, current, applicable, eval_at, tx_id)
        # 结论节点用稳定的结论签名做键：重复计算不会重复写入，结论变化才追加新节点。
        added += self._append_node(
            nodes,
            tx_id=tx_id,
            baseline_no=baseline_no,
            version_no=state.get("当前版本"),
            node_type="结论确认",
            node_time=eval_at,
            title=f"基线结论：{state.get('健康状态')}",
            detail=state.get("基线结论") or "",
            risk=state.get("破线风险") or "待观测",
            run_id=state.get("最近运行编号") or "",
            version_basis="提交后确认",
            water=state.get("运行水位"),
            missing=state.get("缺失分区数"),
            eta=state.get("预计完成时间") or "",
            warning=state.get("预警时间") or "",
            signature=state.get("结论签名") or "",
            key_time=False,
        )
        return added, state

    def _build_state(
        self,
        baseline: dict[str, Any],
        current: dict[str, Any],
        applicable: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]],
        eval_at: datetime,
        tx_id: str,
    ) -> dict[str, Any]:
        baseline_no = str(baseline.get("基线编号", ""))
        baseline_name = str(baseline.get("基线名称", ""))
        latest_by_job: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]] = {}
        for item in applicable:
            run = item[0]
            job = str(run.get("作业名称", ""))
            existing = latest_by_job.get(job)
            if existing is None or self._run_order(run) > self._run_order(existing[0]):
                latest_by_job[job] = item

        jobs = [str(job) for job in (current.get("关联作业列表") or [])]
        attributions: list[dict[str, Any]] = []
        waters: list[int] = []
        missing_total = 0
        missing_samples: list[str] = []
        etas: list[datetime] = []
        completions: list[datetime] = []
        warnings: list[datetime] = []
        all_complete = True
        missing_jobs = [job for job in jobs if job not in latest_by_job]

        for job, (run, version, facts, basis) in latest_by_job.items():
            risk = self._run_risk(facts, eval_at)
            waters.append(facts["water"])
            missing_total += facts["missing_count"]
            missing_samples.extend(facts["missing_list"][:10])
            if facts["eta"]:
                etas.append(facts["eta"])
            if facts["warning"]:
                warnings.append(facts["warning"])
            if facts["complete"]:
                if facts["completion"]:
                    completions.append(facts["completion"])
            else:
                all_complete = False
            attributions.append({
                "运行编号": run.get("运行编号"),
                "作业名称": job,
                "数据日期": run.get("数据日期"),
                "版本号": version.get("版本号"),
                "选取依据": basis,
                "承诺完成时间": version.get("承诺完成时间"),
                "预警时间": fmt_dt(facts["warning"]),
                "运行水位": facts["water"],
                "缺失分区": facts["missing_count"],
                "缺失分区明细": facts["missing_list"],
                "预计完成时间": fmt_dt(facts["eta"]),
                "实际完成时间": fmt_dt(facts["completion"]),
                "执行状态": run.get("执行状态"),
                "风险级别": risk,
                "是否完成": facts["complete"],
            })
        attributions.sort(key=lambda item: RISK_ORDER.get(str(item.get("风险级别")), 0), reverse=True)

        current_commit = parse_dt(current.get("承诺完成时间"))
        current_margin = int(current.get("预警余量分钟") or 0)
        current_warning = current_commit - timedelta(minutes=current_margin) if current_commit else None

        # 基线口径按“运行结果实际归属的版本”汇总，避免用新版本承诺去判旧版本归属的运行结果。
        # 尚无运行结果的关联作业才回落到当前版本承诺。
        attributed_commits = [
            parse_dt(item.get("承诺完成时间")) for item in attributions if item.get("承诺完成时间")
        ]
        attributed_commits = [item for item in attributed_commits if item is not None]
        if missing_jobs and current_commit:
            attributed_commits.append(current_commit)
        # 取归集到的最严承诺：任一关联作业在其归属版本下被要求更早完成，基线就按更早的口径判。
        baseline_commit = min(attributed_commits) if attributed_commits else current_commit
        effective_warning = min(warnings) if warnings else current_warning
        water = min(waters) if waters else 0
        eta = max(etas) if etas else None
        actual_end = max(completions) if completions else None

        health = "待观测"
        risk = "待观测"
        conclusion = f"尚无运行结果，承诺完成时间 {current.get('承诺完成时间') or '—'}"
        if latest_by_job:
            if all_complete and not missing_jobs:
                if baseline_commit and actual_end and actual_end > baseline_commit:
                    health, risk = "已破线", "高"
                    conclusion = (
                        f"全部关联作业完成于 {fmt_dt(actual_end)}，"
                        f"晚于承诺完成时间 {fmt_dt(baseline_commit)}"
                    )
                else:
                    health, risk = "正常", "低"
                    conclusion = (
                        f"全部关联作业已完成，最低水位 {water}%，"
                        f"完成于 {fmt_dt(actual_end) or '—'}"
                    )
            else:
                late_actual = bool(baseline_commit and actual_end and actual_end > baseline_commit)
                late_projected = bool(baseline_commit and eta and eta > baseline_commit)
                overdue = bool(baseline_commit and eval_at > baseline_commit)
                if overdue and not (eta and eta <= current_commit):
                    health, risk = "已破线", "高"
                elif late_actual:
                    health, risk = "已破线", "高"
                elif late_projected:
                    health, risk = "预警中", "高"
                elif (
                    (current_warning and eval_at >= current_warning)
                    or (effective_warning and eta and eta > effective_warning)
                    or missing_total > 0
                    or missing_jobs
                ):
                    health, risk = "预警中", "中"
                else:
                    health, risk = "正常", "低"

                if health != "正常":
                    if health == "已破线" and overdue and not eta:
                        conclusion = (
                            f"已超过承诺完成时间 {fmt_dt(baseline_commit)} 仍未完成，"
                            f"最低水位 {water}%，缺失 {missing_total} 个分区"
                        )
                    elif late_actual:
                        conclusion = (
                            f"实际完成 {fmt_dt(actual_end)}，晚于承诺完成时间 "
                            f"{fmt_dt(baseline_commit)}，缺失 {missing_total} 个分区"
                        )
                    else:
                        conclusion = (
                            f"预计完成 {fmt_dt(eta) or '—'}，承诺完成时间 {fmt_dt(baseline_commit)}，"
                            f"预警时间 {fmt_dt(effective_warning) or '—'}，"
                            f"最低水位 {water}%，缺失 {missing_total} 个分区"
                        )
                    if missing_jobs:
                        conclusion += f"；{'、'.join(missing_jobs)} 尚无运行结果"
                else:
                    conclusion = (
                        f"最低水位 {water}%，预计完成 {fmt_dt(eta) or '—'}，"
                        f"早于预警时间 {fmt_dt(effective_warning) or '—'}"
                    )

        attribution_versions = sorted({int(item["版本号"] or 1) for item in attributions})
        latest_run_id = str(attributions[0].get("运行编号") or "") if attributions else ""
        signature = "|".join([
            health,
            risk,
            ",".join(f"v{number}" for number in attribution_versions),
            str(water),
            str(missing_total),
            fmt_dt(eta),
            fmt_dt(actual_end),
            ",".join(missing_jobs),
        ])
        return {
            "id": _next_id(self._state_rows()),
            "基线编号": baseline_no,
            "基线名称": baseline_name,
            "当前版本": int(current.get("版本号", 1) or 1),
            "归属版本": "、".join(f"v{number}" for number in attribution_versions),
            "承诺完成时间": fmt_dt(baseline_commit),
            "当前版本承诺完成时间": current.get("承诺完成时间") or "",
            "当前版本预警时间": fmt_dt(current_warning),
            "预警时间": fmt_dt(effective_warning),
            "破线级别": current.get("破线级别") or "",
            "健康状态": health,
            "破线风险": risk,
            "运行水位": water,
            "缺失分区": "、".join(missing_samples[:10]),
            "缺失分区数": missing_total,
            "预计完成时间": fmt_dt(eta),
            "实际完成时间": fmt_dt(actual_end),
            "观测时间": fmt_dt(eval_at),
            "关联作业列表": jobs,
            "缺失作业": missing_jobs,
            "归属明细": attributions,
            "最近运行编号": latest_run_id,
            "基线结论": conclusion,
            "结论签名": signature,
            "交易号": tx_id,
            "更新时间": fmt_dt(eval_at),
        }

    @staticmethod
    def _upsert_state(states: list[dict[str, Any]], state: dict[str, Any], tx_id: str) -> None:
        for row in states:
            if row.get("基线编号") == state.get("基线编号"):
                keep_id = row.get("id")
                row.clear()
                row.update(state)
                row["id"] = keep_id
                row["交易号"] = tx_id
                return
        state["id"] = _next_id(states)
        states.append(state)

    def _sync_runtime_projection(self, tx_id: str) -> None:
        """把已确认的时间线结论同步到运行实例列表，供协作业务面直接读取。"""
        by_run: dict[str, list[dict[str, Any]]] = {}
        for state in self._state_rows():
            for item in state.get("归属明细") or []:
                by_run.setdefault(str(item.get("运行编号")), []).append({
                    **item, "基线编号": state.get("基线编号"), "健康状态": state.get("健康状态"),
                })
        for runtime in store.rows(RUNTIME_MODULE):
            run_id = str(runtime.get("记录编号", ""))
            items = by_run.get(run_id) or []
            if not items:
                runtime.update({
                    "基线结论": "未纳入基线时间线",
                    "命中基线": "",
                    "基线风险": "",
                    "运行水位": None,
                    "缺失分区": "",
                    "预计完成时间": "",
                    "基线预警时间": "",
                    "时间线交易号": "",
                })
                continue
            items.sort(key=lambda item: RISK_ORDER.get(str(item.get("风险级别")), 0), reverse=True)
            worst = items[0]
            baselines = sorted({str(item.get("基线编号")) for item in items})
            runtime.update({
                "基线结论": (
                    f"{worst.get('基线编号')} {worst.get('风险级别')}风险："
                    f"{worst.get('作业名称')} 归属 v{worst.get('版本号')}，"
                    f"水位 {worst.get('运行水位')}%，预计完成 {worst.get('预计完成时间') or '—'}"
                ),
                "命中基线": "、".join(baselines),
                "基线风险": worst.get("风险级别"),
                "运行水位": worst.get("运行水位"),
                "缺失分区": worst.get("缺失分区"),
                "预计完成时间": worst.get("预计完成时间"),
                "基线预警时间": worst.get("预警时间"),
                "时间线交易号": tx_id,
            })

    # ------------------------------------------------------------------ 对外写入口
    def submit_run_result(self, values: dict[str, Any]) -> dict[str, Any]:
        try:
            run = self._normalize_run(values)
        except ValueError as error:
            return {"ok": False, "message": f"缺少必填字段：{error}"}
        fingerprint = self._fingerprint(run)

        with store.transaction() as tx_id:
            receipts = store.rows(INGEST_MODULE)
            for receipt in receipts:
                if receipt.get("运行指纹") == fingerprint:
                    return {
                        "ok": True,
                        "duplicate": True,
                        "message": "相同运行结果已生效，本次提交不再写入时间线",
                        "transaction": receipt.get("交易号"),
                        "receipt": receipt,
                    }
            runs = self._run_rows()
            # 运行编号是同一批分区数据的自然键：再次提交视为更新该批数据，
            # 历史节点保留，新数据按到达时间追加新节点。
            existing = next(
                (row for row in runs if str(row.get("运行编号")) == run["运行编号"]),
                None,
            )
            if existing is not None:
                previous = dict(existing)
                existing.clear()
                existing.update(run)
                existing["id"] = previous.get("id", _next_id(runs))
                run = existing
            else:
                run["id"] = _next_id(runs)
                runs.append(run)
            # 观测时刻取真实时钟与已落库事实时间的较大者：调度器回传的到达时间若略超前，
            # 仍按它自己的时间线推进，而不是等到本机时钟追平才生效。
            facts_at = [
                moment for run_row in runs
                for moment in (
                    parse_dt(run_row.get("到达时间")),
                    parse_dt(run_row.get("完成时间")),
                )
                if moment is not None
            ]
            eval_at = max([datetime.now().replace(second=0, microsecond=0), *facts_at])
            result = self._recompute_inner(
                tx_id,
                baseline_nos=None,
                trigger=f"运行结果到达 {run['运行编号']}",
                eval_at=eval_at,
            )
            affected = [
                item for item in result["baselines"]
                if any(
                    str(entry.get("运行编号")) == run["运行编号"]
                    for entry in item.get("归属明细") or []
                )
            ]
            receipt = {
                "id": _next_id(receipts),
                "运行指纹": fingerprint,
                "运行编号": run["运行编号"],
                "作业名称": run["作业名称"],
                "数据日期": run["数据日期"],
                "交易号": tx_id,
                "新增节点数": result["added_nodes"],
                "涉及基线": "、".join(
                    str(item.get("基线编号")) for item in affected if item.get("基线编号")
                ),
                "记录时间": fmt_dt(eval_at),
            }
            receipts.append(receipt)
            return {
                "ok": True,
                "duplicate": False,
                "message": "运行结果已生效，相关基线时间线已同事务更新",
                "transaction": tx_id,
                "added_nodes": result["added_nodes"],
                "baselines": affected,
                "receipt": receipt,
                "entry": run,
            }

    def ensure_computed(self) -> None:
        if self._ready:
            return
        self.recompute(trigger="服务初始化")

    def invalidate(self) -> None:
        """基线定义或版本发生变化后标脏，下一次读取时按最新定义重算时间线。"""
        self._ready = False

    # ------------------------------------------------------------------ 对外读接口
    def overview(self) -> dict[str, Any]:
        self.ensure_computed()
        states = self._state_rows()
        baselines = self._baseline_rows()
        cards = [
            {"label": "基线总数", "value": len(baselines)},
            {"label": "已破线", "value": sum(1 for row in states if row.get("健康状态") == "已破线")},
            {"label": "预警中", "value": sum(1 for row in states if row.get("健康状态") == "预警中")},
            {"label": "正常", "value": sum(1 for row in states if row.get("健康状态") == "正常")},
            {"label": "待观测", "value": sum(1 for row in states if row.get("健康状态") == "待观测")},
            {"label": "缺失分区", "value": sum(int(row.get("缺失分区数") or 0) for row in states)},
        ]
        ordered = sorted(states, key=lambda row: parse_dt(row.get("承诺完成时间")) or datetime.max)
        return {
            "cards": cards,
            "baselines": [self._summary(row) for row in ordered],
            "generated_at": fmt_dt(datetime.now()),
        }

    @staticmethod
    def _summary(state: dict[str, Any]) -> dict[str, Any]:
        fields = [
            "基线编号", "基线名称", "当前版本", "归属版本", "承诺完成时间", "当前版本承诺完成时间", "预警时间",
            "当前版本预警时间", "破线级别", "健康状态", "破线风险", "运行水位",
            "缺失分区", "缺失分区数", "预计完成时间", "实际完成时间", "观测时间",
            "基线结论", "关联作业列表", "缺失作业", "归属明细", "最近运行编号",
            "交易号", "更新时间",
        ]
        return {field: state.get(field) for field in fields}

    def list_baselines(self, *, risk: str | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
        self.ensure_computed()
        rows = [self._summary(row) for row in self._state_rows()]
        if risk:
            rows = [row for row in rows if row.get("破线风险") == risk or row.get("健康状态") == risk]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("基线编号", "")) or keyword in str(row.get("基线名称", ""))
            ]
        return sorted(rows, key=lambda row: parse_dt(row.get("承诺完成时间")) or datetime.max)

    def get_baseline(self, baseline_no: str) -> dict[str, Any] | None:
        self.ensure_computed()
        baseline = next(
            (row for row in self._baseline_rows() if str(row.get("基线编号")) == baseline_no),
            None,
        )
        if baseline is None:
            return None
        state = next((row for row in self._state_rows() if row.get("基线编号") == baseline_no), None)
        nodes = [row for row in self._node_rows() if row.get("基线编号") == baseline_no]
        nodes.sort(key=lambda row: (
            parse_dt(row.get("节点时间")) or datetime.max,
            NODE_PRIORITY.get(str(row.get("节点类型")), 99),
            int(row.get("id", 0) or 0),
        ))
        versions = [self._effective_version(baseline, row) for row in self._version_rows(baseline_no)]
        return {
            "baseline": baseline,
            "state": self._summary(state) if state else None,
            "versions": versions,
            "nodes": nodes,
            "attributions": (state or {}).get("归属明细") or [],
        }

    def risk_board(self) -> dict[str, Any]:
        self.ensure_computed()
        summaries = self.list_baselines()
        columns = [
            {"key": "已破线", "label": "已破线", "items": [
                row for row in summaries if row.get("健康状态") == "已破线"
            ]},
            {"key": "高", "label": "高风险", "items": [
                row for row in summaries
                if row.get("破线风险") == "高" and row.get("健康状态") != "已破线"
            ]},
            {"key": "中", "label": "中风险", "items": [
                row for row in summaries if row.get("破线风险") == "中"
            ]},
            {"key": "低", "label": "低风险", "items": [
                row for row in summaries if row.get("破线风险") == "低"
            ]},
            {"key": "待观测", "label": "待观测", "items": [
                row for row in summaries if row.get("破线风险") == "待观测"
            ]},
        ]
        risk_nodes = [
            row for row in self._node_rows()
            if row.get("风险级别") in {"高", "中"} and row.get("节点类型") != "结论确认"
        ]
        risk_nodes.sort(key=lambda row: (
            parse_dt(row.get("节点时间")) or datetime.max,
            NODE_PRIORITY.get(str(row.get("节点类型")), 99),
        ), reverse=True)
        return {
            "columns": columns,
            "risk_nodes": risk_nodes[:30],
            "generated_at": fmt_dt(datetime.now()),
        }

    def execution_view(self, job: str | None = None) -> dict[str, Any]:
        """作业执行组件读取口径：只返回执行时需要的水位、缺失分区和预计完成时间。"""
        self.ensure_computed()
        rows = []
        for state in self.list_baselines():
            for item in state.get("归属明细") or []:
                if job and item.get("作业名称") != job:
                    continue
                rows.append({
                    "基线编号": state.get("基线编号"),
                    "基线名称": state.get("基线名称"),
                    "版本号": item.get("版本号"),
                    "关联作业": item.get("作业名称"),
                    "运行编号": item.get("运行编号"),
                    "数据日期": item.get("数据日期"),
                    "承诺完成时间": item.get("承诺完成时间"),
                    "预警时间": item.get("预警时间"),
                    "健康状态": state.get("健康状态"),
                    "破线风险": item.get("风险级别"),
                    "运行水位": item.get("运行水位"),
                    "缺失分区": item.get("缺失分区"),
                    "缺失分区明细": item.get("缺失分区明细"),
                    "预计完成时间": item.get("预计完成时间"),
                    "实际完成时间": item.get("实际完成时间"),
                    "是否完成": item.get("是否完成"),
                    "观测时间": state.get("观测时间"),
                    "交易号": state.get("交易号"),
                })
        rows.sort(key=lambda row: parse_dt(row.get("承诺完成时间")) or datetime.max)
        return {"job": job or "", "items": rows, "generated_at": fmt_dt(datetime.now())}

    def runtime_timeline(self, runtime: dict[str, Any]) -> dict[str, Any]:
        """运行实例回查：返回该次运行在基线时间线里的水位、缺失分区与预计完成时间。"""
        self.ensure_computed()
        run_id = str(runtime.get("记录编号", ""))
        run = next((row for row in self._run_rows() if str(row.get("运行编号")) == run_id), None)
        nodes: list[dict[str, Any]] = []
        attributions: list[dict[str, Any]] = []
        for state in self._state_rows():
            for item in state.get("归属明细") or []:
                if str(item.get("运行编号")) != run_id:
                    continue
                attributions.append({
                    **item,
                    "基线编号": state.get("基线编号"),
                    "健康状态": state.get("健康状态"),
                })
                nodes.extend(
                    row for row in self._node_rows()
                    if row.get("基线编号") == state.get("基线编号")
                    and str(row.get("运行编号")) == run_id
                )
        nodes.sort(key=lambda row: (
            parse_dt(row.get("节点时间")) or datetime.max,
            NODE_PRIORITY.get(str(row.get("节点类型")), 99),
        ))
        if not attributions and run is None:
            return {
                "runtime": runtime,
                "attributions": [],
                "nodes": [],
                "water": None,
                "missing": 0,
                "missing_partitions": "",
                "eta": "",
                "message": "该运行实例尚未提交分区运行结果，未进入基线时间线",
            }
        primary = attributions[0] if attributions else {}
        missing_list, missing_count = self._missing_partitions(run or {})
        return {
            "runtime": runtime,
            "attributions": attributions,
            "nodes": nodes,
            "water": primary.get("运行水位") if attributions else self._water_level(run or {}),
            "missing": primary.get("缺失分区") if attributions else missing_count,
            "missing_partitions": (
                "、".join(primary.get("缺失分区明细") or []) if attributions
                else "、".join(missing_list)
            ),
            "eta": primary.get("预计完成时间") if attributions else fmt_dt((run or {}).get("预计完成时间")),
            "message": "已回查基线时间线" if attributions else "运行结果尚未归属任何基线",
        }


timeline_service = BaselineTimelineService()
