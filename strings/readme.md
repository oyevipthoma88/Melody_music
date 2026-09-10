# Melody language support

Melody keeps user-facing strings in `strings/langs/` so deployments can provide a consistent multilingual experience without changing command logic.

| Code | Language |
|---|---|
| `en` | English |
| `hi` | Hindi |
| `si` | Sinhala |
| `ar` | Arabic |
| `te` | Telugu |
| `tr` | Turkish |
| `ru` | Russian |
| `hinglish` | Hinglish |
| `bhojpuri` | Bhojpuri |

## Translation workflow

1. Copy the relevant YAML file from [`strings/langs/`](langs/).
2. Translate message values while preserving every placeholder such as `{0}`, `{1}`, `{chat}`, `{user}` and named formatting tokens.
3. Do not rename string keys; handlers use those keys as a stable interface.
4. Run the normal compile and test commands before opening a pull request.

Melody-specific wording should remain clear, respectful and safe for group chats. Do not put tokens, private links or personal information in translation files.
