import { useEffect, useRef } from "react";

interface TurnstileRenderOptions {
  sitekey: string;
  theme?: "light" | "dark" | "auto";
  callback?: (token: string) => void;
  "expired-callback"?: () => void;
  "error-callback"?: () => void;
}

interface TurnstileApi {
  render: (container: HTMLElement, options: TurnstileRenderOptions) => string;
  remove: (widgetId: string) => void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

const TURNSTILE_SCRIPT_SRC =
  "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

export interface TurnstileWidgetProps {
  siteKey: string | null | undefined;
  onToken: (token: string | null) => void;
  resetKey?: number;
}

export function TurnstileWidget({ siteKey, onToken, resetKey = 0 }: TurnstileWidgetProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const onTokenRef = useRef(onToken);

  useEffect(() => {
    onTokenRef.current = onToken;
  }, [onToken]);

  useEffect(() => {
    if (!siteKey || typeof window === "undefined") {
      return;
    }

    let cancelled = false;
    let widgetId: string | null = null;

    const mountWidget = () => {
      if (cancelled || !containerRef.current || !window.turnstile) {
        return;
      }
      widgetId = window.turnstile.render(containerRef.current, {
        sitekey: siteKey,
        theme: "dark",
        callback: (token) => onTokenRef.current(token),
        "expired-callback": () => onTokenRef.current(null),
        "error-callback": () => onTokenRef.current(null),
      });
    };

    if (window.turnstile) {
      mountWidget();
    } else {
      let script = document.querySelector<HTMLScriptElement>(
        `script[src="${TURNSTILE_SCRIPT_SRC}"]`,
      );
      if (!script) {
        script = document.createElement("script");
        script.src = TURNSTILE_SCRIPT_SRC;
        script.async = true;
        script.defer = true;
        document.head.appendChild(script);
      }
      script.addEventListener("load", mountWidget);
      return () => {
        cancelled = true;
        script?.removeEventListener("load", mountWidget);
        if (widgetId && window.turnstile) {
          window.turnstile.remove(widgetId);
        }
      };
    }

    return () => {
      cancelled = true;
      if (widgetId && window.turnstile) {
        window.turnstile.remove(widgetId);
      }
    };
  }, [siteKey, resetKey]);

  if (!siteKey) {
    return null;
  }

  return (
    <div className="space-y-1.5">
      <div
        ref={containerRef}
        data-testid="turnstile-widget"
        className="min-h-[65px] overflow-hidden rounded-md"
      />
    </div>
  );
}
