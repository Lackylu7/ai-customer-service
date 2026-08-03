# DingTalk enterprise assistant integration

The DingTalk connector remains a thin transport adapter. `app.dingtalk_adapter`
handles validated event mapping, MVP deduplication, and backend conversation state.
Business routing and pre-answer safety checks live in
`app.enterprise_assistant.EnterpriseAssistant`.

## Request mapping

Map each DingTalk event to:

- `question`: normalized message text;
- `user`: a stable namespaced identifier such as `dingtalk:<staff-id>`;
- `conversation_id`: the stored backend conversation ID for the employee and
  DingTalk session;
- `channel`: `group` or `direct`.

`DingTalkAssistantAdapter.handle` maps the event and calls
`EnterpriseAssistant.answer`. The result contains the routed domain, action,
employee-visible text, policy reason, and optional backend conversation ID.

The assistant accepts the legacy `dify_client` argument or a channel-independent
`brain`. It also accepts an optional `shareholder_brain`. When policy routing selects
the shareholder domain, that specialized brain is used; all other domains keep the
default brain.

`OpenClawShareholderBrain` first calls Dify for reviewed evidence and requires the
approved shareholder document in retriever resources. It then invokes the isolated
`shareholder-3-0` agent through `openclaw agent --json` without `--deliver`. Missing
evidence is handed off. CLI failure or locally invalid output falls back to the Dify
answer, while the Python policy gateway remains authoritative for denied requests.

## Action mapping

- `allow`: send `text` and persist `conversation_id`.
- `handoff`: send `text` that asks the employee to contact the responsible owner and
  preserve `reason` for operations reporting. Do not claim a human case was created
  until a real human-service record or notification succeeds.
- `deny`: send `text`; do not forward the original request to Dify or echo sensitive
  content into a group.

## Required connector controls

The in-memory adapter filters bot messages and duplicate event IDs for local tests.
Before enabling a real DingTalk endpoint, add Stream SDK integration, signature or
connection verification, persistent replay protection and event deduplication,
payload limits, outbound retry/idempotency, and secret loading from environment or a
secret manager. Do not store app secrets, tokens, user private data, or bank-account
data in source files or logs.

Conversation continuity does not grant access. A follow-up is forwarded only when a
previous business domain exists and the new message looks like a contextual follow-up;
every turn still passes credential, personal-data, historical-rule, and operation
controls before reaching the configured brain.

The knowledge-scope primitives in `app.knowledge_scope` are not yet wired into the
Dify retrieval path. Time, audience, city, and store metadata must be enforced before
retrieved content can be exposed through DingTalk.

## Restricted Stream MVP

`python -m app.dingtalk_mvp_agent "<question>"` is the one-shot command used by the
local DWS Stream connector during the single-user smoke test. It loads server-side
Dify configuration from `.env`, always evaluates messages as group-channel input,
and prints only the employee-visible answer. This intentionally gives up conversation
continuity and per-user context until the native Stream event runner is implemented.

The Stream connector must also enforce an explicit `allowed-users` list and a rate
limit. Do not run the command through a general-purpose coding agent or with system
execution permissions. The one-shot command exposes only the existing policy gateway
and Dify chat client; it cannot access files, databases, DingTalk tools, or operating
system actions on behalf of an employee.
