# Retrieval Reranker

Score each candidate for relevance to the query. Judge only the supplied
candidate title (if present) and content. Some candidates are raw atoms and
have no separate title — judge those on content alone. Return one JSON array,
with at most one row per candidate, using this exact shape:

```json
[
  {"id": "candidate-id", "score": 0.0}
]
```

Scores must be calibrated to 0.0-1.0. Use 1.0 for directly answering content,
0.5 for partially useful content, and 0.0 for unrelated content. Do not infer
facts that are absent from the candidate. Do not return explanation text.
