# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.

Môi trường drill: bare mode, `--mock`, chạy trong WSL2 Ubuntu (Windows host không có `SIGSTOP`),
`WARMUP_SECONDS=6`, `EDGE_TTL_SECONDS=5`, chaos mode `netblock` (SIGSTOP). Mọi thời gian ISO là UTC.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T12:58:38` | chaos kill region-a, netblock | `chaos/chaos-events.jsonl:1` |
| Request OK cuối cùng trước kill | seq 15, `ok:true`, served_by a | dòng `ok:true` cuối trước t_outage | `reports/drill-1-nodr.jsonl:16` |
| Request fail đầu tiên | `+0.3s` (ReadTimeout, 2026ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | 16/16 request sau t_outage đều fail, tới dòng cuối | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` (16/32 request fail) | `tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl` | `reports/drill-1-nodr.jsonl:17` |

## 2. Drill 2 — có DR

t_outage = `2026-10-09T12:59:32` (ts `1791550772.39`).

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.1 | dòng `ok:false` đầu (ReadTimeout) | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +15.0 | `to:UNHEALTHY, region:a`, `consecutive_fails:3` | `reports/health-events.jsonl:4` |
| Snapshot restore xong | +15.3 | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +21.8 | `step:4_wait_ready`, `waited_s:6.55` | `reports/failover-events.jsonl:4` |
| DNS cutover | +21.9 | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+22.4** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 22.4s (11 request fail) | 300s (5 phút) | **PASS** |
| RPO — Vector DB | 2.0s / 1 doc | 300s (5 phút) | **PASS** |

RPO lấy từ `rpo_seconds` / `docs_lost` ở `reports/failover-events.jsonl:2` (snapshot
chụp 2.48s trước khi restore; replication mỗi 30s nên RPO lần này thấp do may mắn về thời điểm,
trường hợp xấu nhất ≈ 30s / ~15 doc ở tốc độ ingest 0.5 doc/s).
`embed_model_version` được restore cùng index: `embed-model=vi-e5-base@v3`.

Output `tools/measure_rto.py` cho drill 2: `"valid": true`, `"warnings": []`,
`"recovered_by_region": "b"`, `"rto_measured_s": 22.4`, `"rto_verdict": "PASS"`,
`"rpo_at_restore_s": 2.0`, `"docs_lost": 1`.

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0 | `interval_s × threshold` = 5 × 3 trong `reports/health-events.jsonl:4` | Hạ interval (vd 2s × 3 = 6s) hoặc dùng probe timeout ngắn hơn; đổi lại tăng nguy cơ flapping và tải probe |
| Runbook confirm + snapshot restore | 0.3 | t_detect → `3_scale_pool` (`reports/failover-events.jsonl:3`): verify 0.09s + restore 0.17s + scale 0.02s | Đã nhỏ (fs local). Thật (S3 cross-region, index GB) sẽ là phút → giữ region phụ luôn "pilot light" đã restore sẵn, chỉ apply delta |
| GPU pool warm-up | 6.6 | `waited_s` ở `4_wait_ready` (`reports/failover-events.jsonl:4`) | Warm standby (giữ pool B ở `full` với ít replica), pre-load weights, hoặc active-active |
| DNS/LB TTL cache | 0.5 | t_recovered − t_cutover = 22.4 − 21.9 (`reports/drill-2-withdr.jsonl:36`) | TTL ngắn hơn (hiện 5s — lần này cache vừa hết hạn nên chỉ tốn 0.5s; xấu nhất +5s) hoặc health-check-based routing ở LB thay vì DNS |
| **Tổng** | **22.4** | 15.0 + 0.3 + 6.6 + 0.5 (+0.02s ghi file cutover) = `rto_measured_s` | |

Detect floor chiếm 15.0 / 22.4 ≈ **67%** RTO.
