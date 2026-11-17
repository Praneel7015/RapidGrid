/*
 * Installed-app status: an offline banner and a "new version" prompt.
 *
 * The service worker caches only the app shell. Incidents, chat, routing and
 * sessions always need the server, so going offline is announced plainly
 * rather than letting screens look current while showing old data.
 */
import React, { useEffect, useRef, useState } from 'react';
import { useRegisterSW } from 'virtual:pwa-register/react';
import { RefreshCw, WifiOff } from 'lucide-react';
import { Button } from './ui';

function useOnline() {
  const [online, setOnline] = useState(() => (typeof navigator === 'undefined' ? true : navigator.onLine));
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener('online', up);
    window.addEventListener('offline', down);
    return () => {
      window.removeEventListener('online', up);
      window.removeEventListener('offline', down);
    };
  }, []);
  return online;
}

export default function PwaStatus() {
  const online = useOnline();
  // The new worker takes over every open tab at once. Only a tab whose user
  // pressed Reload reloads; any other tab keeps its call open and is told.
  const choseReload = useRef(false);
  const [updatedElsewhere, setUpdatedElsewhere] = useState(false);
  const {
    needRefresh: [needRefresh, setNeedRefresh],
    updateServiceWorker,
  } = useRegisterSW({
    onNeedReload() {
      if (choseReload.current) window.location.reload();
      else setUpdatedElsewhere(true);
    },
    onRegisterError(error) {
      console.warn('Service worker registration failed', error);
    },
  });

  const reload = () => {
    choseReload.current = true;
    if (updatedElsewhere) window.location.reload();
    else updateServiceWorker(true);
  };
  const showPrompt = needRefresh || updatedElsewhere;

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-0 z-[1000] flex flex-col items-center gap-2 p-3">
      {!online && (
        <div
          role="status"
          className="pointer-events-auto flex max-w-[560px] items-start gap-2 rounded-sm border border-critical bg-ink px-3 py-2.5 text-[13px] leading-snug text-on-ink shadow-lg"
        >
          <WifiOff size={15} className="mt-px shrink-0 text-critical" />
          <span>
            You are offline. Live calls, chat and routing need a connection; what is on screen
            may be out of date. Call 112 for a real emergency.
          </span>
        </div>
      )}
      {showPrompt && (
        <div
          role="status"
          className="pointer-events-auto flex max-w-[560px] flex-wrap items-center gap-3 rounded-sm border border-ink bg-paper px-3 py-2.5 text-[13px] text-text shadow-lg"
        >
          <span>
            {updatedElsewhere
              ? 'RapidGrid was updated in another tab. Reload when this screen is free.'
              : 'A new version of RapidGrid is ready.'}
          </span>
          <span className="flex gap-2">
            <Button
              variant="quiet"
              size="sm"
              onClick={() => {
                setNeedRefresh(false);
                setUpdatedElsewhere(false);
              }}
            >
              Later
            </Button>
            <Button size="sm" onClick={reload}>
              <RefreshCw size={14} />
              Reload
            </Button>
          </span>
        </div>
      )}
    </div>
  );
}
