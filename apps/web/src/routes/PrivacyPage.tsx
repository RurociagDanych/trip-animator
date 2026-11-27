import { Shield } from "lucide-react";
import { LegalPageShell } from "../components/legal/LegalPageShell";

export function PrivacyPage() {
  return (
    <LegalPageShell
      title="Privacy Policy"
      lastUpdated="November 26, 2026"
      icon={<Shield className="h-4 w-4" aria-hidden="true" />}
      siblingTo="/terms"
      siblingLabel="Terms of Service"
    >
      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">1. Information We Collect</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          Trip Animator stores only the data required to operate your workspace: your email address,
          display name, authentication credentials (hashed passwords or federated login
          identifiers), trip projects, uploaded GPX/TCX/FIT tracks, uploaded route photos, and
          optional Strava activity data you explicitly choose to import.
        </p>
      </section>

      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">2. Location &amp; Media Data</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          GPS coordinates and timestamps from your activity tracks and photos are private to your
          account by default. They become publicly viewable only if you explicitly enable a share
          link for a specific project, which you can disable or rotate at any time. Uploaded photos
          are re-encoded into sanitized WebP images, stripping unnecessary metadata after optional
          timestamp or location alignment.
        </p>
      </section>

      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">3. Cookies &amp; Security Controls</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          We use strictly necessary HttpOnly session cookies and CSRF tokens to keep you signed in
          securely. We do not use advertising trackers or third-party analytics cookies. Sign-up and
          password recovery forms may use Cloudflare Turnstile to prevent automated abuse.
        </p>
      </section>

      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">4. Third-Party Integrations</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          Optional Google sign-in uses OpenID Connect to verify your email address, account subject
          identifier, and display name. Connecting Strava is optional; OAuth access and refresh
          tokens are encrypted at rest and used strictly to import activities you request. Map tiles
          are requested from configured public basemap providers (such as OpenFreeMap) to display
          routes.
        </p>
      </section>

      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">5. Retention &amp; Deletion</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          Server-rendered MP4 outputs are retained temporarily (default 7 days) before automatic
          cleanup. Browser-based Local Exports stay on your device and are never uploaded to our
          servers. You can delete individual projects, disconnect Strava, or permanently delete your
          entire account and all associated projects, tracks, photos, renders, and OAuth tokens at
          any time from your Account security page.
        </p>
      </section>
    </LegalPageShell>
  );
}
