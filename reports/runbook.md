# Runbook 1 trang — Region chính (A) down → failover sang Region B

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong. Mọi lệnh chạy từ thư mục gốc repo.

**Trigger:** health checker ghi `"to":"UNHEALTHY","region":"a"` vào `reports/health-events.jsonl`
(= 3 lần `/readyz` fail liên tiếp, interval 5s → sớm nhất 15s sau outage), HOẶC user báo lỗi 503 qua edge.

**Đường tắt (bán tự động, khuyến nghị):** sau khi đọc xong bước 1, chạy
`python3 dr/runbook.py --primary a --target b --backend fs` → script tự làm bước 1–7,
hỏi `y/N` trước khi cutover. Log: `reports/runbook-run.jsonl` + `reports/failover-events.jsonl`.
Chỉ dùng `--auto` cho drill/CI. Bảng dưới là các bước làm tay tương đương khi script hỏng.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage (không tin 1 lần fail) | `for i in 1 2 3; do python3 chaos/kill_region.py status; sleep 5; done; grep UNHEALTHY reports/health-events.jsonl` | `"a": {"ready": false}` cả 3 lần **và** có dòng `"to": "UNHEALTHY", "region": "a"`; đồng thời `"b": {"alive": true}` (nếu B cũng chết → DỪNG, escalate, không failover) | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` — script log bước 1–2 rồi DỪNG ở prompt `y/N`. Song song: mở kênh `#inc-region-a` (SEV1), dán `tail -1 chaos/chaos-events.jsonl` (drill) / ts alert đầu tiên (thật) | Có dòng `"step": 2, "name": "thong_bao_incident"` trong `reports/runbook-run.jsonl` với `t_outage` và `notify_delay_s`; IC đã được page | On-call SRE (Incident Commander) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py lag --backend fs; python3 state/snapshot.py get --region b --backend fs` | Output JSON có `"embed_model_version": "embed-model=vi-e5-base@v3"`; `curl -s localhost:8002/v1/state` cho `count>0`, `"weights": true` | On-call SRE |
| 4 | Scale pool warm→full, chờ warm-up | `printf full > state/region-b/pool_state; for i in $(seq 60); do curl -sf -o /dev/null localhost:8002/readyz && echo READY && break; sleep 1; done` | In ra `READY` (= `/readyz` của B trả **200**, thường ~6s warm-up). Hết 60s không có `READY` → **ABORT, không làm bước 5**: `printf warm > state/region-b/pool_state`, escalate ML Platform | On-call SRE |
| 5 | DNS/LB cutover (CHỈ sau khi bước 4 = 200) | `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` cho `"active_region":"b"` (chờ tối đa TTL 5s) và `curl -s localhost:8080/v1/infer` trả `"region":"b"` | On-call SRE, IC xác nhận |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' localhost:8002/v1/infer; done` | 10/10 trả 200, **p95 < 500ms, error rate = 0%**. Nếu không → coi như bước 5 thất bại, escalate | On-call SRE |
| 7 | Đo RTO/RPO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"valid":true`, `rto_verdict` = `PASS`; mở postmortem trong 48h theo `reports/postmortem.md` | IC → owner postmortem |

**Rollback (failover ngược B → A):**

- **Điều kiện** (phải đủ CẢ BA): (1) region A `/readyz` = 200 liên tục ≥ 15 phút
  (health checker ghi `to:HEALTHY, region:a` và không flap lại); (2) dữ liệu A đã được đồng bộ
  ngược các doc ingest vào B trong lúc B active (snapshot `put --region b` → `get --region a`);
  (3) đang trong cửa sổ traffic thấp.
- **Ai quyết định:** Incident Commander + Tech Lead của service (2 người) — **không** để
  automation tự failback. §4 Anti-Patterns: full-auto không có circuit breaker → 2 region
  flap qua lại. Circuit breaker: tối đa 1 failover mỗi chiều / 1 giờ.
- **Lệnh:** `python3 chaos/kill_region.py restore --region a --backend bare` (drill) →
  chờ A ready → `python3 dr/failover.py --target a --backend fs` (failover dùng cùng 5 bước,
  cũng có `4_wait_ready` trước khi đổi `edge/active_region`).
- **Rollback ngay lập tức** (không cần chờ 15 phút) chỉ khi: B lỗi golden signals sau cutover
  **và** A đã khỏe trở lại. Nếu cả hai đều lỗi → giữ nguyên, escalate, không đổi DNS qua lại.
