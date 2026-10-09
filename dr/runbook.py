"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH = pathlib.Path("reports/health-events.jsonl")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N (mặc định N)."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # không có TTY -> coi như không confirm
        return False


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def last_outage(region: str) -> dict | None:
    """Sự kiện kill gần nhất của region trong chaos log (mốc t_outage)."""
    kills = [e for e in _jsonl(CHAOS) if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1] if kills else None


def checker_running() -> bool:
    """Health checker có đang chạy không (start gần nhất chưa hết duration, chưa có stop)."""
    ev = [e for e in _jsonl(HEALTH) if e.get("event") in ("start", "stop")]
    return bool(ev) and ev[-1]["event"] == "start" \
        and ev[-1]["ts"] + ev[-1].get("duration_s", 0) > time.time()


def wait_alert(region: str, since: float, wait: float) -> dict | None:
    """Chờ health checker phát alert UNHEALTHY cho region (sau mốc `since`).

    Không có health checker đang chạy -> chỉ đọc log 1 lần, không ngồi chờ vô ích."""
    end = time.time() + (wait if checker_running() else 0)
    while True:
        hit = next((e for e in _jsonl(HEALTH) if e.get("event") == "state_change"
                    and e.get("to") == "UNHEALTHY" and e.get("region") == region
                    and e["ts"] >= since), None)
        if hit or time.time() >= end:
            return hit
        time.sleep(0.5)


def golden_signals(region: str, n: int = 10) -> dict:
    lat, errors = [], 0
    for i in range(n):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[region]}/v1/infer", params={"q": f"hoa don thang {i % 12 + 1}"},
                          timeout=3.0)
            ok = r.status_code == 200 and r.json().get("region") == region
        except Exception:
            ok = False
        lat.append((time.time() - t0) * 1000)
        errors += not ok
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(round(0.95 * len(lat))) - 1)]
    return {"requests": n, "errors": errors, "error_rate": round(errors / n, 3),
            "p50_ms": round(lat[len(lat) // 2], 1), "p95_ms": round(p95, 1)}


def run(primary: str, target: str, backend: str, auto: bool,
        probes: int = 3, alert_wait: float = 60.0, ready_wait: float = 60.0) -> dict:
    """7 bước runbook §4 "Region Chính Down"."""
    t_start = time.time()
    out = {"ok": False, "primary": primary, "target": target}

    # 1 — xác nhận outage: N probe liên tiếp + alert từ health checker (không tin 1 lần fail)
    results = []
    for i in range(probes):
        ok, reason = hc.probe(primary, 2.0)
        results.append({"ok": ok, "reason": reason})
        if i < probes - 1:
            time.sleep(1.0)
    t_ok, t_reason = hc.probe(target, 2.0)
    kill = last_outage(primary)
    alert = wait_alert(primary, kill["ts"] if kill else t_start - 300, alert_wait)
    confirmed = all(not r["ok"] for r in results)
    step(1, "xac_nhan_outage", primary=primary, probes=results, confirmed=confirmed,
         target_probe={"ok": t_ok, "reason": t_reason},
         health_alert_ts=alert["ts"] if alert else None,
         health_alert_line=None if not alert else
         f"{HEALTH}:{_jsonl(HEALTH).index(alert) + 1}")
    if not confirmed:
        step(7, "post_incident", result="no_outage_confirmed",
             elapsed_s=round(time.time() - t_start, 2))
        return {**out, "reason": "primary con tra loi /readyz -> khong failover"}

    # 2 — mở incident, bấm giờ. ts dòng này = "operator biết tin", luôn SAU t_outage.
    t_outage = kill["ts"] if kill else None
    rec2 = step(2, "thong_bao_incident", severity="SEV1",
                summary=f"region-{primary} khong ready, failover sang region-{target}",
                t_outage=t_outage, t_outage_iso=kill.get("iso") if kill else None,
                t_detect=alert["ts"] if alert else None,
                notify_delay_s=None if t_outage is None else round(time.time() - t_outage, 2))
    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        step(3, "scale_gpu_pool", skipped=True, reason="operator_khong_confirm")
        step(7, "post_incident", result="aborted_by_operator",
             elapsed_s=round(time.time() - t_start, 2))
        return {**out, "reason": "operator khong confirm"}
    t_confirm = time.time()

    # 3 — gọi failover MỘT LẦN DUY NHẤT (nó tự làm verify/restore/scale/wait/cutover)
    res = fo.failover(target, backend, ready_wait)
    step(3, "scale_gpu_pool", operator_confirm_ts=t_confirm, auto=auto,
         failover_ok=res.get("ok"), aborted_at=res.get("aborted_at"),
         waited_s=res.get("waited_s"), failover_elapsed_s=res.get("elapsed_s"))

    # 4 — chỉ ĐỌC kết quả state replica từ dict của bước 3
    after = res.get("after") or {}
    step(4, "verify_state_replica", vector_count=after.get("count"),
         weights=after.get("weights"), pool_state=after.get("pool_state"),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         embed_model_version=res.get("embed_model_version"))

    # 5 — chỉ ĐỌC lại kết quả cutover
    active = fo.ACTIVE.read_text().strip() if fo.ACTIVE.exists() else None
    step(5, "dns_cutover", ok=bool(res.get("ok")), cutover_from=res.get("cutover_from"),
         active_region_file=active)
    if not res.get("ok"):
        step(7, "post_incident", result="failover_aborted", reason=res.get("reason"),
             elapsed_s=round(time.time() - t_start, 2))
        return {**out, "failover": res}

    # 6 — golden signals: 10 request thật vào region phụ
    g = golden_signals(target)
    healthy = g["error_rate"] == 0 and g["p95_ms"] < 500
    step(6, "verify_golden_signals", region=target, healthy=healthy, **g)

    # 7 — tổng kết, lệnh đo RTO từ log
    step(7, "post_incident", result="resolved" if healthy else "degraded",
         elapsed_s=round(time.time() - t_start, 2),
         since_outage_s=None if t_outage is None else round(time.time() - t_outage, 2),
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300",
         rollback_cmd=f"python3 chaos/kill_region.py restore --region {primary} --backend bare")
    return {**out, "ok": healthy, "failover": res, "golden_signals": g,
            "elapsed_s": round(time.time() - t_start, 2)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
