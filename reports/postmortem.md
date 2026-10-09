# Postmortem — DR Drill Lab 23 (Region A down → failover Region B)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

**Tóm tắt:** 2026-10-09 12:59:32 UTC, region A bị netblock (SIGSTOP) giữa lúc đang có traffic 2 rps.
Health checker phát hiện sau 15.0s, runbook bán tự động (`--auto` cho drill) restore snapshot
sang region B, chờ warm-up 6.55s rồi cutover. Request đầu tiên thành công từ B sau **22.4s**.
11 request user bị lỗi. Mất **1 document** (RPO 2.0s). Baseline không có DR (drill 1): `NO_RECOVERY`.

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time (UTC) | +s | Sự kiện | Evidence |
|---|---:|---|---|
| 2026-10-09T12:59:20 | −12.0 | health checker start (interval 5s, threshold 3, timeout 2s) | `reports/health-events.jsonl:1` |
| 2026-10-09T12:59:32 | 0 | **outage bắt đầu** — kill region-a, mode netblock, region-b alive | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T12:59:32 | +0.1 | **user đầu tiên bị ảnh hưởng** — 503 ReadTimeout qua edge | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T12:59:47 | +15.0 | **health check alert** — region-a UNHEALTHY, 3 fail liên tiếp (`timeout>2.0s`) | `reports/health-events.jsonl:4` |
| 2026-10-09T12:59:47 | +15.0 | runbook xác nhận outage (3/3 probe timeout) + mở incident SEV1, notify delay 15.04s | `reports/runbook-run.jsonl:1`, `reports/runbook-run.jsonl:2` |
| 2026-10-09T12:59:47 | +15.0 | **operator confirm cutover** (`--auto`), failover bắt đầu `1_verify_target` (B: warm, 0 vector, no weights) | `reports/runbook-run.jsonl:3`, `reports/failover-events.jsonl:1` |
| 2026-10-09T12:59:47 | +15.3 | snapshot restore xong: RPO 2.0s, 1 doc lost, `embed-model=vi-e5-base@v3` | `reports/failover-events.jsonl:2` |
| 2026-10-09T12:59:54 | +21.8 | region-b `/readyz` = 200 sau 6.55s warm-up | `reports/failover-events.jsonl:4` |
| 2026-10-09T12:59:54 | +21.9 | DNS cutover `edge/active_region` a → b | `reports/failover-events.jsonl:5` |
| 2026-10-09T12:59:54 | +22.4 | **resolved** — request đầu tiên OK, `served_by:b` | `reports/drill-2-withdr.jsonl:36` |
| 2026-10-09T12:59:54 | +22.6 | golden signals B: 10/10 OK, p50 58.9ms, p95 96.7ms | `reports/runbook-run.jsonl:6` |

(`reports/runbook-run.jsonl` mặc định bị `.gitignore` bỏ qua — file này được `git add -f` để nộp kèm
vì nó là timeline của operator. Các mốc chấm điểm chính đều có bản sao trong `reports/failover-events.jsonl`.)

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `22.4s` · gap: `−277.6s` (đạt, dư 92.5% ngân sách)
- RPO mục tiêu: 300s · đo được: `2.0s` (`1` doc bị mất) · gap: `−298.0s` (đạt)
- **Bước tốn nhiều giây nhất:** `health-check detect floor` = 15.0s (67% RTO) — vì
  `interval 5s × threshold 3` là cái giá cố ý trả để chống flapping: 1–2 probe timeout
  không được phép kích hoạt failover. Bước thứ hai là GPU pool warm-up 6.55s (29%) vì region B
  là *warm standby không có data* — chỉ được scale `warm → full` sau khi đã có outage.
- RPO 2.0s lần này là **may mắn về thời điểm**: snapshot gần nhất được `put` chỉ 2.48s trước restore.
  Replication chạy mỗi 30s → RPO xấu nhất ≈ 30s (~15 doc ở 0.5 doc/s). Không nên báo cáo 2.0s
  như một cam kết.

## 3. Root cause (5 whys)

