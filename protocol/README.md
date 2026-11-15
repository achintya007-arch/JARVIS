# BBP schemas

Machine-readable form of the Brain–Body Protocol spec
([`docs/protocol/BBP_v0.1.md`](../docs/protocol/BBP_v0.1.md)). Both the Brain
(Python, `jsonschema`) and the Body (C#, `JsonSchema.Net`) validate against
these files.

| File | Validates |
| --- | --- |
| `envelope.schema.json` | Every message: `v`, `id`, `kind`, `topic`, `ts`, `corr`, `payload` |
| `session/<kind>.schema.json` | The `payload` of `hello`, `welcome`, `ping`, `pong` |
| `topics/<topic>.schema.json` | The `payload` of an event, command or request (and the `error` response) |
| `topics/<topic>.response.schema.json` | The `payload` of a successful response to a request: `{ ok: true, result }` |

`topics/confirm.response.schema.json` only looks like a response schema: it is
the *command* topic `confirm.response` (the Body's answer to a `confirm.request`).
There is no topic called `confirm`, so the two names never clash.

How a message is validated:

1. Validate the whole message against `envelope.schema.json`.
2. Validate `payload` against `session/<kind>.schema.json` for session kinds,
   `topics/<topic>.response.schema.json` for a `response` whose topic isn't
   `error`, otherwise `topics/<topic>.schema.json`. A topic with no schema is
   unknown.

Conventions:

- JSON Schema Draft 2020-12; each file is self-contained (no cross-file `$ref`;
  `$ref` into the file's own `$defs` is fine, e.g. the recursive `uia.read` node).
- Payloads allow unknown fields (`additionalProperties: true`): receivers ignore
  fields they don't know, so adding a field never bumps `v`.
- Removing or renaming a field or topic bumps `v`.
