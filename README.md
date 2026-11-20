# Landing Page Preview

This repository hosts an isolated staging copy of the iYakuza website and GitHub-only CMS.

## Preview environment

- Planned URL: https://iyakuza.github.io/landing-page-preview/
- Source snapshot: `iYakuza/landing-page`, branch `recovery/supabase-content-export-20261119`
- Preview public content and media: this repository's `content/` and `media/` folders on `main`
- Preview private drafts and metadata: `iYakuza/iyakuza-cms-private`, branch `preview`
- Production repository and production private branch are not CMS write targets.

## Publishing

The `.github/workflows/preview.yml` workflow builds the static preview. It is triggered by changes to preview `content/` or `media/`, or manually from GitHub Actions.

In repository Settings → Pages, set **Build and deployment → Source** to **GitHub Actions** before the first deployment.

The preview is intentionally marked noindex and has a disallow-all `robots.txt`. Do not store secrets in this public repository.
