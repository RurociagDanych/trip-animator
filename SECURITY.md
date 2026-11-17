# Security and publication safety

## Reporting an issue

Do not put credentials, private activity coordinates, photos, session cookies, or
share links in public issues or pull requests. Use a private GitHub security
advisory if private vulnerability reporting is enabled for this repository.
Otherwise, ask the maintainer to arrange a private reporting channel without
including sensitive details in the public request.

## Before publishing or sharing a demo

- Use synthetic activities and generated images. Review screenshots manually for
  private routes, names, email addresses, browser URLs, share links, and tokens;
  inspect image metadata as well as the visible pixels. A secret scanner does
  not establish that location data or photographs are safe to publish.
- Keep real `.env` files, credentials, object-store data, databases, raw activity
  files, and Terraform state and saved plans outside version control. Ignore
  rules are preventive only: they do not remove previously tracked content or
  make force-added files safe. Do not distribute the working directory or `.git`
  directory as a substitute for a reviewed source export.
- Treat `.env.example`, development Compose defaults, and Terraform example
  values as public development placeholders, never as production credentials.
  Replace them and restrict network exposure before deploying a public service.
  Terraform state can contain generated passwords even when outputs are marked
  `sensitive`; do not attach state or plan files to issues or build artifacts.
- Scan both the complete locally available Git history and the exact publication
  candidate, including intended untracked files. Use a full clone in CI; a
  shallow checkout cannot validate earlier history. Review the candidate file
  list rather than scanning an entire developer directory containing real data.
- Use Gitleaks 8.30.1 or a reviewed newer release with `.gitleaks.toml`, full
  redaction (`--redact=100`), and `--ignore-gitleaks-allow`. Keep reports only in a
  private temporary location outside the repository. The checked-in configuration
  extends all default rules and limits exceptions to reviewed, exact non-secret
  lines in specific files. Do not bypass findings by excluding source directories
  or adding broad allowlists. Review scanner warnings and omissions as well as
  its exit status; a clean scan is not proof that no secrets exist.
- If an actual credential was committed, stop publication, revoke or rotate it,
  and coordinate history cleanup with the repository owner. Removing a file or
  adding an ignore rule does not revoke a secret or erase it from history. Do not
  rewrite shared history without explicit authorization.

## Audit scope limits

A local review covers only the refs, reflogs, and files available at review time.
Recheck the final candidate after other changes. Hosting-provider secrets,
remote-only refs, Git LFS payloads, external object storage, and future uploads
require their own access and review. Ignored local data is intentionally not a
publication candidate and should not be uploaded to a scanning service.
