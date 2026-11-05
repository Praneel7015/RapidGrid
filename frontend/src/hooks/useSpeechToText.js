/**
 * Dictation.
 *
 * Edge and Chrome use the Web Speech API and show words as you speak.
 * Firefox has no speech recognizer, so the mic records a clip and a small
 * Whisper model in the browser transcribes it when you tap again.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { preloadWhisper, recorderOptions, transcribeRecording } from './whisperTranscribe';

function getRecognitionCtor() {
  if (typeof window === 'undefined') return null;
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function canRecord() {
  return typeof navigator !== 'undefined'
    && typeof MediaRecorder !== 'undefined'
    && Boolean(navigator.mediaDevices?.getUserMedia);
}

function failureMessage(err, phase) {
  const name = err?.name || '';
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'Microphone permission was blocked.';
  }
  if (name === 'NotFoundError') return 'No microphone was found.';
  if (phase === 'loading') {
    return 'Could not download the speech model. Check the connection and try again.';
  }
  return 'Could not transcribe that clip. You can still type.';
}

// Web Speech reports bare codes; show the caller a sentence instead.
const WEBSPEECH_ERRORS = {
  'not-allowed': 'Microphone permission was blocked.',
  'service-not-allowed': 'Microphone permission was blocked.',
  'audio-capture': 'No microphone was found.',
  network: 'Voice input needs a network connection. You can still type.',
};

function webSpeechMessage(code) {
  return WEBSPEECH_ERRORS[code] || 'Voice input stopped. You can still type.';
}

export default function useSpeechToText({ lang = 'en-IN', continuous = false } = {}) {
  const Ctor = getRecognitionCtor();
  const engine = Ctor ? 'webspeech' : canRecord() ? 'whisper' : null;
  const supported = engine !== null;
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState(null);
  const [phase, setPhase] = useState('idle');
  const recRef = useRef(null);
  const mediaRef = useRef(null);
  const onFinalRef = useRef(null);
  const startingRef = useRef(false);
  const aliveRef = useRef(true);
  // activeRef: a recognizer or recording is running, or a clip is still being
  // transcribed. finishRef: a pending finish() and the final text it collects.
  const activeRef = useRef(false);
  const finishRef = useRef(null);

  const settle = useCallback(() => {
    activeRef.current = false;
    const pending = finishRef.current;
    if (!pending) return;
    finishRef.current = null;
    clearTimeout(pending.timer);
    pending.resolve(pending.text.trim());
  }, []);

  const deliverFinal = useCallback((text) => {
    if (finishRef.current) finishRef.current.text += ` ${text}`;
    if (onFinalRef.current) onFinalRef.current(text);
  }, []);

  const setOnFinal = useCallback((fn) => {
    onFinalRef.current = fn;
  }, []);

  const releaseMic = useCallback(() => {
    mediaRef.current?.getTracks().forEach((track) => track.stop());
    mediaRef.current = null;
  }, []);

  useEffect(() => () => {
    aliveRef.current = false;
    try { recRef.current?.stop(); } catch { /* ignore */ }
    releaseMic();
    onFinalRef.current = null;
    settle();
  }, [releaseMic, settle]);

  const stop = useCallback(() => {
    startingRef.current = false;
    try { recRef.current?.stop(); } catch { /* ignore */ }
    if (engine !== 'whisper') {
      setListening(false);
      setInterim('');
    }
  }, [engine]);

  const startWebSpeech = useCallback(() => {
    if (!Ctor || startingRef.current) return;
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
        settle();
      };
      rec.onerror = (ev) => {
        startingRef.current = false;
        setListening(false);
        setInterim('');
        if (ev.error !== 'aborted' && ev.error !== 'no-speech') {
          setError(webSpeechMessage(ev.error));
        }
        settle();
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
        if (finalText) deliverFinal(finalText.trim());
      };
      recRef.current = rec;
      activeRef.current = true;
      rec.start();
    } catch (err) {
      startingRef.current = false;
      setError(webSpeechMessage(err?.name === 'NotAllowedError' ? 'not-allowed' : null));
      setListening(false);
      settle();
    }
  }, [Ctor, continuous, lang, deliverFinal, settle]);

  const startWhisper = useCallback(async () => {
    if (startingRef.current || phase === 'loading' || phase === 'transcribing') return;
    startingRef.current = true;
    setError(null);
    setInterim('');
    preloadWhisper().catch(() => { /* surfaced when the clip is transcribed */ });
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      mediaRef.current = stream;
      if (!aliveRef.current) {
        releaseMic();
        return;
      }
      const rec = new MediaRecorder(stream, recorderOptions());
      const chunks = [];
      rec.ondataavailable = (ev) => {
        if (ev.data?.size) chunks.push(ev.data);
      };
      rec.onstop = async () => {
        releaseMic();
        if (!aliveRef.current) {
          settle();
          return;
        }
        setListening(false);
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/ogg' });
        let stage = 'loading';
        setPhase('loading');
        try {
          await preloadWhisper();
          if (!aliveRef.current) return;
          stage = 'transcribing';
          setPhase('transcribing');
          const text = await transcribeRecording(blob);
          if (!aliveRef.current) return;
          if (text) deliverFinal(text);
          else setError('No speech detected. Try again.');
        } catch (err) {
          if (!aliveRef.current) return;
          setError(failureMessage(err, stage));
        } finally {
          if (aliveRef.current) setPhase('idle');
          settle();
        }
      };
      mediaRef.current = stream;
      recRef.current = rec;
      activeRef.current = true;
      rec.start();
      startingRef.current = false;
      setListening(true);
    } catch (err) {
      startingRef.current = false;
      releaseMic();
      setListening(false);
      setPhase('idle');
      setError(failureMessage(err, 'idle'));
      settle();
    }
  }, [phase, releaseMic, deliverFinal, settle]);

  // Stop listening and resolve with the final text that arrives after the call
  // (the recognizer delivers its last words after stop). Resolves '' at once
  // when nothing is running. Callers append it to the text they already hold.
  const finish = useCallback(() => {
    if (!activeRef.current) return Promise.resolve('');
    if (finishRef.current) return finishRef.current.promise;
    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    // Never hold a send forever: Whisper may still be downloading its model.
    const timer = setTimeout(settle, engine === 'whisper' ? 60000 : 4000);
    finishRef.current = { resolve, text: '', promise, timer };
    stop();
    return promise;
  }, [engine, settle, stop]);

  const start = useCallback(() => {
    if (engine === 'webspeech') startWebSpeech();
    else if (engine === 'whisper') startWhisper();
  }, [engine, startWebSpeech, startWhisper]);

  const toggle = useCallback(() => {
    if (phase === 'loading' || phase === 'transcribing') return;
    if (listening) stop();
    else start();
  }, [listening, phase, start, stop]);

  return {
    supported, engine, phase, listening, interim, error, start, stop, finish, toggle, setOnFinal,
  };
}
