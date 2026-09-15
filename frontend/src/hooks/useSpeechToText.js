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

  const setOnFinal = useCallback((fn) => {
    onFinalRef.current = fn;
  }, []);

  useEffect(() => () => {
    try { recRef.current?.stop(); } catch { /* ignore */ }
  }, []);

  const stop = useCallback(() => {
    try { recRef.current?.stop(); } catch { /* ignore */ }
    setListening(false);
    setInterim('');
  }, []);

  const start = useCallback(() => {
    if (!Ctor) return;
    setError(null);
    try {
      const rec = new Ctor();
      rec.lang = lang;
      rec.continuous = continuous;
      rec.interimResults = true;
      rec.onstart = () => setListening(true);
      rec.onend = () => {
        setListening(false);
        setInterim('');
      };
      rec.onerror = (ev) => {
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
        if (finalText && onFinalRef.current) onFinalRef.current(finalText.trim());
      };
      recRef.current = rec;
      rec.start();
    } catch (err) {
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
