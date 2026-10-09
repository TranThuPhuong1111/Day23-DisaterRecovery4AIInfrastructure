"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException:
        return False, f"timeout>{timeout}s"
    except Exception as e:  # ConnectError khi process chết (mode stop)
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = r.json().get("reasons") or []
    except ValueError:
        reasons = []
    return False, f"http_{r.status_code}:" + ",".join(reasons)


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL (chỉ khi đổi trạng thái)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    # None = chưa biết. Lần đầu probe chỉ "khởi tạo" trạng thái, không tính là transition
    # -> region phụ đang warm (503) lúc start không bị log là outage.
    state = {r: None for r in URL}
    fails = {r: 0 for r in URL}
    oks = {r: 0 for r in URL}
    end = time.time() + duration
    with out.open("a") as f:
        def emit(**kw):
            rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                   "interval_s": interval, "threshold": threshold, **kw}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print("HEALTH", json.dumps(rec))

        emit(event="start", timeout_s=timeout, duration_s=duration,
             detect_floor_s=round(interval * threshold, 2))
        while time.time() < end:
            t = time.time()
            for region in URL:
                ok, reason = probe(region, timeout)
                if ok:
                    oks[region] += 1
                    fails[region] = 0
                else:
                    fails[region] += 1
                    oks[region] = 0
                # Chống flapping: chỉ đổi trạng thái sau `threshold` lần LIÊN TIẾP.
                if not ok and fails[region] >= threshold and state[region] != "UNHEALTHY":
                    prev, state[region] = state[region], "UNHEALTHY"
                    emit(event="state_change", region=region, frm=prev, to="UNHEALTHY",
                         reason=reason, consecutive_fails=fails[region])
                elif ok and oks[region] >= threshold and state[region] != "HEALTHY":
                    prev, state[region] = state[region], "HEALTHY"
                    emit(event="state_change", region=region, frm=prev, to="HEALTHY",
                         reason=reason, consecutive_oks=oks[region])
            time.sleep(max(0.0, interval - (time.time() - t)))
        emit(event="stop", final_state=state)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
