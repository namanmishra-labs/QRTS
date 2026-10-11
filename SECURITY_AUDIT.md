# QRTS security and ownership audit

Audit date: 11 October 2026 (local working copy only)

## Scope inspected

Reviewed the tracked Python files, access and logging files, README, dependencies, GitHub Actions workflow, image assets, local Git remote/history metadata, and the added Flask app, templates, browser scripts, Apps Script adapter, Render blueprint, environment example, and tests. Secret-pattern checks reported file locations and categories only; credential values were not included in this report.

## Findings and completed cleanup

- `Acess.txt` contained a legacy QR allow-list value. The file has been removed from the current working tree and is now ignored. `main_webcam.py` is a migration notice and no longer treats QR contents as coordinator authorization or writes a local access log.
- The allow-list value appears in six commits in the available Git history. It must be treated as exposed. This checkout's history has not been rewritten, and no remote history has been changed. Do not use that value. If it was configured in any other system, an authorized administrator must revoke it there.
- The local Git `origin` remote pointed to an inherited upstream. It has been changed to the QRTS repository URL previously supplied by the project owner. This updated only local Git configuration; no GitHub request, commit, or push was made.
- `.github/workflows/main.yml` previously attempted to run the local webcam scanner in CI. It now installs the web dependencies and runs the mocked unit tests on pushes and pull requests.
- `README.md` and the application pages identify Naman Mishra as project maintainer. The README no longer presents an inherited stock image as a project logo. The sample images remain for reference; they contain Shutterstock branding/payloads and should be redistributed only if their license permits.
- `.gitignore` now excludes local environments, logs, `.env` files, service-account/client-secret files, token files, private keys, and the legacy access-list filename. `.env.example` contains placeholders only.

## Credential and integration inventory

- **Potential credential:** the legacy QR allow-list entry in historical `Acess.txt` versions. The current file is removed. Rotation/revocation is required wherever that value may have been used. This project cannot identify or revoke a separate system that may have accepted it.
- **Test fixture:** `tests/test_app.py` contains a deliberately fake coordinator password for isolated tests. It is not used by the deployed app.
- **No live Google configuration found:** no real spreadsheet ID, Apps Script deployment URL, service-account email/key, API key, webhook, or deployment token was present in the current source/configuration. Apps Script properties and `.env.example` are blank or placeholder-driven. No Google account or spreadsheet was accessed.
- **Coordinator access:** the web app reads `COORDINATOR_PASSWORD` and `FLASK_SECRET_KEY` from server environment variables. The repository contains no default production coordinator password. Attendee QR tokens are separate from coordinator login. Coordinators currently share the configured event password; rotate it when the authorized group changes.
- **Git identity history:** historical commit metadata contains inherited contributor identity information. Current source and UI branding are updated; Git authorship metadata is preserved. Changing it requires a history rewrite.
- The historical public upstream remains outside this local cleanup. Its owner or an authorized administrator must handle any access revocation or repository-history cleanup there.

## Still requires owner action

1. Treat any copy of the legacy allow-list value in public Git history as exposed. Revoke it from any system where it was reused. Do not rely on deleting the file from the current branch to invalidate old copies.
2. This cleanup does not purge historical `Acess.txt` blobs or contributor metadata from Git history. Rewriting history and force-pushing are intentionally pending explicit approval; doing so changes remote history and can disrupt other clones.
3. Create a new spreadsheet and Apps Script deployment under an account Naman Mishra is authorized to administer. Add only the minimum necessary spreadsheet access for that account, configure fresh secrets, and test against a copy before event use.
4. Confirm licensing/redistribution rights for the inherited sample images before publishing them in a public repository.
5. Review GitHub repository visibility, collaborators, and any original upstream resources directly in the relevant account. This audit did not change account settings or revoke external resources.

No deployment, commit, push, Git history rewrite, Google authorization, or external-account change was performed.
