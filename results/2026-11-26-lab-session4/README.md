# Learner Lab session 4, 26 November 2026

| Recording beat | Observed result |
| --- | --- |
| Clean push | The push completed the pipeline and deployed successfully. |
| F1 (Q8_0 → Q4_K_M weights) | D2 blocked the run during Build, before Approval or Deploy. |
| F9 (2 → 1 serving threads) | The gate passed on t3.large. ValidateService took 11m22s versus about 10.4 min at 2 threads. The D6 p95 inter-token latency ratio stayed within 1.37. |
| F3 (q4_0 KV cache) | Build passed. ValidateService failed and stopped measuring early on the first instance of `d-DXAUXSI1N`. D3 agreement was 0.0039 and mean approximate KL was 11.34. CodeDeploy skipped the second instance and triggered automatic rollback. |

On t3.large the two vCPUs are hardware threads on one physical core. This
helps explain why dropping one serving thread caused only a modest slowdown
and stayed within the D6 threshold in this run. See the
[F9 measured result](../../docs/deviations/2026-11-24-t3large-thresholds.md#f9-measured-result-26-november-2026).

The session also exposed a duplicate pipeline run. Commit `fc9986d` ran twice
after a push received while the lock was held was replayed when the run ended.
