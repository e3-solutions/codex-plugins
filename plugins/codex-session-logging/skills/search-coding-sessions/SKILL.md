---
name: search-coding-sessions
description: Retrieve prior E3 coding-session knowledge through Cosmos/Sesh when a user asks what was tried before, why code or behavior changed, or where earlier ticket, file, path, or symbol work happened.
---

# Search Coding Sessions

Use Cosmos/Sesh to answer questions about prior E3 coding work from the passages that were actually retrieved.

## Retrieval

1. Discover or load the configured Cosmos/Sesh coding-session search tool when the environment supports lazy tool loading, then call it with the user's question. Preserve distinctive identifiers such as ticket IDs, file paths, symbols, hashes, error text, and repository names in the query. Apply repository or contributor filters only when the user supplies them or the context makes them unambiguous.
2. Inspect `retrieval_status`, `answerability`, `context_complete`, and the returned `evidence` passages when those fields are present. A matching session, title, repository, contributor, or identifier is not enough: classify each material part of the proposed answer as supported, unsupported, or partial from the returned passage itself.
3. Use the returned source-open metadata with Cosmos `timetracker__get_chat` when a supporting passage is truncated or ambiguous, when `context_complete` is false, or when exact source verification is needed. Pass the returned source arguments rather than reconstructing a session identifier. Read the relevant neighboring messages, preserve qualifications or contradictions, and confirm that the cited message identifier and content agree with the selected evidence.
4. If the correct session is returned with a non-supporting passage, keep that as a passage-selection miss. Opening the full source may find evidence that helps the user, but it does not retroactively make the returned passage a retrieval success; report the distinction.
5. Answer only the supported parts, cite the session/source plus supporting message identifiers when available, and label the result partial when any material part remains unsupported. Quote only the shortest text needed to make the support clear.

## Outcome Discipline

- If search succeeds but no returned passage supports the answer, say that no supporting passage was found in the returned results. Do not overstate a top-result miss as proof that the full corpus lacks the answer, and do not infer an answer from a plausible session or nearby topic.
- If the search tool is unavailable, errors, or times out, report a search failure. Do not describe that as no supporting result.
- If required source opening fails, abstain from claims that depend on omitted context. If the returned passage independently supports the whole claim, it may be reported only as passage-supported and source-unverified.
- Treat similarity and result order as retrieval signals, not confidence that an answer is true.
- Do not change ingestion, access grants, source sessions, or index contents as part of retrieval.
