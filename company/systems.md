# Systems

| System | URL | Used for |
|---|---|---|
| Acme Mail | http://127.0.0.1:8001 | The recruiting mailbox hr@acme.test. Job applications, candidate emails, sending emails. Already signed in. |
| HireDesk (ATS) | http://127.0.0.1:8002 | System of record for jobs, candidates, stages, interviews and interviewer calendars. |

## Signing in to HireDesk
- Use the credentials stored as secrets `ATS_USERNAME` and `ATS_PASSWORD` (never type or reveal the actual values; use the fill-secret capability).
- Sessions can expire. If you see the sign-in page again, sign in again and continue where you left off.

## Notes
- HireDesk is the source of truth. A task is only done when HireDesk reflects it.
- Every candidate record must have the resume PDF attached (download it from the application email, then upload it in HireDesk).
- Before adding a candidate, search HireDesk by email: referrals are often pre-created by employees. Never create duplicates; complete the existing profile instead.
- HireDesk occasionally returns "temporarily unavailable" errors. Unsaved changes are lost in that case; retry.
