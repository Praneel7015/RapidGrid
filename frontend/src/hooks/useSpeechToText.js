/**
 * Web Speech API dictation. Chrome/Edge support SpeechRecognition;
 * unsupported browsers get supported: false and the mic stays hidden.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

function getRecognitionCtor() {
  if (typeof window === 'undefined') return null;
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

export default function useSpeechToText({ lang = 'en-IN', continuous = false } = {}) {
  const Ctor = getRecognitionCtor();
  const supported = Boolean(Ctor);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState(null);
  const recRef = useRef(null);
  const onFinalRef = useRef(null);
  const startingRef = useRef(false); // guard against double-tap before onstart fires

  const setOnFinal = useCallback((fn) => {
    onFinalRef.current = fn;
  }, []);

  useEffect(() => () => {
    try { recRef.current?.stop(); } catch { /* ignore */ }
    onFinalRef.current = null; // clear callback on unmount to prevent setState after unmount
  }, []);

  const stop = useCallback(() => {
    startingRef.current = false;
    try { recRef.current?.stop(); } catch { /* ignore */ }
    setListening(false);
    setInterim('');
  }, []);

  const start = useCallback(() => {
    if (!Ctor || startingRef.current) return; // already starting — ignore double-tap
    startingRef.current = true;
    setError(null);
    try {
      const rec = new Ctor();
      rec.lang = lang;
      rec.continuous = continuous;
      rec.interimResults = true;
      rec.onstart = () => { startingRef.current = false; setListening(true); };
      rec.onend = () => {
        startingRef.current = false;
        setListening(false);
        setInterim('');
      };
      rec.onerror = (ev) => {
        startingRef.current = false;
        setListening(false);
        setInterim('');
        if (ev.error !== 'aborted' && ev.error !== 'no-speech') {
          setError(ev.error || 'speech_error');
        }
      };
      rec.onresult = (ev) => {
        let interimText = '';
        let finalText = '';
        for (let i = ev.resultIndex; i < ev.results.length; i += 1) {
          const piece = ev.results[i][0]?.transcript ?? '';
          if (ev.results[i].isFinal) finalText += piece;
          else interimText += piece;
        }
        setInterim(interimText);
        // Guard: only fire if the callback is still registered (component mounted)
        if (finalText && onFinalRef.current) onFinalRef.current(finalText.trim());
      };
      recRef.current = rec;
      rec.start();
    } catch (err) {
      startingRef.current = false;
      setError(err?.message || 'speech_start_failed');
      setListening(false);
    }
  }, [Ctor, continuous, lang]);

  const toggle = useCallback(() => {
    if (listening) stop();
    else start();
  }, [listening, start, stop]);

  return { supported, listening, interim, error, start, stop, toggle, setOnFinal };
}
