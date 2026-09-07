Each mode accepts the same query inputs and returns `RawCandidate` values.
The hybrid mode delegates fusion to Qdrant's native `Fusion.RRF` query; no
client-side RRF implementation belongs in this package.
