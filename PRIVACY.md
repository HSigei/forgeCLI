# Privacy Policy

**Last updated: 11 November 2026**

This policy explains what happens to data when you use Forge. Forge is made by Harry Sigei,
trading as HK Labs ("we"). Short version: **Forge runs on your computer, and we do not receive
your code, prompts, files or API keys.**

## What stays on your computer

Forge stores these locally, in your user folder (normally `~/.forge`):

- your API keys (in your operating system's keychain where available, otherwise in
  `~/.forge/keys.json`);
- conversation history and sessions (`~/.forge/sessions`);
- settings, approval choices, skill copies, update and sponsor-line state.

We have no access to any of this. You can delete it at any time by deleting `~/.forge`.

## What Forge sends, and to whom

Forge only sends data where it must to do the work you ask for:

| Destination | What is sent | When |
|---|---|---|
| AI providers you configure (for example Groq, NVIDIA, OpenRouter, OpenAI, Google Gemini, Cerebras, Mistral) | your messages, and the file contents, command output and context Forge needs for the request, plus your API key for that provider | every request to a model |
| Keyless gateways (kilo, llm7, ovh) | the same as above, without a key | only if every keyed provider failed and keyless gateways are enabled (`FORGE_KEYLESS=0` turns them off) |
| Web search (DuckDuckGo) | the search query | only when Forge searches the web |
| GitHub | requests needed to sign in, read repositories, or check for updates | only when you use those features |
| Sponsor-line server (if enabled in your version) | a random install identifier and the Forge version, to choose at most one sponsor line per day and count showings | at most once a day |

**These providers are independent of us.** Each one handles your data under its own terms and
privacy policy, which may include logging, retention, human review or training on your data,
depending on the provider and your plan. Read their policies before sending sensitive material,
and do not send secrets, personal data or confidential code you are not allowed to share.

Forge tries to detect and redact API keys you paste into the chat before they are sent or saved.
This is a best-effort feature and may miss things.

## What we collect

We do not run analytics or telemetry in Forge, and we do not sell, rent or share personal data.

- **Sponsor-line server** (only where enabled): a random install identifier, Forge version,
  and count of lines shown. It contains no name, email, code or prompts. Your IP address is
  processed by our hosting provider (Cloudflare) to deliver the request.
- **Website**: the site is static and hosted on GitHub Pages; GitHub may log visits under its
  own privacy policy. The site stores your chosen style in your browser's local storage only.
- **GitHub issues and email**: if you contact us, we receive what you send.

## Your rights

You can stop all data flows by uninstalling Forge and deleting `~/.forge`. Depending on where
you live (for example under the Kenya Data Protection Act 2019 or the EU/UK GDPR) you may have
rights to access, correct or delete personal data we hold. Since we hold almost none, most
requests are about third-party providers, so contact them directly. For anything we hold, open
an issue at https://github.com/HSigei/forgeCLI or use the contact in `SECURITY.md`.

## Children

Forge is not intended for children under 13 (or the minimum age in your country).

## Changes

We may update this policy. The date at the top shows the latest version.
