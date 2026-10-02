# Cross-language API impact

ChangeGuard can statically connect Spring HTTP endpoints to common JavaScript and TypeScript request sites during a commit-pinned change analysis. It is a conservative source-level signal; it is not a complete language parser, an API schema validator, or a runtime integration test.

## What is analyzed

The stage runs when the diff contains a Java source or JavaScript/TypeScript source file. Backend and frontend source are read from the requested Git revision rather than the working tree.

The Java side recognizes Spring `@RequestMapping`, `@GetMapping`, `@PostMapping`, `@PutMapping`, `@PatchMapping`, and `@DeleteMapping` annotations. Class-level `@RequestMapping` paths are composed with method-level paths. Literal `value`/`path` strings and explicit `RequestMethod` values are supported. An untyped `@RequestMapping` is retained with method `UNKNOWN`.

The frontend side recognizes common static request shapes, including:

- `fetch(url, options)` (default method `GET`, with a literal `options.method` when present)
- `axios.get/post/put/patch/delete(url)`
- Named client methods such as `client.get(url)` and generic `client.request({ url, method })`
- Simple string literals, local string constants, and object properties resolving to static URLs
- A constrained local wrapper that forwards a URL parameter to `fetch`

Supported routes are normalized to a leading slash with collapsed repeated separators and no trailing slash except for `/`. Query and fragment suffixes are removed. Matching is case-sensitive and requires the HTTP method to match. A route placeholder such as `/items/{id}` matches one concrete path segment such as `/items/42`.

## Relationship and uncertainty semantics

Each unique deterministic match is a typed `API_CONSUMER` edge from the Java provider symbol to the frontend consumer. The edge includes the endpoint identity, confidence, and source references. It is separate from Java call-graph relationships.

- More than one matching provider is recorded as ambiguous; no edge is selected.
- A dynamic URL or unknown method is recorded as unresolved; ChangeGuard does not guess its destination.
- An endpoint that cannot be linked to a Java provider symbol remains unresolved.
- Static requests with no matching endpoint do not create edges.
- Bounded-out source files and Java parse errors are surfaced through metrics or uncertainty.

An endpoint/consumer is considered changed when an added current-source line intersects its declaration/request or route-mapping annotation. This uses exact added-line ranges for the API stage rather than the broader diff hunk context used by the existing Java symbol analysis. Class-level route annotation edits affect the endpoints composed with that class-level mapping. If Java symbol mapping falls back to a file-level change, endpoints in that file are conservatively marked. A deletion-only route mapping has no current endpoint node to identify as a removed endpoint, so this version does not provide a complete removed-endpoint inventory.

## Bounds and exclusions

Frontend source analysis is limited to 200 files, 256 KiB per file, and 8 MiB total. The tracked path listing is capped at 10,000 entries. `.git`, generated/build/vendor directories, and minified, bundled, or chunked assets are excluded. Source reads remain revision-pinned. The frontend scanner uses a standard-library tokenizer and adds no language runtime or parsing dependency; unsupported syntax may be missed and must not be interpreted as proof that no API relationship exists.

## Outputs and integration

`ChangeGuardAnalysis.api_impact` serializes the discovered endpoints, consumers, edges, ambiguity/unresolved issues, changed endpoint/consumer IDs, impacted consumers, provider symbols for changed consumers, metrics, and uncertainty. Existing SQLite persistence stores this as additive JSON within `change_analyses`; legacy analysis payloads without `api_impact` remain deserializable.

Known consumer edges can add frontend consumers to the existing blast-radius affected-file and affected-caller counts. They do not add a separate score or multiplier. JSON impact and PR analysis include the complete structured data; human-readable PR output prints a compact summary. Read-only MCP operation `get_api_impact` returns the stored result by analysis ID or `UNAVAILABLE` when that analysis has no API result.

The independent evaluation corpus includes authored endpoint, consumer, edge, changed/impacted-node, ambiguity, and unresolved-request expectations. API details are incorporated into deterministic evaluation fingerprints. See [evaluation.md](evaluation.md).

## Limitations

- The frontend scanner is intentionally incomplete and does not implement ECMAScript/TypeScript parsing, type checking, imports/module resolution, framework-specific routing, or arbitrary HTTP wrappers.
- URL strings computed through runtime configuration, environment values, interpolation, or complex expressions remain unresolved or are not extracted.
- Java inherited routes, custom mapping annotations, servlet configuration, proxies, gateways, and external API specifications are not resolved.
- This analysis does not establish that a request is executed, that an endpoint is reachable in deployment, or that the response contract is compatible.
- Verification remains a separate opt-in capability and does not execute API calls.