Câu hỏi: *nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. **Vì sao user bị lỗi 22.4s?** Vì traffic vẫn trỏ vào region A cho tới khi DNS cutover ở +21.9s.
2. **Vì sao cutover không sớm hơn?** Vì phải chờ 15s phát hiện + 6.55s warm-up — region B không
   ở trạng thái sẵn sàng nhận traffic (không data, pool `warm`).
3. **Vì sao region B không sẵn sàng?** Vì kiến trúc là active-passive "pilot light": state chỉ được
   copy sang B *tại thời điểm failover* từ snapshot, không replicate liên tục vào B.
4. **Vì sao snapshot là nguồn duy nhất?** Vì replication là batch mỗi 30s vào một object store
   (`state/_replica/`) — trong lab nằm trên **cùng đĩa** với region A, nên nếu đây là mất đĩa/mất
   region thật thì bước `2_restore_snapshot` sẽ **thất bại**: không có bản copy thật sự cross-region.
5. **Vì sao chưa ai thấy điều này?** Vì trước drill này chưa từng có game day; RTO/RPO trên slide
   chưa bao giờ được đo bằng log.

Các điểm yếu khác lộ ra khi xét "outage thật":

- `dr/failover.py` tính RPO bằng cách **đọc DB của region chính** — được trong lab vì SIGSTOP
  chỉ treo process, file vẫn đọc được. Mất region thật thì không đọc được → RPO chỉ có thể ước
  lượng từ `MANIFEST.json` + log ingest phía client, và phải được tính lại sau incident.
- **Ingest không failover:** `state/ingest.py` vẫn ghi vào region A sau cutover. Mọi document
  ingest trong lúc outage không có ở B → `docs_lost` thực tế tăng theo thời gian outage.
- Không có failback tự động có kiểm soát: ingest vào B sau cutover chưa có đường đồng bộ ngược về A.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Chuyển snapshot sang object store cross-region thật (MinIO/S3 CRR, `--backend minio`), kiểm tra `get` từ region B khi A đã mất đĩa | Platform/SRE | 2026-10-23 | Không giảm số, nhưng biến `2_restore_snapshot` từ "sẽ fail khi outage thật" thành chạy được |
| 2 | Giữ region B ở warm standby: restore delta mỗi chu kỳ replicate + giữ pool `full` với 1 replica nhỏ | ML Platform | 2026-10-30 | RTO −6.5s (bỏ warm-up khỏi đường găng), restore còn ~0s |
| 3 | Replication thường xuyên hơn (30s → 10s) hoặc CDC/WAL streaming cho vector DB | Data/Platform | 2026-11-06 | RPO xấu nhất 30s → 10s (CDC: < 1s) |
| 4 | Health check: interval 5s → 2s, threshold 3, timeout 1s, cộng kiểm tra từ 2 vị trí (quorum) để chống false positive | SRE | 2026-10-16 | RTO −9s (detect floor 15s → 6s) |
| 5 | Ingest đi qua edge/active_region (hoặc queue có buffer) để ghi vào region đang active | Backend | 2026-10-30 | Ngăn `docs_lost` tăng theo độ dài outage |
| 6 | Hạ EDGE TTL 5s → 1s hoặc health-check-based routing ở LB | SRE | 2026-10-16 | RTO xấu nhất −4s |
| 7 | Game day hằng quý với mode `stop` + `netblock`, random thời điểm kill, báo mean/stddev RTO | SRE lead | 2026-12-15 | Không trực tiếp — giữ con số RTO luôn là số đo, không phải số đoán |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   5s × 3 = **15s**, là detect floor. Health checker phát hiện ở đúng +15.0s
   (`reports/health-events.jsonl:4`) → chiếm 15.0 / 22.4 ≈ **67%** RTO. Với mục tiêu RTO 5 phút,
   về lý thuyết có thể chọn interval tới ~(300 − 6.6 warm-up − 5 TTL − restore) / 3 ≈ 95s, nhưng
   thực tế nên giữ detect ≤ 10–20% ngân sách RTO (≈ interval 10–20s) để còn chỗ cho restore thật
   (phút, không phải 0.17s) và cho con người confirm.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì?**
   Detect floor 15s → 3s, RTO giảm **~12s** (22.4s → ~10.4s). Cái giá (§4 flapping):
   một GC pause, một lần deploy rolling, hay network blip 3s là đủ kích hoạt failover —
   failover thừa tốn restore + warm-up, và nếu failback cũng tự động thì traffic flap A↔B.
   Tải probe tăng 5×. Ngoài ra timeout 2s > interval 1s khiến các vòng poll chồng nhau,
   buộc phải hạ timeout → lại tăng false positive khi region chỉ chậm chứ không chết.
   Giải pháp tốt hơn: interval 2s + quorum nhiều vị trí probe + giữ failover bán tự động.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   `docs_lost = 1` chỉ là số đo **tại thời điểm restore**: 1 ticket khách gửi trong 2.0s cuối
   trước snapshot sẽ biến mất vĩnh viễn — hệ thống RAG không bao giờ trả lời dựa trên nó nữa,
   và khách không được báo. Nhưng nếu ingest vẫn trỏ vào region A (như hiện tại), mọi ticket gửi
   trong 6 giờ outage cũng mất: ở 0.5 doc/s là ~10,800 doc — con số thật khách hàng cảm nhận là
   `docs_lost tại restore + toàn bộ ingest bị gửi vào region chết`. Với khách hàng điều đó nghĩa là:
   phải thông báo minh bạch khoảng thời gian bị mất dữ liệu, cung cấp danh sách doc_id/khoảng thời
   gian để họ gửi lại, và action item #5 (ingest đi theo region active) là bắt buộc trước khi cam
   kết RPO 5 phút với bất kỳ ai.

