# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Report them privately through
GitHub's **"Report a vulnerability"** button on the Security tab of
https://github.com/HSigei/forgeCLI.

Include what you found, how to reproduce it, and the Forge version (`forge --version`). We aim to
acknowledge reports within 7 days. Please give us reasonable time to fix the issue before
disclosing it publicly.

## Safe harbour

We will not pursue action against good-faith research that:

- only tests your own installation of Forge;
- does not access, change or destroy other people's data or systems;
- does not degrade any service we or third parties run;
- is reported to us privately and not disclosed before a fix or 90 days, whichever is sooner.

This safe harbour does not cover third-party services Forge connects to (AI providers, GitHub,
Cloudflare and others). Testing those is governed by their own policies.

There is no paid bug bounty.

## Supported versions

Only the latest released version of Forge receives security fixes.

## Using Forge safely

- Run Forge in folders you are willing to have it change, under version control.
- Keep the approval mode on "ask" for anything you have not reviewed.
- Never paste secrets you don't want sent to an AI provider.
- Use `skill-inspector` on third-party skills before installing them, and read them yourself.
