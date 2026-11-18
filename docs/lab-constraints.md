# Lab Constraints and Equivalences

Appendix required by FR-28. The first two columns come from PRD Section 14.2 (probe results, 17 Nov 2026); "session stops" comes from Section 14.1 because no probe covers it yet. Other columns are filled in by T-27.

| Constraint | What the Lab does | Equivalent used | Evidence | What is lost | Enforced or documented |
| --- | --- | --- | --- | --- | --- |
| CodePipeline | CodePipeline V2 with LabRole as service role: blocked, cannot assume LabRole | TBD | TBD | TBD | TBD |
| CodeBuild | CodeBuild with LabRole: blocked, AccessDeniedException | TBD | TBD | TBD | TBD |
| IAM role creation and least privilege | IAM role creation: blocked, denied | TBD | TBD | TBD | TBD |
| Research layer on laptop | Local llama-server in WSL (Qwen2.5-0.5B Q4_K_M, CPU): works, about 90 to 100 tokens/s generation on the laptop | TBD | TBD | TBD | TBD |
| Cost Explorer lag | Cost Explorer: works; per-service data visible, about a day behind | TBD | TBD | TBD | TBD |
| Session stops | 4-hour sessions after which machines stop (Section 14.1; probe "End session, list what still bills" not yet run) | TBD | TBD | TBD | TBD |
