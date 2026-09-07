# Retrieval Reranker

Score each candidate for relevance to the query. Judge only the supplied
candidate title and content. Candidates always use the exact shape
`{id, title, content}`. Raw atoms have no title, so their `title` is `null`.
Return one JSON array, with at most one row per candidate, using this exact shape:

[
  {"id": "candidate-id", "score": 0.0}
]

Scores must be calibrated to 0.0-1.0. Use 1.0 for directly answering content,
0.5 for partially useful content, and 0.0 for unrelated content. Do not infer
facts that are absent from the candidate. Do not return explanation text.
