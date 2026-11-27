import { FileText } from "lucide-react";
import { LegalPageShell } from "../components/legal/LegalPageShell";

export function TermsPage() {
  return (
    <LegalPageShell
      title="Terms of Service"
      lastUpdated="November 26, 2026"
      icon={<FileText className="h-4 w-4" aria-hidden="true" />}
      siblingTo="/privacy"
      siblingLabel="Privacy Policy"
    >
      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">1. Using Trip Animator</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          By creating an account or accessing Trip Animator, you agree to use the platform lawfully
          and refrain from automated abuse, unauthorized access attempts, or uploading malicious
          payloads. You are responsible for keeping your account credentials secure.
        </p>
      </section>

      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">2. Your Content &amp; Ownership</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          You retain full ownership of the activity tracks, photos, and trip films you upload or
          create. You grant Trip Animator only the limited technical license needed to parse, store,
          display, render, and—when you enable a share link—serve your project content.
        </p>
      </section>

      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">3. Local Export &amp; Server Renders</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          Browser-based Local Export runs directly on your device. Asynchronous Server Renders are
          subject to technical limits and access controls set by the operator—including project
          count, file size, duration, and concurrency—to keep the service fair and reliable.
        </p>
      </section>

      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">4. Third-Party Services</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          Optional integrations with Google sign-in, Strava, and public map tile providers are
          subject to those providers&apos; respective terms and rate limits. We cannot guarantee
          uninterrupted availability of third-party APIs or map tile servers.
        </p>
      </section>

      <section className="space-y-2.5">
        <h2 className="text-base font-semibold text-ink">5. Service Availability</h2>
        <p className="text-sm leading-relaxed text-ink-muted">
          Trip Animator is provided on an &ldquo;as is&rdquo; basis during beta and public
          operation. We may enforce storage quotas, suspend abusive accounts, or modify service
          limits to maintain operational stability.
        </p>
      </section>
    </LegalPageShell>
  );
}