## 6. Phụ lục — câu hỏi baseline (GUIDE Step 1) và Reflection Questions

**Baseline, trước khi viết code** (đo lúc 12:58 UTC, trước drill 1):

1. *Region A chết thì component nào phát hiện?* — Không component nào. Chưa có health checker,
   edge proxy vẫn đọc `edge/active_region = a` và trả 503 mãi (drill 1: 16/16 request sau kill đều
   fail, `reports/drill-1-nodr.jsonl:17` → `reports/drill-1-nodr.jsonl:32`).
2. *Region B có data/weights không?* — Không: `/v1/state` của B trả `count:0`, `weights:false`,
   `pool_state:warm`; `/readyz` trả 503 với `pool_state=warm, model_weights_missing, vector_db_empty(count=0)`.
3. *Flip `edge/active_region` sang `b` ngay lúc đó thì sao?* — User nhận `region_not_ready` (503) từ B
   thay vì timeout từ A: đổi DNS không cứu được gì khi state chưa có. Process sống ≠ region serve được.

**Reflection Questions:**

1. *Thành phần nào giảm được mà không tăng nguy cơ flapping?* — GPU warm-up (6.6s) và DNS TTL (0.5s,
   xấu nhất 5s). Giữ B ở warm standby (pool `full`, state restore liên tục) bỏ warm-up khỏi đường găng
   mà không đụng tới logic phát hiện; cái giá là **tiền**: trả GPU cho region phụ 24/7 dù không có traffic.
   Hạ TTL thì giá là thêm request resolve/đọc pointer. Còn detect floor (15s) thì giảm được nhưng
   chính là thứ đánh đổi trực tiếp với flapping (xem câu 2 ở mục 5).
2. *Health checker chạy chung process với serving thì ai báo động khi process chết?* — Không ai: process
   chết thì checker chết theo, im lặng trông y như "không có lỗi". Vì vậy `dr/health_checker.py` là
   process riêng, chỉ import `httpx` và gọi `/readyz` qua mạng — **không import gì từ `serving/`**.
   Thật ngoài production nên chạy nó ở region thứ ba / nhiều vị trí.
3. *"RTO 5 phút có thật không?" mở file nào?* — Chạy
   `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`, nó đọc
   `reports/drill-2-withdr.jsonl` (đồng hồ phía user), `chaos/chaos-events.jsonl` (mốc 0),
   `reports/health-events.jsonl` (t_detect) và `reports/failover-events.jsonl` (t_cutover). Con số trả lời:
   **22.4s**, request phục hồi đầu tiên ở `reports/drill-2-withdr.jsonl:36` — không phải số trên slide.
