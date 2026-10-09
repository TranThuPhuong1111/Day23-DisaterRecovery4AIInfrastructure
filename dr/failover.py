"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec), flush=True)
    return rec


def state_of(region: str) -> dict:
    """GET /v1/state của region: weights? vector count? pool_state?"""
    return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()


def wait_ready(region: str, wait: float, every: float = 0.5) -> tuple[bool, float, str]:
    """Poll /readyz tới khi 200 hoặc hết `wait` giây. Trả (ready, waited_s, last_reason)."""
    t0, last = time.time(), "never_polled"
    while True:
        try:
            r = httpx.get(f"{URL[region]}/readyz", timeout=2.0)
            if r.status_code == 200:
                return True, round(time.time() - t0, 2), "ready"
            last = ",".join(r.json().get("reasons") or []) or f"http_{r.status_code}"
        except Exception as e:
            last = type(e).__name__
        if time.time() - t0 >= wait:
            return False, round(time.time() - t0, 2), last
        time.sleep(every)


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước, đúng thứ tự. Bước 4 timeout -> ABORT, KHÔNG cutover."""
    t_start = time.time()
    primary = "a" if target == "b" else "b"
    tdir = pathlib.Path(f"state/region-{target}")
    out = {"ok": False, "target": target, "primary": primary, "backend": backend}

    # 1 — region phụ đang ở trạng thái nào? (không có process thì restore cũng vô ích)
    try:
        before = state_of(target)
    except Exception as e:
        emit(step="1_verify_target", target=target, reachable=False, error=type(e).__name__)
        emit(step="abort", at="1_verify_target", reason="target_unreachable",
             cutover=False, elapsed_s=round(time.time() - t_start, 2))
        return {**out, "aborted_at": "1_verify_target", "reason": "target_unreachable"}
    emit(step="1_verify_target", target=target, reachable=True,
         pool_state=before.get("pool_state"), weights=before.get("weights"),
         vector_count=before.get("count"))
    out["before"] = before

    # 2 — restore state (vector DB + weights + VERSION) từ object store, đo RPO thật
    try:
        meta = snapshot.get(target, backend)
    except (Exception, SystemExit) as e:
        emit(step="2_restore_snapshot", ok=False, error=str(e)[:300])
        emit(step="abort", at="2_restore_snapshot", reason="no_snapshot",
             cutover=False, elapsed_s=round(time.time() - t_start, 2))
        return {**out, "aborted_at": "2_restore_snapshot", "reason": str(e)[:300]}
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     tdir / "vectors.sqlite")
    emit(step="2_restore_snapshot", ok=True, backend=backend,
         snapshot_at=meta.get("snapshot_at"),
         snapshot_age_s=None if meta.get("snapshot_at") is None
         else round(time.time() - meta["snapshot_at"], 2),
         embed_model_version=meta.get("embed_model_version"),
         rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
         primary_latest_doc_ts=r["primary_latest_doc_ts"],
         restored_latest_doc_ts=r["restored_latest_doc_ts"])
    out.update(rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
               embed_model_version=meta.get("embed_model_version"))

    # 3 — warm -> full. serving/app.py bắt đầu đếm WARMUP_SECONDS từ lúc này.
    pool_file = tdir / "pool_state"
    prev_pool = pool_file.read_text().strip() if pool_file.exists() else "cold"
    tdir.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full")
    emit(step="3_scale_pool", target=target, frm=prev_pool, to="full")

    # 4 — CHỜ region phụ thật sự ready. Đây là GPU pool warm-up nằm trong RTO.
    ready, waited, reason = wait_ready(target, wait)
    emit(step="4_wait_ready", target=target, ready=ready, waited_s=waited, last_reason=reason)
    if not ready:
        # Trả pool về như cũ (không đốt GPU cho 1 region không serve được) và KHÔNG
        # đụng vào edge/active_region: user vẫn ở region cũ, không bị 503 từ cả hai phía.
        pool_file.write_text(prev_pool)
        emit(step="abort", at="4_wait_ready", reason=f"target_not_ready_after_{wait}s:{reason}",
             cutover=False, pool_reverted_to=prev_pool,
             elapsed_s=round(time.time() - t_start, 2))
        return {**out, "aborted_at": "4_wait_ready", "reason": reason, "waited_s": waited}

    # 5 — chỉ bây giờ mới đổi "DNS"
    old = ACTIVE.read_text().strip() if ACTIVE.exists() else "a"
    ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE.write_text(target)
    emit(step="5_dns_cutover", frm=old, to=target, elapsed_s=round(time.time() - t_start, 2))

    try:
        after = state_of(target)
    except Exception:
        after = {}
    return {**out, "ok": True, "waited_s": waited, "cutover_from": old, "after": after,
            "elapsed_s": round(time.time() - t_start, 2)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
