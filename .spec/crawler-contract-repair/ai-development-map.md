# AI development map

The user requested repair and upload after reviewing S1, S3 and S4. The root
agent owns continuation metadata, documentation, integration, verification and
publication. A builder in an isolated worktree owns only adapters and their
contract tests. A separate reviewer checks the resulting immutable diff.
Original implementation is not attributed to these agents. Synthetic transports
exercise real local orchestration, persistence and workflow while substituting
external HTTP. Merge/deployment remain separate decisions.
