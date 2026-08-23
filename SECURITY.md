# Security and privacy

Relay is a local controller for provider CLIs. The repository is public, but
run data is not public by default.

## Keep out of GitHub

Do not commit or attach:

- provider credentials, access tokens, API keys, or login/session files;
- Relay runtime directories, raw provider streams, prompts, requests, or
  generated worktrees;
- private repository contents or validation output that contains secrets.

Relay stores exact prompts, responses, raw streams, and evidence under the
local runtime directory. On Windows the default is
`%LOCALAPPDATA%\ToledoOrchestrator`; protect that directory as private data.

## Provider authorization

Use only provider accounts and repositories you control or are authorized to
use. Do not share credentials, route another person's consumer subscription
login through Relay, or use Relay to bypass provider limits or access controls.
When building a product or service for other users, use the provider's
supported API, team, enterprise, or cloud authorization path and review its
current terms before deployment.

## Reporting

Please do not publish a suspected credential or private run in a public issue.
Use GitHub's private security reporting mechanism for the repository when
available; otherwise contact the maintainer through GitHub with a minimal
reproduction and no sensitive data.
